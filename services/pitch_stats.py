"""What the pitches actually did — recorded from real matches, read by /pitchstats.

``tools/pitch_calibration`` answers "what does the engine produce when it plays
itself". This answers the different question: what happened when *people* played
on these surfaces. The two should agree, and the command shows them side by side
precisely so it is obvious when they do not.

**What counts.** Only ``/letsplay`` and Challenge League (``/cipl``,
``/c<league>``) matches, tournament fixtures included — those are the modes that
run the full Approach engine between two humans. Practice matches against the AI
captain are excluded: they are unranked, and the bot's picks would drown the
human record. Matches abandoned or force-ended never reach the recorder at all.

**Where the numbers come from.** The ``matches`` row already holds the pitch, the
scores and the winner. Two things it cannot hold are what this module captures at
match end, while the live state still exists:

* how many BALLS each innings lasted — a chase won in 17.2 overs makes
  runs-per-over a lie if you divide by the match length;
* the over-by-over approach duel, which is the only record of which batting
  intent met which bowling plan and what the over was worth.

Everything is rated **per six balls**, not per over bowled, so a partial last
over and The Hundred's five-ball sets are both counted honestly.

Public API:
    record_match(session, match, state, result)     — call at match end
    overview(session, mode=None, tournament_only=False)
    pitch_detail(session, pitch, mode=None, tournament_only=False)
    approach_table(session, pitch=None, mode=None, phase=None, ...)
"""

import logging

from sqlalchemy import func

from engine import pitch_registry
from models import PitchApproachStat, PitchMatchStat
from services.match_outcome import TYPE_CHALLENGE, TYPE_CIPL, TYPE_LETSPLAY

logger = logging.getLogger(__name__)

# Match types that count, mapped to the two buckets /pitchstats slices by.
# /cipl and /c<league> are both Challenge League — different lobbies, the same
# drafted-squad contest — so they share a bucket.
MODE_LETSPLAY = "letsplay"
MODE_CHALLENGE = "challenge"

MODE_OF_MATCH_TYPE = {
    TYPE_LETSPLAY: MODE_LETSPLAY,
    TYPE_CIPL: MODE_CHALLENGE,
    TYPE_CHALLENGE: MODE_CHALLENGE,
}

MODE_LABELS = {
    MODE_LETSPLAY: "Lets Play",
    MODE_CHALLENGE: "Challenge League",
}

PHASES = ("powerplay", "middle", "death")
PHASE_LABELS = {"powerplay": "Powerplay", "middle": "Middle", "death": "Death"}

# Column prefix per phase on PitchMatchStat.
_PHASE_COLS = {"powerplay": "pp", "middle": "mid", "death": "death"}

RESULT_BAT_FIRST = "bat_first"
RESULT_BAT_SECOND = "bat_second"
RESULT_TIE = "tie"


# ══════════════════════════════════════════════════════════════════════
# WRITING
# ══════════════════════════════════════════════════════════════════════

def is_countable(match, state):
    """True when this finished match belongs in the pitch record.

    Deliberately checks the live state for the bot flag rather than the match
    type: a practice match against the AI captain is written to the same
    ``matches`` row with the same ``match_type`` as a human one, and the only
    thing that tells them apart is ``state["is_bot_match"]``.
    """
    if match is None or not state:
        return False
    if state.get("is_bot_match"):
        return False
    if MODE_OF_MATCH_TYPE.get(getattr(match, "match_type", None)) is None:
        return False
    return pitch_registry.is_known(getattr(match, "pitch_type", None))


