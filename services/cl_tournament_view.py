"""Read-only player-facing views of a Challenge League Tournament.

The admin site has a full dashboard; this is the same competition rendered for
Telegram — a points table, the fixture list, the field of teams (with their
owners) and a front page tying them together. It is deliberately read-only:
nothing here starts, edits or records a match.

The fixture list is the piece players actually open. Each line shows the surface
the match must be played on and which side is at home, and a match that has been
played is struck through with its result, so a glance says what is left to do.
"Your next matches" means the teams the viewer owns *or co-owns* — a franchise
can be run by several people, and all of them need to see what they have to play.
"""

import logging
from html import escape

from models import (Tournament, TournamentTeam, TournamentMatch,
                    TournamentPlayerStats)
from services import tournament_service
from utils.message_chunks import expandable_quotes

logger = logging.getLogger(__name__)

_STATUS_LABEL = {
    "draft": "📝 Draft (not started)",
    "scheduled": "🗓️ Scheduled",
    "active": "🟢 Live",
    "paused": "⏸️ Paused",
    "completed": "🏁 Completed",
    "cancelled": "❌ Cancelled",
}

_STAGE_LABEL = {
    "league": "League", "group": "Group",
    "quarterfinal": "Quarter-final", "semifinal": "Semi-final",
    "qualifier1": "Qualifier 1", "eliminator": "Eliminator",
    "qualifier2": "Qualifier 2", "final": "Final",
    "round_of_16": "Round of 16", "round_of_32": "Round of 32",
}

# Views the hub card can switch between, in tab order. "injuries" is only worth
# a tab when the tournament actually has them on — see ``views_for``.
VIEWS = ("overview", "table", "fixtures", "teams", "injuries")


def views_for(tour):
    """The views worth showing for this tournament."""
    from services import injury_service
    if injury_service.enabled_for(tour):
        return VIEWS
    return tuple(v for v in VIEWS if v != "injuries")


def status_label(tour):
    """A tournament's lifecycle status as a badge players can read."""
    return _STATUS_LABEL.get((tour.status or "").lower(), tour.status or "—")


def _nrr_text(value):
    """Net run rate, always signed — a leading '+' reads as deliberate."""
    return f"{value:+.3f}"


def active_tournament(session):
    """The live Challenge League tournament, or None."""
    return tournament_service.get_active_tournament(
        session, kind=tournament_service.KIND_CHALLENGE)


def teams(session, tournament_id):
    """Participating teams in display order."""
    return (session.query(TournamentTeam)
            .filter_by(tournament_id=int(tournament_id))
            .order_by(TournamentTeam.sort_order, TournamentTeam.id).all())


# ──────────────────────────────────────────────────────────────────────
# Renderers — each returns one HTML message body
# ──────────────────────────────────────────────────────────────────────

def render_table(session, tour):
    """The points table as an HTML message body."""
    rows = tournament_service.points_table(session, tour.id)
    if not rows:
        return (f"🏆 <b>{escape(tour.name)}</b>\n"
                "No teams have been added to this tournament yet.")
    played, total = tournament_service.league_progress(session, tour.id)
    out = [f"🏆 <b>{escape(tour.name)}</b> — Points Table",
           status_label(tour)
           + (f" · {played}/{total} league matches played" if total else ""),
           "",
           "<code>#  TEAM            P  W  L  T Pts   NRR</code>"]
    for i, tt in enumerate(rows, 1):
        name = tournament_service.table_name_cell(tt)
        out.append(
            f"<code>{str(i).rjust(2)} {escape(name)} "
            f"{str(tt.played or 0).rjust(2)} {str(tt.won or 0).rjust(2)} "
            f"{str(tt.lost or 0).rjust(2)} {str(tt.tied or 0).rjust(2)} "
            f"{str(tt.points or 0).rjust(3)} {_nrr_text(tt._nrr).rjust(6)}</code>")
    out += tournament_service.points_adjust_footnote(rows)
    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        out += ["", f"🥇 <b>Champion:</b> {escape(champion.name or '—')}"]
    return "\n".join(out)


