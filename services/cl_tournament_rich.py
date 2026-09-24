"""Every Challenge League Tournament view as a rich message, beside its HTML twin.

``services/cl_tournament_view.py`` keeps the queries and the plain-HTML
renderers (``render_table``, ``render_fixtures``, ``render_team_schedule`` …).
This module adds the Bot API 10.1 block rendering of the same things, following
the recipe in ``docs/rich-text-messages.md``: every builder here has an HTML
renderer next to it, the two share the logic that decides *content* (they call
into the view module for it), and a sender always passes the HTML as the
fallback so a refused rich message never loses the answer.

Why these views in particular: three of them are columns of numbers that the
HTML rendering has to fake with ``<code>`` and ``str.rjust``. That only lines up
while every team name is the same width, which lasts until somebody enters
"Royal Challengers Bengaluru". A native table is aligned by the client, so the
points table, the fixture list and a team's card stay readable at any name
length and on any screen.

Nothing here talks to Telegram; ``handlers/cl_tournament.py`` sends what these
build.
"""

import logging

from services import cl_tournament_view as V
from services import rich_message as R
from services import tournament_service

logger = logging.getLogger(__name__)

# The struck-through "already played" fixture, the pitch a match is pinned to,
# and the home-side marker are the three things a fixture list is read for, so
# they get the same glyphs in both renderings.
_LEGEND = ("🏠 home side · 🌱 the pitch this match must be played on · "
           "struck-through matches are done.")


def _guarded(name):
    """Wrap a builder so a failure costs the rendering, never the answer.

    ``handlers/cl_tournament.py`` calls the team cards directly rather than
    through :func:`render_blocks`, so the guard belongs on each builder rather
    than on the dispatcher. ``None`` is what tells ``rich_message`` to send the
    HTML twin instead.
    """
    def decorate(build):
        def guarded(*args, **kwargs):
            try:
                return build(*args, **kwargs)
            except Exception:
                logger.exception("Challenge League rich %s failed to build",
                                 name)
                return None
        guarded.__name__ = build.__name__
        guarded.__doc__ = build.__doc__
        return guarded
    return decorate


def _fixture_tag(fx):
    """``M7``, or the stage name for a knockout with no match number."""
    stage = V._STAGE_LABEL.get(fx.stage or "league", (fx.stage or "").title())
    return f"M{fx.match_no}" if fx.match_no else stage


def _form_run(form):
    """A team's last results as coloured pips, oldest first."""
    return " ".join(V._FORM_EMOJI.get(f, "⚪") for f in reversed(form))


# ── Points table ─────────────────────────────────────────────────────

@_guarded("points table")
def table_blocks(session, tour):
    """The points table as a native table — the twin of ``render_table``.

    The HTML version pads ``table_name_cell`` to fourteen characters and hopes;
    here the client aligns the columns, so the name column carries the full name
    and the adjustment star keeps its meaning without eating a letter of it.
    """
    rows = tournament_service.points_table(session, tour.id)
    blocks = [R.heading(f"🏆 {tour.name}", size=2),
              R.paragraph(R.bold("📊 Points Table"))]
    if not rows:
        blocks.append(R.paragraph(
            R.italic("No teams have been added to this tournament yet.")))
        return blocks

    played, total = tournament_service.league_progress(session, tour.id)
    status = [V.status_label(tour)]
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
        name = (tt.name or "—") + ("*" if adjusted else "")
        cells.append([
            R.cell(str(position), align="center"),
            R.cell(name),
            R.cell(str(tt.played or 0), align="right"),
            R.cell(str(tt.won or 0), align="right"),
            R.cell(str(tt.lost or 0), align="right"),
            R.cell(str(tt.tied or 0), align="right"),
            R.cell(R.bold(str(tt.points or 0)), align="right"),
            R.cell(V._nrr_text(tt._nrr), align="right"),
        ])
    blocks.append(R.table(cells, bordered=True, striped=True, compact=True))

    adjustments = [tt for tt in rows
                   if int(getattr(tt, "points_adjust", 0) or 0)]
    if adjustments:
        blocks.append(R.details(
            R.italic("* points adjusted by an admin"),
            [R.list_block([
                [R.bold(tt.name or "—"), f"  {int(tt.points_adjust):+d} — ",
                 R.italic(tt.points_adjust_note or "no reason recorded")]
                for tt in adjustments])]))

    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        blocks.append(R.pullquote(
            ["🥇 ", R.bold(champion.name or "—")], caption="Champion"))
    return blocks