def _result_of(state, result):
    """``"bat_first"`` / ``"bat_second"`` / ``"tie"`` for a finished match.

    A user id in ``result["winner_id"]`` is used first because it cannot be
    ambiguous; ``cipl_match.compute_result`` speaks in team NAMES, so that is
    the fallback, and a Super Over hands us the same shape with its own winner
    substituted in.

    The margin type is deliberately NOT a last resort here. A Super Over's
    "wickets" margin describes the tie-breaker over, not the innings this row is
    about, and guessing "batting second won" from it would quietly corrupt the
    very split the command exists to report. An unrecognisable winner is
    recorded as a tie, which is what the main innings actually were.
    """
    if not result or result.get("tie"):
        return RESULT_TIE

    winner_uid = result.get("winner_id")
    if winner_uid:
        if winner_uid == state.get("inn1_bat_team_id"):
            return RESULT_BAT_FIRST
        if winner_uid == state.get("inn1_bowl_team_id"):
            return RESULT_BAT_SECOND

    winner = result.get("winner")
    if not winner:
        return RESULT_TIE
    first = state.get("inn1_bat_team")
    if first and winner == first:
        return RESULT_BAT_FIRST
    second = state.get("inn1_bowl_team")
    if second and winner == second:
        return RESULT_BAT_SECOND
    logger.warning("pitch stats: winner %r matches neither innings side", winner)
    return RESULT_TIE


def _phase_totals(state):
    """Per-phase (runs, wickets, balls) across both innings, from the duels."""
    totals = {p: [0, 0, 0] for p in PHASES}
    for key in ("inn1_approach_log", "approach_log"):
        for entry in state.get(key) or []:
            phase = entry.get("phase")
            if phase not in totals:
                continue
            row = totals[phase]
            row[0] += int(entry.get("runs") or 0)
            row[1] += int(entry.get("wickets") or 0)
            # Overs logged before the ball count was recorded fall back to a
            # whole over, which is what all but the last one of an innings is.
            row[2] += int(entry.get("balls") or 6)
    return totals


def _toss_facts(match, state, outcome):
    """``(decision, by_the_book, toss_winner_won)`` for this match.

    ``by_the_book`` compares the elected decision against the surface's own
    recommendation in engine.pitch_registry — the same call the Pitch Report
    card shows the captain before they choose.
    """
    decision = (getattr(match, "toss_decision", None) or "").strip().lower() or None
    if decision not in ("bat", "bowl"):
        decision = None
    pitch = pitch_registry.normalise(getattr(match, "pitch_type", None))
    day_night = (state or {}).get("day_night")
    book = pitch_registry.toss_call(pitch, day_night)
    by_the_book = None if decision is None else (decision == book)

    toss_winner_won = None
    toss_uid = getattr(match, "toss_winner_id", None)
    winner_uid = getattr(match, "winner_id", None)
    if toss_uid and winner_uid:
        toss_winner_won = bool(toss_uid == winner_uid)
    elif toss_uid and outcome == RESULT_TIE:
        toss_winner_won = None
    return decision, by_the_book, toss_winner_won


