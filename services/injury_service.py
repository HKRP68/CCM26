"""Cricket injuries for a Challenge League Tournament.

Off by default. When a tournament has ``injuries_enabled``, finishing one of its
matches can leave a player carrying a knock that rules them out of their team's
next few tournament matches — they simply cannot be picked in the XI until they
are fit again.

How an injury is decided
------------------------
The victim is drawn from the players who actually did the work in that match,
weighted by workload: a bowler who sent down four overs is far likelier to pull
up sore than a batter who faced two balls, and someone who neither batted nor
bowled is never picked. What they were doing then decides the *kind* of injury —
a side strain is a bowler's injury, a concussion is a batter's — so the news
reads like something that happened in the match just played.

Severity is a three-rung ladder (niggle → strain → serious) weighted heavily
towards the minor end, and the tournament's ``injury_max_matches`` is a hard
ceiling on the layoff. The default ladder tops out at 3 matches.

Rules that hold no matter what the dice say
-------------------------------------------
* A team is never injured below a fieldable XI. If a knock would leave fewer
  than 11 fit players in the squad, it is not applied — the tournament must
  stay playable.
* Nobody is injured twice over. An already-injured player is not in the pool.
* Only Challenge League tournaments generate injuries. A Lets Play tournament's
  "team" is a person playing their own roster, which this does not model.
* Counting down and rolling both happen inside the match-recording transaction,
  which is idempotent on ``match_id`` — so a replayed finalize cannot serve a
  match twice or injure a second player.
"""

import logging
import random

from models import Tournament, TournamentTeam, TournamentInjury

logger = logging.getLogger(__name__)

# A squad must always be able to field this many fit players.
XI_SIZE = 11

# Severity ladder: (key, label, matches out, weight). Weighted hard towards the
# minor end — a tournament where every knock is a three-match layoff stops being
# cricket and starts being attrition.
SEVERITIES = (
    ("niggle", "Niggle", 1, 55),
    ("strain", "Strain", 2, 30),
    ("serious", "Serious", 3, 15),
)
_SEVERITY_LABEL = {key: label for key, label, _m, _w in SEVERITIES}

# Injury catalogue, keyed by what the player was doing. Each entry is
# (name, how it happened, hardest severity it can reach).
INJURIES = {
    "bowl": [
        ("Side Strain", "pulled up in his delivery stride", "serious"),
        ("Shoulder Niggle", "after a long spell", "strain"),
        ("Ankle Roll", "landed awkwardly in the crease", "serious"),
        ("Hamstring Tightness", "felt it running in", "strain"),
        ("Back Spasm", "stiffened up after his last over", "serious"),
        ("Split Webbing", "off his own follow-through", "niggle"),
    ],
    "bat": [
        ("Hamstring Tear", "going for a second run", "serious"),
        ("Concussion", "took a bouncer on the helmet", "serious"),
        ("Blow to the Hand", "gloved a rising delivery", "strain"),
        ("Cramp", "late in a long innings", "niggle"),
        ("Groin Strain", "stretching for the crease", "strain"),
        ("Bruised Ribs", "wore one into the body", "strain"),
    ],
    "field": [
        ("Dislocated Finger", "taking a sharp chance in the ring", "strain"),
        ("Shoulder Knock", "diving to cut one off", "strain"),
        ("Corked Thigh", "sliding in the deep", "niggle"),
        ("Twisted Knee", "turning in the outfield", "serious"),
    ],
}

# Severity is capped per injury type, so a Cramp is never a three-match layoff.
_SEVERITY_RANK = {"niggle": 0, "strain": 1, "serious": 2}


# ──────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────

def settings(tour):
    """``(enabled, chance_pct, max_matches)`` for a tournament.

    Everything is clamped here so a hand-edited row can never produce a
    500%-chance, 40-match injury.
    """
    if tour is None:
        return False, 0, 3
    enabled = bool(getattr(tour, "injuries_enabled", False))
    try:
        chance = int(getattr(tour, "injury_chance", None) or 0)
    except (TypeError, ValueError):
        chance = 0
    try:
        cap = int(getattr(tour, "injury_max_matches", None) or 3)
    except (TypeError, ValueError):
        cap = 3
    return enabled, max(0, min(100, chance)), max(1, min(5, cap))