# ── Fixtures ─────────────────────────────────────────────────────────

def _fixture_row(fx, names, mine_ids):
    """One fixture as a table row: tag, the two sides, and where it stands."""
    def slot(team_id, label):
        if team_id and team_id in names:
            text = names[team_id]
            return f"{text} 🏠" if fx.home_team_id and team_id == fx.home_team_id \
                else text
        return label or "TBD"

    a = slot(fx.team1_id, fx.slot1_label)
    b = slot(fx.team2_id, fx.slot2_label)
    versus = f"{a}  vs  {b}"
    if fx.status == "completed":
        # Struck through, exactly as the HTML does with <s> — a played fixture
        # is still worth reading, it just is not something left to do.
        state = ["✅ ", R.italic(fx.result_text or "done")]
        versus = R.strikethrough(versus)
    elif fx.status == "live":
        state = ["🔴 ", R.bold("in progress")]
    else:
        pitch = (fx.pitch_type or "").strip()
        state = ["⚪ ", "🌱 " + pitch if pitch else "—"]
    row = [R.cell(R.code(_fixture_tag(fx)), align="center"),
           R.cell(versus), R.cell(state)]
    if mine_ids and mine_ids & {fx.team1_id, fx.team2_id}:
        row[1] = R.cell([R.bold("👈 "), versus])
    return row


@_guarded("fixtures")
def fixtures_blocks(session, tour, viewer_tg_id=None, limit=None):
    """The schedule — the twin of ``render_fixtures``.

    Keeps the HTML version's shape: the viewer's own remaining matches first,
    then the full list. "Yours" still means owner *or* co-owner.

    Every fixture is rendered. ``limit`` (default :data:`V.FIXTURE_OPEN`) only
    decides how many sit in the open table; the rest go into a ``details``
    block, which is the client's own collapsible rather than the HTML twin's
    expandable blockquote.
    """
    limit = V.FIXTURE_OPEN if limit is None else int(limit)
    from models import TournamentMatch
    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                          TournamentMatch.id).all())
    blocks = [R.heading(f"🗓️ {tour.name}", size=2),
              R.paragraph(R.bold("Fixtures"))]
    if not fixtures:
        blocks.append(R.paragraph(
            "No schedule has been generated yet — this tournament is "
            "free-play: any two participating teams can start a match."))
        return blocks

    rows = V.teams(session, tour.id)
    names = {tt.id: (tt.name or "—") for tt in rows}
    mine = {tt.id for tt in rows
            if viewer_tg_id is not None
            and tournament_service.is_team_member(tt, viewer_tg_id)}

    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("MATCH"), header=True),
              R.cell(R.bold("STATE"), header=True)]

    my_rows = [_fixture_row(fx, names, mine) for fx in fixtures
               if mine and fx.status != "completed"
               and mine & {fx.team1_id, fx.team2_id}][:8]
    if my_rows:
        blocks.append(R.table([header] + my_rows, bordered=True, compact=True,
                              caption=R.bold("👈 Your next matches")))

    all_rows = [_fixture_row(fx, names, mine) for fx in fixtures]
    blocks.append(R.table([header] + all_rows[:limit], bordered=True,
                          striped=True, compact=True,
                          caption=R.bold(f"Full schedule · {len(all_rows)} "
                                         f"matches")))
    rest = all_rows[limit:]
    if rest:
        blocks.append(R.details(
            R.bold(f"👇 Matches {limit + 1}–{len(all_rows)}"),
            [R.table([header] + rest, bordered=True, striped=True,
                     compact=True)]))
    blocks.append(R.footer(R.italic(_LEGEND)))
    return blocks


# ── One team: schedule, then numbers ─────────────────────────────────

def _standing_paragraph(session, tour, team):
    """The standing line, or None when the team is not in the table yet."""
    pos, standing = V._standing_of(session, tour, team.id)
    if standing is None:
        return None
    return R.paragraph([
        "📊 ", R.bold(f"#{pos}"),
        f"  ·  {standing.played or 0}P {standing.won or 0}W "
        f"{standing.lost or 0}L {standing.tied or 0}T  ·  ",
        R.bold(f"{standing.points or 0} pts"),
        f"  ·  NRR {V._nrr_text(standing._nrr)}"])


