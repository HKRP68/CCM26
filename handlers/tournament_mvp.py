"""``/mvp`` — the Most Valuable Player table for the tournament that's running.

``/tournamentstats`` ranks one column at a time, which is what you want when the
question is "who has the most sixes?". This is the other card: one number for a
whole tournament, so the all-rounder who won three games with 40-odd and two
wickets stops being invisible behind ten batsmen who each batted more.

Three tabs, all the same points scored three ways:

    🏅 Overall   batting + bowling + wins + Player of the Match awards
    🏏 Batting   the batting half of the same total
    🎯 Bowling   the bowling half

Read-only and open to anyone, like the rest of the follow-along commands. It
searches the live Challenge League tournament first and the Lets Play one after
— both store their matches in the same tables, so one card serves both and a
player in a chat running either gets an answer rather than "no tournament is
active". Scoring lives in ``services.tournament_mvp``; the card prints its rubric
verbatim so nobody has to guess why they are fourth.
"""

import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
# How deep the card ranks, and how much of it opens without a tap: the same
# two depths /tournamentstats uses, so the sibling cards agree.
from handlers.tournament import BOARD_LIMIT, BOARD_OPEN
from services import tournament_mvp
from utils.message_chunks import expandable_quotes

logger = logging.getLogger(__name__)

NO_ACTIVE = ("❌ No tournament is currently running.\n"
             "An admin activates one from the tournament panel.")

# Its own callback namespace: ``tstat_`` belongs to /tournamentstats and
# ``lptstat_`` to /lptstats.
CB_PREFIX = "mvp_"

BOARDS = (("overall", "🏅 Overall"), ("batting", "🏏 Batting"),
          ("bowling", "🎯 Bowling"))
_BOARD_LABELS = dict(BOARDS)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _live_tournament(session):
    """The tournament an MVP table should be built for, or ``None``.

    The Challenge League one wins when both are running: it is the bigger
    competition, and the Lets Play card is a tap away on /lptstats.
    """
    from services import tournament_service
    tour = tournament_service.get_active_tournament(
        session, kind=tournament_service.KIND_CHALLENGE)
    if tour:
        return tour
    try:
        from services import lp_tournament_service
        return lp_tournament_service.active_tournament(session)
    except Exception:
        logger.exception("/mvp: Lets Play tournament lookup failed")
        return None


def _overs(balls):
    """Balls as cricket overs — ``27`` → ``4.3``."""
    balls = int(balls or 0)
    return f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)


def _summary(row, board):
    """The one-line season under a player's name.

    Only the halves they actually played appear: a specialist bowler's line
    should not carry an empty batting stat, and the batting board leads with the
    batting even for an all-rounder.
    """
    bat = None
    if row.bat_balls:
        sr = row.bat_runs / row.bat_balls * 100.0
        bat = f"🏏 {row.bat_runs} ({row.bat_balls}b, SR {sr:.1f})"
    bowl = None
    if row.bowl_balls:
        econ = row.bowl_runs / row.bowl_balls * 6.0
        bowl = (f"🎯 {row.bowl_wickets}w in {_overs(row.bowl_balls)} ov "
                f"(Econ {econ:.2f})")
    parts = [p for p in ((bowl, bat) if board == "bowling" else (bat, bowl)) if p]
    parts.insert(0, f"🎮 {row.matches}")
    if row.awards:
        parts.append(f"⭐ {row.awards} POTM")
    return " · ".join(parts)


def render(session, tour, board="overall", limit=BOARD_LIMIT):
    """The MVP card for one board as an HTML message body.

    The first ``BOARD_OPEN`` places are printed outright; the rest ride in an
    expandable blockquote. A player chasing the table is almost never in the top
    ten — that is the whole reason they opened it — so the board has to go
    deeper than ten without costing everyone else a screenful.
    """
    rows = tournament_mvp.mvp_table(session, tour.id, limit=limit, board=board)
    field = {"batting": "bat_points", "bowling": "bowl_points"}.get(board, "points")
    out = [f"🏅 <b>{escape(tour.name or '—')}</b> — Most Valuable Player",
           f"<b>{_BOARD_LABELS.get(board, 'Overall')}</b> · "
           f"the whole tournament, one number", ""]
    if not rows:
        out.append("<i>No matches have been played yet — "
                   "the table fills in as results come in.</i>")
        return "\n".join(out)

    def _entry(position, row):
        rank = _MEDALS.get(position, f"{position}.")
        team = f" · {escape(row.team_name)}" if row.team_name else ""
        points = getattr(row, field)
        return (f"{rank} <b>{escape(row.name or 'Player')}</b>{team} — "
                f"<b>{points:g}</b> pts\n"
                f"      <i>{_summary(row, board)}</i>")

    out += [_entry(i, row) for i, row in enumerate(rows[:BOARD_OPEN], 1)]
    rest = rows[BOARD_OPEN:]
    if rest:
        out += ["", f"<b>👇 Ranks {BOARD_OPEN + 1}–{len(rows)}</b> "
                    f"<i>(tap to expand)</i>"]
        out += expandable_quotes(_entry(i, row)
                                 for i, row in enumerate(rest, BOARD_OPEN + 1))

    if board == "overall":
        lead = rows[0]
        out += ["", f"<b>How {escape(lead.name or 'the leader')} got there</b>",
                f"🏏 {lead.bat_points:g} batting · 🎯 {lead.bowl_points:g} bowling · "
                f"🏆 {lead.result_points:g} results · ⭐ {lead.award_points:g} awards"]
    out += ["", "<b>Scoring</b>"] + [f"<i>{line}</i>"
                                     for line in tournament_mvp.rubric_lines()]
    return "\n".join(out)


def _keyboard(active, opener_tg):
    """The board tabs, marking the active one and bound to whoever opened it."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("● " if key == active else "") + label,
                             callback_data=f"{CB_PREFIX}{key}_{opener_tg}")
        for key, label in BOARDS]])


async def mvp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mvp — the tournament's Most Valuable Player table."""
    message = update.effective_message
    if message is None:
        return
    session = get_session()
    try:
        tour = _live_tournament(session)
        if not tour:
            await message.reply_text(NO_ACTIVE)
            return
        opener = update.effective_user.id if update.effective_user else 0
        await message.reply_text(
            render(session, tour, "overall"), parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_keyboard("overall", opener))
    except Exception:
        logger.exception("/mvp failed")
        await message.reply_text("⚠️ Could not load the MVP table right now.")
    finally:
        session.close()


async def mvp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``mvp_<board>_<opener>`` — switch board, for the user who opened the card."""
    q = update.callback_query
    try:
        board, opener = (q.data or "")[len(CB_PREFIX):].rsplit("_", 1)
        opener = int(opener)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    if board not in _BOARD_LABELS:
        await q.answer("Unknown board.", show_alert=True)
        return
    if q.from_user.id != opener:
        await q.answer("Only the person who opened this can use these buttons.",
                       show_alert=True)
        return
    await q.answer()
    session = get_session()
    try:
        tour = _live_tournament(session)
        if not tour:
            await q.edit_message_text(NO_ACTIVE)
            return
        await q.edit_message_text(
            render(session, tour, board), parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_keyboard(board, opener))
    except Exception:
        logger.exception("/mvp callback failed")
    finally:
        session.close()
