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

# The symbol each board's number wears, and the word for it. "412" alone says a
# player is top of *something*; a board read on a phone should not need its own
# header held in mind to be read.
_CAT_ICON = {
    "mvp": "🏅", "runs": "🏃", "wkts": "🎯", "sixes": "6️⃣", "fours": "4️⃣",
    # ⚖️ rather than the 📊 the button wears, so the average does not collide
    # with the match count that closes the same line.
    "hs": "⭐", "avg": "⚖️", "fig": "💥", "sr": "⚡", "econ": "🛡️",
}
_CAT_UNIT = {
    "mvp": "pts", "runs": "runs", "wkts": "wkts", "sixes": "sixes",
    "fours": "fours", "hs": "", "avg": "avg", "fig": "", "sr": "SR",
    "econ": "econ",
}
# What closes a line: how many matches it took. Carried only by the boards that
# rank a season — Highest Score ranks one knock and Best Figure one spell, and
# "in how many matches" has one answer there, so those leave it off.
_MATCHES_ICON = "📊"


def _sr(r):
    """Batting strike rate (runs per 100 balls) for a stats row, 0 if no balls."""
    return ((r.bat_runs or 0) / r.bat_balls * 100.0) if r.bat_balls else 0.0


def _econ(r):
    """Bowling economy (runs per over) for a stats row, 0 if no balls bowled."""
    overs = (r.bowl_balls or 0) / 6.0
    return ((r.bowl_runs or 0) / overs) if overs else 0.0


def _overs(balls):
    """Balls as cricket overs — 110 balls is 18.2, never 18.33."""
    balls = int(balls or 0)
    return f"{balls // 6}.{balls % 6}"


def _figure(r):
    """A best bowling figure, or ``None`` when nobody has taken one yet.

    ``best_bowl_runs`` is seeded at -1 rather than 0 precisely because 0 runs is
    a real (excellent) figure, so the sentinel is what this checks.
    """
    wkts = getattr(r, "best_bowl_wickets", None)
    runs = getattr(r, "best_bowl_runs", None)
    if wkts is None or runs is None or runs < 0:
        return None
    return f"{wkts}/{runs}"


def _leader(name, team, value, *, extras=(), matches=None, cells=()):
    """One ranked row, with the numbers behind its number.

    ``extras`` is what the HTML board prints in the bracket and ``cells`` the
    columns the table gives them; they carry the same figures in the two shapes
    the two renderers want. Everything is optional — a board with nothing to add
    still renders as the single-number line it always was.
    """
    return {"name": name, "team": team, "value": str(value),
            "extras": [e for e in extras if e], "matches": matches,
            "cells": [c for c in cells if c]}


def _unpack(row):
    """A board row as a dict, whatever shape the caller built it in.

    Rows used to be ``(name, team, value)`` tuples and a caller outside this
    module may still build one; those simply have nothing extra to show.
    """
    if isinstance(row, dict):
        return row
    name, team, value = (list(row) + [None, None, None])[:3]
    return _leader(name, team, value)