# How many fixtures the schedule shows without a tap. Everything past this
# still ships — it goes into an expandable blockquote (a ``details`` block in
# the rich rendering), because "…and 22 more." is not a schedule: the fixture
# somebody opened this card for was usually one of the 22.
FIXTURE_OPEN = 20


def render_fixtures(session, tour, viewer_tg_id=None, limit=None):
    """The fixture list as an HTML message body.

    A completed fixture is struck through and carries its result; a live one is
    flagged as in progress. When the viewer owns a team, their own remaining
    fixtures are pulled out into a "Your next matches" block — the thing a team
    owner actually opens this for.

    **Every** fixture is rendered. ``limit`` caps how many are shown open, and
    defaults to :data:`FIXTURE_OPEN`; the rest are behind Telegram's own tap-to-
    expand rather than dropped. A schedule long enough to outgrow one message is
    split across sends by ``rich_message.html_parts``, which never cuts inside a
    blockquote.
    """
    limit = FIXTURE_OPEN if limit is None else int(limit)
    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                          TournamentMatch.id).all())
    rows = teams(session, tour.id)
    names = {tt.id: (tt.name or "—") for tt in rows}
    header = [f"🗓️ <b>{escape(tour.name)}</b> — Fixtures"]
    if not fixtures:
        header += ["",
                   "No schedule has been generated yet — this tournament is "
                   "free-play: any two participating teams can start a match."]
        return "\n".join(header)

    # "Yours" means owner *or* co-owner: a franchise can be run by more than
    # one person, and they all need to see the matches they have to play.
    mine = {tt.id for tt in rows
            if viewer_tg_id is not None
            and tournament_service.is_team_member(tt, viewer_tg_id)}

    def _slot(team_id, label, home_id):
        if team_id and team_id in names:
            text = escape(names[team_id])
            return f"{text} 🏠" if home_id and team_id == home_id else text
        return escape(label or "TBD")

    my_lines, all_lines = [], []
    for fx in fixtures:
        stage = _STAGE_LABEL.get(fx.stage or "league", (fx.stage or "").title())
        a = _slot(fx.team1_id, fx.slot1_label, fx.home_team_id)
        b = _slot(fx.team2_id, fx.slot2_label, fx.home_team_id)
        tag = f"M{fx.match_no}" if fx.match_no else stage
        pitch = (fx.pitch_type or "").strip()
        if fx.status == "completed":
            body = f"✅ <s>{a} vs {b}</s> — {escape(fx.result_text or 'done')}"
        elif fx.status == "live":
            body = f"🔴 {a} vs {b} — <i>in progress</i>"
        else:
            body = f"⚪ {a} vs {b}"
            if pitch:
                body += f" · 🌱 {escape(pitch)}"
        line = f"<code>{tag}</code> {body}"
        all_lines.append(line)
        if mine and fx.status != "completed" and mine & {fx.team1_id, fx.team2_id}:
            my_lines.append(line)

    out = list(header)
    if my_lines:
        out += ["", "<b>Your next matches</b>"] + my_lines[:8]
    out += ["", f"<b>Full schedule</b> · {len(all_lines)} matches"]
    out += all_lines[:limit]
    rest = all_lines[limit:]
    if rest:
        out.append(f"<b>👇 Matches {limit + 1}–{len(all_lines)}</b> "
                   f"<i>(tap to expand)</i>")
        out += expandable_quotes(rest)
    out += ["", "<i>🏠 home side · 🌱 the pitch this match must be played on · "
                "struck-through matches are done.</i>"]
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────
# Finding one team by the name a player typed
# ──────────────────────────────────────────────────────────────────────
#
# ``/clsd <TEAM NAME>`` is typed by hand, in a chat, usually on a phone. It has
# to cope with case, with the short name everybody actually uses ("MI"), and
# with a half-typed name — and when it genuinely cannot tell two teams apart it
# has to say so rather than guess, because guessing shows somebody the wrong
# schedule and they act on it.

