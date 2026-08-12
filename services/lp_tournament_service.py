"""Lets Play Tournament service — a CIPL-style competition whose teams are users.

A Challenge League tournament is contested by admin-built ``ChallengeTeam``
squads. A Lets Play tournament is contested by *people*: every participating
``TournamentTeam`` row carries a ``user_tg_id`` instead of a ``challenge_team_id``,
and each match is a /letsplay match — both sides field their own roster, with
their own cards and traits.

Everything downstream of "who are the two teams" is shared with the Challenge
League tournament (``services.tournament_service``): the points table, net run
rate, the round-robin schedule generator (``services.league_schedule_service``),
the knockout bracket (``services.knockout_service``) and the per-player
leaderboards all key off ``TournamentTeam.id`` and neither know nor care which
kind of team is behind it. This module owns only the parts that are specific to
"a team is a Telegram user":

  • creating a Lets Play tournament (no league, ``kind='letsplay'``),
  • adding / removing / renaming participants by Telegram id,
  • deciding whether a given pair of users may play right now, and
  • rendering the table / fixtures / squad list for the bot.

All mutating helpers raise ``LPTError`` with a message written for the admin who
typed the command, and leave the commit to the caller.
"""

import logging
from html import escape

from models import (
    Tournament, TournamentTeam, TournamentMatch, User,
)
from services import tournament_service
from services.tournament_service import KIND_LETSPLAY

logger = logging.getLogger(__name__)

# A Lets Play match is always a 20-over contest (see handlers/letsplay.py), so a
# Lets Play tournament is too — there is no per-league format to read.
LP_OVERS = 20

# Team-name length is bounded by TournamentTeam.name.
MAX_NAME = 120

# Structures the admin can generate from chat, mapped to what the shared
# services expect. ``league_format`` drives the round-robin generator;
# ``knockout_type`` (when set) is the bracket seeded from the final table.
LEAGUE_FORMATS = {
    "single": ("single_rr", "Round Robin"),
    "double": ("double_rr", "Double Round Robin"),
}
KNOCKOUT_TYPES = {
    "top4": ("top4_sf", "Top 4 — Semi-finals + Final"),
    "playoffs": ("ipl_playoffs", "IPL Playoffs (Q1 / Eliminator / Q2 / Final)"),
    "knockout": ("pure_knockout", "Straight Knockout"),
}


class LPTError(Exception):
    """A refusal carrying a message written for whoever typed the command.

    The message is **plain text**, never HTML — it can quote a tournament or team
    name, and those are user-supplied. Callers escape it once when they render it.
    The ``render_*`` helpers at the bottom of this module are the opposite: they
    build HTML message bodies and escape as they go.
    """


# ──────────────────────────────────────────────────────────────────────
# Lookup
# ──────────────────────────────────────────────────────────────────────

def active_tournament(session):
    """The live Lets Play tournament, or None."""
    return tournament_service.get_active_tournament(session, kind=KIND_LETSPLAY)


def list_tournaments(session, include_finished=True):
    """Every Lets Play tournament, newest first."""
    q = (session.query(Tournament)
         .filter(Tournament.kind == KIND_LETSPLAY))
    if not include_finished:
        q = q.filter(Tournament.status.notin_(tournament_service.TERMINAL_STATUSES))
    return q.order_by(Tournament.is_active.desc(), Tournament.id.desc()).all()


def get_tournament(session, tournament_id):
    """A Lets Play tournament by id, or None if it is missing or another kind."""
    tour = session.query(Tournament).get(int(tournament_id))
    if not tour or tournament_service.tournament_kind(tour) != KIND_LETSPLAY:
        return None
    return tour


def participants(session, tournament_id):
    """Participating rows in table order (sort_order, then name)."""
    return (session.query(TournamentTeam)
            .filter_by(tournament_id=int(tournament_id))
            .order_by(TournamentTeam.sort_order, TournamentTeam.name).all())


def team_for_tg(session, tournament_id, user_tg_id):
    """The participating row for a Telegram id, or None."""
    return tournament_service.tournament_team_for_user_tg(
        session, tournament_id, user_tg_id)