def record_match(session, match, state, result):
    """Record one finished match's contribution to the pitch record.

    Idempotent on ``match_id``: replaying a completion (a retried finalise, a
    Super Over finishing a match the main path already wrote) updates the match
    row in place and re-bases the approach rollup, rather than counting the
    match twice. The re-base subtracts the overs in the state it is handed,
    which is correct for every replay the engine can produce — a tie returns to
    start the Super Over *before* the ordinary finalise runs, so both paths see
    the same finished main-match state.

    Returns the :class:`PitchMatchStat` row, or ``None`` when the match does not
    count. Never raises — the caller is a match-completion path, and a stats
    write must not be able to cost a player their result.
    """
    try:
        if not is_countable(match, state):
            return None

        mode = MODE_OF_MATCH_TYPE[match.match_type]
        pitch = pitch_registry.normalise(match.pitch_type)
        outcome = _result_of(state, result)
        phases = _phase_totals(state)
        decision, by_the_book, toss_won = _toss_facts(match, state, outcome)

        row = (session.query(PitchMatchStat)
               .filter(PitchMatchStat.match_id == match.id).first())
        replacing = row is not None
        if replacing:
            # Undo this match's earlier contribution before adding it again, so
            # a re-record cannot inflate the rollup.
            _apply_approach_delta(session, row, state, sign=-1)
        else:
            row = PitchMatchStat(match_id=match.id)
            session.add(row)

        row.pitch_type = pitch
        row.mode = mode
        row.is_tournament = bool(getattr(match, "tournament_id", None))
        row.ball_format = state.get("ball_format") or "T20"
        row.overs = int(state.get("overs") or getattr(match, "overs", 20) or 20)
        row.inn1_runs = int(state.get("inn1_runs") or 0)
        row.inn1_wickets = int(state.get("inn1_wickets") or 0)
        row.inn1_balls = int(state.get("inn1_balls") or 0)
        row.inn2_runs = int(state.get("total_runs") or 0)
        row.inn2_wickets = int(state.get("total_wickets") or 0)
        row.inn2_balls = int(_innings2_balls(state))
        row.result = outcome
        row.toss_decision = decision
        row.toss_by_the_book = by_the_book
        row.toss_winner_won = toss_won
        for phase, prefix in _PHASE_COLS.items():
            runs, wkts, balls = phases[phase]
            setattr(row, f"{prefix}_runs", runs)
            setattr(row, f"{prefix}_wickets", wkts)
            setattr(row, f"{prefix}_balls", balls)

        _apply_approach_delta(session, row, state, sign=+1)
        return row
    except Exception:
        logger.exception("pitch stats recording failed for match %s",
                         getattr(match, "id", None))
        return None


def _innings2_balls(state):
    """Legal balls bowled in the second innings."""
    try:
        from services import cipl_match
        return cipl_match.balls_bowled(state)
    except Exception:
        # Fall back to the duel log, which counts the same deliveries.
        return sum(int(e.get("balls") or 6) for e in (state.get("approach_log") or []))


def _apply_approach_delta(session, row, state, sign):
    """Add (or, with ``sign=-1``, remove) this match's overs from the rollup."""
    cells = {}
    for key in ("inn1_approach_log", "approach_log"):
        for entry in state.get(key) or []:
            phase = entry.get("phase")
            bat, bowl = entry.get("bat"), entry.get("bowl")
            if phase not in PHASES or not bat or not bowl:
                continue
            cell = cells.setdefault((phase, bat, bowl), [0, 0, 0, 0])
            cell[0] += 1
            cell[1] += int(entry.get("balls") or 6)
            cell[2] += int(entry.get("runs") or 0)
            cell[3] += int(entry.get("wickets") or 0)
    if not cells:
        return

    existing = {(r.phase, r.bat_approach, r.bowl_approach): r
                for r in session.query(PitchApproachStat)
                .filter(PitchApproachStat.pitch_type == row.pitch_type,
                        PitchApproachStat.mode == row.mode).all()}
    for (phase, bat, bowl), (overs, balls, runs, wkts) in cells.items():
        stat = existing.get((phase, bat, bowl))
        if stat is None:
            if sign < 0:
                continue            # nothing to subtract from
            stat = PitchApproachStat(
                pitch_type=row.pitch_type, mode=row.mode, phase=phase,
                bat_approach=bat, bowl_approach=bowl,
                overs=0, balls=0, runs=0, wickets=0)
            session.add(stat)
            existing[(phase, bat, bowl)] = stat
        stat.overs = max(0, (stat.overs or 0) + sign * overs)
        stat.balls = max(0, (stat.balls or 0) + sign * balls)
        stat.runs = max(0, (stat.runs or 0) + sign * runs)
        stat.wickets = max(0, (stat.wickets or 0) + sign * wkts)


# ══════════════════════════════════════════════════════════════════════
# READING
# ══════════════════════════════════════════════════════════════════════

def _scope(query, model, mode=None, tournament_only=False):
    if mode:
        query = query.filter(model.mode == mode)
    if tournament_only and hasattr(model, "is_tournament"):
        query = query.filter(model.is_tournament.is_(True))
    return query


