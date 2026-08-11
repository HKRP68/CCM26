"""Release player handlers — /release (supports name, position, ranges) + /releasemultiple.

Which numbering a position means
────────────────────────────────
Both ``/release N`` and ``/releasemultiple N M`` resolve against the shared
display numbering in ``services.roster_view`` — the same one /pxi and /myroster
print, XI 1-11 (category-sorted) then bench 12+. A user quoting a number off
either listing gets the card they are looking at.

That number is deliberately *not* ``UserRoster.order_position``, which is the
batting slot and differs for most of the XI. Nothing in this module writes to
``order_position`` except ``_renumber_roster``, which closes the gaps a release
leaves behind.

One release at a time
─────────────────────
Both commands work the same way: they post a confirmation prompt and the actual
delete happens when the button is tapped. Two of those open at once is a trap —
the roster is renumbered by the first confirmation, so the second prompt is
describing positions that have already moved, and a player the user never looked
at gets sold. So while a user has a release (or a multi-release) waiting for an
answer, a second one is refused until they confirm it, cancel it, or it times
out with the buttons.
"""

import logging
import threading
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from config import get_sell_value, get_buy_value, MAX_ROSTER
from utils.idempotency import claim_once, release
from services.activity_service import log_activity
from services.flags import get_flag
from services.roster_view import (get_display_roster, find_position, zone_of,
                                  is_in_xi, XI_SIZE)
from services.roster_lock import MARKET_REASON, match_lock_alert, match_lock_message

logger = logging.getLogger(__name__)

# How long a confirmation prompt stays live. The buttons are cleared on the same
# schedule (``schedule_button_timeout`` below), so an abandoned prompt can never
# block the next release for longer than it is tappable.
RELEASE_PROMPT_TTL = 120

# telegram_id -> {"kind", "chat_id", "expires_at"} for the prompt awaiting an
# answer, behind a lock like utils.idempotency's claim table: the bot runs with
# concurrent_updates, and the Flask Mini App reads this table from its own thread
# (admin._release_prompt_open), so every lookup-then-mutate below has to be
# atomic or a chat prompt and a Mini App release can cross past each other.
_open_prompts = {}
_PROMPTS_LOCK = threading.Lock()

_KIND_LABEL = {"release": "release", "multi": "multi-release"}


def pending_release(tg_id):
    """This user's un-answered release prompt, or ``None``.

    Expired entries are dropped on read, so a prompt whose buttons have already
    timed out never blocks anything.
    """
    with _PROMPTS_LOCK:
        row = _open_prompts.get(tg_id)
        if not row:
            return None
        if row["expires_at"] <= time.monotonic():
            _open_prompts.pop(tg_id, None)
            return None
        return row


def _open_prompt(tg_id, kind, chat_id):
    now = time.monotonic()
    with _PROMPTS_LOCK:
        # Opportunistic sweep: an abandoned prompt is only ever read again if that
        # same user comes back, so purge the dead ones when the table grows.
        if len(_open_prompts) > 256:
            for stale in [k for k, v in _open_prompts.items()
                          if v["expires_at"] <= now]:
                _open_prompts.pop(stale, None)
        _open_prompts[tg_id] = {"kind": kind, "chat_id": chat_id,
                                "expires_at": now + RELEASE_PROMPT_TTL}


def _close_prompt(tg_id):
    with _PROMPTS_LOCK:
        _open_prompts.pop(tg_id, None)


def _busy_text(row):
    """What to say when a second release is attempted."""
    seconds = max(1, int(row["expires_at"] - time.monotonic()))
    what = _KIND_LABEL.get(row.get("kind"), "release")
    return (
        f"⚠️ <b>You already have a {what} waiting.</b>\n"
        f"Confirm it or tap ❌ Cancel first — releasing two sets of players at "
        f"once would renumber your roster underneath the other prompt.\n\n"
        f"<i>It expires on its own in {seconds}s.</i>"
    )


def _schedule_prompt_timeout(context, sent):
    """Clear the prompt's buttons after the TTL (best-effort, as before)."""
    try:
        from services.button_timeout import schedule_button_timeout
        schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                delay_seconds=RELEASE_PROMPT_TTL)
    except Exception:
        logger.debug("release: button timeout scheduling failed", exc_info=True)


# ── Helpers ──────────────────────────────────────────────────────────