def is_participant(session, tournament_id, user_tg_id):
    """True if this Telegram id is entered in the tournament."""
    return team_for_tg(session, tournament_id, user_tg_id) is not None


# ──────────────────────────────────────────────────────────────────────
# Naming
# ──────────────────────────────────────────────────────────────────────

def default_team_name(session, user_tg_id, user=None):
    """The team name to use when the admin doesn't supply one.

    Prefers the user's own team name, then their handle, then their first name,
    and finally falls back to the Telegram id — which is all we have when the
    person has never run /debut.
    """
    if user is None:
        user = (session.query(User)
                .filter(User.telegram_id == int(user_tg_id)).first())
    for value in (getattr(user, "team_name", None),
                  getattr(user, "username", None),
                  getattr(user, "first_name", None)):
        text = (value or "").strip()
        if text:
            return text[:MAX_NAME]
    return f"Player {int(user_tg_id)}"


def _short_code(name):
    """A 3-4 letter badge code derived from a team name (mirrors /letsplay)."""
    letters = "".join(ch for ch in (name or "") if ch.isalnum())
    return (letters[:4] or "TEAM").upper()


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────

def create_tournament(session, name, *, league_format="single", knockout=None,
                      max_teams=8, description=None,
                      points_win=2, points_tie=1, points_loss=0,
                      points_no_result=1):
    """Create a Lets Play tournament in ``draft`` status.

    ``league_format`` is a key of ``LEAGUE_FORMATS`` and ``knockout`` a key of
    ``KNOCKOUT_TYPES`` (or None for a league with no playoffs). Nothing is
    scheduled yet — participants are added first, then
    :func:`generate_schedule`. Caller commits.
    """
    label = (name or "").strip()
    if not label:
        raise LPTError("Give the tournament a name.")
    if league_format not in LEAGUE_FORMATS:
        raise LPTError("Format must be 'single' or 'double'.")
    if knockout not in (None, "") and knockout not in KNOCKOUT_TYPES:
        raise LPTError("Playoffs must be one of: "
                       + ", ".join(sorted(KNOCKOUT_TYPES)) + ".")
    lf, lf_label = LEAGUE_FORMATS[league_format]
    kt = KNOCKOUT_TYPES[knockout][0] if knockout else None
    try:
        cap = max(2, int(max_teams))
    except (TypeError, ValueError):
        raise LPTError("Max teams must be a number.")

    tour = Tournament(
        kind=KIND_LETSPLAY,
        name=label[:120],
        description=(description or "").strip() or None,
        # No Challenge League behind a Lets Play tournament; the snapshot columns
        # stay NULL and the play command is the fixed /lptour.
        league_id=None, league_name=None, command_snapshot="/lptour",
        format=(lf_label if not kt else f"{lf_label} + Playoffs")[:40],
        league_format=lf, knockout_type=kt,
        overs=LP_OVERS, max_teams=cap,
        points_win=int(points_win), points_tie=int(points_tie),
        points_loss=int(points_loss), points_no_result=int(points_no_result),
        status="draft",
    )
    session.add(tour)
    session.flush()
    logger.info("Created Lets Play tournament %s (%s)", tour.id, tour.name)
    return tour


def activate(session, tournament_id, *, keep_draft=False):
    """Make a Lets Play tournament the active one. Caller commits.

    The shared activator promotes a ``draft`` tournament straight to ``active``,
    which is right for a Challenge League tournament (its teams already exist)
    but wrong for a brand-new Lets Play one — the field is entered *after*
    creation, and going live early would let two players start a fixture before
    the schedule exists. ``keep_draft=True`` therefore holds the status where it
    was, so /lptstart stays the deliberate go-live step.
    """
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    before = tour.status
    tournament_service.activate_tournament(session, tour.id)
    if keep_draft and before in ("draft", "scheduled"):
        tour.status = before
    session.flush()
    return tour


def _has_results(session, tournament_id):
    """True once at least one fixture of *any* stage has been completed.

    Deliberately not ``league_progress``, which only counts the ``league`` and
    ``group`` stages: a ``pure_knockout`` tournament has no league fixtures at
    all, so a league-only check would read "no results" however far through the
    bracket it was — and then let the squad list change out from under a
    half-played bracket, deleting the remaining rounds and orphaning the
    ``feeds_winner_to_id`` links out of the completed ones.
    """
    return (session.query(TournamentMatch)
            .filter_by(tournament_id=int(tournament_id), status="completed")
            .first()) is not None


