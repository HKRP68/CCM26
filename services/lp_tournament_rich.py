"""The Lets Play Tournament's four public views as rich messages.

``services/lp_tournament_service.py`` keeps the competition and its HTML
renderers (``render_table``, ``render_fixtures``, ``render_teams``,
``render_overview``); this module is their Bot API 10.1 twin, built the way
``docs/rich-text-messages.md`` prescribes — same content, same order, HTML
always passed along as the fallback.

A Lets Play team is a person rather than a franchise, so the columns differ from
the Challenge League's (no home ground, no co-owners, a Telegram id instead of
an owner name) and the two modules stay separate rather than sharing a builder
that would have to branch on every row.
"""

import logging

from models import TournamentMatch
from services import lp_tournament_service as L
from services import rich_message as R
from services import tournament_service

logger = logging.getLogger(__name__)


def table_blocks(session, tour):
    """The points table — the twin of ``render_table``."""
    rows = tournament_service.points_table(session, tour.id)
    blocks = [R.heading(f"🏆 {tour.name}", size=2),
              R.paragraph(R.bold("📊 Points Table"))]
    if not rows:
        blocks.append(R.paragraph(R.italic("No teams have been added yet.")))
        return blocks

    played, total = tournament_service.league_progress(session, tour.id)
    status = [L.status_label(tour)]
    if total:
        status.append(f"  ·  {played}/{total} league matches played")
    blocks.append(R.paragraph(status))

    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("TEAM"), header=True)]
    header += [R.cell(R.bold(label), header=True, align="right")
               for label in ("P", "W", "L", "T", "PTS", "NRR")]
    cells = [header]
    for position, tt in enumerate(rows, 1):
        adjusted = int(getattr(tt, "points_adjust", 0) or 0)
        cells.append([
            R.cell(str(position), align="center"),
            R.cell((tt.name or "—") + ("*" if adjusted else "")),
            R.cell(str(tt.played or 0), align="right"),
            R.cell(str(tt.won or 0), align="right"),
            R.cell(str(tt.lost or 0), align="right"),
            R.cell(str(tt.tied or 0), align="right"),
            R.cell(R.bold(str(tt.points or 0)), align="right"),
            R.cell(L._nrr_text(tt._nrr), align="right"),
        ])
    blocks.append(R.table(cells, bordered=True, striped=True, compact=True))

    adjustments = [tt for tt in rows if int(getattr(tt, "points_adjust", 0) or 0)]
    if adjustments:
        blocks.append(R.details(
            R.italic("* points adjusted by an admin"),
            [R.list_block([
                [R.bold(tt.name or "—"), f"  {int(tt.points_adjust):+d} — ",
                 R.italic(tt.points_adjust_note or "no reason recorded")]
                for tt in adjustments])]))

    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        blocks.append(R.pullquote(["🥇 ", R.bold(champion.name or "—")],
                                  caption="Champion"))
    return blocks


def fixtures_blocks(session, tour, viewer_tg_id=None, limit=None):
    """The schedule — the twin of ``render_fixtures``.

    Every fixture is rendered; ``limit`` (default ``L.FIXTURE_OPEN``) only
    decides how many sit in the open table, the rest going behind a ``details``
    block the client collapses.
    """
    limit = L.FIXTURE_OPEN if limit is None else int(limit)
    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .order_by(TournamentMatch.match_no, TournamentMatch.round_no,
                          TournamentMatch.id).all())
    blocks = [R.heading(f"🗓️ {tour.name}", size=2),
              R.paragraph(R.bold("Fixtures"))]
    if not fixtures:
        blocks.append(R.paragraph(
            "No schedule has been generated — this tournament is free-play: "
            "any two entered players can start /lptour."))
        return blocks

    names = {tt.id: (tt.name or "—") for tt in L.participants(session, tour.id)}
    mine = None
    if viewer_tg_id is not None:
        my_team = L.team_for_tg(session, tour.id, viewer_tg_id)
        mine = my_team.id if my_team else None

    def row(fx):
        def slot(team_id, label):
            return names.get(team_id) or label or "TBD"
        versus = f"{slot(fx.team1_id, fx.slot1_label)}  vs  " \
                 f"{slot(fx.team2_id, fx.slot2_label)}"
        if fx.status == "completed":
            versus = R.strikethrough(versus)
            state = ["✅ ", R.italic(fx.result_text or "done")]
        elif fx.status == "live":
            state = ["🔴 ", R.bold("in progress")]
        else:
            state = ["⚪ ", R.italic("to play")]
        return [R.cell(R.code(_tag(fx)), align="center"),
                R.cell(versus), R.cell(state)]

    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("MATCH"), header=True),
              R.cell(R.bold("STATE"), header=True)]
    my_rows = [row(fx) for fx in fixtures
               if mine and fx.status != "completed"
               and mine in (fx.team1_id, fx.team2_id)][:8]
    if my_rows:
        blocks.append(R.table([header] + my_rows, bordered=True, compact=True,
                              caption=R.bold("👈 Your next matches")))
    all_rows = [row(fx) for fx in fixtures]
    blocks.append(R.table([header] + all_rows[:limit],
                          bordered=True, striped=True, compact=True,
                          caption=R.bold(f"Full schedule · {len(all_rows)} "
                                         f"matches")))
    rest = all_rows[limit:]
    if rest:
        blocks.append(R.details(
            R.bold(f"👇 Matches {limit + 1}–{len(all_rows)}"),
            [R.table([header] + rest, bordered=True, striped=True,
                     compact=True)]))
    blocks.append(R.footer(["Play your fixture: reply to your opponent with ",
                            R.code("/lptour")]))
    return blocks


