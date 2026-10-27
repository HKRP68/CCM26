"""Balanced partial second round robins, with a deterministic preview.

The repeat phase uses the capacity lower bound ceil(matches / floor(N / 2))
rounds. Start with that many circle-method matchings and balance team degrees
by exchanging two team labels along alternating components. Links sharing a
round or an opponent must be exchanged together, preserving both constraints.
Each exchange reduces a degree difference of at least two, so it terminates.
No graph solver, random retries or team-count-specific cases are needed.
"""

import hashlib
import json
import math
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def parse_match_target(raw):
    """An optional positive, finite per-team league target (blank = automatic)."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError("League match target must be a number.") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("League match target must be a positive finite number.")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("League match target must be a positive finite number.")
    return parsed


def _balance(rounds, team_ids):
    degrees = Counter(t for pairs in rounds for pair in pairs for t in pair)
    while True:
        high = max(team_ids, key=lambda t: degrees[t])
        low = min(team_ids, key=lambda t: degrees[t])
        if degrees[high] - degrees[low] <= 1:
            return

        # Each node is an incident fixture, with at most two links: another
        # fixture in its round, and another against the same opponent. Components
        # alternate high/low; an excess of high fixtures guarantees a path with
        # one more high node. Swapping that path transfers exactly one game.
        nodes, by_round, by_opponent = {}, {}, {}
        for rno, pairs in enumerate(rounds):
            for pno, (a, b) in enumerate(pairs):
                if {a, b} == {high, low}:
                    continue
                if a in (high, low) or b in (high, low):
                    side = a if a in (high, low) else b
                    opponent = b if side == a else a
                    key = (rno, pno)
                    nodes[key] = (side, opponent)
                    by_round.setdefault(rno, []).append(key)
                    by_opponent.setdefault(opponent, []).append(key)
        remaining = set(nodes)
        chosen = None
        for start in nodes:  # insertion order makes the preview reproducible
            if start not in remaining:
                continue
            stack, component, excess = [start], [], 0
            remaining.remove(start)
            while stack:
                key = stack.pop()
                component.append(key)
                side, opponent = nodes[key]
                excess += 1 if side == high else -1
                for other in by_round[key[0]] + by_opponent[opponent]:
                    if other in remaining:
                        remaining.remove(other)
                        stack.append(other)
            if excess == 1:
                chosen = component
                break
        if chosen is None:  # violated matching / simple-graph precondition
            raise ValueError("Could not balance repeat fixtures.")
        exchange = {high: low, low: high}
        for rno, pno in chosen:
            a, b = rounds[rno][pno]
            rounds[rno][pno] = (exchange.get(a, a), exchange.get(b, b))
        degrees[high] -= 1
        degrees[low] += 1


def validate_schedule(team_ids, rounds, expected_matches=None):
    """Validate both phases, opponent frequencies, round capacity and balance."""
    ids = list(team_ids)
    known = set(ids)
    if len(ids) < 2 or len(known) != len(ids) or None in known:
        raise ValueError("Select at least two distinct teams.")
    n = len(ids)
    first, second, games = Counter(), Counter(), Counter()
    phase_rounds = Counter()
    previous_phase = 1
    for rno, rnd in enumerate(rounds, 1):
        phase = rnd["phase"]
        if rnd["round_no"] != rno or phase not in (1, 2) or phase < previous_phase:
            raise ValueError("Invalid round or phase ordering.")
        previous_phase = phase
        phase_rounds[phase] += 1
        seen = set()
        if not rnd["pairs"]:
            raise ValueError("A scheduled round must contain a fixture.")
        for a, b in rnd["pairs"]:
            if a not in known or b not in known or a == b or a in seen or b in seen:
                raise ValueError("Invalid opponents or a team plays twice in a round.")
            seen.update((a, b))
            games.update((a, b))
            (first if phase == 1 else second)[frozenset((a, b))] += 1
        if set(rnd["byes"]) != known - seen or len(rnd["byes"]) != n - len(seen):
            raise ValueError("Round byes do not match the fixtures.")
    if (len(first) != n * (n - 1) // 2 or any(c != 1 for c in first.values())
            or phase_rounds[1] != (n if n % 2 else n - 1)):
        raise ValueError("Phase 1 must contain exactly one full round robin.")
    if any(c != 1 or pair not in first for pair, c in second.items()):
        raise ValueError("Each pair may meet at most twice in the league.")
    if max(games.values()) - min(games.values()) > 1:
        raise ValueError("Team match totals must differ by at most one.")
    repeat_count = sum(second.values())
    minimum_rounds = (repeat_count + n // 2 - 1) // (n // 2)
    if phase_rounds[2] != minimum_rounds:
        raise ValueError("Repeat fixtures use more rounds than necessary.")
    if expected_matches is not None and sum(first.values()) + repeat_count != expected_matches:
        raise ValueError("Fixture count does not match the league target.")


def build_schedule(team_ids, match_target=None):
    """Return a validated 1.5-RR plan, including fixtures, byes and team totals.

    The closest whole fixture total is chosen; exact half-fixture ties round up.
    An explicit target is the desired average league games per team, including
    phase 1, and must lie between a single and a double round robin.
    """
    from services.league_schedule_service import round_robin_rounds

    ids = list(team_ids)
    n = len(ids)
    if n < 2 or len(set(ids)) != n or None in ids:
        raise ValueError("Select at least two distinct teams.")
    configured = parse_match_target(match_target)
    target = Decimal(str(configured)) if configured is not None else Decimal(3 * (n - 1)) / 2
    if not n - 1 <= target <= 2 * (n - 1):
        raise ValueError("For %d teams, league match target must be between %d and %d games per team."
                         % (n, n - 1, 2 * (n - 1)))
    first = round_robin_rounds(ids)
    first_count = n * (n - 1) // 2
    total = int((target * n / 2).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    repeats = total - first_count
    remaining, second = repeats, []
    for pairs in first:
        if not remaining:
            break
        chosen = list(pairs[:remaining])
        second.append(chosen)
        remaining -= len(chosen)
    _balance(second, ids)
    # Repeated pairs reverse their original home/away, even after balancing.
    reverse = {frozenset((a, b)): (b, a) for pairs in first for a, b in pairs}
    second = [[reverse[frozenset(pair)] for pair in pairs] for pairs in second]
    rounds = []
    for phase, phase_rounds in ((1, first), (2, second)):
        for pairs in phase_rounds:
            playing = {t for pair in pairs for t in pair}
            rounds.append({"round_no": len(rounds) + 1, "phase": phase,
                           "pairs": pairs, "byes": [t for t in ids if t not in playing]})
    validate_schedule(ids, rounds, total)
    games = Counter(t for rnd in rounds for pair in rnd["pairs"] for t in pair)
    byes = Counter(t for rnd in rounds for t in rnd["byes"])
    team_rows = [{"team_id": t, "matches": games[t], "repeats": games[t] - (n - 1),
                  "byes": byes[t]} for t in ids]
    low, high = min(games.values()), max(games.values())
    exact = all(Decimal(games[t]) == target for t in ids)
    explanation = ("Every team plays exactly %s league games." % target if exact else
                   "Closest feasible balanced schedule to %s games per team: %s. "
                   "The total is rounded to the nearest whole fixture; half-fixture ties round up."
                   % (target, ", ".join("%d team%s %s %d" % (count, "" if count == 1 else "s",
                                                           "plays" if count == 1 else "play", count_games)
                                         for count_games, count in sorted(Counter(games.values()).items()))))
    fingerprint = hashlib.sha256(json.dumps(
        {"ids": ids, "target": configured, "rounds": rounds}, sort_keys=True).encode()).hexdigest()
    return {"rounds": rounds, "teams": team_rows, "team_count": n,
            "match_target": float(target), "matches": total,
            "phase1_matches": first_count, "phase2_matches": repeats,
            "phase1_rounds": len(first), "phase2_rounds": len(second),
            "round_count": len(rounds), "min_matches": low, "max_matches": high,
            "bye_count": sum(byes.values()), "exact": exact,
            "explanation": explanation, "fingerprint": fingerprint}