def _structure_is_locked(session, tour):
    """True when the fixture list can no longer be rebuilt.

    A generated schedule plus a recorded result is the point of no return:
    regenerating would throw away real results, and leaving the old list in place
    while the field changes would silently corrupt the points table. Either
    condition alone is recoverable — an unplayed schedule can just be rebuilt,
    and a free-play tournament has no fixture list to invalidate in the first
    place, so a latecomer can be waved in and simply starts on zero.
    """
    if not (tour.schedule_generated or tour.knockout_generated):
        return False
    return _has_results(session, tour.id)


def _clear_unplayed_schedule(session, tour):
    """Drop the unplayed league fixtures so the schedule can be regenerated.

    Called when the squad list changes before any result is in: the old fixture
    list no longer covers the right set of teams.
    """
    removed = (session.query(TournamentMatch)
               .filter_by(tournament_id=tour.id)
               .filter(TournamentMatch.status != "completed")
               .delete(synchronize_session=False))
    if removed:
        tour.schedule_generated = False
        tour.knockout_generated = False
    return removed


def add_participant(session, tournament_id, user_tg_id, name=None):
    """Add a user to a Lets Play tournament by Telegram id.

    The user does not have to exist in the database yet — the Telegram id is the
    identity, and the display name is resolved from their account when they have
    one. Returns ``(team, schedule_cleared)``; caller commits.
    """
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    try:
        tg_id = int(user_tg_id)
    except (TypeError, ValueError):
        raise LPTError("That doesn't look like a Telegram id.")
    if tg_id <= 0:
        raise LPTError("A Telegram id is a positive number — a negative id is a "
                       "group or channel, not a person.")

    if team_for_tg(session, tour.id, tg_id) is not None:
        raise LPTError("That user is already in this tournament.")
    if _structure_is_locked(session, tour):
        raise LPTError("The schedule is already under way — reset the tournament "
                       "before adding anyone else.")

    rows = participants(session, tour.id)
    if tour.max_teams and len(rows) >= int(tour.max_teams):
        raise LPTError(f"This tournament is full ({tour.max_teams} teams). Raise "
                       "the limit or remove someone first.")

    label = (name or "").strip() or default_team_name(session, tg_id)
    label = label[:MAX_NAME]
    # Team names double as the display identity in the points table and the
    # fixture list, so keep them distinct within a tournament.
    taken = {(r.name or "").strip().lower() for r in rows}
    if label.lower() in taken:
        label = f"{label} ({tg_id})"[:MAX_NAME]

    team = TournamentTeam(
        tournament_id=tour.id,
        user_tg_id=tg_id,
        challenge_team_id=None,
        name=label,
        short_name=_short_code(label),
        sort_order=len(rows),
    )
    session.add(team)
    cleared = _clear_unplayed_schedule(session, tour)
    session.flush()
    logger.info("Lets Play tournament %s: added %s as %r", tour.id, tg_id, label)
    return team, bool(cleared)


def remove_participant(session, tournament_id, user_tg_id):
    """Remove a participant by Telegram id. Returns ``(name, schedule_cleared)``."""
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    team = team_for_tg(session, tour.id, user_tg_id)
    if team is None:
        raise LPTError("That user isn't in this tournament.")
    # Removal is stricter than entry: their completed matches would be orphaned
    # and everyone who played them would lose those points, so any result at all
    # blocks it — free-play or not.
    if _has_results(session, tour.id):
        raise LPTError("Matches have already been played — reset the tournament "
                       "before removing anyone. To keep the results, leave them "
                       "in and stop scheduling them.")
    label = team.name
    session.delete(team)
    session.flush()
    cleared = _clear_unplayed_schedule(session, tour)
    session.flush()
    logger.info("Lets Play tournament %s: removed %s", tour.id, user_tg_id)
    return label, bool(cleared)