def enabled_for(tour):
    """True when this tournament's injuries are switched on.

    A Lets Play tournament is excluded: its "team" is a person playing their own
    roster, and there is no squad to rule a player out of.
    """
    if tour is None:
        return False
    from services import tournament_service
    if tournament_service.tournament_kind(tour) != tournament_service.KIND_CHALLENGE:
        return False
    return settings(tour)[0]


# ──────────────────────────────────────────────────────────────────────
# Reading the treatment table
# ──────────────────────────────────────────────────────────────────────

def active_injuries(session, tournament_id, tournament_team_id=None):
    """Injuries still keeping somebody out, newest first."""
    q = (session.query(TournamentInjury)
         .filter(TournamentInjury.tournament_id == int(tournament_id),
                 TournamentInjury.matches_remaining > 0))
    if tournament_team_id is not None:
        q = q.filter(TournamentInjury.tournament_team_id == int(tournament_team_id))
    return q.order_by(TournamentInjury.created_at.desc(),
                      TournamentInjury.id.desc()).all()


def injured_roster_ids(session, tournament_id, tournament_team_id):
    """The ``ChallengePlayer`` ids this team may not pick right now."""
    if not tournament_team_id:
        return set()
    rows = (session.query(TournamentInjury.roster_id)
            .filter(TournamentInjury.tournament_id == int(tournament_id),
                    TournamentInjury.tournament_team_id == int(tournament_team_id),
                    TournamentInjury.matches_remaining > 0).all())
    return {int(r[0]) for r in rows if r[0] is not None}


def unavailable_for_team(session, tour, challenge_team_id):
    """``(injured roster ids, [injury rows])`` for a Challenge League team.

    Takes the ``ChallengeTeam`` id the bot's draft already knows and follows it
    to the participating row. Returns empty when injuries are off, the team is
    not in the tournament, or nobody is hurt — so every caller can use it
    unconditionally.
    """
    if not enabled_for(tour) or not challenge_team_id:
        return set(), []
    tt = (session.query(TournamentTeam)
          .filter_by(tournament_id=int(tour.id),
                     challenge_team_id=int(challenge_team_id)).first())
    if tt is None:
        return set(), []
    rows = active_injuries(session, tour.id, tt.id)
    return {int(r.roster_id) for r in rows if r.roster_id is not None}, rows


def severity_label(key):
    return _SEVERITY_LABEL.get(key, (key or "").title() or "Knock")


# ──────────────────────────────────────────────────────────────────────
# Counting down and healing
# ──────────────────────────────────────────────────────────────────────

def serve_match(session, tournament_id, team_ids):
    """Count one match off every active injury of the given teams. Caller commits.

    Called as a completed match is recorded, *before* new injuries are rolled —
    a player hurt in this match must not have it counted as time served.
    Returns the injuries that ran out (i.e. the players now fit again).
    """
    ids = [int(t) for t in (team_ids or []) if t]
    if not ids:
        return []
    rows = (session.query(TournamentInjury)
            .filter(TournamentInjury.tournament_id == int(tournament_id),
                    TournamentInjury.tournament_team_id.in_(ids),
                    TournamentInjury.matches_remaining > 0).all())
    recovered = []
    from datetime import datetime
    for row in rows:
        row.matches_remaining = max(0, int(row.matches_remaining or 0) - 1)
        if row.matches_remaining == 0:
            row.recovered_at = datetime.utcnow()
            recovered.append(row)
    if rows:
        session.flush()
    return recovered


def heal(session, injury_id):
    """Declare a player fit immediately. Caller commits."""
    from datetime import datetime
    row = session.get(TournamentInjury, int(injury_id))
    if row is None:
        return None
    row.matches_remaining = 0
    row.recovered_at = datetime.utcnow()
    session.flush()
    return row


def clear_for_tournament(session, tournament_id, *, only_active=False):
    """Wipe a tournament's injury table. Caller commits. Returns rows removed."""
    q = (session.query(TournamentInjury)
         .filter(TournamentInjury.tournament_id == int(tournament_id)))
    if only_active:
        q = q.filter(TournamentInjury.matches_remaining > 0)
    n = q.delete(synchronize_session=False)
    session.flush()
    return n


# ──────────────────────────────────────────────────────────────────────
# Ruling somebody out
# ──────────────────────────────────────────────────────────────────────

def _squad_size(session, tournament_team_id):
    """How many ``ChallengePlayer`` rows this participating team can pick from."""
    from models import ChallengePlayer
    tt = session.get(TournamentTeam, int(tournament_team_id))
    if tt is None or not tt.challenge_team_id:
        return 0
    return (session.query(ChallengePlayer)
            .filter(ChallengePlayer.team_id == int(tt.challenge_team_id)).count())


