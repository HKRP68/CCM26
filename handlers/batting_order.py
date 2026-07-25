"""/setbo + /sbo — set your own batting order.

Until now the batting line-up could only be arranged in the Mini App, and the
chat modes that build an XI automatically (notably /letsplay) always sorted it
by batting rating, high → low. These commands put the order in the user's hands
from Telegram, and the order they set is what actually walks out to bat.

The batting order IS the roster's ``order_position``: slots 1-11 are the
Playing XI in batting order, 12+ is the bench. That is the same order the Mini
App XI screen edits, so both stay in sync.

Commands
────────
  /setbo                      show the current order (XI + bench)
  /setbo 2 11                 swap batting slots 2 and 11
  /setbo 2 11 3 7             several swaps in one go (applied left to right)
  /setbo 2 13                 bring bench player 13 in at slot 2
  /setbo 4 1 2 3 …            full reorder — 11 slot numbers, any permutation
  /setbo auto                 rebuild by batting rating (high → low)
  /sbo …                      same thing, shorter (swap batting order)

Every change is validated against the normal Playing-XI rules before it is
saved, so a bench swap can never leave an illegal XI behind.
"""

import html
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.activity_service import log_activity
from services.bowling_service import is_spinner as _is_spin
from handlers.lineup import _get_ordered_roster, validate_xi

logger = logging.getLogger(__name__)

XI_SIZE = 11

USAGE = (
    "🏏 <b>How to set your batting order</b>\n"
    "<code>/sbo 2 11</code> — swap batting slots 2 and 11\n"
    "<code>/sbo 2 11 3 7</code> — several swaps at once\n"
    "<code>/sbo 2 13</code> — bring bench player 13 in at slot 2\n"
    "<code>/sbo 4 1 2 3 5 6 7 8 9 10 11</code> — full reorder (11 numbers)\n"
    "<code>/sbo auto</code> — rebuild by batting rating (high → low)\n"
    "<i>/setbo works exactly the same.</i>"
)


# ════════════════════════════════════════════════════════════════════
# Formatting
# ════════════════════════════════════════════════════════════════════

def _role_tag(player):
    cat = player.category or ""
    if cat == "Wicket Keeper":
        return "🧤"
    if cat == "All-rounder":
        return "⚡"
    if cat == "Bowler":
        return "🌀" if _is_spin(player.bowl_style) else "🎯"
    return "🏏"


def _slot_label(i):
    """Cricket-speak for a batting position, so the card reads like a line-up."""
    if i <= 2:
        return "Opener"
    if i == 3:
        return "First drop"
    if i <= 5:
        return "Middle"
    if i <= 7:
        return "Finisher"
    return "Tail"