def rename_participant(session, tournament_id, user_tg_id, name):
    """Change a participant's team name. Returns ``(old_name, new_name)``."""
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    team = team_for_tg(session, tour.id, user_tg_id)
    if team is None:
        raise LPTError("That user isn't in this tournament.")
    label = (name or "").strip()[:MAX_NAME]
    if not label:
        raise LPTError("Give the team a name.")
    taken = {(r.name or "").strip().lower() for r in participants(session, tour.id)
             if r.id != team.id}
    if label.lower() in taken:
        raise LPTError("Another team in this tournament already uses that name.")
    old, team.name = team.name, label
    team.short_name = _short_code(label)
    session.flush()
    return old, label


def sync_participant_names(session, tournament_id):
    """Refresh auto-generated team names from the users' current accounts.

    A participant added before they ran /debut is named "Player <id>"; once they
    register, this replaces that placeholder with their real name. Names an admin
    set by hand are never touched. Returns the number of rows changed.
    """
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    changed = 0
    rows = participants(session, tour.id)
    taken = {(r.name or "").strip().lower() for r in rows}
    for team in rows:
        if not team.user_tg_id:
            continue
        if (team.name or "") != f"Player {int(team.user_tg_id)}":
            continue        # a real name — admin-set or already resolved
        fresh = default_team_name(session, team.user_tg_id)
        if fresh == team.name or fresh.lower() in taken:
            continue
        taken.discard((team.name or "").strip().lower())
        taken.add(fresh.lower())
        team.name = fresh
        team.short_name = _short_code(fresh)
        changed += 1
    if changed:
        session.flush()
    return changed


# ──────────────────────────────────────────────────────────────────────
# Schedule + bracket (thin wrappers that add the Lets Play guard rails)
# ──────────────────────────────────────────────────────────────────────

def generate_schedule(session, tournament_id):
    """Generate the round-robin fixture list. Returns the fixture count."""
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    if _has_results(session, tour.id):
        raise LPTError("Matches have already been played — reset the tournament "
                       "before regenerating the schedule.")
    if len(participants(session, tour.id)) < 2:
        raise LPTError("Add at least two teams first.")
    from services import league_schedule_service
    # Drop any earlier unplayed fixture list so regenerating doesn't stack.
    _clear_unplayed_schedule(session, tour)
    session.flush()
    try:
        count = league_schedule_service.generate_schedule(session, tour.id)
    except ValueError as exc:
        raise LPTError(str(exc))
    return count


def generate_knockout(session, tournament_id, knockout=None):
    """Seed the playoff bracket from the current table. Returns fixture count."""
    tour = get_tournament(session, tournament_id)
    if not tour:
        raise LPTError("That Lets Play tournament no longer exists.")
    if knockout:
        if knockout not in KNOCKOUT_TYPES:
            raise LPTError("Playoffs must be one of: "
                           + ", ".join(sorted(KNOCKOUT_TYPES)) + ".")
        tour.knockout_type = KNOCKOUT_TYPES[knockout][0]
    if not tour.knockout_type:
        raise LPTError("This tournament has no playoff format set. Pass one: "
                       + ", ".join(sorted(KNOCKOUT_TYPES)) + ".")
    if tour.knockout_type != "pure_knockout" \
            and not tournament_service.league_stage_complete(session, tour.id):
        played, total = tournament_service.league_progress(session, tour.id)
        raise LPTError(f"The league stage isn't finished yet ({played}/{total} "
                       "played) — the bracket would be seeded from a partial table.")
    from services import knockout_service
    try:
        count = knockout_service.generate_knockout(session, tour.id)
    except ValueError as exc:
        raise LPTError(str(exc))
    if tour.knockout_type == "pure_knockout":
        # A straight knockout has no league stage, so the bracket *is* the
        # schedule — flag it so the play command gates on bracket pairings.
        tour.schedule_generated = True
    session.flush()
    return count


# ──────────────────────────────────────────────────────────────────────
# Match eligibility — the gate behind /lptour
# ──────────────────────────────────────────────────────────────────────