def _workload(line):
    """How much of the match this player got through, as a weight.

    Balls bowled count double: bowling is the repetitive, high-load action that
    actually breaks people down, and weighting it makes the injury list read
    like a real one instead of a lottery.
    """
    balls_bowled = int(line.get("bowl_balls") or 0)
    balls_faced = int(line.get("bat_balls") or 0)
    return balls_bowled * 2 + balls_faced


def _activity_for(line, rng):
    """Which catalogue a player's injury comes from: bowl, bat or field.

    Weighted by what they actually did, with a standing chance of a fielding
    injury for anyone who was on the park at all.
    """
    buckets = []
    if int(line.get("bowl_balls") or 0) > 0:
        buckets += ["bowl"] * (2 + int(line["bowl_balls"]) // 6)
    if int(line.get("bat_balls") or 0) > 0:
        buckets += ["bat"] * (2 + int(line["bat_balls"]) // 10)
    buckets += ["field", "field"]
    return rng.choice(buckets)


def _pick_severity(cap_key, max_matches, rng):
    """``(key, label, matches)`` — a weighted severity, capped two ways.

    ``cap_key`` is the worst this *injury type* can be (Cramp is never serious);
    ``max_matches`` is the tournament's own ceiling.
    """
    ceiling = _SEVERITY_RANK.get(cap_key, 2)
    options = [(k, lbl, m, w) for (k, lbl, m, w) in SEVERITIES
               if _SEVERITY_RANK[k] <= ceiling]
    if not options:
        options = [SEVERITIES[0]]
    key, label, matches, _w = rng.choices(
        options, weights=[o[3] for o in options], k=1)[0]
    matches = max(1, min(int(max_matches), int(matches)))
    # The top rung stretches to the tournament's ceiling, so a 4-match cap
    # actually produces the occasional 4-match layoff rather than never.
    if key == "serious" and max_matches > 3:
        matches = rng.randint(3, int(max_matches))
    return key, label, matches


def _eligible_lines(lines, user_ids, already_out):
    """Players from these users who did something and are not already hurt."""
    out = []
    for line in lines or []:
        if int(line.get("user_id") or 0) not in user_ids:
            continue
        rid = line.get("roster_id")
        if rid is None or int(rid) in already_out:
            continue
        if _workload(line) <= 0:
            continue
        out.append(line)
    return out


def roll_for_team(session, tour, tt, lines, user_id, *, match_id=None,
                  tournament_match_id=None, rng=None):
    """Maybe injure one of this team's players. Caller commits.

    Returns the new ``TournamentInjury``, or None when the dice said no, nobody
    was eligible, or ruling someone out would leave the squad unable to field an
    XI. Never raises for a missing/ill-formed state — an injury roll must not be
    able to cost somebody their match result.
    """
    rng = rng or random
    enabled, chance, cap = settings(tour)
    if not enabled or chance <= 0 or tt is None:
        return None
    if rng.randint(1, 100) > chance:
        return None

    already_out = injured_roster_ids(session, tour.id, tt.id)
    # A squad that is already down to its bare XI cannot lose anybody else.
    squad = _squad_size(session, tt.id)
    if squad - len(already_out) - 1 < XI_SIZE:
        logger.info("Injury skipped for tournament team %s: squad of %s already "
                    "has %s out", tt.id, squad, len(already_out))
        return None

    pool = _eligible_lines(lines, {int(user_id)}, already_out)
    if not pool:
        return None

    line = rng.choices(pool, weights=[_workload(p) for p in pool], k=1)[0]
    activity = _activity_for(line, rng)
    name, how, worst = rng.choice(INJURIES[activity])
    severity, _label, matches = _pick_severity(worst, cap, rng)

    injury = TournamentInjury(
        tournament_id=int(tour.id),
        tournament_team_id=int(tt.id),
        roster_id=int(line["roster_id"]),
        player_id=line.get("player_id"),
        player_name=(line.get("name") or "Player")[:150],
        injury_type=name[:80],
        severity=severity,
        how=how[:120] if how else None,
        matches_out=matches,
        matches_remaining=matches,
        match_id=match_id,
        tournament_match_id=tournament_match_id,
    )
    session.add(injury)
    session.flush()
    logger.info("Tournament %s: %s (%s) out for %s match(es) — %s",
                tour.id, injury.player_name, tt.name, matches, name)
    return injury


def add_manual(session, tournament_id, tournament_team_id, roster_id, *,
               injury_type="Knock", matches=1, severity="niggle",
               player_name=None, player_id=None):
    """Rule a player out by hand (the admin panel). Caller commits.

    Replaces any injury the player is already carrying rather than stacking a
    second one on top, so an admin correcting a layoff gets the layoff they
    typed instead of two overlapping ones.
    """
    tour = session.get(Tournament, int(tournament_id))
    _e, _c, cap = settings(tour)
    matches = max(1, min(cap, int(matches or 1)))
    (session.query(TournamentInjury)
     .filter(TournamentInjury.tournament_id == int(tournament_id),
             TournamentInjury.tournament_team_id == int(tournament_team_id),
             TournamentInjury.roster_id == int(roster_id),
             TournamentInjury.matches_remaining > 0)
     .delete(synchronize_session=False))
    injury = TournamentInjury(
        tournament_id=int(tournament_id),
        tournament_team_id=int(tournament_team_id),
        roster_id=int(roster_id),
        player_id=player_id,
        player_name=(player_name or "Player")[:150],
        injury_type=(injury_type or "Knock")[:80],
        severity=severity if severity in _SEVERITY_LABEL else "niggle",
        matches_out=matches,
        matches_remaining=matches,
    )
    session.add(injury)
    session.flush()
    return injury


# ──────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────

def describe(injury, *, team_name=None):
    """One line of injury news, e.g. "Bumrah (MI) — Side Strain, out 2 matches"."""
    who = injury.player_name or "Player"
    where = f" ({team_name})" if team_name else ""
    n = int(injury.matches_remaining or 0)
    span = "1 match" if n == 1 else f"{n} matches"
    return f"{who}{where} — {injury.injury_type}, out {span}"


def process_match(session, tour, tm, lines, *, user_by_team=None, rng=None):
    """Serve one match off the clock, then roll for fresh injuries.

    ``user_by_team`` maps ``TournamentTeam.id`` → the db user id that played it,
    which is how a scorecard line is matched to a side. Returns
    ``{"new": [...], "recovered": [...]}`` of ``TournamentInjury`` rows — empty
    lists when injuries are off, so the caller needs no guard of its own.

    Order matters: the count-down runs first, so a player hurt in *this* match
    does not have it counted as a match already served. Both steps live inside
    the match-recording transaction, which is idempotent on ``match_id``.
    """
    blank = {"new": [], "recovered": []}
    if not enabled_for(tour) or tm is None:
        return blank
    team_ids = [tid for tid in (getattr(tm, "team1_id", None),
                                getattr(tm, "team2_id", None)) if tid]
    if not team_ids:
        return blank
    try:
        recovered = serve_match(session, tour.id, team_ids)
        created = []
        for tid in team_ids:
            uid = (user_by_team or {}).get(int(tid))
            if uid is None:
                continue
            tt = session.get(TournamentTeam, int(tid))
            injury = roll_for_team(
                session, tour, tt, lines, uid,
                match_id=getattr(tm, "match_id", None),
                tournament_match_id=tm.id, rng=rng)
            if injury is not None:
                created.append(injury)
        return {"new": created, "recovered": recovered}
    except Exception:
        # An injury roll must never cost somebody their match result.
        logger.exception("Injury processing failed for tournament %s", tour.id)
        return blank


def render_report(session, tournament_id, report):
    """The injury news for one match as an HTML block, or "" when there is none."""
    new = (report or {}).get("new") or []
    recovered = (report or {}).get("recovered") or []
    if not new and not recovered:
        return ""
    from html import escape
    names = {tt.id: (tt.name or "Team") for tt in
             session.query(TournamentTeam)
             .filter_by(tournament_id=int(tournament_id)).all()}
    out = ["🚑 <b>Injury news</b>"]
    for row in new:
        how = f" — {escape(row.how)}" if row.how else ""
        out.append(
            f"❌ <b>{escape(row.player_name or 'Player')}</b> "
            f"({escape(names.get(row.tournament_team_id, 'Team'))}){how}. "
            f"{escape(row.injury_type)} — out for "
            f"{row.matches_out} match{'' if row.matches_out == 1 else 'es'}.")
    for row in recovered:
        out.append(
            f"✅ <b>{escape(row.player_name or 'Player')}</b> "
            f"({escape(names.get(row.tournament_team_id, 'Team'))}) is fit again.")
    return "\n".join(out)