def _renumber_roster(session, user_id):
    """Close any gaps in order_position after a release."""
    from sqlalchemy import asc
    remaining = (session.query(UserRoster)
                 .filter(UserRoster.user_id == user_id)
                 .order_by(
                     asc(UserRoster.order_position).nullslast(),
                     UserRoster.acquired_date,
                     UserRoster.id,
                 ).all())
    for i, entry in enumerate(remaining, 1):
        if entry.order_position != i:
            entry.order_position = i


def _do_release(session, user, entries, *, value_fn=get_sell_value,
                record_undo=True):
    """Release a list of (UserRoster, Player) tuples atomically.

    Cleans up all known references to the doomed roster rows BEFORE deleting,
    so foreign key constraints don't bite us:
      - PlayerTrait rows (returns each trait to inventory)
      - pending Trade rows (cancelled, FKs nulled)
      - User.captain_roster_id (nulled if it points to a doomed row)
      - any other lingering references caught by the catch-all SQL below

    ``value_fn`` maps a rating to the coins credited per card. It defaults to
    the normal lossy sell price; the squad-downsizing migration passes
    ``get_buy_value`` instead, because a forced release is not a sale and the
    captain should not eat the buy/sell spread on a card they never chose to
    part with.

    ``record_undo`` writes the 60-second /cmuundo record. Bulk/automated
    releases pass False: restoring them would put the squad straight back over
    the cap that forced the release in the first place.

    Returns dict with success, released list, total_coins, new_balance, new_count.
    """
    from sqlalchemy import text
    from models import Trade
    from services.trait_service import return_traits_to_inventory

    # Career Players are personal and permanent. This is the one choke point
    # every release path funnels through (chat, callbacks, multi-release and the
    # Mini App), so refusing here means no route can sell one by accident.
    if any(getattr(p, "is_career", False) for _, p in entries):
        from services.career_service import CAREER_LOCKED_MESSAGE
        return {
            "success": False, "error": "career_player",
            "message": CAREER_LOCKED_MESSAGE, "released": [],
            "total_coins": 0, "new_balance": user.total_coins,
            "new_count": user.roster_count,
        }

    total_coins = 0
    released = []
    captain_released = False
    traits_returned = 0

    roster_ids = [e.id for e, _ in entries]
    if not roster_ids:
        return {
            "success": True, "released": [],
            "total_coins": 0, "new_balance": user.total_coins,
            "new_count": user.roster_count,
            "captain_released": False, "traits_returned": 0,
        }

    # 1. Cancel pending trades that reference these roster entries
    stale_trades = (session.query(Trade)
                    .filter(Trade.status == "pending")
                    .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                            (Trade.receiver_roster_id.in_(roster_ids)))
                    .all())
    for t in stale_trades:
        t.status = "cancelled"
        if t.initiator_roster_id in roster_ids:
            t.initiator_roster_id = None
        if t.receiver_roster_id in roster_ids:
            t.receiver_roster_id = None

    # ALSO null FK on completed/cancelled trades — even those still hold a FK
    # pointer in Postgres, and "ON DELETE NO ACTION" (default) blocks the delete.
    historical_trades = (session.query(Trade)
                         .filter(Trade.status != "pending")
                         .filter((Trade.initiator_roster_id.in_(roster_ids)) |
                                 (Trade.receiver_roster_id.in_(roster_ids)))
                         .all())
    for t in historical_trades:
        if t.initiator_roster_id in roster_ids:
            t.initiator_roster_id = None
        if t.receiver_roster_id in roster_ids:
            t.receiver_roster_id = None
    session.flush()

    # 2. Return any equipped traits to inventory (same level they were on)
    traits_returned = return_traits_to_inventory(session, roster_ids)

    # 3. Captain check: null user.captain_roster_id BEFORE deleting roster rows.
    # We check ALL the entries being deleted up front so the captain reference
    # is gone before any row delete is flushed.
    if user.captain_roster_id in roster_ids:
        user.captain_roster_id = None
        captain_released = True
        session.flush()

    # 4. Catch-all: production DB may have FK constraints not in the model
    # (e.g. legacy users.captain_roster_id FK from an old migration). Use raw
    # SQL UPDATE that's safe-on-no-match — these are idempotent best-effort
    # cleanups. Wrapped in try/except so missing tables don't crash the release.
    safety_updates = [
        ("UPDATE users SET captain_roster_id = NULL WHERE captain_roster_id IN :ids",
         {"ids": tuple(roster_ids) if len(roster_ids) > 1 else (roster_ids[0],)}),
    ]
    for sql, params in safety_updates:
        try:
            session.execute(text(sql), params)
        except Exception:
            pass  # best-effort

    # 5. Delete the roster entries
    undo_items = []  # for /cmuundo
    for entry, player in entries:
        sv = value_fn(player.rating)
        undo_items.append({
            "player_id": player.id,
            "player_name": player.name,
            "rating": player.rating,
            "price": sv,
        })
        session.delete(entry)
        user.total_coins += sv
        user.roster_count = max(0, user.roster_count - 1)
        total_coins += sv
        released.append({"name": player.name, "rating": player.rating, "value": sv})
        log_activity(session, user.id, "release",
                     f"Released {player.name} ({player.rating}) for {sv:,}",
                     coins_change=sv, player_name=player.name, player_rating=player.rating)

    session.flush()
    _renumber_roster(session, user.id)

    # Record for /cmuundo
    if undo_items and record_undo:
        try:
            from services.undo_service import record_release
            record_release(session, user.id,
                           items=undo_items, total_refund=total_coins)
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).exception("record_release failed (non-fatal)")

    return {
        "success": True,
        "released": released,
        "total_coins": total_coins,
        "new_balance": user.total_coins,
        "new_count": user.roster_count,
        "captain_released": captain_released,
        "traits_returned": traits_returned,
    }


