"""Knockout / playoff bracket generation + advancement.

Phase 2 of the League Tournament Structure. After the league stage is decided the
admin picks a ``knockout_type`` and generates a bracket; this seeds knockout
``TournamentMatch`` rows (``status='scheduled'``, knockout ``stage``) from the
final standings and wires each match's winner/loser into the next via
``feeds_winner_to_id`` / ``feeds_loser_to_id``. As each knockout match is played
``advance_bracket`` drops the winner (and, where relevant, loser) into the
downstream match's empty slot.

Knockout matches are played through the very same fixture-gated path as league
fixtures (``find_open_fixture`` matches any uncompleted fixture for a pair,
regardless of stage), so no separate bot plumbing is needed — a knockout match
becomes playable as soon as both its slots are filled.

Supported types:
  * ``top4_sf``        – Top 4 (combined): SF1 (1v4), SF2 (2v3), Final.
  * ``ipl_playoffs``   – Top 4: Q1 (1v2), Eliminator (3v4), Q2 (LQ1 v WElim), Final.
  * ``groups_top2_sf`` – Top 2 of each group: cross semis → Final.
  * ``groups_top4_qf`` – Top 4 of each group: QFs → SFs → Final.
  * ``custom``         – bracket described by ``knockout_config_json`` (see below).
"""

import json
import logging

logger = logging.getLogger(__name__)

KNOCKOUT_STAGES = ("quarterfinal", "semifinal", "qualifier1", "eliminator",
                   "qualifier2", "final", "round_of_16", "round_of_32")


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _max_match_no(session, tid):
    from sqlalchemy import func
    from models import TournamentMatch
    return (session.query(func.max(TournamentMatch.match_no))
            .filter_by(tournament_id=tid).scalar()) or 0


def _seed_order(size):
    """Standard single-elimination bracket seeding positions for a power-of-two.

    Returns a list of seed indices (0-based) in bracket order so that, paired
    consecutively, the top seeds are kept apart as long as possible (1 plays the
    lowest, 2 the next-lowest, etc.).
    """
    order = [0]
    while len(order) < size:
        m = len(order) * 2
        nxt = []
        for x in order:
            nxt.append(x)
            nxt.append(m - 1 - x)
        order = nxt
    return order


def _stage_for(num_pairs):
    return {1: "final", 2: "semifinal", 4: "quarterfinal"}.get(
        num_pairs, "round_of_%d" % (num_pairs * 2))


def _clear_existing(session, tid):
    """Remove any non-completed knockout fixtures so regeneration is clean."""
    from models import TournamentMatch
    (session.query(TournamentMatch)
     .filter_by(tournament_id=tid)
     .filter(TournamentMatch.status != "completed")
     .filter(TournamentMatch.stage.in_(KNOCKOUT_STAGES))
     .delete(synchronize_session=False))


def _combined_seeds(session, tid):
    """Final combined standings as a list of TournamentTeam (best first)."""
    from services import tournament_service
    return tournament_service.points_table(session, tid)


def _group_qualifiers(session, tid, per_group):
    """Interleaved qualifier ids: rank-1 of every group, then rank-2, etc.

    Interleaving (A1, B1, A2, B2, …) combined with standard bracket seeding keeps
    teams from the same group apart until the final.
    """
    from services import tournament_service
    from models import TournamentGroup
    groups = (session.query(TournamentGroup).filter_by(tournament_id=tid)
              .order_by(TournamentGroup.sort_order, TournamentGroup.id).all())
    tables = [tournament_service.points_table(session, tid, group_id=g.id) for g in groups]
    seeds = []
    for rank in range(per_group):
        for tbl in tables:
            if rank < len(tbl):
                seeds.append(tbl[rank].id)
    return seeds, len(groups)


def _mk(session, tid, stage, round_no, match_no, *, t1=None, t2=None,
        label1=None, label2=None):
    from models import TournamentMatch
    tm = TournamentMatch(
        tournament_id=tid, stage=stage, status="scheduled",
        team1_id=t1, team2_id=t2, round_no=round_no, match_no=match_no,
        slot1_label=label1, slot2_label=label2)
    session.add(tm)
    session.flush()
    return tm


# ──────────────────────────────────────────────────────────────────────
# Single-elimination builder (covers top4_sf and both group brackets)
# ──────────────────────────────────────────────────────────────────────

