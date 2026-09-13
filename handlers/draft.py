"""Tournament Draft commands — /pick and the rest of the draft floor.

A draft is bound to one group chat. Everything here is refused outside it: a
pick is a public, irreversible event that the rest of the room has to be able to
see and argue with, and one made quietly in a DM is how an order gets disputed.

Who may pick is deliberately narrow — the team's Owner Tag ID and any co-owner
an admin has added, and nobody else. Not even bot admins, who have ``/dskip``
instead: unsticking a draft is a visible admin action, recorded as an auto-pick,
rather than a pick passed off as the owner's own choice.

Command surface
---------------
Players (in the draft group)
  /pick <player>      make the pick that is on the clock (alias /pk)
  /dboard             the live board, with buttons for order / pool / squads
  /dsquad [team]      a squad by tier, with slot progress
  /dqueue <player>    your wishlist, which the clock picks from if you time out

Admin
  /dadmin /dnew /dbind /dstart /dpause /dtimer /dco /dskip /dundo /dpublish

Callback prefixes are all ``dr_`` (``dr_view_`` for the board's tabs,
``dr_pick_`` for the disambiguation buttons), which is why ``dr_`` has to be in
``services.button_access.SHARED_CALLBACK_PREFIXES`` — without it the group's
button guard would let only whoever ran the command press anything.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import DraftPlayer, DraftTeam
from services import draft_service as ds
from services import draft_scheduler as dsched
from services.admin_ids import is_admin
from services.draft_service import DraftError

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")

NOT_ADMIN = "⛔ Only bot admins can manage a draft."
GROUP_ONLY = ("❌ Draft commands only work in the group the draft is bound to.\n"
              "An admin binds one with <code>/dbind</code>.")
NO_DRAFT = ("❌ No draft is running in this chat.\n"
            "An admin can create one with <code>/dnew &lt;name&gt;</code> and "
            "bind it here with <code>/dbind</code>.")


# ════════════════════════════════════════════════════════════════════
# Shared plumbing
# ════════════════════════════════════════════════════════════════════

async def _reply(update, text, **kwargs):
    """Reply with this module's defaults: HTML, and no link previews."""
    msg = update.effective_message
    if msg is None:
        return None
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await msg.reply_text(text, **kwargs)


def _arg_text(context):
    return " ".join(context.args or []).strip()


def _split_pipes(text, count):
    parts = [p.strip() for p in (text or "").split("|")]
    parts += [""] * (count - len(parts))
    return parts[:count]


def _parse_tg_id(token):
    token = (token or "").strip().lstrip("@")
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value > 0 else None


async def _require_admin(update):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


def _load_for_chat(session, update):
    """The draft bound to this chat, or None."""
    chat = update.effective_chat
    if chat is None:
        return None
    return ds.draft_for_chat(session, chat.id)