def _find_by_arg(session, user_id, arg_str):
    """Find roster entries matching the argument.
    - If arg is a number, returns entry at that position.
    - If arg is a name, returns all matching entries (for disambiguation).
    Returns list of (UserRoster, Player).
    """
    arg_str = arg_str.strip()

    # Try position first — the shared display numbering (/pxi, /myroster).
    if arg_str.isdigit():
        pos = int(arg_str)
        display_entries = get_display_roster(session, user_id)
        if 1 <= pos <= len(display_entries):
            return [display_entries[pos - 1]]
        return []

    # Name search — exact match first, then substring
    exact = (session.query(UserRoster, Player)
             .join(Player, UserRoster.player_id == Player.id)
             .filter(UserRoster.user_id == user_id, Player.name.ilike(arg_str))
             .order_by(UserRoster.order_position).all())
    if exact:
        return exact

    substr = (session.query(UserRoster, Player)
              .join(Player, UserRoster.player_id == Player.id)
              .filter(UserRoster.user_id == user_id,
                      Player.name.ilike(f"%{arg_str}%"))
              .order_by(UserRoster.order_position).all())
    return substr


def _fmt_player_line(position, player):
    """One preview line, tagged XI or Bench.

    ``position`` is the shared display number — never the row's
    ``order_position``, which is the batting slot and a different number for
    most of the XI.
    """
    sv = get_sell_value(player.rating)
    flag = get_flag(player.country) if player.country else ""
    tag = "🏏" if is_in_xi(position) else "📋"
    return f"{tag} #{position}. {player.name} {flag} | {player.rating} OVR | 💸 {sv:,}"


def _squad_impact(released_count, current_count):
    """Warn when a release eats into the XI the user needs to play matches."""
    remaining = current_count - released_count
    if remaining >= XI_SIZE:
        return ""
    short = XI_SIZE - remaining
    return (f"\n\n⚠️ <b>This leaves you {remaining} player(s)</b> — {short} short "
            f"of the {XI_SIZE} needed to field a Playing XI.")


# ── /release — smart single/name/position release ────────────────────