def _build_single_elim(session, tid, seed_ids):
    """Build a seeded single-elimination bracket from an ordered id list.

    ``seed_ids`` is ordered best-first (or interleaved for groups). The field is
    padded to a power of two with byes; a bye advances its team to the next round
    without a match. Returns the list of created matches.
    """
    from models import TournamentMatch  # noqa: F401  (ensures models import side effects)
    seeds = [s for s in seed_ids if s]
    n = len(seeds)
    if n < 2:
        return []
    size = 1
    while size < n:
        size *= 2
    order = _seed_order(size)
    slot_team = [(seeds[idx] if idx < n else None) for idx in order]

    match_no = _max_match_no(session, tid)
    created = []
    round_no = 1

    # Round 1: a "slot" in the advancing list is either ('team', id) — a known
    # team, possibly via a bye — or ('match', tm) — winner of a created match.
    pairs = [(slot_team[i], slot_team[i + 1]) for i in range(0, size, 2)]
    st = _stage_for(len(pairs))
    cur = []
    for (a, b) in pairs:
        if a and b:
            match_no += 1
            tm = _mk(session, tid, st, round_no, match_no, t1=a, t2=b)
            created.append(tm)
            cur.append(("match", tm))
        elif a or b:
            cur.append(("team", a or b))   # bye → advances
        else:
            cur.append(None)

    # Subsequent rounds.
    while len([c for c in cur if c is not None]) > 1:
        round_no += 1
        nxt = []
        npairs = len(cur) // 2
        st = _stage_for(npairs)
        for i in range(0, len(cur), 2):
            left, right = cur[i], cur[i + 1]
            if left is None and right is None:
                nxt.append(None)
                continue
            if left is None or right is None:
                # One side is empty — the other advances untouched (bye).
                nxt.append(left or right)
                continue
            match_no += 1
            t1 = left[1] if left[0] == "team" else None
            t2 = right[1] if right[0] == "team" else None
            lbl1 = None if t1 else "Winner of M%d" % left[1].match_no
            lbl2 = None if t2 else "Winner of M%d" % right[1].match_no
            tm = _mk(session, tid, st, round_no, match_no,
                     t1=t1, t2=t2, label1=lbl1, label2=lbl2)
            if left[0] == "match":
                left[1].feeds_winner_to_id = tm.id
            if right[0] == "match":
                right[1].feeds_winner_to_id = tm.id
            created.append(tm)
            nxt.append(("match", tm))
        cur = nxt
    return created


def _build_ipl_playoffs(session, tid, seeds):
    """IPL-style playoffs from the top 4: Q1, Eliminator, Q2, Final."""
    if len(seeds) < 4:
        raise ValueError("IPL playoffs need at least 4 teams in the standings.")
    s1, s2, s3, s4 = seeds[0].id, seeds[1].id, seeds[2].id, seeds[3].id
    mn = _max_match_no(session, tid)
    q1 = _mk(session, tid, "qualifier1", 1, mn + 1, t1=s1, t2=s2)
    elim = _mk(session, tid, "eliminator", 1, mn + 2, t1=s3, t2=s4)
    q2 = _mk(session, tid, "qualifier2", 2, mn + 3,
             label1="Loser Qualifier 1", label2="Winner Eliminator")
    final = _mk(session, tid, "final", 3, mn + 4,
                label1="Winner Qualifier 1", label2="Winner Qualifier 2")
    q1.feeds_winner_to_id = final.id
    q1.feeds_loser_to_id = q2.id
    elim.feeds_winner_to_id = q2.id
    q2.feeds_winner_to_id = final.id
    return [q1, elim, q2, final]


# ──────────────────────────────────────────────────────────────────────
# Custom bracket from JSON
# ──────────────────────────────────────────────────────────────────────