def _norm(text):
    """Casefold and squeeze whitespace — the form names are compared in."""
    return " ".join(str(text or "").split()).casefold()


def _initials(name):
    """``"Mumbai Indians"`` → ``"mi"``, so the short form people say out loud
    finds the team even when nobody entered a ``short_name``."""
    parts = [p for p in str(name or "").split() if p]
    return "".join(p[0] for p in parts).casefold() if len(parts) > 1 else ""


def find_team(session, tournament_id, query):
    """``(team, candidates)`` for a typed team name.

    Exactly one of the two is meaningful: a resolved ``team`` with an empty
    ``candidates``, or ``None`` plus every team the query could have meant (which
    is empty when it matched nothing at all). Matching runs strongest-first —
    exact name, exact short name or initials, prefix, then substring — and stops
    at the first round that matches, so typing the full name of a team whose name
    is also the prefix of another still lands on the one that was typed.
    """
    rows = teams(session, tournament_id)
    q = _norm(query)
    if not q:
        return None, rows

    def _pick(predicate):
        return [tt for tt in rows if predicate(tt)]

    for predicate in (
            lambda tt: _norm(tt.name) == q,
            lambda tt: _norm(tt.short_name) == q or _initials(tt.name) == q,
            lambda tt: _norm(tt.name).startswith(q),
            lambda tt: q in _norm(tt.name)):
        hits = _pick(predicate)
        if len(hits) == 1:
            return hits[0], []
        if hits:
            return None, hits
    return None, []


def team_form(session, tournament_id, team_id, limit=5):
    """The team's last ``limit`` results, most recent first: ``['W', 'L', …]``.

    'T' covers a tie and 'N' a no-result — a completed fixture with no winner is
    one or the other, and neither is a loss.
    """
    rows = (session.query(TournamentMatch)
            .filter_by(tournament_id=int(tournament_id), status="completed")
            .filter((TournamentMatch.team1_id == int(team_id))
                    | (TournamentMatch.team2_id == int(team_id)))
            .order_by(TournamentMatch.round_no.desc(),
                      TournamentMatch.match_no.desc(),
                      TournamentMatch.id.desc()).limit(int(limit)).all())
    out = []
    for fx in rows:
        if fx.winner_team_id == int(team_id):
            out.append("W")
        elif fx.winner_team_id:
            out.append("L")
        else:
            out.append("T")
    return out


_FORM_EMOJI = {"W": "🟢", "L": "🔴", "T": "🟡", "N": "⚪"}


def _standing_of(session, tour, team_id):
    """``(position, row)`` in the points table, or ``(None, None)``."""
    table = tournament_service.points_table(session, tour.id)
    for i, tt in enumerate(table, 1):
        if tt.id == int(team_id):
            return i, tt
    return None, None