async def releasepl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /release <name>     → release by name (disambiguates duplicates)
    /release <position> → release by roster position (1-based)
    /release            → show usage
    """
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/release &lt;player name&gt;</code> — by name\n"
            "<code>/release &lt;position&gt;</code> — by roster position\n"
            "<code>/releasemultiple &lt;from&gt; &lt;to&gt;</code> — range",
            parse_mode="HTML")
        return

    arg = " ".join(context.args).strip()

    busy = pending_release(tg_user.id)
    if busy:
        await update.message.reply_text(_busy_text(busy), parse_mode="HTML")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        locked = match_lock_message(session, user.id, "sell players",
                                    reason=MARKET_REASON)
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
            return

        matches = _find_by_arg(session, user.id, arg)

        if not matches:
            await update.message.reply_text(f"❌ No match for '<code>{arg}</code>'", parse_mode="HTML")
            return

        # Drop any Career Player from the candidates and say so, rather than
        # offering a confirm button that would be refused. Positions are NOT
        # renumbered here — /pxi numbering has to keep matching what the user
        # typed, so only the career card itself is filtered out.
        career_hit = any(getattr(p, "is_career", False) for _, p in matches)
        matches = [(e, p) for e, p in matches if not getattr(p, "is_career", False)]
        if career_hit and not matches:
            from services.career_service import CAREER_LOCKED_MESSAGE
            await update.message.reply_text(CAREER_LOCKED_MESSAGE, parse_mode="HTML")
            return

        # Every position shown from here on is the display number, so the name
        # search and the position command speak the same language.
        display = get_display_roster(session, user.id)

        # Multiple matches — let user pick
        if len(matches) > 1:
            # Show up to 10 choices
            btns = []
            for entry, player in matches[:10]:
                sv = get_sell_value(player.rating)
                pos = find_position(display, entry.id)
                label = f"#{pos} " if pos else ""
                btns.append([InlineKeyboardButton(
                    f"{label}{player.name} ({player.rating}) — 💸 {sv:,}",
                    callback_data=f"rlone_{entry.id}")])
            btns.append([InlineKeyboardButton("❌ Cancel", callback_data="rlcancel")])

            text = f"🔍 Found <b>{len(matches)}</b> matching players:\n\nChoose one to release:"
            sent = await update.message.reply_text(text, parse_mode="HTML",
                                            reply_markup=InlineKeyboardMarkup(btns))
            _open_prompt(tg_user.id, "release", sent.chat_id)
            _schedule_prompt_timeout(context, sent)
            return

        # Single match — show confirm
        entry, player = matches[0]
        sv = get_sell_value(player.rating)
        flag = get_flag(player.country) if player.country else ""
        captain_warn = ""
        if user.captain_roster_id == entry.id:
            captain_warn = "\n\n⚠️ <b>This is your Captain!</b> You'll need to set a new one."

        pos = find_position(display, entry.id)
        heading = f"#{pos}. " if pos else ""
        where = f" · {zone_of(pos)}" if pos else ""

        text = (
            "🔴 <b>RELEASE PLAYER?</b>\n\n"
            f"{heading}{player.name} {flag}\n"
            f"⭐ Rating: {player.rating} OVR\n"
            f"🏷 {player.category}{where}\n\n"
            f"💸 You will receive: <b>{sv:,}</b> 🪙"
            f"{captain_warn}"
            f"{_squad_impact(1, len(display))}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Release", callback_data=f"rlone_{entry.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        _open_prompt(tg_user.id, "release", sent.chat_id)
        _schedule_prompt_timeout(context, sent)

    except Exception:
        logger.exception("Release error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def release_one_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm single release — callback: rlone_<roster_id>"""
    query = update.callback_query
    tg_user = query.from_user

    try:
        roster_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid")
        return

    # Dedup rapid taps so a player can't be released (and credited) twice.
    key = f"rlone_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            release(key)
            await query.answer("Not authorized")
            return

        locked = match_lock_alert(session, user.id, "sell players")
        if locked:
            release(key)
            await query.answer(locked, show_alert=True)
            return

        entry = session.query(UserRoster).filter(
            UserRoster.id == roster_id, UserRoster.user_id == user.id).first()
        if not entry:
            release(key)
            await query.answer("Not yours or already released")
            try: await query.edit_message_text("❌ This player is no longer in your roster.")
            except Exception: pass
            return

        await query.answer()
        player = session.query(Player).get(entry.player_id)
        if not player:
            # Roster entry orphaned — just delete it
            session.delete(entry)
            user.roster_count = max(0, user.roster_count - 1)
            session.commit()
            release(key)  # nothing credited — let the user retry if needed
            try: await query.edit_message_text("⚠️ Player data missing — roster entry cleaned up.")
            except Exception: pass
            return

        result = _do_release(session, user, [(entry, player)])
        if not result.get("success"):
            session.rollback()
            release(key)  # nothing was sold — let the user act again
            try:
                await query.edit_message_text(result["message"], parse_mode="HTML")
            except Exception:
                pass
            return
        session.commit()

        r = result["released"][0]
        text = (
            f"✅ <b>PLAYER RELEASED</b>\n\n"
            f"{r['name']} ({r['rating']} OVR)\n\n"
            f"💸 Received: <b>{r['value']:,}</b> 🪙\n"
            f"💰 Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/{MAX_ROSTER}"
        )
        if result["captain_released"]:
            text += "\n\n⚠️ Captain slot cleared. Use /setcaptain to assign new one."
        if result.get("traits_returned"):
            text += f"\n💎 {result['traits_returned']} trait(s) returned to inventory."
        text += "\n\n<i>↩️ Made a mistake? /cmuundo within 60 seconds to reverse.</i>"

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        # Keep the claim (may be post-commit) so a stale tap can't re-credit.
        session.rollback()
        logger.exception(f"Release one callback FAILED: {type(e).__name__}: {e}")
        msg = str(e)
        import re
        m = re.search(r'constraint "([^"]+)"', msg)
        constraint_hint = f"\nConstraint: <code>{m.group(1)}</code>" if m else ""
        try:
            await query.edit_message_text(
                f"⚠️ Error releasing player.\n"
                f"<code>{type(e).__name__}</code>{constraint_hint}\n\n"
                f"<i>{msg[:300]}</i>",
                parse_mode="HTML")
        except Exception:
            pass
    finally:
        # However this ended, the prompt has been answered — let the user start
        # another release straight away.
        _close_prompt(tg_user.id)
        session.close()