def per_six(total, balls):
    """A rate per six balls, or ``None`` when nothing was bowled.

    Every rate this module reports is per six balls rather than per over
    bowled, so a chase that ended in 17.2 and a Hundred's five-ball sets are
    both counted for what they actually were.
    """
    balls = int(balls or 0)
    if balls <= 0:
        return None
    return (total or 0) * 6.0 / balls


def overview(session, mode=None, tournament_only=False):
    """One summary row per surface that has been played on.

    Returns a list of dicts ordered by ``engine.pitch_registry`` (rankest
    bowling surface first), each with matches, rates, the average first-innings
    score and the two win percentages.
    """
    q = _scope(session.query(PitchMatchStat), PitchMatchStat, mode, tournament_only)
    rows = q.all()
    by_pitch = {}
    for r in rows:
        by_pitch.setdefault(r.pitch_type, []).append(r)

    out = []
    for pitch in pitch_registry.PITCHES:
        matches = by_pitch.get(pitch)
        if not matches:
            continue
        out.append(_summarise(pitch, matches))
    return out


def _summarise(pitch, matches):
    runs = sum((m.inn1_runs or 0) + (m.inn2_runs or 0) for m in matches)
    wkts = sum((m.inn1_wickets or 0) + (m.inn2_wickets or 0) for m in matches)
    balls = sum((m.inn1_balls or 0) + (m.inn2_balls or 0) for m in matches)
    first_scores = [m.inn1_runs or 0 for m in matches if (m.inn1_balls or 0) > 0]
    decided = [m for m in matches if m.result != RESULT_TIE]
    bat_first_wins = sum(1 for m in decided if m.result == RESULT_BAT_FIRST)

    band = pitch_registry.par_band(pitch)
    avg_first = (sum(first_scores) / len(first_scores)) if first_scores else None
    return {
        "pitch": pitch,
        "matches": len(matches),
        "innings": sum(1 for m in matches for b in ((m.inn1_balls or 0),
                                                    (m.inn2_balls or 0)) if b > 0),
        "runs": runs,
        "wickets": wkts,
        "balls": balls,
        "rpo": per_six(runs, balls),
        "wpo": per_six(wkts, balls),
        "avg_first": avg_first,
        "high_first": max(first_scores) if first_scores else None,
        "low_first": min(first_scores) if first_scores else None,
        "decided": len(decided),
        "ties": len(matches) - len(decided),
        "bat_first_win_pct": (bat_first_wins / len(decided) * 100) if decided else None,
        "bat_second_win_pct": ((len(decided) - bat_first_wins) / len(decided) * 100)
        if decided else None,
        "par_band": band,
        # Where the measured average sits against the surface's designed par
        # band: "under" / "in" / "over", or None when it has no band.
        "par_verdict": _par_verdict(avg_first, band),
    }


def _par_verdict(avg_first, band):
    if avg_first is None or not band:
        return None
    if avg_first < band[0]:
        return "under"
    if avg_first > band[1]:
        return "over"
    return "in"