def render_team_schedule(session, tour, team, viewer_tg_id=None, limit=40):
    """One team's whole tournament on a single card — the ``/clsd`` answer.

    The fixture list (``render_fixtures``) is the competition seen from above:
    every match, in order, with the viewer's own pulled to the top. This is the
    competition seen from inside one dressing room — where that team stands, how
    it has been going, who it plays next and on what, and every result it has
    already posted, each one marked won or lost *from this team's side* rather
    than by which name sits on the left.
    """
    name = escape(team.name or "—")
    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .filter((TournamentMatch.team1_id == team.id)
                        | (TournamentMatch.team2_id == team.id))
                .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                          TournamentMatch.id).all())
    rows = teams(session, tour.id)
    names = {tt.id: (tt.name or "—") for tt in rows}

    # Whether this is the viewer's own team is decided up front: it belongs on
    # the card whether or not the team has any fixtures yet, and an owner
    # opening their brand-new franchise is exactly the person who needs telling
    # which one they are looking at.
    mine = (viewer_tg_id is not None
            and tournament_service.is_team_member(team, viewer_tg_id))
    out = [f"🗓️ <b>{name}</b>{' 👈 <b>your team</b>' if mine else ''}"
           f" — {escape(tour.name)}"]

    # ── Where they stand ──
    pos, standing = _standing_of(session, tour, team.id)
    if standing is not None and (standing.played or 0) >= 0:
        out.append(
            f"📊 <b>#{pos}</b> · {standing.played or 0}P "
            f"{standing.won or 0}W {standing.lost or 0}L {standing.tied or 0}T · "
            f"<b>{standing.points or 0}</b> pts · NRR {_nrr_text(standing._nrr)}")
    form = team_form(session, tour.id, team.id)
    if form:
        out.append("📈 <b>Form:</b> "
                   + " ".join(_FORM_EMOJI.get(f, "⚪") for f in reversed(form))
                   + "  <i>(oldest → latest)</i>")
    owner = (team.owner_name or "").strip()
    extras = len(tournament_service.co_owner_ids(team))
    if owner or extras:
        who = f"👤 {escape(owner)}" if owner else "👤 <i>no owner</i>"
        out.append(who + (f" 🤝 +{extras}" if extras else ""))
    home_pitch = (team.home_pitch or "").strip()
    if home_pitch:
        out.append(f"🏟️ <b>Home pitch:</b> 🌱 {escape(home_pitch)}")

    if not fixtures:
        out += ["", "No fixtures are scheduled for this team yet — this "
                    "tournament is free-play: any two participating teams can "
                    "start a match."]
        return "\n".join(out)

    def _line(fx):
        """One fixture, written from this team's point of view."""
        other_id = fx.team2_id if fx.team1_id == team.id else fx.team1_id
        other = escape(names.get(other_id) or
                       (fx.slot2_label if fx.team1_id == team.id
                        else fx.slot1_label) or "TBD")
        at_home = fx.home_team_id == team.id
        venue = "🏠 vs" if at_home else ("✈️ at" if fx.home_team_id else "vs")
        stage = _STAGE_LABEL.get(fx.stage or "league", (fx.stage or "").title())
        tag = f"M{fx.match_no}" if fx.match_no else stage
        if fx.status == "completed":
            if fx.winner_team_id == team.id:
                mark = "✅ <b>WON</b>"
            elif fx.winner_team_id:
                mark = "❌ <b>LOST</b>"
            else:
                mark = "🤝 <b>TIED</b>"
            body = f"{mark} {venue} {other} — {escape(fx.result_text or 'done')}"
        elif fx.status == "live":
            body = f"🔴 {venue} {other} — <i>in progress</i>"
        else:
            body = f"⚪ {venue} {other}"
            pitch = (fx.pitch_type or "").strip()
            if pitch:
                body += f" · 🌱 {escape(pitch)}"
        return f"<code>{tag}</code> {body}"

    upcoming = [fx for fx in fixtures if fx.status != "completed"]
    played = [fx for fx in fixtures if fx.status == "completed"]

    def _section(title, entries):
        """A block of fixtures: the first ``limit`` open, the rest a tap away."""
        lines = [_line(fx) for fx in entries]
        out = [title] + lines[:limit]
        rest = lines[limit:]
        if rest:
            out.append(f"<b>👇 {len(rest)} more</b> <i>(tap to expand)</i>")
            out += expandable_quotes(rest)
        return out

    if upcoming:
        out += [""] + _section(f"<b>Next up</b> ({len(upcoming)} to play)",
                               upcoming)
    else:
        out += ["", "<b>Next up</b>", "<i>Nothing left to play — "
                "every fixture is done.</i>"]
    if played:
        # Newest first, so the ones a tap away are the oldest — which is the
        # right way round for a results list, and never drops one.
        out += [""] + _section(f"<b>Results</b> ({len(played)})",
                               list(reversed(played)))

    out += ["", "<i>🏠 home · ✈️ away · 🌱 the pitch this match must be played "
                "on.</i>"]
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════
# /teamtourstats — one team's numbers, not its schedule
# ══════════════════════════════════════════════════════════════════════
#
# ``render_team_schedule`` answers "what do we play next?". This answers the
# other half of the same question: "how have we actually been doing?" — the
# standing and form, what the team scores and concedes, its best and worst days
# with the bat, and which of its own players are carrying it.
#
# Everything here is derived from the recorded matches and the tournament's
# player-stat rows, so correcting or deleting a match moves this card exactly as
# it moves the points table.