def _tag(fx):
    """``M7``, or the stage name for a knockout fixture with no number."""
    stage = L._STAGE_LABEL.get(fx.stage or "league", (fx.stage or "").title())
    return f"M{fx.match_no}" if fx.match_no else stage


def teams_blocks(session, tour):
    """Who is entered — the twin of ``render_teams``."""
    rows = L.participants(session, tour.id)
    blocks = [R.heading(f"👥 {tour.name}", size=2),
              R.paragraph([R.bold("Teams"),
                           f"  ·  {len(rows)}/{tour.max_teams or '∞'}"])]
    if not rows:
        blocks.append(R.paragraph(
            ["Nobody has been added yet. An admin adds players with ",
             R.code("/lptadd <telegram_id>"), "."]))
        return blocks
    # Plain text, never a mention: a squad list must not ping everyone in it.
    blocks.append(R.table(
        [[R.cell(R.bold("#"), header=True, align="center"),
          R.cell(R.bold("TEAM"), header=True),
          R.cell(R.bold("TELEGRAM ID"), header=True, align="right")]]
        + [[R.cell(str(i), align="center"),
            R.cell(R.bold(tt.name or "—")),
            R.cell(R.code(str(tt.user_tg_id or "—")), align="right")]
           for i, tt in enumerate(rows, 1)],
        bordered=True, striped=True, compact=True))
    return blocks


def overview_blocks(session, tour):
    """The front page — the twin of ``render_overview``."""
    rows = L.participants(session, tour.id)
    played, total = tournament_service.league_progress(session, tour.id)
    table = tournament_service.points_table(session, tour.id)

    facts = [
        [R.cell(R.bold("Status")), R.cell(L.status_label(tour))],
        [R.cell(R.bold("Format")),
         R.cell(f"{tour.format or 'League'} · {tour.overs} overs · own rosters")],
        [R.cell(R.bold("Teams")),
         R.cell(f"{len(rows)}/{tour.max_teams or '∞'}")],
    ]
    if total:
        facts.append([R.cell(R.bold("League")),
                      R.cell(f"{played}/{total} matches played")])
    elif rows:
        facts.append([R.cell(R.bold("Schedule")),
                      R.cell("free-play (no fixture list)")])

    blocks = [R.heading(f"🏆 {tour.name}", size=1),
              R.table(facts, bordered=True, compact=True)]
    if tour.description:
        blocks.append(R.blockquote([R.paragraph(R.italic(tour.description))]))
    if table:
        blocks.append(R.table(
            [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("TEAM"), header=True),
              R.cell(R.bold("PTS"), header=True, align="right"),
              R.cell(R.bold("W-L"), header=True, align="right"),
              R.cell(R.bold("NRR"), header=True, align="right")]]
            + [[R.cell(str(i), align="center"),
                R.cell(tt.name or "—"),
                R.cell(R.bold(str(tt.points or 0)), align="right"),
                R.cell(f"{tt.won or 0}-{tt.lost or 0}", align="right"),
                R.cell(L._nrr_text(tt._nrr), align="right")]
               for i, tt in enumerate(table[:5], 1)],
            bordered=True, striped=True, compact=True,
            caption=R.bold("Top of the table")))
    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        blocks.append(R.pullquote(["🥇 ", R.bold(champion.name or "—")],
                                  caption="Champion"))
    blocks.append(R.footer(["Play a fixture: reply to your opponent with ",
                            R.code("/lptour")]))
    return blocks


def render_blocks(session, tour, view, viewer_tg_id=None):
    """The block twin of the /lpt card's four views, by name.

    ``None`` on any failure, which tells the caller to send the HTML: a broken
    builder must never cost a player the answer.
    """
    try:
        if view == "table":
            return table_blocks(session, tour)
        if view == "fixtures":
            return fixtures_blocks(session, tour, viewer_tg_id=viewer_tg_id)
        if view == "teams":
            return teams_blocks(session, tour)
        return overview_blocks(session, tour)
    except Exception:
        logger.exception("Lets Play rich view %r failed to build", view)
        return None