async def release_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _close_prompt(query.from_user.id)
    await query.answer("Cancelled")
    try:
        await query.edit_message_text("❌ Release cancelled.")
    except Exception:
        pass


# ── /releasemultiple — range release ─────────────────────────────────

async def releasemultiple_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/releasemultiple <from> <to> — release a range of display positions."""
    tg_user = update.effective_user

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/releasemultiple &lt;from&gt; &lt;to&gt;</code>\n"
            "Example: <code>/releasemultiple 12 15</code>\n\n"
            "<i>Positions are the numbers shown on /myroster and /pxi — "
            f"1-{XI_SIZE} is your XI, {XI_SIZE + 1}+ is the bench.</i>",
            parse_mode="HTML")
        return

    try:
        pos_from = int(context.args[0])
        pos_to = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Positions must be numbers.")
        return

    if pos_from > pos_to:
        pos_from, pos_to = pos_to, pos_from

    if pos_from < 1:
        await update.message.reply_text("❌ Position must be 1 or higher.")
        return

    busy = pending_release(tg_user.id)
    if busy:
        await update.message.reply_text(_busy_text(busy), parse_mode="HTML")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        locked = match_lock_message(session, user.id, "sell players",
                                    reason=MARKET_REASON)
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
            return

        entries = get_display_roster(session, user.id)

        if pos_to > len(entries):
            await update.message.reply_text(
                f"❌ You only have {len(entries)} players. Max position is {len(entries)}.")
            return

        to_release = entries[pos_from - 1:pos_to]
        if not to_release:
            await update.message.reply_text("❌ Nothing to release in that range.")
            return

        # Refuse the whole range rather than quietly selling everything around a
        # Career Player: the user asked for positions N→M and should re-pick.
        career_hit = next((p for _, p in to_release
                           if getattr(p, "is_career", False)), None)
        if career_hit is not None:
            from services.career_service import CAREER_LOCKED_MESSAGE
            await update.message.reply_text(
                f"{CAREER_LOCKED_MESSAGE}\n\n<b>{career_hit.name}</b> is in "
                f"positions {pos_from}→{pos_to}. Choose a range that skips it.",
                parse_mode="HTML")
            return

        total_sell = 0
        lines = []
        captain_in_range = False
        xi_in_range = 0
        for offset, (entry, player) in enumerate(to_release):
            position = pos_from + offset
            sv = get_sell_value(player.rating)
            total_sell += sv
            lines.append(_fmt_player_line(position, player))
            if is_in_xi(position):
                xi_in_range += 1
            if user.captain_roster_id == entry.id:
                captain_in_range = True

        # Build preview (truncate if too long)
        preview = "\n".join(lines[:15])
        if len(lines) > 15:
            preview += f"\n<i>... and {len(lines) - 15} more</i>"

        captain_warn = "\n\n⚠️ <b>Captain is in this range!</b>" if captain_in_range else ""

        # Spell out how the range straddles the XI/bench line. Selling bench is
        # routine; selling out of the XI is the thing worth a second look.
        bench_in_range = len(to_release) - xi_in_range
        if xi_in_range and bench_in_range:
            span = f"🏏 {xi_in_range} from your XI · 📋 {bench_in_range} from the bench"
        elif xi_in_range:
            span = f"🏏 All {xi_in_range} from your <b>Playing XI</b>"
        else:
            span = f"📋 All {bench_in_range} from your <b>bench</b>"

        text = (
            f"🔴 <b>RELEASE {len(to_release)} PLAYERS?</b>\n\n"
            f"<b>Positions {pos_from} → {pos_to}</b>\n"
            f"{span}\n\n"
            f"{preview}\n\n"
            f"💸 Total: <b>{total_sell:,}</b> 🪙"
            f"{captain_warn}"
            f"{_squad_impact(len(to_release), len(entries))}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Release All",
                callback_data=f"rlm_{user.telegram_id}_{pos_from}_{pos_to}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rlcancel"),
        ]])
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        _open_prompt(tg_user.id, "multi", sent.chat_id)
        _schedule_prompt_timeout(context, sent)

    except Exception:
        logger.exception("ReleaseMultiple handler error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def releasemultiple_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rlm_<tg_user_id>_<from>_<to>"""
    query = update.callback_query
    tg_user = query.from_user

    parts = query.data.split("_")
    if len(parts) < 4:
        await query.answer("Expired")
        try: await query.edit_message_text("❌ Expired. Run /releasemultiple again.")
        except Exception: pass
        return

    try:
        authorized_tg_id = int(parts[1])
        pos_from = int(parts[2])
        pos_to = int(parts[3])
    except ValueError:
        await query.answer("Bad data")
        return

    # Authorization — only the user who issued the command can confirm
    if tg_user.id != authorized_tg_id:
        await query.answer("Not your release!", show_alert=True)
        return

    # Dedup rapid taps so a batch can't be released (and credited) twice.
    key = f"rlm_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            release(key)
            await query.answer("Not authorized")
            return

        locked = match_lock_alert(session, user.id, "sell players")
        if locked:
            release(key)
            await query.answer(locked, show_alert=True)
            return

        await query.answer()

        entries = get_display_roster(session, user.id)

        if pos_from < 1 or pos_to > len(entries) or pos_from > pos_to:
            release(key)
            try: await query.edit_message_text(
                f"❌ Roster changed. You now have {len(entries)} players.\n"
                f"Please run /releasemultiple again.")
            except Exception: pass
            return

        to_release = entries[pos_from - 1:pos_to]
        if not to_release:
            release(key)
            try: await query.edit_message_text("❌ Nothing to release.")
            except Exception: pass
            return

        result = _do_release(session, user, to_release)
        if not result.get("success"):
            session.rollback()
            release(key)
            try:
                await query.edit_message_text(
                    result["message"] + "\n\nPick a range that skips it.",
                    parse_mode="HTML")
            except Exception:
                pass
            return
        session.commit()

        released = result["released"]
        names_str = ", ".join(r["name"] for r in released[:8])
        if len(released) > 8:
            names_str += f", +{len(released) - 8} more"

        text = (
            f"✅ <b>RELEASED {len(released)} PLAYERS</b>\n\n"
            f"{names_str}\n\n"
            f"💸 Total: <b>{result['total_coins']:,}</b> 🪙\n"
            f"💰 Balance: {result['new_balance']:,}\n"
            f"📊 Roster: {result['new_count']}/{MAX_ROSTER}"
        )
        if result["captain_released"]:
            text += "\n\n⚠️ Captain slot cleared. Use /setcaptain."
        if result.get("traits_returned"):
            text += f"\n💎 {result['traits_returned']} trait(s) returned to inventory."
        text += "\n\n<i>↩️ Made a mistake? /cmuundo within 60 seconds to reverse.</i>"

        await query.edit_message_text(text, parse_mode="HTML")

    except Exception as e:
        # Keep the claim (may be post-commit) so a stale tap can't re-credit a batch.
        session.rollback()
        logger.exception(f"ReleaseMultiple confirm FAILED: {type(e).__name__}: {e}")
        # Extract the actual constraint name from psycopg2 errors so we can
        # diagnose which lingering reference is blocking the delete.
        msg = str(e)
        # Postgres FK violations look like:
        #   ... violates foreign key constraint "fk_name" on table "x"
        constraint_hint = ""
        import re
        m = re.search(r'constraint "([^"]+)"', msg)
        if m:
            constraint_hint = f"\nConstraint: <code>{m.group(1)}</code>"
        # Snippet of underlying error (longer than before)
        try: await query.edit_message_text(
            f"⚠️ Error releasing players.\n"
            f"<code>{type(e).__name__}</code>{constraint_hint}\n\n"
            f"<i>{msg[:300]}</i>",
            parse_mode="HTML")
        except Exception: pass
    finally:
        # The prompt has been answered either way — unblock the next release.
        _close_prompt(tg_user.id)
        session.close()