def _team_heading(session, tour, team, viewer_tg_id, emoji):
    """The shared head of both team cards: who they are and where they stand."""
    mine = (viewer_tg_id is not None
            and tournament_service.is_team_member(team, viewer_tg_id))
    title = [f"{emoji} ", R.bold(team.name or "—")]
    if mine:
        title += ["  ", R.italic("👈 your team")]
    blocks = [R.heading(title, size=2),
              R.paragraph(R.italic(tour.name or "—"))]
    standing = _standing_paragraph(session, tour, team)
    if standing is not None:
        blocks.append(standing)
    form = V.team_form(session, tour.id, team.id)
    if form:
        blocks.append(R.paragraph(
            ["📈 ", R.bold("Form: "), _form_run(form), "  ",
             R.italic("(oldest → latest)")]))
    return blocks


@_guarded("team schedule")
def team_schedule_blocks(session, tour, team, viewer_tg_id=None, limit=40):
    """One team's whole tournament — the twin of ``render_team_schedule``.

    Every fixture is written from this team's side, won or lost, exactly as the
    HTML version writes it; the results go into a collapsed ``details`` because
    the question the card is opened for is what comes *next*.

    Nothing is dropped. ``limit`` caps how many fixtures the open table holds,
    and a longer list continues in a second, collapsed one.
    """
    from models import TournamentMatch
    blocks = _team_heading(session, tour, team, viewer_tg_id, "🗓️")

    owner = (team.owner_name or "").strip()
    extras = len(tournament_service.co_owner_ids(team))
    if owner or extras:
        line = ["👤 ", owner] if owner else ["👤 ", R.italic("no owner")]
        if extras:
            line.append(f"  🤝 +{extras}")
        blocks.append(R.paragraph(line))
    home_pitch = (team.home_pitch or "").strip()
    if home_pitch:
        blocks.append(R.paragraph([R.bold("🏟️ Home pitch: "),
                                   f"🌱 {home_pitch}"]))

    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .filter((TournamentMatch.team1_id == team.id)
                        | (TournamentMatch.team2_id == team.id))
                .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                          TournamentMatch.id).all())
    if not fixtures:
        blocks.append(R.paragraph(
            "No fixtures are scheduled for this team yet — this tournament is "
            "free-play: any two participating teams can start a match."))
        return blocks

    names = {tt.id: (tt.name or "—") for tt in V.teams(session, tour.id)}

    def row(fx):
        other_id = fx.team2_id if fx.team1_id == team.id else fx.team1_id
        other = (names.get(other_id)
                 or (fx.slot2_label if fx.team1_id == team.id
                     else fx.slot1_label) or "TBD")
        at_home = fx.home_team_id == team.id
        venue = "🏠 vs" if at_home else ("✈️ at" if fx.home_team_id else "vs")
        if fx.status == "completed":
            if fx.winner_team_id == team.id:
                mark = ["✅ ", R.bold("WON")]
            elif fx.winner_team_id:
                mark = ["❌ ", R.bold("LOST")]
            else:
                mark = ["🤝 ", R.bold("TIED")]
            state = [mark, "  ", R.italic(fx.result_text or "done")]
        elif fx.status == "live":
            state = ["🔴 ", R.bold("in progress")]
        else:
            pitch = (fx.pitch_type or "").strip()
            state = ["⚪ ", "🌱 " + pitch if pitch else "—"]
        return [R.cell(R.code(_fixture_tag(fx)), align="center"),
                R.cell(f"{venue}  {other}"),
                R.cell(state)]

    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("OPPONENT"), header=True),
              R.cell(R.bold("STATE"), header=True)]
    upcoming = [fx for fx in fixtures if fx.status != "completed"]
    played = [fx for fx in fixtures if fx.status == "completed"]

    if upcoming:
        open_rows = [row(fx) for fx in upcoming[:limit]]
        blocks.append(R.table(
            [header] + open_rows, bordered=True, compact=True,
            caption=R.bold(f"Next up ({len(upcoming)} to play)")))
        rest = [row(fx) for fx in upcoming[limit:]]
        if rest:
            blocks.append(R.details(
                R.bold(f"👇 {len(rest)} more to play"),
                [R.table([header] + rest, bordered=True, compact=True)]))
    else:
        blocks.append(R.paragraph(
            R.italic("Nothing left to play — every fixture is done.")))
    if played:
        # Newest first: a results list is read backwards, and reversing here is
        # also what stops the oldest ones falling off the end.
        blocks.append(R.details(
            R.bold(f"📜 Results ({len(played)})"),
            [R.table([header] + [row(fx) for fx in reversed(played)],
                     bordered=True, striped=True, compact=True)]))

    blocks.append(R.footer(R.italic(
        "🏠 home · ✈️ away · 🌱 the pitch this match must be played on.")))
    return blocks


