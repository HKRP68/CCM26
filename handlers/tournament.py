"""Bot commands for Challenge League Tournament stats.

- ``/statstour <player name>`` — a player's stats in the active tournament.
- ``/tournamentstats`` — stat leaderboards with category buttons (only the user
  who opened the command can switch categories). Each board ranks the top
  ``BOARD_LIMIT`` players: the first ten are shown open and the rest sit behind
  a tap, because 11th place is exactly the position somebody is chasing and a
  board that stops at 10 cannot tell them how far away they are.

The first of those categories is 🏅 **MVP**: one impact-point total for a whole
tournament rather than one column of it, so an all-rounder who keeps winning
matches with 40-odd and two wickets ranks where they belong. Its own card, with
the batting/bowling split and the scoring rubric, is ``/mvp``
(``handlers.tournament_mvp``); the scoring itself lives in
``services.tournament_mvp``.

Both boards are ranked columns of numbers, which is exactly what a proportional
font ruins, so each has a Bot API 10.1 block rendering beside its HTML one — a
native table, aligned by the client. ``services.rich_message`` falls back to the
HTML whenever the rich send is refused, so the two renderers stay live together
and must agree; see ``docs/rich-text-messages.md``.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
import html

from database import get_session
from models import TournamentPlayerStats
from services import rich_message as R
from services import tournament_service
from utils.message_chunks import expandable_quotes

logger = logging.getLogger(__name__)

NO_ACTIVE = "❌ No Challenge League Tournament is currently active."

# How deep each board is ranked, and how much of it is shown without a tap.
#
# A Top 10 answers "who is winning" and nothing else. The player reading it is
# usually 12th, and the old board could not tell them that — it ended one line
# above them. So each board now ranks BOARD_LIMIT players and shows BOARD_OPEN
# of them straight away; the rest ride in a collapsible (a ``details`` block
# where the server renders one, an expandable blockquote in the HTML), so the
# card stays the same size on screen and the chase is one tap away.
BOARD_OPEN = 10
BOARD_LIMIT = 25

# (callback key, button label). "runs" is the default view.
_CATEGORIES = [
    ("mvp", "🏅 MVP"),
    ("runs", "🏏 Most Runs"),
    ("wkts", "🎯 Most Wickets"),
    ("sixes", "6️⃣ Most Sixes"),
    ("fours", "4️⃣ Most Fours"),
    ("hs", "⭐ Highest Score"),
    ("avg", "📊 Batting Average"),
    ("fig", "💥 Best Figure"),
    ("sr", "⚡ Best Strike Rate"),
    ("econ", "🛡️ Best Economy"),
]
_CAT_LABELS = dict(_CATEGORIES)

# What the ranked number *is*, per category. The HTML board leaves it implicit
# ("— 412"); a table has a column to name it in.
_CAT_COLUMN = {
    "mvp": "PTS", "runs": "RUNS", "wkts": "WKTS", "sixes": "6s",
    "fours": "4s", "hs": "HS", "avg": "AVG", "fig": "BEST",
    "sr": "SR", "econ": "ECON",
}


def _sr(r):
    """Batting strike rate (runs per 100 balls) for a stats row, 0 if no balls."""
    return ((r.bat_runs or 0) / r.bat_balls * 100.0) if r.bat_balls else 0.0


def _econ(r):
    """Bowling economy (runs per over) for a stats row, 0 if no balls bowled."""
    overs = (r.bowl_balls or 0) / 6.0
    return ((r.bowl_runs or 0) / overs) if overs else 0.0


def _leaders_for(session, tour, category, limit=BOARD_LIMIT):
    """``[(name, team, value_str), …]`` for one category, best first.

    Ranked ``limit`` deep rather than ten, and the renderers below decide how
    much of that is open and how much is behind a tap.
    """
    leaders = tournament_service.stat_leaders(session, tour.id, limit=limit)
    out = []
    if category == "runs":
        out = [(r.name, r.team_name, str(r.bat_runs)) for r in leaders["most_runs"]]
    elif category == "wkts":
        out = [(r.name, r.team_name, str(r.bowl_wickets)) for r in leaders["most_wickets"]]
    elif category == "sixes":
        out = [(r.name, r.team_name, str(r.bat_sixes)) for r in leaders["most_sixes"]]
    elif category == "fours":
        out = [(r.name, r.team_name, str(r.bat_fours)) for r in leaders["most_fours"]]
    elif category == "hs":
        out = [(r.name, r.team_name, f"{r.highest_score}{'*' if r.not_out else ''}")
               for r in leaders["highest_score"]]
    elif category == "avg":
        out = [(r.name, r.team_name, f"{v:.2f}") for r, v in leaders["top_average"]]
    elif category == "fig":
        out = [(r.name, r.team_name, f"{r.best_bowl_wickets}/{r.best_bowl_runs}")
               for r in leaders["best_figure"]]
    elif category == "sr":
        out = [(r.name, r.team_name, f"{v:.1f}") for r, v in leaders["best_strike_rate"]]
    elif category == "econ":
        out = [(r.name, r.team_name, f"{v:.2f}") for r, v in leaders["best_economy"]]
    elif category == "mvp":
        # MVP is the one board whose number means nothing on its own, so each
        # row carries the season behind it — the total is what ranks them, the
        # line under it is why.
        out = [(r.name, r.team_name, f"{r.points:g} pts") for r in leaders["mvp"]]
    return out


_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _board_caption(rows):
    """What the board says it is showing: "Top 10", or "Top 10 of 23"."""
    if len(rows) <= BOARD_OPEN:
        return f"Top {len(rows)}" if rows else "Top 10"
    return f"Top {BOARD_OPEN} of {len(rows)}"


def _render(tour, category, rows):
    """Format a leaderboard message (HTML) for one category's ranked rows.

    The first ``BOARD_OPEN`` places are printed outright and everything below
    them goes into an expandable blockquote — Telegram's own "tap to expand", so
    the ranks past tenth cost a tap rather than a screenful.
    """
    label = _CAT_LABELS.get(category, "Stats")
    lines = [f"🏆 <b>{html.escape(tour.name)}</b> — Tournament Stats",
             f"<b>{label}</b> · {_board_caption(rows)}", ""]
    if not rows:
        lines.append("<i>No qualifying players yet.</i>")
    else:
        lines += [_rank_line(i, row) for i, row in enumerate(rows[:BOARD_OPEN], 1)]
        rest = rows[BOARD_OPEN:]
        if rest:
            lines += ["",
                      f"<b>👇 Ranks {BOARD_OPEN + 1}–{len(rows)}</b> "
                      f"<i>(tap to expand)</i>"]
            lines += expandable_quotes(
                _rank_line(i, row)
                for i, row in enumerate(rest, BOARD_OPEN + 1))
    if category == "mvp":
        lines += ["", "<i>Impact points across the whole tournament — "
                      "/mvp for the full card.</i>"]
    return "\n".join(lines)


def _rank_line(position, row):
    """One ranked line of the HTML board: medal or number, player, team, value."""
    name, team, value = row
    rank = _MEDALS.get(position, f"{position}.")
    team_s = f" · {html.escape(team)}" if team else ""
    return (f"{rank} {html.escape(name or 'Player')}{team_s} — "
            f"<b>{html.escape(str(value))}</b>")


def _leaderboard_blocks(tour, category, rows):
    """The board as rich blocks — the block twin of :func:`_render`.

    Same heading, same rows in the same order, same split at
    ``BOARD_OPEN`` and same MVP footnote. Two deliberate differences: the table
    gives the rank its own cell, so 🥇🥈🥉 and "4." line up instead of shunting
    the names along; and the ranks past tenth sit in a ``details`` block, which
    is the client's own collapsible rather than the HTML's quoted stand-in.
    """
    try:
        return _leaderboard_tree(tour, category, rows)
    except Exception:
        # A renderer bug costs the rendering, not the board: None sends the HTML.
        logger.exception("tournament leaderboard blocks failed to build")
        return None


def _rank_cells(category, rows, start=1):
    """``rows`` as table rows, headed and numbered from ``start``."""
    cells = [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("TEAM"), header=True),
              R.cell(R.bold(_CAT_COLUMN.get(category, "VALUE")), header=True,
                     align="right")]]
    for position, (name, team, value) in enumerate(rows, start):
        cells.append([
            R.cell(_MEDALS.get(position, f"{position}."), align="center"),
            R.cell(name or "Player"),
            R.cell(team or "—"),
            R.cell(R.bold(str(value)), align="right"),
        ])
    return cells


def _leaderboard_tree(tour, category, rows):
    label = _CAT_LABELS.get(category, "Stats")
    blocks = [R.heading(f"🏆 {tour.name}", size=2),
              R.paragraph([R.bold(label), f" · {_board_caption(rows)}"])]
    if not rows:
        blocks.append(R.paragraph(R.italic("No qualifying players yet.")))
        return blocks

    blocks.append(R.table(_rank_cells(category, rows[:BOARD_OPEN]),
                          bordered=True, striped=True, compact=True))
    rest = rows[BOARD_OPEN:]
    if rest:
        blocks.append(R.details(
            R.bold(f"👇 Ranks {BOARD_OPEN + 1}–{len(rows)}"),
            [R.table(_rank_cells(category, rest, start=BOARD_OPEN + 1),
                     bordered=True, striped=True, compact=True)]))
    if category == "mvp":
        blocks.append(R.footer(["Impact points across the whole tournament — ",
                                R.code("/mvp"), " for the full card."]))
    return blocks


def _player_stats_blocks(tour, rows):
    """One card per matched player — the block twin of :func:`statstour_handler`.

    Batting and bowling are separate tables rather than one run-on paragraph,
    because they are read separately; a second or third match for the same name
    is collapsed behind a ``details`` so the first one stays the answer.
    """
    try:
        return _player_stats_tree(tour, rows)
    except Exception:
        logger.exception("tournament player-stats blocks failed to build")
        return None


def _player_stats_tree(tour, rows):
    blocks = [R.heading(f"🏆 {tour.name}", size=2),
              R.paragraph(R.italic("Player tournament stats"))]
    for index, r in enumerate(rows):
        header = ["👤 ", R.bold(r.name or "Player")]
        if r.team_name:
            header.append(f"  ·  {r.team_name}")
        body = [R.paragraph(header),
                R.paragraph(["🎮 Matches: ", R.bold(str(r.matches or 0))]),
                R.table(_batting_rows(r), bordered=True, compact=True,
                        caption=R.bold("🏏 Batting")),
                R.table(_bowling_rows(r), bordered=True, compact=True,
                        caption=R.bold("🎯 Bowling"))]
        if index == 0:
            blocks.extend(body)
        else:
            blocks.append(R.details(
                R.bold(f"👤 {r.name or 'Player'}"
                       + (f" · {r.team_name}" if r.team_name else "")),
                body[1:]))
    blocks.append(R.footer(["Leaderboards: ", R.code("/tournamentstats")]))
    return blocks


def _batting_rows(r):
    avg = tournament_service.batting_average(r)
    return [
        [R.cell(R.bold("Runs")),
         R.cell(R.bold(str(r.bat_runs or 0)), align="right"),
         R.cell(R.bold("Balls")), R.cell(str(r.bat_balls or 0), align="right")],
        [R.cell(R.bold("SR")), R.cell(f"{_sr(r):.1f}", align="right"),
         R.cell(R.bold("Avg")),
         R.cell(f"{avg:.2f}" if avg is not None else "—", align="right")],
        [R.cell(R.bold("4s / 6s")),
         R.cell(f"{r.bat_fours or 0} / {r.bat_sixes or 0}", align="right"),
         R.cell(R.bold("HS")), R.cell(str(r.highest_score or 0), align="right")],
    ]


def _bowling_rows(r):
    best = (f"{r.best_bowl_wickets}/{r.best_bowl_runs}"
            if r.best_bowl_wickets is not None and r.best_bowl_runs is not None
            and r.best_bowl_runs >= 0 else "—")
    return [
        [R.cell(R.bold("Wickets")),
         R.cell(R.bold(str(r.bowl_wickets or 0)), align="right"),
         R.cell(R.bold("Runs")), R.cell(str(r.bowl_runs or 0), align="right")],
        [R.cell(R.bold("Econ")), R.cell(f"{_econ(r):.2f}", align="right"),
         R.cell(R.bold("Best")), R.cell(best, align="right")],
    ]


def _keyboard(active, opener_tg):
    """Build the category inline keyboard, marking the active one and binding the opener."""
    rows, row = [], []
    for key, label in _CATEGORIES:
        mark = "● " if key == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}",
                                        callback_data=f"tstat_{key}_{opener_tg}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def tournamentstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tournamentstats — Top-10 tournament leaderboards with category buttons."""
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await update.message.reply_text(NO_ACTIVE)
            return
        opener = update.effective_user.id
        rows = _leaders_for(session, tour, "runs")
        await R.reply_rich(
            update.message, _leaderboard_blocks(tour, "runs", rows),
            _render(tour, "runs", rows),
            reply_markup=_keyboard("runs", opener))
    except Exception:
        logger.exception("/tournamentstats failed")
        await update.message.reply_text("⚠️ Could not load tournament stats.")
    finally:
        session.close()