def format_order_card(pairs, user, custom: bool):
    """The batting-order card: XI 1-11 in batting order, then the bench."""
    xi = pairs[:XI_SIZE]
    bench = pairs[XI_SIZE:]
    cap_rid = user.captain_roster_id

    source = ("✍️ <i>Your custom order</i>" if custom
              else "🤖 <i>Auto order (batting rating, high → low)</i>")
    lines = [
        "🏏 <b>MY BATTING ORDER</b>",
        "━━━━━━━━━━━━━━━━━━━",
        source,
        "",
    ]
    for i, (entry, player) in enumerate(xi, start=1):
        cap = " 👑" if cap_rid and entry.id == cap_rid else ""
        lines.append(
            f"<b>{i:>2}.</b> {_role_tag(player)} {html.escape(str(player.name))}"
            f"  <i>BAT {player.bat_rating}</i>{cap}"
            f"  <code>{_slot_label(i)}</code>")

    if bench:
        lines += ["", f"🪑 <b>Bench</b> ({len(bench)})"]
        for i, (_entry, player) in enumerate(bench, start=XI_SIZE + 1):
            lines.append(
                f"<b>{i:>2}.</b> {_role_tag(player)} {html.escape(str(player.name))}"
                f"  <i>BAT {player.bat_rating}</i>")
    else:
        lines += ["", "🪑 <i>Bench: none — your roster is exactly 11.</i>"]

    top = sum(p.bat_rating or 0 for _e, p in xi[:6])
    lines += [
        "",
        f"📊 <b>Top-6 batting strength:</b> {top}",
        "━━━━━━━━━━━━━━━━━━━",
        USAGE,
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# Order maths — all operate on a list of 11 roster ids
# ════════════════════════════════════════════════════════════════════

def apply_moves(xi_ids, bench_ids, numbers):
    """Apply the number pairs to ``xi_ids``. Returns (new_xi_ids, changes, error).

    ``numbers`` is a flat list of slot numbers taken in pairs:
      • both 1-11        → swap those two batting slots
      • first 1-11, second 12+ → the bench player takes that batting slot
    ``changes`` is a list of (a, b) pairs actually applied, for the reply.
    """
    total = len(xi_ids) + len(bench_ids)
    if len(numbers) % 2 != 0:
        return None, [], ("Give the numbers in pairs — each swap needs two "
                          "(e.g. <code>/sbo 2 11</code>).")

    new_ids = list(xi_ids)
    changes = []
    for a, b in zip(numbers[::2], numbers[1::2]):
        if not (1 <= a <= XI_SIZE):
            return None, [], (f"The first number of each pair must be a batting "
                              f"slot (1-{XI_SIZE}), not {a}.")
        if 1 <= b <= XI_SIZE:
            if a == b:
                return None, [], (f"Slot {a} twice does nothing — pick two "
                                  f"different slots.")
            new_ids[a - 1], new_ids[b - 1] = new_ids[b - 1], new_ids[a - 1]
        elif XI_SIZE < b <= total:
            bench_rid = bench_ids[b - XI_SIZE - 1]
            if bench_rid in new_ids:
                return None, [], (f"Player {b} is already in your XI — "
                                  f"pick a different bench number.")
            new_ids[a - 1] = bench_rid
        else:
            if bench_ids:
                return None, [], (
                    f"The second number must be a batting slot (1-{XI_SIZE}) to "
                    f"swap, or a bench player ({XI_SIZE + 1}-{total}) to bring "
                    f"in — not {b}.")
            return None, [], (
                f"The second number must be a batting slot (1-{XI_SIZE}). You "
                f"have no bench players — your roster is exactly {XI_SIZE}.")
        changes.append((a, b))
    return new_ids, changes, None


def apply_full_order(xi_ids, numbers):
    """``numbers`` is a permutation of 1-11: slot i takes the player at
    position numbers[i]. Returns (new_xi_ids, error)."""
    if sorted(numbers) != list(range(1, XI_SIZE + 1)):
        return None, (f"A full reorder needs each slot 1-{XI_SIZE} exactly once. "
                      f"Check for repeats or missing numbers.")
    return [xi_ids[n - 1] for n in numbers], None


def auto_order(xi_pairs):
    """Batting rating high → low (ties: overall rating, then name) — the order
    /letsplay used to force on everyone."""
    return sorted(
        xi_pairs,
        key=lambda ep: (int(ep[1].bat_rating or 0), int(ep[1].rating or 0),
                        str(ep[1].name or "")),
        reverse=True)


def _persist(session, user, ordered_pairs, bench_pairs, custom: bool):
    """Write the new batting order to order_position (XI 1-11, bench 12+)."""
    for i, (entry, _p) in enumerate(ordered_pairs, start=1):
        entry.order_position = i
    for i, (entry, _p) in enumerate(bench_pairs, start=XI_SIZE + 1):
        entry.order_position = i
    user.batting_order_set_at = datetime.utcnow() if custom else None


def has_custom_order(user) -> bool:
    return getattr(user, "batting_order_set_at", None) is not None


# ════════════════════════════════════════════════════════════════════
# Handler
# ════════════════════════════════════════════════════════════════════

async def setbo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setbo, /sbo — view or change the batting order."""
    msg = update.effective_message
    tg = update.effective_user
    if msg is None or tg is None:
        return

    args = [str(a) for a in (getattr(context, "args", None) or [])]
    # Accept "2,11" and "2-11" as well as plain spaces — people type both.
    tokens = []
    for a in args:
        tokens.extend(t for t in a.replace(",", " ").replace("-", " ").split() if t)

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await msg.reply_text("❌ Do /debut first!")
            return

        pairs = _get_ordered_roster(session, user.id)
        if len(pairs) < XI_SIZE:
            session.commit()
            await msg.reply_text(
                f"❌ You need {XI_SIZE} players to set a batting order — you "
                f"have <b>{len(pairs)}</b>.\nGet more with /claim, /gspin or "
                f"/buypack.", parse_mode="HTML")
            return

        xi_pairs = pairs[:XI_SIZE]
        bench_pairs = pairs[XI_SIZE:]
        by_id = {int(e.id): (e, p) for e, p in pairs}
        xi_ids = [int(e.id) for e, _p in xi_pairs]
        bench_ids = [int(e.id) for e, _p in bench_pairs]

        # ── No args → just show the current order ──
        if not tokens:
            text = format_order_card(pairs, user, has_custom_order(user))
            session.commit()
            await msg.reply_text(text, parse_mode="HTML",
                                 disable_web_page_preview=True)
            return

        # ── /setbo auto → back to rating order ──
        if tokens[0].lower() in ("auto", "reset", "default"):
            new_xi = auto_order(xi_pairs)
            _persist(session, user, new_xi, bench_pairs, custom=False)
            log_activity(session, user.id, "batting_order",
                         "Batting order reset to auto (batting rating)")
            text = format_order_card(new_xi + bench_pairs, user, False)
            session.commit()
            await msg.reply_text(
                "🤖 <b>Batting order rebuilt by batting rating.</b>\n\n" + text,
                parse_mode="HTML", disable_web_page_preview=True)
            return

        if any(not t.isdigit() for t in tokens):
            await msg.reply_text("❌ Numbers only.\n\n" + USAGE, parse_mode="HTML")
            return
        numbers = [int(t) for t in tokens]

        # ── Full reorder (11 numbers) vs swap pairs ──
        if len(numbers) == XI_SIZE:
            new_ids, err = apply_full_order(xi_ids, numbers)
            changes = []
        else:
            new_ids, changes, err = apply_moves(xi_ids, bench_ids, numbers)
        if err:
            await msg.reply_text(f"❌ {err}\n\n" + USAGE, parse_mode="HTML")
            return

        new_xi = [by_id[rid] for rid in new_ids]
        ok, errors = validate_xi(new_xi)
        if not ok:
            await msg.reply_text(
                "❌ That would leave an illegal Playing XI:\n"
                + "\n".join(f"• {e}" for e in errors)
                + "\n\n<i>Nothing was changed.</i>", parse_mode="HTML")
            return

        # Anyone dropped out of the XI goes to the front of the bench so they
        # are easy to find (and to bring straight back in).
        new_set = set(new_ids)
        dropped = [by_id[rid] for rid in xi_ids if rid not in new_set]
        new_bench = dropped + [by_id[rid] for rid in bench_ids
                               if rid not in new_set]

        _persist(session, user, new_xi, new_bench, custom=True)

        if changes:
            detail = ", ".join(f"#{a}↔#{b}" for a, b in changes)
        else:
            detail = "full reorder"
        log_activity(session, user.id, "batting_order",
                     f"Batting order changed ({detail})")

        # Report where each touched slot ended up — correct even when several
        # swaps were chained in one command.
        touched = sorted({n for pair in changes for n in pair if n <= XI_SIZE})
        if touched:
            header = "🔁 <b>Batting order updated</b>\n" + "\n".join(
                f"   #{n} → {html.escape(str(new_xi[n - 1][1].name))}"
                for n in touched)
        else:
            header = "🔁 <b>Batting order updated.</b>"

        text = format_order_card(new_xi + new_bench, user, True)
        session.commit()
        await msg.reply_text(header + "\n\n" + text, parse_mode="HTML",
                             disable_web_page_preview=True)
    except Exception:
        session.rollback()
        logger.exception("/setbo failed")
        await msg.reply_text("⚠️ Couldn't update your batting order. Try again.")
    finally:
        session.close()