def check_pair(session, tour, host_tg_id, guest_tg_id):
    """Decide whether these two users may play a tournament match right now.

    Returns ``(host_team, guest_team)`` on success and raises ``LPTError`` with a
    player-facing explanation otherwise. Checks, in the order a player would ask
    them: is the tournament playable, are both of us in it, and is there a
    fixture left for this pairing?
    """
    if tour is None:
        raise LPTError("No Lets Play tournament is running right now.")
    status = (tour.status or "").lower()
    if status == "completed":
        raise LPTError(f"“{tour.name}” has already been completed.")
    if status == "cancelled":
        raise LPTError(f"“{tour.name}” was cancelled.")
    if status == "paused":
        raise LPTError(f"“{tour.name}” is paused — an admin has to resume it "
                       "before the next match.")
    if status != tournament_service.PLAYABLE_STATUS:
        raise LPTError(f"“{tour.name}” hasn't started yet. An admin needs to "
                       "start it first.")

    host_team = team_for_tg(session, tour.id, host_tg_id)
    guest_team = team_for_tg(session, tour.id, guest_tg_id)
    if host_team is None and guest_team is None:
        raise LPTError(f"Neither of you is entered in “{tour.name}”.")
    if host_team is None:
        raise LPTError(f"You aren't entered in “{tour.name}”. Ask an admin to add "
                       "you with /lptadd.")
    if guest_team is None:
        raise LPTError(f"They aren't entered in “{tour.name}”. Ask an admin to add "
                       "them with /lptadd.")
    if host_team.id == guest_team.id:
        raise LPTError("You can't play a tournament match against yourself.")

    # Fixture gating. Without a generated schedule the tournament is free-play:
    # any two participants may meet any number of times.
    if tour.schedule_generated or tour.knockout_generated:
        from services import league_schedule_service
        fixture = league_schedule_service.find_open_fixture(
            session, tour.id, host_team.id, guest_team.id)
        if fixture is None:
            raise LPTError(
                f"There's no fixture left between {host_team.name} and "
                f"{guest_team.name} in “{tour.name}”. "
                "Check /lptfixtures for who you still have to play.")
    return host_team, guest_team


def reserve_pair_fixture(session, tour, host_team_id, guest_team_id):
    """Claim this pairing's fixture (scheduled → live) just before launch.

    Returns the fixture id, or None when the tournament is free-play (nothing to
    reserve). Raises ``LPTError`` when a fixture is required but somebody else
    just took it. Caller commits.
    """
    if not (tour.schedule_generated or tour.knockout_generated):
        return None
    from services import league_schedule_service
    fixture = league_schedule_service.reserve_fixture(
        session, tour.id, host_team_id, guest_team_id)
    if fixture is None:
        raise LPTError("That fixture has just been taken or played. Check "
                       "/lptfixtures and try again.")
    return fixture.id


# ──────────────────────────────────────────────────────────────────────
# Rendering for the bot
# ──────────────────────────────────────────────────────────────────────

_STATUS_LABEL = {
    "draft": "📝 Draft (not started)",
    "scheduled": "🗓️ Scheduled",
    "active": "🟢 Live",
    "paused": "⏸️ Paused",
    "completed": "🏁 Completed",
    "cancelled": "❌ Cancelled",
}


def status_label(tour):
    """A tournament's lifecycle status as a badge players can read."""
    return _STATUS_LABEL.get((tour.status or "").lower(), tour.status or "—")


def _nrr_text(value):
    """Net run rate, always signed — a leading '+' reads as deliberate."""
    return f"{value:+.3f}"


