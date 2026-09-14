"""Read-only player-facing views of a Challenge League Tournament.

The admin site has a full dashboard; this is the same competition rendered for
Telegram — a points table, the fixture list, the field of teams (with their
owners) and a front page tying them together. It is deliberately read-only:
nothing here starts, edits or records a match.

The fixture list is the piece players actually open. Each line shows the surface
the match must be played on and which side is at home, and a match that has been
played is struck through with its result, so a glance says what is left to do.
"""

import logging
from html import escape

from models import Tournament, TournamentTeam, TournamentMatch
from services import tournament_service

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

# Views the hub card can switch between, in tab order.
VIEWS = ("overview", "table", "fixtures", "teams")


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
        name = (tt.name or "—")[:14].ljust(14)
        out.append(
            f"<code>{str(i).rjust(2)} {escape(name)} "
            f"{str(tt.played or 0).rjust(2)} {str(tt.won or 0).rjust(2)} "
            f"{str(tt.lost or 0).rjust(2)} {str(tt.tied or 0).rjust(2)} "
            f"{str(tt.points or 0).rjust(3)} {_nrr_text(tt._nrr).rjust(6)}</code>")
    champion = tournament_service.tournament_champion(session, tour.id)
    if champion:
        out += ["", f"🥇 <b>Champion:</b> {escape(champion.name or '—')}"]
    return "\n".join(out)


def render_fixtures(session, tour, viewer_tg_id=None, limit=40):
    """The fixture list as an HTML message body.

    A completed fixture is struck through and carries its result; a live one is
    flagged as in progress. When the viewer owns a team, their own remaining
    fixtures are pulled out into a "Your next matches" block — the thing a team
    owner actually opens this for.
    """
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

    mine = {tt.id for tt in rows
            if viewer_tg_id is not None and tt.owner_tg_id
            and int(tt.owner_tg_id) == int(viewer_tg_id)}

    def _slot(team_id, label, home_id):
        if team_id and team_id in names:
            text = escape(names[team_id])
            return f"{text} 🏠" if home_id and team_id == home_id else text
        return escape(label or "TBD")

    my_lines, all_lines = [], []
    for fx in fixtures[:limit]:
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
    out += ["", "<b>Full schedule</b>"] + all_lines
    if len(fixtures) > limit:
        out.append(f"<i>…and {len(fixtures) - limit} more.</i>")
    out += ["", "<i>🏠 home side · 🌱 the pitch this match must be played on · "
                "struck-through matches are done.</i>"]
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
        suffix = f" · 👤 {escape(owner)}" if owner else (
            " · <i>unowned</i>" if owned else "")
        home = (tt.home_pitch or "").strip()
        if home:
            suffix += f" · 🌱 {escape(home)}"
        out.append(f"{i}. <b>{escape(tt.name or '—')}</b>{suffix}")
    if owned:
        out += ["", "<i>🔒 Owner-locked: only a team's owner may play with it.</i>"]
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
                   "may play with it")
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


def render(session, tour, view, viewer_tg_id=None):
    """Render one of the hub's views by name."""
    if view == "table":
        return render_table(session, tour)
    if view == "fixtures":
        return render_fixtures(session, tour, viewer_tg_id=viewer_tg_id)
    if view == "teams":
        return render_teams(session, tour)
    return render_overview(session, tour)