async def _with_draft(update, work, *, admin=False, allow_dm=False):
    """Run ``work(session, draft)`` against this chat's draft.

    Centralises what every command has to do: the group gate, the admin check,
    finding the bound draft, turning a ``DraftError`` into a plain reply, and
    closing the session. ``work`` returns the reply text and may mutate — the
    commit happens here, only if ``work`` returns without raising.
    """
    chat = update.effective_chat
    if not allow_dm and (chat is None or chat.type not in GROUP_CHAT_TYPES):
        await _reply(update, GROUP_ONLY)
        return
    if admin and not await _require_admin(update):
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        text = work(session, draft)
        session.commit()
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("Draft command failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    if text:
        await _reply(update, text)


# ════════════════════════════════════════════════════════════════════
# /pick — the whole point
# ════════════════════════════════════════════════════════════════════

async def pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    user = update.effective_user
    if user is None:
        return
    query = _arg_text(context)

    session = get_session()
    sent = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        if draft.status != ds.STATUS_LIVE:
            await _reply(update, f"⚠️ This draft is {ds.status_label(draft)} — "
                                 f"no picks can be made right now.")
            return

        pick = ds.current_pick(session, draft)
        if pick is None:
            await _reply(update, "🏁 Every slot is filled — the draft is done.")
            return
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        if not ds.may_pick_for(team, user.id):
            mine = ds.team_for_actor(session, draft.id, user.id)
            if mine is None:
                await _reply(update, "⛔ You don't own a team in this draft.")
            else:
                await _reply(update,
                             f"⛔ It's <b>{html.escape(team.name if team else '')}</b>'s "
                             f"pick, not {html.escape(mine.name)}'s. "
                             f"{dsched.team_mention(session, team)} is on the clock.")
            return

        if not query:
            await _reply(update,
                         "Usage: <code>/pick &lt;player name&gt;</code>\n"
                         f"You're picking for <b>{html.escape(team.name)}</b> — "
                         f"R{pick.round_no} P{pick.pick_no}, "
                         f"{ds.tier_badge(pick.tier)} slot or below.")
            return

        player, candidates = ds.find_available(session, draft.id, query)
        if player is None:
            await _reply(update, _ambiguous_text(query, candidates),
                         reply_markup=_ambiguous_keyboard(pick, candidates))
            return

        done = ds.make_pick(session, draft, pick, player, by_tg_id=user.id)
        session.commit()
        sent = (draft, done, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/pick failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if sent is None:
            session.close()

    # Announce outside the try/finally above so a Telegram failure can never
    # roll back a pick that is already committed.
    try:
        draft, done, player = sent
        await dsched.announce_pick(context.bot, session, draft, done, player)
        if draft.status == ds.STATUS_COMPLETED:
            await _reply(update, "🏁 <b>Draft complete.</b> Every slot is filled — "
                                 "an admin can publish the squads with /dpublish.")
    finally:
        session.close()


def _ambiguous_text(query, candidates):
    if not candidates:
        return (f"❌ No available player matches “{html.escape(query)}”.\n"
                f"Check /dboard → <b>Available</b> for who's left.")
    if len(candidates) > 5:
        return (f"🔎 {len(candidates)} available players match "
                f"“{html.escape(query)}”. Type more of the name.")
    return (f"🔎 “{html.escape(query)}” matches more than one player — "
            f"tap the one you mean:")


def _ambiguous_keyboard(pick, candidates):
    if not candidates or len(candidates) > 5:
        return None
    rows = [[InlineKeyboardButton(
        f"{p.name} · {p.rating} · {p.tier}",
        callback_data=f"dr_pick_{pick.id}_{p.id}")] for p in candidates]
    return InlineKeyboardMarkup(rows)


async def pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One of the disambiguation buttons. Re-checks everything /pick checks."""
    query = update.callback_query
    if query is None:
        return
    try:
        _, _, pick_id, player_id = query.data.split("_", 3)
        pick_id, player_id = int(pick_id), int(player_id)
    except (ValueError, AttributeError):
        await query.answer("That button is out of date.", show_alert=True)
        return

    session = get_session()
    sent = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None or draft.status != ds.STATUS_LIVE:
            await query.answer("This draft isn't taking picks right now.",
                               show_alert=True)
            return
        pick = ds.current_pick(session, draft)
        if pick is None or pick.id != pick_id:
            await query.answer("That pick has already been made.", show_alert=True)
            return
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        if not ds.may_pick_for(team, query.from_user.id):
            await query.answer("That's not your team's pick.", show_alert=True)
            return
        player = (session.query(DraftPlayer)
                  .filter(DraftPlayer.id == player_id,
                          DraftPlayer.draft_id == draft.id).first())
        done = ds.make_pick(session, draft, pick, player,
                            by_tg_id=query.from_user.id)
        session.commit()
        sent = (draft, done, player)
        await query.answer("Picked.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    except DraftError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        session.rollback()
        logger.exception("draft pick callback failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        if sent is None:
            session.close()

    try:
        draft, done, player = sent
        await dsched.announce_pick(context.bot, session, draft, done, player)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Readouts
# ════════════════════════════════════════════════════════════════════

def _board_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Order", callback_data="dr_view_order"),
        InlineKeyboardButton("📦 Available", callback_data="dr_view_pool"),
        InlineKeyboardButton("👥 Teams", callback_data="dr_view_teams"),
    ]])


async def dboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Not routed through _with_draft: the board carries a keyboard.
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        pick = ds.current_pick(session, draft)
        team = None
        if pick is not None:
            team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        text = ds.render_board(session, draft,
                               mention=dsched.team_mention(session, team))
    except Exception:
        # There is no global PTB error handler, so an unhandled failure here
        # would leave the command looking ignored rather than broken.
        logger.exception("/dboard failed")
        await _reply(update, "⚠️ Couldn't build the board. Try again.")
        return
    finally:
        session.close()
    await _reply(update, text, reply_markup=_board_keyboard())


async def board_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The board's tabs. Anyone in the draft group may press these."""
    query = update.callback_query
    if query is None:
        return
    view = (query.data or "").replace("dr_view_", "", 1)
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await query.answer("No draft is running here.", show_alert=True)
            return
        if view == "order":
            text = ds.render_order(session, draft)
        elif view == "pool":
            text = ds.render_available(session, draft)
        elif view == "teams":
            text = ds.render_teams(session, draft)
        else:
            await query.answer()
            return
    except Exception:
        logger.exception("draft board view failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        session.close()
    await query.answer()
    await query.message.reply_text(text, parse_mode="HTML",
                                   disable_web_page_preview=True)


async def dsquad_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        wanted = _arg_text(context)
        user = update.effective_user
        team = (ds.find_team(session, draft.id, wanted) if wanted
                else ds.team_for_actor(session, draft.id, user.id if user else None))
        if team is None:
            if wanted:
                raise DraftError(f"No team here matches “{wanted}”.")
            raise DraftError("You don't own a team in this draft — name one, "
                             "e.g. /dsquad Mumbai.")
        return ds.render_squad(session, draft, team)
    await _with_draft(update, work)


async def dqueue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The owner's wishlist, which the clock picks from if their time runs out."""
    def work(session, draft):
        user = update.effective_user
        team = ds.team_for_actor(session, draft.id, user.id if user else None)
        if team is None:
            raise DraftError("You don't own a team in this draft.")
        arg = _arg_text(context)
        if arg.lower() in ("clear", "reset", "empty"):
            ds.queue_clear(team)
            return "🗑 Queue cleared."
        if not arg:
            rows = ds.queue_players(session, team)
            if not rows:
                return ("Your queue is empty. Add players with "
                        "<code>/dqueue &lt;player name&gt;</code> — if your clock "
                        "runs out, the bot picks the first one that's still "
                        "legal for your squad.")
            lines = [f"📝 <b>{html.escape(team.name)}</b> — queue"]
            for index, row in enumerate(rows, start=1):
                gone = "" if row.picked_by_team_id is None else " <i>(gone)</i>"
                lines.append(f"{index}. {ds.tier_emoji(row.tier)} "
                             f"{html.escape(row.name or '')} "
                             f"<code>{row.rating}</code>{gone}")
            return "\n".join(lines)
        player, candidates = ds.find_available(session, draft.id, arg)
        if player is None:
            if not candidates:
                raise DraftError(f"No available player matches “{arg}”.")
            raise DraftError("That matches more than one player: "
                             + ", ".join(c.name for c in candidates[:5]))
        ds.queue_add(session, draft, team, player)
        return (f"📝 Queued <b>{html.escape(player.name)}</b> "
                f"({ds.tier_badge(player.tier)}) for "
                f"<b>{html.escape(team.name)}</b>.")
    await _with_draft(update, work)


# ════════════════════════════════════════════════════════════════════
# Admin
# ════════════════════════════════════════════════════════════════════

ADMIN_HELP = """🎯 <b>Tournament Draft — admin reference</b>

<b>Setting one up</b>
<code>/dnew Summer Mega Draft</code> — create it and bind it to this group
<code>/dbind</code> — bind (or re-bind) the draft to the chat you type it in
Upload the <b>player pool</b> and the <b>draft order</b> on the website:
Admin → Tournament Panel → 🎯 Player Drafts → your draft.

<b>Running it</b>
<code>/dtimer 15</code> — minutes per pick (default 15)
<code>/dco Mumbai | 123456789</code> — add a co-owner who may also /pick
<code>/dstart</code> <code>/dpause</code> <code>/dresume</code> — lifecycle
<code>/dskip</code> — resolve the pick on the clock right now, without waiting
<code>/dundo</code> — roll the last pick back onto the clock
<code>/dcancel</code> — stop the draft

<b>Finishing</b>
<code>/dpublish</code> — write the squads into a Challenge League so they can play

<b>The rules the bot enforces</b>
• A slot's tier is a <b>ceiling</b> — a Platinum slot takes Platinum or below.
  Picking below spends the slot; there is no refund.
• The overseas cap and any role minimums are checked on every pick, and a pick
  that would make a role minimum unreachable is refused while it can still be fixed.
• Only the Owner Tag ID and co-owners may pick for a team. Admins use /dskip.
• When a clock runs out the bot picks: the owner's /dqueue first, otherwise the
  player closest to the average rating of what's legal in that tier."""


async def dadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await _reply(update, ADMIN_HELP)


async def dnew_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a draft and bind it to this group in one step."""
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, "❌ Run /dnew in the group the draft should live in.")
        return
    name = _arg_text(context)
    if not name:
        await _reply(update, "Usage: <code>/dnew &lt;draft name&gt;</code>")
        return
    session = get_session()
    try:
        existing = ds.draft_for_chat(session, chat.id)
        if existing is not None:
            await _reply(update, f"⚠️ This chat already runs the draft "
                                 f"“{html.escape(existing.name)}”. Unbind it first "
                                 f"from the website, or use that one.")
            return
        draft = ds.create_draft(session, name)
        ds.bind_chat(session, draft, chat.id)
        session.commit()
        text = (f"🎯 Created <b>{html.escape(draft.name)}</b> and bound it to this "
                f"chat.\n\nNext: upload the player pool and the draft order on the "
                f"website (Tournament Panel → 🎯 Player Drafts), then /dstart.\n"
                f"Pick clock is {draft.pick_seconds // 60} min — change it with "
                f"/dtimer.")
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dnew failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    await _reply(update, text)


async def dbind_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Point an existing draft (by id) at this chat."""
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, "❌ Run /dbind in the group the draft should live in.")
        return
    draft_id = _parse_tg_id(_arg_text(context))
    session = get_session()
    try:
        if draft_id:
            draft = ds.get_draft(session, draft_id)
        else:
            draft = ds.draft_for_chat(session, chat.id)
        if draft is None:
            await _reply(update, "❌ No such draft. <code>/dbind &lt;id&gt;</code> — "
                                 "the id is on the website's Player Drafts page.")
            return
        ds.bind_chat(session, draft, chat.id)
        session.commit()
        text = (f"🔗 <b>{html.escape(draft.name)}</b> is now bound to this chat. "
                f"Picks happen here.")
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    finally:
        session.close()
    await _reply(update, text)


async def dtimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        arg = _arg_text(context)
        if not arg:
            return (f"⏱ Pick clock is <b>{draft.pick_seconds // 60} min</b> "
                    f"({draft.pick_seconds}s). Change it with "
                    f"<code>/dtimer &lt;minutes&gt;</code>.")
        try:
            minutes = float(arg)
        except ValueError:
            raise DraftError("Give the clock in minutes, e.g. /dtimer 15.")
        draft.pick_seconds = ds.clamp_pick_seconds(int(minutes * 60))
        # Re-arm the running clock so the change takes effect on the pick that
        # is on it, rather than only on the next one.
        if draft.status == ds.STATUS_LIVE and ds.current_pick(session, draft):
            ds.advance(session, draft)
        return (f"⏱ Pick clock set to <b>{draft.pick_seconds // 60} min</b> "
                f"({draft.pick_seconds}s).")
    await _with_draft(update, work, admin=True)


async def dco_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add (or list) the co-owners allowed to pick for a team."""
    def work(session, draft):
        team_name, token = _split_pipes(_arg_text(context), 2)
        if not team_name:
            lines = ["👥 <b>Co-owners</b>"]
            for team in ds.teams(session, draft.id):
                ids = ds.co_owner_ids(team)
                lines.append(f"• {html.escape(team.name)} — owner "
                             f"<code>{team.owner_tg_id or '—'}</code>"
                             + (f", co: {', '.join(str(i) for i in ids)}" if ids else ""))
            lines.append("\nAdd one with "
                         "<code>/dco &lt;team&gt; | &lt;telegram id&gt;</code>")
            return "\n".join(lines)
        team = ds.find_team(session, draft.id, team_name)
        if team is None:
            raise DraftError(f"No team here matches “{team_name}”.")
        tg_id = _parse_tg_id(token)
        if not tg_id:
            raise DraftError("Give the co-owner's numeric Telegram id, e.g. "
                             "/dco Mumbai | 123456789.")
        ds.add_co_owner(session, team, tg_id)
        return (f"✅ <code>{tg_id}</code> can now pick for "
                f"<b>{html.escape(team.name)}</b>.")
    await _with_draft(update, work, admin=True)


async def dstart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    opened = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        pick = ds.resume(session, draft) if draft.status == ds.STATUS_PAUSED \
            else ds.start(session, draft)
        session.commit()
        opened = (draft, pick)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dstart failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if opened is None:
            session.close()
    try:
        draft, pick = opened
        await dsched.announce_turn(
            context.bot, session, draft, pick,
            prefix=f"🎯 <b>{html.escape(draft.name)}</b> is under way!\n"
                   f"Pick clock: {draft.pick_seconds // 60} min.\n\n")
    finally:
        session.close()


async def dpause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        ds.pause(session, draft)
        return ("⏸ <b>Draft paused.</b> The clock is stopped and no picks can be "
                "made until an admin runs /dstart.")
    await _with_draft(update, work, admin=True)


async def dcancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        ds.set_status(session, draft, ds.STATUS_CANCELLED)
        return "🚫 <b>Draft cancelled.</b>"
    await _with_draft(update, work, admin=True)


async def dskip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve the pick on the clock now, exactly as the timer would.

    The admin's way to unstick a draft. It is recorded as an auto-pick, because
    that is what it is — it must never read as the owner's own choice.
    """
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    resolved = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        if draft.status != ds.STATUS_LIVE:
            await _reply(update, f"⚠️ This draft is {ds.status_label(draft)}.")
            return
        pick = ds.current_pick(session, draft)
        if pick is None:
            await _reply(update, "🏁 Every slot is already filled.")
            return
        done, player = ds.resolve_expired(session, draft, pick,
                                          by_tg_id=update.effective_user.id)
        session.commit()
        resolved = (draft, done, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dskip failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if resolved is None:
            session.close()
    try:
        draft, done, player = resolved
        if player is None:
            await dsched.announce_skip(context.bot, session, draft, done)
        else:
            await dsched.announce_pick(context.bot, session, draft, done, player)
    finally:
        session.close()


async def dundo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    undone = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        last, player = ds.undo_last(session, draft)
        session.commit()
        undone = (draft, last, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dundo failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if undone is None:
            session.close()
    try:
        draft, last, player = undone
        freed = (f"<b>{html.escape(player.name)}</b> is back in the pool."
                 if player is not None else "That slot had been passed.")
        await dsched.announce_turn(
            context.bot, session, draft, ds.current_pick(session, draft),
            prefix=f"↩️ <b>Undone:</b> R{last.round_no} P{last.pick_no}. "
                   f"{freed}\n\n")
    finally:
        session.close()


async def dpublish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        league = ds.publish_to_league(session, draft)
        teams = len(ds.teams(session, draft.id))
        return (f"✅ Published <b>{html.escape(draft.name)}</b> as the Challenge "
                f"League <b>{html.escape(league.name)}</b> "
                f"(id <code>{league.id}</code>) — {teams} squads, ready to play.\n"
                f"Enter them in a tournament from the admin site's Tournament "
                f"Panel.")
    await _with_draft(update, work, admin=True)