async def tournamentstats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch the /tournamentstats view — restricted to the user who opened it."""
    q = update.callback_query
    try:
        _, cat, opener = q.data.split("_", 2)
        opener = int(opener)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    if q.from_user.id != opener:
        await q.answer("Only the person who opened this can use these buttons.",
                       show_alert=True)
        return
    if cat not in _CAT_LABELS:
        await q.answer("Unknown category.", show_alert=True)
        return
    await q.answer()
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await q.edit_message_text(NO_ACTIVE)
            return
        rows = _leaders_for(session, tour, cat)
        await R.edit_rich(q, _leaderboard_blocks(tour, cat, rows),
                          _render(tour, cat, rows),
                          reply_markup=_keyboard(cat, opener))
    except Exception:
        logger.exception("/tournamentstats callback failed")
    finally:
        session.close()


async def statstour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/statstour <player name> — a player's stats in the active tournament."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /statstour <player name>\nExample: /statstour Virat Kohli")
        return
    search = " ".join(context.args).strip()
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await update.message.reply_text(NO_ACTIVE)
            return
        rows = (session.query(TournamentPlayerStats)
                .filter(TournamentPlayerStats.tournament_id == tour.id,
                        func.lower(TournamentPlayerStats.name).like(f"%{search.lower()}%"))
                .all())
        if not rows:
            await update.message.reply_text(
                f"❌ No tournament stats found for “{search}” in {tour.name}.")
            return
        # Prefer an exact name match; a player may appear for more than one team.
        exact = [r for r in rows if (r.name or "").strip().lower() == search.lower()]
        rows = exact or rows
        rows = sorted(rows, key=lambda r: (r.bat_runs or 0), reverse=True)[:3]

        blocks = [f"🏆 <b>{html.escape(tour.name)}</b> — Player Tournament Stats"]
        for r in rows:
            fig = (f"{r.best_bowl_wickets}/{r.best_bowl_runs}"
                   if r.best_bowl_wickets is not None and r.best_bowl_runs is not None
                   and r.best_bowl_runs >= 0 else "—")
            team = f" · {html.escape(r.team_name)}" if r.team_name else ""
            avg = tournament_service.batting_average(r)
            avg_s = f"{avg:.2f}" if avg is not None else "—"
            blocks.append(
                f"\n👤 <b>{html.escape(r.name or 'Player')}</b>{team}\n"
                f"🎮 Matches: {r.matches}\n"
                f"🏏 Runs: {r.bat_runs} ({r.bat_balls}b) · SR {_sr(r):.1f} · Avg {avg_s}\n"
                f"   4s: {r.bat_fours} · 6s: {r.bat_sixes} · HS: {r.highest_score}\n"
                f"🎯 Wickets: {r.bowl_wickets} · Runs: {r.bowl_runs} · Econ {_econ(r):.2f}\n"
                f"   Best: {fig}")
        await R.reply_rich(update.message, _player_stats_blocks(tour, rows),
                           "\n".join(blocks))
    except Exception:
        logger.exception("/statstour failed")
        await update.message.reply_text("⚠️ Could not load player tournament stats.")
    finally:
        session.close()