def render_table(session, tour):
    """The points table as an HTML message body."""
    rows = tournament_service.points_table(session, tour.id)
    if not rows:
        return (f"🏆 <b>{escape(tour.name)}</b>\n"
                "No teams have been added yet.")
    played, total = tournament_service.league_progress(session, tour.id)
    out = [f"🏆 <b>{escape(tour.name)}</b> — Points Table",
           f"{status_label(tour)}"
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


_STAGE_LABEL = {
    "league": "League", "group": "Group",
    "quarterfinal": "Quarter-final", "semifinal": "Semi-final",
    "qualifier1": "Qualifier 1", "eliminator": "Eliminator",
    "qualifier2": "Qualifier 2", "final": "Final",
    "round_of_16": "Round of 16", "round_of_32": "Round of 32",
}


def render_fixtures(session, tour, viewer_tg_id=None, limit=40):
    """The fixture list as an HTML message body.

    When ``viewer_tg_id`` belongs to a participant, their own remaining fixtures
    are pulled out into a "Your next matches" section — the thing a player
    actually opens this for.
    """
    fixtures = (session.query(TournamentMatch)
                .filter_by(tournament_id=tour.id)
                .order_by(TournamentMatch.match_no, TournamentMatch.round_no,
                          TournamentMatch.id).all())
    names = {tt.id: (tt.name or "—") for tt in participants(session, tour.id)}
    header = [f"🗓️ <b>{escape(tour.name)}</b> — Fixtures"]
    if not fixtures:
        header.append("")
        header.append("No schedule has been generated — this tournament is "
                      "free-play: any two entered players can start /lptour.")
        return "\n".join(header)

    def _slot(team_id, label):
        if team_id and team_id in names:
            return escape(names[team_id])
        return escape(label or "TBD")

    mine = None
    if viewer_tg_id is not None:
        my_team = team_for_tg(session, tour.id, viewer_tg_id)
        mine = my_team.id if my_team else None

    my_lines, all_lines = [], []
    for fx in fixtures[:limit]:
        stage = _STAGE_LABEL.get(fx.stage or "league", (fx.stage or "").title())
        a = _slot(fx.team1_id, fx.slot1_label)
        b = _slot(fx.team2_id, fx.slot2_label)
        tag = f"M{fx.match_no}" if fx.match_no else stage
        if fx.status == "completed":
            body = f"✅ <s>{a} vs {b}</s> — {escape(fx.result_text or 'done')}"
        elif fx.status == "live":
            body = f"🔴 {a} vs {b} — <i>in progress</i>"
        else:
            body = f"⚪ {a} vs {b}"
        line = f"<code>{tag}</code> {body}"
        all_lines.append(line)
        if mine and fx.status != "completed" and mine in (fx.team1_id, fx.team2_id):
            my_lines.append(line)

    out = list(header)
    if my_lines:
        out += ["", "<b>Your next matches</b>"] + my_lines[:8]
    out += ["", "<b>Full schedule</b>"] + all_lines
    if len(fixtures) > limit:
        out.append(f"<i>…and {len(fixtures) - limit} more.</i>")
    return "\n".join(out)


def render_teams(session, tour):
    """The participating-squad list as an HTML message body."""
    rows = participants(session, tour.id)
    out = [f"👥 <b>{escape(tour.name)}</b> — Teams "
           f"({len(rows)}/{tour.max_teams or '∞'})"]
    if not rows:
        out += ["", "Nobody has been added yet. An admin adds players with "
                    "<code>/lptadd &lt;telegram_id&gt;</code>."]
        return "\n".join(out)
    out.append("")
    for i, tt in enumerate(rows, 1):
        # Plain text, never a mention: a squad list must not ping everyone in it.
        out.append(f"{i}. <b>{escape(tt.name or '—')}</b> · "
                   f"<code>{tt.user_tg_id or '—'}</code>")
    return "\n".join(out)


def render_overview(session, tour):
    """The tournament's front page as an HTML message body."""
    rows = participants(session, tour.id)
    played, total = tournament_service.league_progress(session, tour.id)
    table = tournament_service.points_table(session, tour.id)
    out = [f"🏆 <b>{escape(tour.name)}</b>",
           "━━━━━━━━━━━━━━━━━━━",
           f"<b>Status:</b> {status_label(tour)}",
           f"<b>Format:</b> {escape(tour.format or 'League')} · {tour.overs} overs "
           "· own rosters",
           f"<b>Teams:</b> {len(rows)}/{tour.max_teams or '∞'}"]
    if total:
        out.append(f"<b>League:</b> {played}/{total} matches played")
    elif rows:
        out.append("<b>Schedule:</b> free-play (no fixture list)")
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
    out += ["━━━━━━━━━━━━━━━━━━━",
            "Play a fixture: reply to your opponent with <code>/lptour</code>"]
    return "\n".join(out)