def pitch_detail(session, pitch, mode=None, tournament_only=False):
    """Everything :func:`overview` has for one surface, plus phases and toss."""
    pitch = pitch_registry.normalise(pitch)
    q = _scope(session.query(PitchMatchStat).filter(PitchMatchStat.pitch_type == pitch),
               PitchMatchStat, mode, tournament_only)
    matches = q.all()
    if not matches:
        return None

    detail = _summarise(pitch, matches)
    detail["phases"] = {
        phase: {
            "runs": sum(getattr(m, f"{prefix}_runs") or 0 for m in matches),
            "wickets": sum(getattr(m, f"{prefix}_wickets") or 0 for m in matches),
            "balls": sum(getattr(m, f"{prefix}_balls") or 0 for m in matches),
        }
        for phase, prefix in _PHASE_COLS.items()
    }
    for row in detail["phases"].values():
        row["rpo"] = per_six(row["runs"], row["balls"])
        row["wpo"] = per_six(row["wickets"], row["balls"])

    chose_bat = [m for m in matches if m.toss_decision == "bat"]
    book = [m for m in matches if m.toss_by_the_book is True]
    against = [m for m in matches if m.toss_by_the_book is False]
    detail["toss"] = {
        "recorded": sum(1 for m in matches if m.toss_decision),
        "chose_bat": len(chose_bat),
        "chose_bowl": sum(1 for m in matches if m.toss_decision == "bowl"),
        "book_call": pitch_registry.toss_call(pitch),
        "followed": len(book),
        "against": len(against),
        "followed_win_pct": _toss_win_pct(book),
        "against_win_pct": _toss_win_pct(against),
        "toss_winner_win_pct": _toss_win_pct(matches),
    }
    return detail


def _toss_win_pct(matches):
    judged = [m for m in matches if m.toss_winner_won is not None]
    if not judged:
        return None
    return sum(1 for m in judged if m.toss_winner_won) / len(judged) * 100


def approach_table(session, pitch=None, mode=None, phase=None):
    """Approach effectiveness, as ``(batting rows, bowling rows, pairings)``.

    Batting rows are per batting intent, bowling rows per bowling plan, each
    with a per-six-ball run rate and wicket rate over every over that intent or
    plan was used. ``pairings`` is the same thing per (intent, plan) cell, which
    is the only view that can say a pick is good *against a specific reply*.
    """
    q = session.query(PitchApproachStat)
    if pitch:
        q = q.filter(PitchApproachStat.pitch_type == pitch_registry.normalise(pitch))
    if mode:
        q = q.filter(PitchApproachStat.mode == mode)
    if phase:
        q = q.filter(PitchApproachStat.phase == phase)
    rows = q.all()

    def _fold(key_fn):
        acc = {}
        for r in rows:
            cell = acc.setdefault(key_fn(r), [0, 0, 0, 0])
            cell[0] += r.overs or 0
            cell[1] += r.balls or 0
            cell[2] += r.runs or 0
            cell[3] += r.wickets or 0
        return acc

    def _render(acc, name_key):
        out = []
        for key, (overs, balls, runs, wkts) in acc.items():
            out.append({
                name_key: key, "overs": overs, "balls": balls,
                "runs": runs, "wickets": wkts,
                "rpo": per_six(runs, balls), "wpo": per_six(wkts, balls),
            })
        return out

    batting = _render(_fold(lambda r: r.bat_approach), "approach")
    bowling = _render(_fold(lambda r: r.bowl_approach), "approach")
    pairs = _render(_fold(lambda r: (r.bat_approach, r.bowl_approach)), "pair")
    batting.sort(key=lambda d: (d["rpo"] is None, -(d["rpo"] or 0)))
    bowling.sort(key=lambda d: (d["rpo"] is None, d["rpo"] or 0))
    pairs.sort(key=lambda d: (d["rpo"] is None, -(d["rpo"] or 0)))
    return batting, bowling, pairs


def totals(session, mode=None, tournament_only=False):
    """``(matches, balls)`` recorded in scope — the sample size, for headers."""
    q = _scope(session.query(func.count(PitchMatchStat.id),
                            func.sum(PitchMatchStat.inn1_balls
                                     + PitchMatchStat.inn2_balls)),
               PitchMatchStat, mode, tournament_only)
    count, balls = q.one()
    return int(count or 0), int(balls or 0)


def played_pitches(session, mode=None, tournament_only=False):
    """Surfaces that have at least one recorded match, in registry order."""
    q = _scope(session.query(PitchMatchStat.pitch_type).distinct(),
               PitchMatchStat, mode, tournament_only)
    seen = {row[0] for row in q.all()}
    return [p for p in pitch_registry.PITCHES if p in seen]