@_guarded("team stats")
def team_stats_blocks(session, tour, team, viewer_tg_id=None):
    """One team by the numbers — the twin of ``render_team_stats``."""
    blocks = _team_heading(session, tour, team, viewer_tg_id, "📊")
    blocks.append(R.paragraph(V.status_label(tour)))

    _pos, standing = V._standing_of(session, tour, team.id)
    if standing is not None:
        adjust = int(standing.points_adjust or 0)
        if adjust:
            note = (standing.points_adjust_note or "").strip()
            blocks.append(R.paragraph([
                "⚖️ ", R.bold(f"Points adjustment: {adjust:+d}"),
                (f" — {note}" if note else "")]))

    innings = V.team_innings(session, tour.id, team.id)
    if not innings:
        blocks.append(R.paragraph(R.italic(
            "No completed match with a recorded score yet — the batting and "
            "bowling numbers appear once this team has played one.")))
        return blocks

    names = {tt.id: (tt.name or "—") for tt in V.teams(session, tour.id)}

    def versus(entry):
        return names.get(entry["opponent_id"]) or "—"

    runs_for = sum(e["runs"] for e in innings)
    balls_for = sum(e["balls"] for e in innings)
    wkts_lost = sum(e["wickets"] for e in innings)
    rr_for = V._run_rate(runs_for, balls_for)
    best = max(innings, key=lambda e: e["runs"])
    worst = min(innings, key=lambda e: e["runs"])
    bat_rows = [
        [R.cell(R.bold("Runs")),
         R.cell(R.bold(f"{runs_for:,}"), align="right"),
         R.cell(f"in {V._overs_text(balls_for)} ov")],
        [R.cell(R.bold("Run rate")),
         R.cell(f"{rr_for:.2f}" if rr_for is not None else "—", align="right"),
         R.cell(f"{wkts_lost} wkts lost")],
        [R.cell(R.bold("Average total")),
         R.cell(str(runs_for // len(innings)), align="right"),
         R.cell(f"over {len(innings)} inns")],
        [R.cell(R.bold("Highest")),
         R.cell(R.bold(f"{best['runs']}/{best['wickets']}"), align="right"),
         R.cell(f"vs {versus(best)}")],
    ]
    if worst is not best:
        bat_rows.append([R.cell(R.bold("Lowest")),
                         R.cell(f"{worst['runs']}/{worst['wickets']}",
                                align="right"),
                         R.cell(f"vs {versus(worst)}")])
    blocks.append(R.table(bat_rows, bordered=True, compact=True,
                          caption=R.bold("🏏 With the bat")))

    runs_against = sum(e["conceded"] for e in innings)
    balls_against = sum(e["conceded_balls"] for e in innings)
    rr_against = V._run_rate(runs_against, balls_against)
    tightest = min(innings, key=lambda e: e["conceded"])
    blocks.append(R.table([
        [R.cell(R.bold("Conceded")),
         R.cell(R.bold(f"{runs_against:,}"), align="right"),
         R.cell(f"in {V._overs_text(balls_against)} ov")],
        [R.cell(R.bold("Run rate")),
         R.cell(f"{rr_against:.2f}" if rr_against is not None else "—",
                align="right"),
         R.cell("")],
        [R.cell(R.bold("Best defence")),
         R.cell(R.bold(f"{tightest['conceded']}/{tightest['conceded_wickets']}"),
                align="right"),
         R.cell(f"vs {versus(tightest)}")],
    ], bordered=True, compact=True, caption=R.bold("🎯 With the ball")))

    rows = V.team_player_stats(session, tour, team)
    batters = V._best_batting(rows)
    bowlers = V._best_bowling(rows)
    if batters or bowlers:
        leading = []
        if batters:
            leading.append(R.table(
                [[R.cell(R.bold("PLAYER"), header=True),
                  R.cell(R.bold("RUNS"), header=True, align="right"),
                  R.cell(R.bold("SR"), header=True, align="right"),
                  R.cell(R.bold("HS"), header=True, align="right")]]
                + [[R.cell(r.name or "Player"),
                    R.cell(R.bold(str(r.bat_runs or 0)), align="right"),
                    R.cell(f"{(r.bat_runs or 0) / r.bat_balls * 100.0:.1f}"
                           if r.bat_balls else "—", align="right"),
                    R.cell(str(r.highest_score or 0), align="right")]
                   for r in batters],
                bordered=True, compact=True, caption=R.bold("🏏 Runs")))
        if bowlers:
            leading.append(R.table(
                [[R.cell(R.bold("PLAYER"), header=True),
                  R.cell(R.bold("WKTS"), header=True, align="right"),
                  R.cell(R.bold("ECON"), header=True, align="right"),
                  R.cell(R.bold("BEST"), header=True, align="right")]]
                + [[R.cell(r.name or "Player"),
                    R.cell(R.bold(str(r.bowl_wickets or 0)), align="right"),
                    R.cell(_econ_text(r), align="right"),
                    R.cell(_figure_text(r), align="right")]
                   for r in bowlers],
                bordered=True, compact=True, caption=R.bold("🎯 Wickets")))
        blocks.append(R.details(R.bold("⭐ Leading the way"), leading,
                                is_open=True))

    blocks.append(R.footer(["🗓️ ", R.code("/clsd"),
                            " for this team's fixtures · 🏆 ",
                            R.code("/tournamentstats"),
                            " for the tournament-wide leaderboards."]))
    return blocks


def _econ_text(r):
    econ = V._run_rate(r.bowl_runs, r.bowl_balls)
    return f"{econ:.2f}" if econ is not None else "—"


def _figure_text(r):
    if (r.best_bowl_runs or -1) >= 0 and r.best_bowl_wickets is not None:
        return f"{r.best_bowl_wickets}/{r.best_bowl_runs}"
    return "—"


# ── The field, the front page, the treatment room ────────────────────

@_guarded("teams")
def teams_blocks(session, tour):
    """The field of teams with their owners — the twin of ``render_teams``."""
    rows = V.teams(session, tour.id)
    blocks = [R.heading(f"👥 {tour.name}", size=2),
              R.paragraph([R.bold("Teams"),
                           f"  ·  {len(rows)}/{tour.max_teams or '∞'}"])]
    if not rows:
        blocks.append(R.paragraph(R.italic("No teams have been entered yet.")))
        return blocks

    owned = tournament_service.owner_enforced(tour)
    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("TEAM"), header=True),
              R.cell(R.bold("OWNER"), header=True),
              R.cell(R.bold("🌱 HOME"), header=True)]
    cells = [header]
    for position, tt in enumerate(rows, 1):
        # Plain text, never a mention: a team list must not ping everyone in it.
        owner = (tt.owner_name or "").strip()
        if tt.owner_tg_id and not owner:
            owner = str(tt.owner_tg_id)
        extras = len(tournament_service.co_owner_ids(tt))
        if owner:
            who = [owner] + ([f"  🤝 +{extras}"] if extras else [])
        elif owned or extras:
            who = [R.italic("no owner")]
        else:
            who = ["—"]
        cells.append([R.cell(str(position), align="center"),
                      R.cell(R.bold(tt.name or "—")),
                      R.cell(who),
                      R.cell((tt.home_pitch or "").strip() or "—")])
    blocks.append(R.table(cells, bordered=True, striped=True, compact=True))
    if owned:
        blocks.append(R.footer(R.italic(
            "🔒 Owner-locked: only a team's owner and its co-owners (🤝) may "
            "play with it.")))
    return blocks