def _build_custom(session, tid, config_json):
    """Build a bracket from a JSON spec. Schema::

        {
          "matches": [
            {"key":"sf1","stage":"semifinal","slot1":{"seed":1},"slot2":{"seed":4}},
            {"key":"sf2","stage":"semifinal","slot1":{"seed":2},"slot2":{"seed":3}},
            {"key":"final","stage":"final",
             "slot1":{"winner_of":"sf1"},"slot2":{"winner_of":"sf2"}}
          ]
        }

    A slot is one of: ``{"seed": N}`` (1-based combined standings),
    ``{"group":"Group A","rank":1}``, ``{"winner_of":"key"}`` or
    ``{"loser_of":"key"}``.
    """
    from services import tournament_service
    from models import TournamentGroup
    cfg = json.loads(config_json or "{}")
    specs = cfg.get("matches") or []
    if not specs:
        raise ValueError("Custom knockout config has no 'matches'.")

    combined = tournament_service.points_table(session, tid)
    groups = {g.name: g for g in
              session.query(TournamentGroup).filter_by(tournament_id=tid).all()}
    group_tables = {name: tournament_service.points_table(session, tid, group_id=g.id)
                    for name, g in groups.items()}

    def resolve_team(ref):
        """Return a team id for direct seed refs, or None for winner/loser refs."""
        if not isinstance(ref, dict):
            return None, None
        if "seed" in ref:
            i = int(ref["seed"]) - 1
            tid_ = combined[i].id if 0 <= i < len(combined) else None
            return tid_, ("#%d" % int(ref["seed"]))
        if "group" in ref and "rank" in ref:
            tbl = group_tables.get(ref["group"]) or []
            i = int(ref["rank"]) - 1
            tid_ = tbl[i].id if 0 <= i < len(tbl) else None
            return tid_, ("%s #%d" % (ref["group"], int(ref["rank"])))
        if "winner_of" in ref:
            return None, ("Winner %s" % ref["winner_of"])
        if "loser_of" in ref:
            return None, ("Loser %s" % ref["loser_of"])
        return None, None

    mn = _max_match_no(session, tid)
    created = {}
    for n, spec in enumerate(specs, start=1):
        t1, l1 = resolve_team(spec.get("slot1"))
        t2, l2 = resolve_team(spec.get("slot2"))
        stage = (spec.get("stage") or "knockout")[:30]
        tm = _mk(session, tid, stage, int(spec.get("round", n)), mn + n,
                 t1=t1, t2=t2, label1=l1, label2=l2)
        created[spec.get("key") or ("m%d" % n)] = (tm, spec)

    # Wire winner/loser feeds in a second pass (all matches now exist).
    for tm, spec in created.values():
        for slot in ("slot1", "slot2"):
            ref = spec.get(slot) or {}
            if "winner_of" in ref and ref["winner_of"] in created:
                created[ref["winner_of"]][0].feeds_winner_to_id = tm.id
            if "loser_of" in ref and ref["loser_of"] in created:
                created[ref["loser_of"]][0].feeds_loser_to_id = tm.id
    return [tm for tm, _ in created.values()]


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def generate_knockout(session, tournament_id):
    """Generate the knockout bracket for ``tournament_id``. Caller commits.

    Returns the number of knockout matches created. Raises ``ValueError`` with a
    human message on bad config (no type, too few teams, etc.).
    """
    from models import Tournament
    tid = int(tournament_id)
    tour = session.query(Tournament).get(tid)
    if not tour:
        return 0
    ktype = (tour.knockout_type or "").strip()
    if not ktype:
        raise ValueError("Pick a knockout type first.")

    _clear_existing(session, tid)

    if ktype == "top4_sf":
        seeds = _combined_seeds(session, tid)
        if len(seeds) < 4:
            raise ValueError("Top-4 semifinals need at least 4 teams.")
        created = _build_single_elim(session, tid, [s.id for s in seeds[:4]])
    elif ktype == "ipl_playoffs":
        created = _build_ipl_playoffs(session, tid, _combined_seeds(session, tid))
    elif ktype == "groups_top2_sf":
        seeds, ngroups = _group_qualifiers(session, tid, 2)
        if ngroups < 2 or len(seeds) < 4:
            raise ValueError("Need at least 2 groups with 2 teams each.")
        created = _build_single_elim(session, tid, seeds)
    elif ktype == "groups_top4_qf":
        seeds, ngroups = _group_qualifiers(session, tid, 4)
        if ngroups < 2 or len(seeds) < 4:
            raise ValueError("Need at least 2 groups with enough teams for top-4.")
        created = _build_single_elim(session, tid, seeds)
    elif ktype == "custom":
        created = _build_custom(session, tid, tour.knockout_config_json)
    else:
        raise ValueError("Unknown knockout type: %s" % ktype)

    tour.knockout_generated = True
    session.flush()
    logger.info("Generated %s knockout matches for tournament %s (%s)",
                len(created), tid, ktype)
    return len(created)


def advance_bracket(session, completed_match):
    """Propagate a completed knockout match's winner/loser downstream.

    Fills the first empty slot of each downstream match. Safe to call for any
    completed match — it no-ops when there are no feed links or no decided winner.
    Caller commits.
    """
    from models import TournamentMatch
    m = completed_match
    if m is None or m.stage not in KNOCKOUT_STAGES:
        return
    winner_id = m.winner_team_id
    if not winner_id:
        return  # tie / undecided — leave the bracket for manual resolution
    loser_id = None
    if m.team1_id and m.team2_id:
        loser_id = m.team2_id if winner_id == m.team1_id else m.team1_id

    def _place(target_match_id, team_id):
        if not target_match_id or not team_id:
            return
        nxt = session.query(TournamentMatch).get(target_match_id)
        if not nxt or nxt.status == "completed":
            return
        if nxt.team1_id is None:
            nxt.team1_id = team_id
        elif nxt.team2_id is None and nxt.team1_id != team_id:
            nxt.team2_id = team_id

    _place(m.feeds_winner_to_id, winner_id)
    _place(m.feeds_loser_to_id, loser_id)
    session.flush()
