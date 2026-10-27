"""Two equal groups: group opponents once, other-group opponents home and away.

For N=2m teams this gives 3m-1 games each in exactly 3m-1 full rounds.
Odd-sized groups share their bye round with a cross-group fixture, so there
are no league byes for any even team count of at least four.
"""

import hashlib
import json
from collections import Counter

from services.partial_round_robin import validate_schedule


def seeded_groups(team_ids):
    """Snake draw from admin seed order, splitting each adjacent seed pair."""
    ids = list(team_ids)
    if len(ids) < 4 or len(ids) % 2 or len(set(ids)) != len(ids) or None in ids:
        raise ValueError("Two-group scheduling needs an even number of at least 4 distinct teams.")
    groups = [[], []]
    for pot in range(len(ids) // 2):
        first = pot % 2
        groups[first].append(ids[2 * pot])
        groups[1 - first].append(ids[2 * pot + 1])
    return groups


def _streaks(values):
    runs = []
    previous = None
    for value in values:
        if value == previous:
            runs[-1] += 1
        else:
            runs.append(1)
            previous = value
    return runs


def _improve_home_runs(rounds, ids):
    """Deterministic local improvements; flip both legs to preserve home totals."""
    homes = {t: [] for t in ids}
    meetings = {}
    for rno, pairs in enumerate(rounds):
        for pno, (a, b) in enumerate(pairs):
            homes[a].append(1)
            homes[b].append(0)
            meetings.setdefault(frozenset((a, b)), []).append((rno, pno))

    def cost(team):
        return sum(length ** 3 for length in _streaks(homes[team]))

    def flip(locations):
        for rno, pno in locations:
            a, b = rounds[rno][pno]
            rounds[rno][pno] = b, a
            homes[a][rno] = 1 - homes[a][rno]
            homes[b][rno] = 1 - homes[b][rno]

    for _ in range(4):  # bounded optimisation; the valid construction needs no search
        changed = False
        for pair, locations in meetings.items():
            if len(locations) != 2:
                continue
            before = sum(cost(t) for t in pair)
            flip(locations)
            if sum(cost(t) for t in pair) < before:
                changed = True
            else:
                flip(locations)
        if not changed:
            break
    return homes


def validate_group_schedule(group_a, group_b, rounds):
    """Validate the full schedule and the group-specific fairness guarantees."""
    a, b = list(group_a), list(group_b)
    ids, m = a + b, len(a)
    if m < 2 or len(b) != m or len(set(ids)) != len(ids) or None in ids:
        raise ValueError("Assign every team to exactly two equal groups of at least 2 teams.")
    games = 3 * m - 1
    validate_schedule(ids, rounds, len(ids) * games // 2)
    membership = {t: i for i, group in enumerate((a, b)) for t in group}
    meetings, home = {}, Counter()
    for rnd in rounds:
        if rnd["byes"] or len(rnd["pairs"]) != m:
            raise ValueError("Every team must play exactly once in every two-group round.")
        for first, second in rnd["pairs"]:
            home[first] += 1
            meetings.setdefault(frozenset((first, second)), []).append((rnd["round_no"], first))
    for pair, legs in meetings.items():
        first, second = tuple(pair)
        repeats = membership[first] != membership[second]
        if len(legs) != (2 if repeats else 1):
            raise ValueError("Group opponents must meet once and cross-group opponents twice.")
        if repeats and (legs[0][1] == legs[1][1] or legs[1][0] - legs[0][0] < m):
            raise ValueError("Repeat meetings must reverse home side and be spaced apart.")
    if any(abs(2 * home[t] - games) > 1 for t in ids):
        raise ValueError("Every team needs a balanced home and away schedule.")


def build_schedule(group_a, group_b):
    """Build an optimal-round, balanced-home schedule from two saved groups."""
    from services.league_schedule_service import round_robin_rounds

    a, b = list(group_a), list(group_b)
    ids, m = a + b, len(a)
    if m < 2 or len(b) != m or len(set(ids)) != len(ids) or None in ids:
        raise ValueError("Assign every team to exactly two equal groups of at least 2 teams.")
    indices = {t: i for group in (a, b) for i, t in enumerate(group)}

    def intra_home(x, y):
        distance = (indices[y] - indices[x]) % m
        return (x, y) if 2 * distance < m or (2 * distance == m and indices[x] < indices[y]) else (y, x)

    intra = []
    for left, right in zip(round_robin_rounds(a), round_robin_rounds(b)):
        pairs = [intra_home(x, y) for x, y in left + right]
        if m % 2:
            playing = {t for pair in pairs for t in pair}
            pairs.append((next(t for t in a if t not in playing),
                          next(t for t in b if t not in playing)))
        intra.append(pairs)
    cross = [[(a[i], b[(i + shift) % m]) for i in range(m)] for shift in range(m)]
    # For odd groups, the diagonal cross matching already fills their byes.
    cross_first = cross[1:] if m % 2 else cross
    first = []
    blocks = (intra, cross_first) if m % 2 else (cross_first, intra)
    for i in range(max(map(len, blocks))):
        for block in blocks:
            if i < len(block):
                first.append(list(block[i]))
    in_a = set(a)
    first = [[((x, y) if (indices[x] + indices[y]) % 2 == 0 else (y, x))
              if (x in in_a) != (y in in_a) else (x, y) for x, y in pairs] for pairs in first]
    original = {frozenset((x, y)): (rno, x, y)
                for rno, pairs in enumerate(first, 1) for x, y in pairs}
    # Oldest complete matching returns first; every rematch is >= m rounds later.
    cross.sort(key=lambda pairs: max(original[frozenset(pair)][0] for pair in pairs))
    second = [[tuple(reversed(original[frozenset(pair)][1:])) for pair in pairs] for pairs in cross]
    all_rounds = first + second
    homes = _improve_home_runs(all_rounds, ids)
    rounds = [{"round_no": i + 1, "phase": 1 if i < len(first) else 2,
               "pairs": pairs, "byes": []} for i, pairs in enumerate(all_rounds)]
    validate_group_schedule(a, b, rounds)
    games = 3 * m - 1
    team_rows = [{"team_id": t, "matches": games, "repeats": m, "byes": 0,
                  "home": sum(homes[t]), "away": games - sum(homes[t]),
                  "longest_streak": max(_streaks(homes[t]))} for t in ids]
    gaps = {}
    for rnd in rounds:
        for pair in rnd["pairs"]:
            gaps.setdefault(frozenset(pair), []).append(rnd["round_no"])
    gap = min(v[1] - v[0] for v in gaps.values() if len(v) == 2)
    fingerprint = hashlib.sha256(json.dumps({"format": "cross_group_rr", "groups": [a, b],
                                            "rounds": rounds}, sort_keys=True).encode()).hexdigest()
    return {"format_label": "Two-Group 1.5 RR", "rounds": rounds, "teams": team_rows,
            "team_count": len(ids), "match_target": games, "matches": len(ids) * games // 2,
            "phase1_matches": len(ids) * (len(ids) - 1) // 2, "phase2_matches": m * m,
            "phase1_rounds": len(first), "phase2_rounds": len(second),
            "round_count": games, "min_matches": games, "max_matches": games,
            "bye_count": 0, "exact": True, "fingerprint": fingerprint,
            "minimum_repeat_gap": gap, "longest_streak": max(row["longest_streak"] for row in team_rows),
            "explanation": ("Every team plays %d league games: %d against its own group and %d "
                            "against the other group. One combined points table; no byes."
                            % (games, m - 1, 2 * m))}
