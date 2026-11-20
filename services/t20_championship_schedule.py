"""Balance distinct opponents, home assignments, and rest windows."""
from collections import defaultdict
from datetime import date, timedelta
from itertools import islice
import random

MAX_TEAM_MATCH_GAP = 5


def _pairings(count, opponents):
    if count % 2 == 0:
        # Each even-sized round is a perfect matching. Taking whole rounds
        # meets the theoretical minimum of one time slot per opponent.
        return list(islice(_round_robin(count), count * opponents // 2))
    edges = [(i, (i + step) % count)
             for step in range(1, opponents // 2 + 1) for i in range(count)]
    if opponents % 2:
        # This step traverses every vertex on one cycle. Match consecutive
        # vertices on that cycle, giving exactly one team one fewer series.
        cycle = [i * (count // 2) % count for i in range(count)]
        edges.extend(zip(cycle[::2], cycle[1::2]))
    return edges


def _orient(count, edges):
    """Orient Euler trails so each team's home/away counts differ by at most one."""
    adjacency = [[] for _ in range(count + 1)]

    def connect(a, b, edge):
        adjacency[a].append((b, edge))
        adjacency[b].append((a, edge))

    for edge, (a, b) in enumerate(edges):
        connect(a, b, edge)
    for a in range(count):
        if len(adjacency[a]) % 2:
            connect(a, count, len(edges) + a)

    used = set()
    directed = {}
    for root in range(count + 1):
        stack = [root]
        while stack:
            a = stack[-1]
            if not adjacency[a]:
                stack.pop()
                continue
            b, edge = adjacency[a].pop()
            if edge in used:
                continue
            used.add(edge)
            stack.append(b)
            if edge < len(edges):
                directed[frozenset((a, b))] = (a, b)
    return directed


def _round_robin(count):
    ring = list(range(count + count % 2))
    for _ in range(len(ring) - 1):
        for a, b in zip(ring[:len(ring)//2], reversed(ring[len(ring)//2:])):
            if a < count and b < count:
                yield a, b
        ring = [ring[0], ring[-1], *ring[1:-1]]


def _pack(ordered, duration, rest, ready_days=None):
    free = defaultdict(int, ready_days or {})
    dates = []
    for home, away in ordered:
        start = max(free[home], free[away])
        dates.append((start, home, away))
        free[home] = free[away] = start + duration + rest
    return sorted(dates)


def _series_lengths(pairs, slots, totals, minimum, maximum, spare_days, rng):
    lengths = [minimum] * len(pairs)
    slot_lengths = [minimum] * (max(slots) + 1)
    totals = list(totals)
    for home, away in pairs:
        totals[home] += minimum
        totals[away] += minimum
    order = list(range(len(pairs)))
    rng.shuffle(order)

    def capacity(i):
        return min(maximum, slot_lengths[slots[i]] + spare_days)

    def extend(i, length):
        nonlocal spare_days
        extra = length - lengths[i]
        home, away = pairs[i]
        totals[home] += extra
        totals[away] += extra
        lengths[i] = length
        slot = slots[i]
        if length > slot_lengths[slot]:
            spare_days -= length - slot_lengths[slot]
            slot_lengths[slot] = length

    # With odd teams and odd opponent counts, one team can start a series
    # short. Give its tours first access to extra days if the gap is too wide.
    while max(totals) - min(totals) > MAX_TEAM_MATCH_GAP:
        team = totals.index(min(totals))
        eligible = [i for i in order if team in pairs[i] and lengths[i] < capacity(i)]
        if not eligible:
            raise ValueError("The settings put team match totals more than five apart. "
                             "Allow more dates or change the opponents or series lengths.")
        i = min(eligible, key=lambda index: lengths[index])
        extend(i, lengths[i] + 1)

    def balanced_capacity(i):
        home, away = pairs[i]
        other_min = min((total for j, total in enumerate(totals) if j not in (home, away)),
                        default=None)
        ceiling = capacity(i)
        if other_min is not None:
            ceiling = min(ceiling, other_min + MAX_TEAM_MATCH_GAP - max(totals[home], totals[away])
                          + lengths[i])
        return ceiling

    # Keep one short series when possible and randomly extend the others.
    randomizable = order[:-1] if len(order) > 1 else order
    for i in randomizable:
        extend(i, rng.randint(lengths[i], balanced_capacity(i)))
    if len(set(lengths)) == 1:
        for i in order:
            if balanced_capacity(i) > lengths[i]:
                extend(i, lengths[i] + 1)
                break
    return lengths, slot_lengths


def _validate_window(duration, max_duration, start, end, rest):
    max_duration = duration if max_duration is None else max_duration
    if not 3 <= duration <= max_duration:
        raise ValueError("Enter at least 3 matches per series and a maximum at or above the minimum.")
    if max_duration > date.max.toordinal():
        raise ValueError("The maximum series length exceeds the supported calendar.")
    if rest < 0 or end < start:
        raise ValueError("Use nonnegative rest days and an end date on or after the start.")
    return max_duration


def _dated_series(teams, dates, totals, duration, maximum, start, end, rng):
    required = dates[-1][0] + duration
    window_days = (end - start).days + 1
    if required > window_days:
        raise ValueError(f"These settings need {required} calendar days. Extend the date range or reduce the schedule.")
    starts = sorted({offset for offset, _, _ in dates})
    index = {offset: slot for slot, offset in enumerate(starts)}
    slots = [index[offset] for offset, _, _ in dates]
    ordered_pairs = [(home, away) for _, home, away in dates]
    lengths, slot_lengths = _series_lengths(ordered_pairs, slots, totals, duration, maximum,
                                          window_days - required, rng or random)
    offsets, extra_days = [], 0
    for original_start, slot_length in zip(starts, slot_lengths):
        offsets.append(original_start + extra_days)
        extra_days += slot_length - duration
    return [(teams[home], teams[away], start + timedelta(days=offsets[slot]), matches)
            for (home, away), slot, matches in zip(ordered_pairs, slots, lengths)]


def plan(teams, opponents, duration, start, end, rest, *, max_duration=None, rng=None):
    """Return (home, away, first day, match count) tuples."""
    count = len(teams)
    if not 1 <= opponents < count:
        raise ValueError("Each team needs between 1 and the number of teams minus 1 opponents.")
    maximum = _validate_window(duration, max_duration, start, end, rest)
    pairs = _orient(count, _pairings(count, opponents))
    round_keys = (frozenset(edge) for edge in _round_robin(count))
    round_order = [pairs[edge] for edge in round_keys if edge in pairs]
    edge_order = list(pairs.values())
    schedules = [_pack(order, duration, rest)
                 for order in (round_order, round_order[::-1], edge_order, edge_order[::-1])]
    dates = min(schedules, key=lambda rows: rows[-1][0])
    return _dated_series(teams, dates, [0] * count, duration, maximum, start, end, rng)


def plan_remaining(teams, matchups, totals, ready_on, duration, maximum, start, end, rest):
    """Fit the remaining pairings around each team's retained tours."""
    maximum = _validate_window(duration, maximum, start, end, rest)
    indices = {team: i for i, team in enumerate(teams)}
    pairs = [(indices[home], indices[away]) for home, away in matchups]
    ready_days = {indices[team]: max(0, (day - start).days) for team, day in ready_on.items()}
    schedules = [_pack(order, duration, rest, ready_days) for order in (pairs, pairs[::-1])]
    dates = min(schedules, key=lambda rows: rows[-1][0])
    return _dated_series(teams, dates, [totals[team] for team in teams], duration, maximum, start, end, None)