def _leaders_for(session, tour, category, limit=BOARD_LIMIT):
    """The ranked rows for one category, best first.

    Ranked ``limit`` deep rather than ten, and the renderers below decide how
    much of that is open and how much is behind a tap.

    Each row carries the numbers *behind* its number. "383" says who is top and
    nothing else — 383 off 144 balls in 13 matches is a season, and it is the
    difference between a board that settles an argument and one that starts
    one. The figures are already loaded (``stat_leaders`` reads whole rows), so
    this costs nothing but the formatting.
    """
    leaders = tournament_service.stat_leaders(session, tour.id, limit=limit)
    out = []
    if category == "runs":
        out = [_leader(r.name, r.team_name, r.bat_runs,
                       extras=[f"{r.bat_balls}b", f"SR {_sr(r):.2f}"],
                       matches=r.matches,
                       cells=[("BALLS", str(r.bat_balls or 0)),
                              ("SR", f"{_sr(r):.2f}"),
                              ("M", str(r.matches or 0))])
               for r in leaders["most_runs"]]
    elif category == "wkts":
        out = [_leader(r.name, r.team_name, r.bowl_wickets,
                       extras=[f"{_overs(r.bowl_balls)} ov",
                               f"Econ {_econ(r):.2f}",
                               (f"Best {_figure(r)}" if _figure(r) else None)],
                       matches=r.matches,
                       cells=[("OVERS", _overs(r.bowl_balls)),
                              ("ECON", f"{_econ(r):.2f}"),
                              ("BEST", _figure(r) or "—"),
                              ("M", str(r.matches or 0))])
               for r in leaders["most_wickets"]]
    elif category == "sixes":
        out = [_leader(r.name, r.team_name, r.bat_sixes,
                       extras=[f"{r.bat_runs} runs", f"SR {_sr(r):.2f}"],
                       matches=r.matches,
                       cells=[("RUNS", str(r.bat_runs or 0)),
                              ("SR", f"{_sr(r):.2f}"),
                              ("M", str(r.matches or 0))])
               for r in leaders["most_sixes"]]
    elif category == "fours":
        out = [_leader(r.name, r.team_name, r.bat_fours,
                       extras=[f"{r.bat_runs} runs", f"SR {_sr(r):.2f}"],
                       matches=r.matches,
                       cells=[("RUNS", str(r.bat_runs or 0)),
                              ("SR", f"{_sr(r):.2f}"),
                              ("M", str(r.matches or 0))])
               for r in leaders["most_fours"]]
    elif category == "hs":
        # A single knock, not a season: the strike rate is this innings' own,
        # and there is no match count to give because the answer is one.
        out = [_leader(r.name, r.team_name,
                       f"{r.highest_score}{'*' if r.not_out else ''}",
                       extras=[f"{r.bat_balls}b",
                               (f"SR {r.highest_score * 100.0 / r.bat_balls:.2f}"
                                if r.bat_balls else None)],
                       cells=[("BALLS", str(r.bat_balls or 0)),
                              ("SR", (f"{r.highest_score * 100.0 / r.bat_balls:.2f}"
                                      if r.bat_balls else "—"))])
               for r in leaders["highest_score"]]
    elif category == "avg":
        out = [_leader(r.name, r.team_name, f"{v:.2f}",
                       extras=[f"{r.bat_runs} runs", f"{r.bat_outs} outs"],
                       matches=r.matches,
                       cells=[("RUNS", str(r.bat_runs or 0)),
                              ("OUTS", str(r.bat_outs or 0)),
                              ("M", str(r.matches or 0))])
               for r, v in leaders["top_average"]]
    elif category == "fig":
        # One spell, so there is nothing behind the figure but the figure.
        out = [_leader(r.name, r.team_name,
                       f"{r.best_bowl_wickets}/{r.best_bowl_runs}")
               for r in leaders["best_figure"]]
    elif category == "sr":
        out = [_leader(r.name, r.team_name, f"{v:.2f}",
                       extras=[f"{r.bat_runs} runs", f"{r.bat_balls}b"],
                       matches=r.matches,
                       cells=[("RUNS", str(r.bat_runs or 0)),
                              ("BALLS", str(r.bat_balls or 0)),
                              ("M", str(r.matches or 0))])
               for r, v in leaders["best_strike_rate"]]
    elif category == "econ":
        out = [_leader(r.name, r.team_name, f"{v:.2f}",
                       extras=[f"{r.bowl_wickets} wkts",
                               f"{_overs(r.bowl_balls)} ov"],
                       matches=r.matches,
                       cells=[("WKTS", str(r.bowl_wickets or 0)),
                              ("OVERS", _overs(r.bowl_balls)),
                              ("M", str(r.matches or 0))])
               for r, v in leaders["best_economy"]]
    elif category == "mvp":
        # MVP is the one board whose number means nothing on its own, so each
        # row carries the season behind it — the total is what ranks them, the
        # line under it is why.
        out = [_leader(r.name, r.team_name, f"{r.points:g} pts",
                       extras=[f"{r.bat_runs} runs", f"{r.bowl_wickets} wkts",
                               f"{r.wins}W",
                               (f"{r.awards}⭐" if r.awards else None)],
                       matches=r.matches,
                       cells=[("RUNS", str(r.bat_runs or 0)),
                              ("WKTS", str(r.bowl_wickets or 0)),
                              ("M", str(r.matches or 0))])
               for r in leaders["mvp"]]
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
        lines += [_rank_line(i, row, category)
                  for i, row in enumerate(rows[:BOARD_OPEN], 1)]
        rest = rows[BOARD_OPEN:]
        if rest:
            lines += ["",
                      f"<b>👇 Ranks {BOARD_OPEN + 1}–{len(rows)}</b> "
                      f"<i>(tap to expand)</i>"]
            lines += expandable_quotes(
                _rank_line(i, row, category)
                for i, row in enumerate(rest, BOARD_OPEN + 1))
    if category == "mvp":
        lines += ["", "<i>Impact points across the whole tournament — "
                      "/mvp for the full card.</i>"]
    return "\n".join(lines)