def _overs_text(balls):
    """Balls as cricket overs — ``27`` → ``4.3``."""
    balls = int(balls or 0)
    return f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)


def _run_rate(runs, balls):
    """Runs per over, or ``None`` when nothing has been bowled."""
    balls = int(balls or 0)
    return (int(runs or 0) / (balls / 6.0)) if balls else None


def team_innings(session, tournament_id, team_id):
    """Every completed innings this team has batted, newest fixture last.

    Returns ``[{runs, wickets, balls, opponent_id, won, match}, …]``. Which
    innings belongs to which side is fixed by the schema: innings 1 is always
    ``team1``'s and innings 2 always ``team2``'s — the same mapping
    ``recompute_standings`` builds net run-rate from.
    """
    tid, team_id = int(tournament_id), int(team_id)
    rows = (session.query(TournamentMatch)
            .filter_by(tournament_id=tid, status="completed")
            .filter((TournamentMatch.team1_id == team_id)
                    | (TournamentMatch.team2_id == team_id))
            .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                      TournamentMatch.id).all())
    out = []
    for fx in rows:
        first = fx.team1_id == team_id
        runs = fx.inn1_runs if first else fx.inn2_runs
        if runs is None:
            continue    # a walkover or a manually recorded result with no score
        out.append({
            "runs": int(runs or 0),
            "wickets": int((fx.inn1_wickets if first else fx.inn2_wickets) or 0),
            "balls": int((fx.inn1_balls if first else fx.inn2_balls) or 0),
            "conceded": int((fx.inn2_runs if first else fx.inn1_runs) or 0),
            "conceded_wickets": int((fx.inn2_wickets if first else fx.inn1_wickets) or 0),
            # The balls this team *bowled* — the other innings. Economy read off
            # their own batting balls would be a different number entirely.
            "conceded_balls": int((fx.inn2_balls if first else fx.inn1_balls) or 0),
            "opponent_id": fx.team2_id if first else fx.team1_id,
            "won": fx.winner_team_id == team_id,
            "match": fx,
        })
    return out


def team_player_stats(session, tour, team):
    """The tournament's player-stat rows belonging to this team.

    Rows carry the team name they were played under, which is what the stat
    leaderboards show — so the name is the primary key here too. A Lets Play
    team *is* a person, though, and a player who renames their team mid-run
    would otherwise vanish from their own card; for those the owning user is
    used as a fallback identity.
    """
    rows = (session.query(TournamentPlayerStats)
            .filter_by(tournament_id=int(tour.id)).all())
    wanted = _norm(team.name)
    mine = [r for r in rows if _norm(r.team_name) == wanted]
    if mine or not getattr(team, "user_tg_id", None):
        return mine
    from models import User
    owner = (session.query(User)
             .filter(User.telegram_id == int(team.user_tg_id)).first())
    if owner is None:
        return mine
    return [r for r in rows if r.user_id == owner.id]


def _best_batting(rows, limit=3):
    """Top run-scorers — runs first, then the better strike rate."""
    scored = [r for r in rows if (r.bat_runs or 0) > 0]
    scored.sort(key=lambda r: ((r.bat_runs or 0),
                               (r.bat_runs or 0) / (r.bat_balls or 1)),
                reverse=True)
    return scored[:limit]