@_guarded("overview")
def overview_blocks(session, tour):
    """The tournament's front page — the twin of ``render_overview``."""
    rows = V.teams(session, tour.id)
    played, total = tournament_service.league_progress(session, tour.id)
    table = tournament_service.points_table(session, tour.id)
    lo, hi = tournament_service.overseas_limits(session, tour)

    facts = [
        [R.cell(R.bold("Status")), R.cell(V.status_label(tour))],
        [R.cell(R.bold("League")), R.cell(tour.league_name or "—")],
        [R.cell(R.bold("Format")),
         R.cell(f"{tour.format or 'League'} · {tour.overs} overs")],
        [R.cell(R.bold("Teams")),
         R.cell(f"{len(rows)}/{tour.max_teams or '∞'}")],
    ]
    if total:
        facts.append([R.cell(R.bold("Played")),
                      R.cell(f"{played}/{total} league matches")])
    elif rows:
        facts.append([R.cell(R.bold("Schedule")),
                      R.cell("free-play (no fixture list)")])
    if lo > 0 or hi < 11:
        facts.append([R.cell(R.bold("Overseas in XI")),
                      R.cell(f"min {lo} · max {hi}")])

    from services import league_schedule_service
    if league_schedule_service.pitch_locked(tour):
        facts.append([R.cell(R.bold("Pitch")),
                      R.cell("fixed per fixture — see 🗓️ Fixtures")])
    if tournament_service.owner_enforced(tour):
        facts.append([R.cell(R.bold("Team rule")),
                      R.cell("🔒 owner-locked — only a team's owner and "
                             "co-owners may play with it")])
    from services import injury_service
    if injury_service.enabled_for(tour):
        _e, _chance, cap = injury_service.settings(tour)
        hurt = len(injury_service.active_injuries(session, tour.id))
        facts.append([R.cell(R.bold("Injuries")),
                      R.cell(f"🚑 on — up to {cap} match"
                             f"{'' if cap == 1 else 'es'} out · "
                             + (f"{hurt} sidelined" if hurt
                                else "nobody sidelined"))])

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
                R.cell(V._nrr_text(tt._nrr), align="right")]
               for i, tt in enumerate(table[:5], 1)],
            bordered=True, striped=True, compact=True,
            caption=R.bold("Top of the table")))
    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        blocks.append(R.pullquote(["🥇 ", R.bold(champion.name or "—")],
                                  caption="Champion"))
    blocks.append(R.footer(["Views: ", R.code("/cttable"), " · ",
                            R.code("/ctfixtures"), " · ", R.code("/ctteams"),
                            " · ", R.code("/clsd")]))
    return blocks