def _rank_line(position, row, category="runs"):
    """One ranked entry of the HTML board.

    Two lines, not one. The first names the player and their side; the second
    is what they actually did — the ranked number with its unit, the figures
    behind it in a bracket, and how many matches it took:

        🥇 Sai Sudharsan (PBKS)
             — 🏃 383 runs (144b · SR 265.97) · 📊 13 M

    A row with nothing to add falls back to the single line this used to be, so
    a board of bare figures (a best spell, say) does not grow a blank second
    line under every entry.
    """
    row = _unpack(row)
    rank = _MEDALS.get(position, f"{position}.")
    team_s = f" <i>({html.escape(str(row['team']))})</i>" if row.get("team") else ""
    head = f"{rank} <b>{html.escape(str(row.get('name') or 'Player'))}</b>{team_s}"

    icon = _CAT_ICON.get(category, "")
    unit = _CAT_UNIT.get(category, "")
    value = f"<b>{html.escape(str(row.get('value')))}</b>"
    detail = f"{icon} {value}".strip()
    if unit:
        detail += f" {unit}"
    if row.get("extras"):
        detail += " (" + " · ".join(html.escape(str(e)) for e in row["extras"]) + ")"
    if row.get("matches"):
        detail += f" · {_MATCHES_ICON} {row['matches']} M"
    if not row.get("extras") and not row.get("matches"):
        # Nothing behind the number — keep it on one line rather than wrapping
        # a bare figure onto its own.
        return f"{head} — {detail}"
    return f"{head}\n     — {detail}"


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
    """``rows`` as table rows, headed and numbered from ``start``.

    The supporting figures get columns of their own rather than the HTML
    board's bracket — a table has headers, which is the one place the numbers
    can be labelled without repeating the label on every row. The headers come
    from the first row that has any, so every row is laid out the same way even
    when one of them is missing a figure.
    """
    unpacked = [_unpack(row) for row in rows]
    extra_heads = next(([head for head, _ in row["cells"]]
                        for row in unpacked if row.get("cells")), [])
    cells = [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("TEAM"), header=True),
              R.cell(R.bold(_CAT_COLUMN.get(category, "VALUE")), header=True,
                     align="right")]
             + [R.cell(R.bold(head), header=True, align="right")
                for head in extra_heads]]
    for position, row in enumerate(unpacked, start):
        values = dict(row.get("cells") or ())
        cells.append([
            R.cell(_MEDALS.get(position, f"{position}."), align="center"),
            R.cell(row.get("name") or "Player"),
            R.cell(row.get("team") or "—"),
            R.cell(R.bold(str(row.get("value"))), align="right"),
        ] + [R.cell(values.get(head, "—"), align="right")
             for head in extra_heads])
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