def _best_bowling(rows, limit=3):
    """Top wicket-takers — wickets first, then the better economy."""
    took = [r for r in rows if (r.bowl_wickets or 0) > 0]
    took.sort(key=lambda r: ((r.bowl_wickets or 0),
                             -((r.bowl_runs or 0) / ((r.bowl_balls or 1) / 6.0))))
    took.reverse()
    return took[:limit]


def render_team_stats(session, tour, team, viewer_tg_id=None):
    """One team's tournament, by the numbers — the ``/teamtourstats`` answer."""
    name = escape(team.name or "—")
    mine = (viewer_tg_id is not None
            and tournament_service.is_team_member(team, viewer_tg_id))
    out = [f"📊 <b>{name}</b>{' 👈 <b>your team</b>' if mine else ''}"
           f" — {escape(tour.name)}", f"{status_label(tour)}"]

    # ── Where they stand ──────────────────────────────────────────────
    pos, standing = _standing_of(session, tour, team.id)
    if standing is not None:
        total = len(teams(session, tour.id))
        out += ["", "<b>🏆 Standing</b>",
                f"Position: <b>#{pos}</b> of {total} · "
                f"<b>{standing.points or 0}</b> pts",
                f"Played {standing.played or 0} · "
                f"W {standing.won or 0} · L {standing.lost or 0} · "
                f"T {standing.tied or 0}",
                f"Net run rate: <b>{_nrr_text(standing._nrr)}</b>"]
        adjust = int(standing.points_adjust or 0)
        if adjust:
            note = (standing.points_adjust_note or "").strip()
            out.append(f"Points adjustment: <b>{adjust:+d}</b>"
                       + (f" — <i>{escape(note)}</i>" if note else ""))
    form = team_form(session, tour.id, team.id)
    if form:
        out.append("Form: "
                   + " ".join(_FORM_EMOJI.get(f, "⚪") for f in reversed(form))
                   + "  <i>(oldest → latest)</i>")

    innings = team_innings(session, tour.id, team.id)
    if not innings:
        out += ["", "<i>No completed match with a recorded score yet — the "
                    "batting and bowling numbers appear once this team has "
                    "played one.</i>"]
        return "\n".join(out)

    names = {tt.id: (tt.name or "—") for tt in teams(session, tour.id)}

    def _versus(entry):
        return escape(names.get(entry["opponent_id"]) or "—")

    # ── With the bat ──────────────────────────────────────────────────
    runs_for = sum(e["runs"] for e in innings)
    balls_for = sum(e["balls"] for e in innings)
    wkts_lost = sum(e["wickets"] for e in innings)
    rr_for = _run_rate(runs_for, balls_for)
    best = max(innings, key=lambda e: e["runs"])
    worst = min(innings, key=lambda e: e["runs"])
    out += ["", "<b>🏏 With the bat</b>",
            f"Runs: <b>{runs_for:,}</b> in {_overs_text(balls_for)} overs"
            + (f" · RR <b>{rr_for:.2f}</b>" if rr_for is not None else ""),
            f"Average total: <b>{runs_for // len(innings)}</b> "
            f"({wkts_lost} wickets lost across {len(innings)} innings)",
            f"Highest: <b>{best['runs']}/{best['wickets']}</b> vs {_versus(best)}"]
    if worst is not best:
        out.append(f"Lowest: <b>{worst['runs']}/{worst['wickets']}</b> "
                   f"vs {_versus(worst)}")

    # ── With the ball ─────────────────────────────────────────────────
    runs_against = sum(e["conceded"] for e in innings)
    balls_against = sum(e["conceded_balls"] for e in innings)
    rr_against = _run_rate(runs_against, balls_against)
    tightest = min(innings, key=lambda e: e["conceded"])
    out += ["", "<b>🎯 With the ball</b>",
            f"Conceded: <b>{runs_against:,}</b>"
            + (f" · RR <b>{rr_against:.2f}</b>" if rr_against is not None else ""),
            f"Best defence: <b>{tightest['conceded']}/"
            f"{tightest['conceded_wickets']}</b> vs {_versus(tightest)}"]

    # ── Who is doing it ───────────────────────────────────────────────
    rows = team_player_stats(session, tour, team)
    batters = _best_batting(rows)
    bowlers = _best_bowling(rows)
    if batters or bowlers:
        out += ["", "<b>⭐ Leading the way</b>"]
    for r in batters:
        sr = ((r.bat_runs or 0) / r.bat_balls * 100.0) if r.bat_balls else None
        out.append(f"🏏 {escape(r.name or 'Player')} — <b>{r.bat_runs}</b> runs"
                   + (f" · SR {sr:.1f}" if sr is not None else "")
                   + f" · HS {r.highest_score}")
    for r in bowlers:
        econ = _run_rate(r.bowl_runs, r.bowl_balls)
        figure = (f" · Best {r.best_bowl_wickets}/{r.best_bowl_runs}"
                  if (r.best_bowl_runs or -1) >= 0 else "")
        out.append(f"🎯 {escape(r.name or 'Player')} — "
                   f"<b>{r.bowl_wickets}</b> wkts"
                   + (f" · Econ {econ:.2f}" if econ is not None else "")
                   + figure)

    out += ["", "<i>🗓️ /clsd for this team's fixtures and results · "
                "🏆 /tournamentstats for the tournament-wide leaderboards.</i>"]
    return "\n".join(out)