@_guarded("injuries")
def injuries_blocks(session, tour, viewer_tg_id=None):
    """The treatment room — the twin of ``render_injuries``."""
    from services import injury_service
    blocks = [R.heading(f"🚑 {tour.name}", size=2),
              R.paragraph(R.bold("Injury List"))]
    if not injury_service.enabled_for(tour):
        blocks.append(R.paragraph(
            "This tournament has no injury system — every player is available "
            "for every match."))
        return blocks
    rows = injury_service.active_injuries(session, tour.id)
    if not rows:
        blocks.append(R.paragraph(["✅ ", R.bold("Nobody is injured."),
                                   " Full squads available."]))
        return blocks

    team_rows = V.teams(session, tour.id)
    names = {tt.id: (tt.name or "—") for tt in team_rows}
    mine = {tt.id for tt in team_rows
            if viewer_tg_id is not None
            and tournament_service.is_team_member(tt, viewer_tg_id)}

    by_team = {}
    for row in rows:
        by_team.setdefault(row.tournament_team_id, []).append(row)

    header = [R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("INJURY"), header=True),
              R.cell(R.bold("OUT"), header=True, align="right")]
    for team_id, hurt in sorted(by_team.items(),
                                key=lambda kv: names.get(kv[0], "")):
        title = [R.bold(names.get(team_id, "Team"))]
        if team_id in mine:
            title.append("  👈 yours")
        cells = [header]
        for row in sorted(hurt, key=lambda r: -(r.matches_remaining or 0)):
            n = int(row.matches_remaining or 0)
            cells.append([R.cell(f"❌ {row.player_name or 'Player'}"),
                          R.cell(row.injury_type),
                          R.cell("1 match" if n == 1 else f"{n} matches",
                                 align="right")])
        blocks.append(R.table(cells, bordered=True, compact=True,
                              caption=title))

    _e, _chance, cap = injury_service.settings(tour)
    blocks.append(R.footer(R.italic(
        f"Injuries are on: up to {cap} match{'' if cap == 1 else 'es'} out. "
        "An injured player cannot be picked in the XI until they are fit.")))
    return blocks


def render_blocks(session, tour, view, viewer_tg_id=None):
    """The block twin of ``cl_tournament_view.render`` — same view names.

    Returns ``None`` when a view fails to build, which is a signal to the caller
    to send the HTML rendering: a broken block builder must never cost a player
    the answer.
    """
    try:
        if view == "table":
            return table_blocks(session, tour)
        if view == "fixtures":
            return fixtures_blocks(session, tour, viewer_tg_id=viewer_tg_id)
        if view == "teams":
            return teams_blocks(session, tour)
        if view == "injuries":
            return injuries_blocks(session, tour, viewer_tg_id=viewer_tg_id)
        return overview_blocks(session, tour)
    except Exception:
        logger.exception("Challenge League rich view %r failed to build", view)
        return None