def render_teams(session, tour):
    """The field of teams, with owners, as an HTML message body."""
    rows = teams(session, tour.id)
    out = [f"👥 <b>{escape(tour.name)}</b> — Teams "
           f"({len(rows)}/{tour.max_teams or '∞'})"]
    if not rows:
        out += ["", "No teams have been entered yet."]
        return "\n".join(out)
    owned = tournament_service.owner_enforced(tour)
    out.append("")
    for i, tt in enumerate(rows, 1):
        # Plain text, never a mention: a team list must not ping everyone in it.
        owner = (tt.owner_name or "").strip()
        if tt.owner_tg_id and not owner:
            owner = str(tt.owner_tg_id)
        extras = len(tournament_service.co_owner_ids(tt))
        if owner:
            suffix = f" · 👤 {escape(owner)}"
        elif extras:
            suffix = " · 👤 <i>no owner</i>"
        else:
            suffix = " · <i>unowned</i>" if owned else ""
        if extras:
            suffix += f" 🤝 +{extras}"
        home = (tt.home_pitch or "").strip()
        if home:
            suffix += f" · 🌱 {escape(home)}"
        out.append(f"{i}. <b>{escape(tt.name or '—')}</b>{suffix}")
    if owned:
        out += ["", "<i>🔒 Owner-locked: only a team's owner and its co-owners "
                    "(🤝) may play with it.</i>"]
    return "\n".join(out)


def render_overview(session, tour):
    """The tournament's front page as an HTML message body."""
    rows = teams(session, tour.id)
    played, total = tournament_service.league_progress(session, tour.id)
    table = tournament_service.points_table(session, tour.id)
    lo, hi = tournament_service.overseas_limits(session, tour)
    out = [f"🏆 <b>{escape(tour.name)}</b>",
           "━━━━━━━━━━━━━━━━━━━",
           f"<b>Status:</b> {status_label(tour)}",
           f"<b>League:</b> {escape(tour.league_name or '—')}",
           f"<b>Format:</b> {escape(tour.format or 'League')} · {tour.overs} overs",
           f"<b>Teams:</b> {len(rows)}/{tour.max_teams or '∞'}"]
    if total:
        out.append(f"<b>Played:</b> {played}/{total} league matches")
    elif rows:
        out.append("<b>Schedule:</b> free-play (no fixture list)")
    if lo > 0 or hi < 11:
        out.append(f"<b>Overseas in XI:</b> min {lo} · max {hi}")
    from services import league_schedule_service
    if league_schedule_service.pitch_locked(tour):
        out.append("<b>Pitch:</b> fixed per fixture — see 🗓️ Fixtures")
    if tournament_service.owner_enforced(tour):
        out.append("<b>Team rule:</b> 🔒 owner-locked — only a team's owner "
                   "and co-owners may play with it")
    from services import injury_service
    if injury_service.enabled_for(tour):
        _e, _chance, cap = injury_service.settings(tour)
        hurt = len(injury_service.active_injuries(session, tour.id))
        out.append(f"<b>Injuries:</b> 🚑 on — up to {cap} match"
                   f"{'' if cap == 1 else 'es'} out"
                   + (f" · {hurt} player{'' if hurt == 1 else 's'} sidelined"
                      if hurt else " · nobody sidelined"))
    if tour.description:
        out += ["", f"<i>{escape(tour.description)}</i>"]
    if table:
        out += ["", "<b>Top of the table</b>"]
        for i, tt in enumerate(table[:5], 1):
            out.append(f"{i}. {escape(tt.name or '—')} — "
                       f"{tt.points or 0} pts ({tt.won or 0}W {tt.lost or 0}L) "
                       f"NRR {_nrr_text(tt._nrr)}")
    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        out += ["", f"🥇 <b>Champion:</b> {escape(champion.name or '—')}"]
    out.append("━━━━━━━━━━━━━━━━━━━")
    return "\n".join(out)


def render_injuries(session, tour, viewer_tg_id=None):
    """The treatment room: who is out, for what, and for how much longer."""
    from services import injury_service
    head = f"🚑 <b>{escape(tour.name)}</b> — Injury List"
    if not injury_service.enabled_for(tour):
        return (f"{head}\n\nThis tournament has no injury system — "
                "every player is available for every match.")
    rows = injury_service.active_injuries(session, tour.id)
    names = {tt.id: (tt.name or "—") for tt in teams(session, tour.id)}
    if not rows:
        return f"{head}\n\n✅ Nobody is injured. Full squads available."

    mine = {tt.id for tt in teams(session, tour.id)
            if viewer_tg_id is not None
            and tournament_service.is_team_member(tt, viewer_tg_id)}

    by_team = {}
    for row in rows:
        by_team.setdefault(row.tournament_team_id, []).append(row)

    out = [head, ""]
    for team_id, hurt in sorted(
            by_team.items(), key=lambda kv: names.get(kv[0], "")):
        flag = " 👈 <b>yours</b>" if team_id in mine else ""
        out.append(f"<b>{escape(names.get(team_id, 'Team'))}</b>{flag}")
        for row in sorted(hurt, key=lambda r: -(r.matches_remaining or 0)):
            n = int(row.matches_remaining or 0)
            span = "1 more match" if n == 1 else f"{n} more matches"
            out.append(f"  ❌ {escape(row.player_name or 'Player')} — "
                       f"{escape(row.injury_type)} · out {span}")
        out.append("")
    _e, chance, cap = injury_service.settings(tour)
    out.append(f"<i>Injuries are on: up to {cap} match"
               f"{'' if cap == 1 else 'es'} out. An injured player cannot be "
               "picked in the XI until they are fit.</i>")
    return "\n".join(out)


def render(session, tour, view, viewer_tg_id=None):
    """Render one of the hub's views by name."""
    if view == "table":
        return render_table(session, tour)
    if view == "fixtures":
        return render_fixtures(session, tour, viewer_tg_id=viewer_tg_id)
    if view == "teams":
        return render_teams(session, tour)
    if view == "injuries":
        return render_injuries(session, tour, viewer_tg_id=viewer_tg_id)
    return render_overview(session, tour)
