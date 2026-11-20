"""Balance distinct opponents, home assignments, and rest windows."""
from collections import defaultdict
from datetime import date, timedelta
from itertools import islice


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


def _pack(ordered, duration, rest):
    free = defaultdict(int)
    dates = []
    for home, away in ordered:
        start = max(free[home], free[away])
        dates.append((start, home, away))
        free[home] = free[away] = start + duration + rest
    return sorted(dates)


def _slot_windows(count, minimum, maximum, rest, spare_days):
    """Vary round lengths within the range while keeping parallel series equal."""
    if count == 1:
        return [(0, minimum + min(maximum - minimum, spare_days))]
    width = maximum - minimum + 1
    windows = []
    offset = 0
    for slot in range(count):
        # Alternate from the ends of the range so short and long series both
        # appear even when there are fewer rounds than possible lengths.
        index = slot % width
        extra = index // 2 if index % 2 == 0 else width - 1 - index // 2
        extra = min(extra, spare_days)
        duration = minimum + extra
        windows.append((offset, duration))
        offset += duration + rest
        spare_days -= extra
    return windows


def plan(teams, opponents, duration, start, end, rest, *, max_duration=None):
    """Return (home, away, first day, match count) tuples."""
    count = len(teams)
    if not 1 <= opponents < count:
        raise ValueError("Each team needs between 1 and the number of teams minus 1 opponents.")
    max_duration = duration if max_duration is None else max_duration
    if not 3 <= duration <= max_duration:
        raise ValueError("Enter at least 3 matches per series and a maximum at or above the minimum.")
    if max_duration > date.max.toordinal():
        raise ValueError("The maximum series length exceeds the supported calendar.")
    if rest < 0 or end < start:
        raise ValueError("Use nonnegative rest days and an end date on or after the start.")
    pairs = _orient(count, _pairings(count, opponents))
    round_keys = (frozenset(edge) for edge in _round_robin(count))
    round_order = [pairs[edge] for edge in round_keys if edge in pairs]
    edge_order = list(pairs.values())
    schedules = [_pack(order, duration, rest)
                 for order in (round_order, round_order[::-1], edge_order, edge_order[::-1])]
    dates = min(schedules, key=lambda rows: rows[-1][0])
    required = dates[-1][0] + duration
    window_days = (end - start).days + 1
    if required > window_days:
        raise ValueError(f"These settings need {required} calendar days. Extend the date range or reduce the schedule.")
    unit = duration + rest
    windows = _slot_windows(dates[-1][0] // unit + 1, duration, max_duration, rest,
                            window_days - required)
    series = []
    for first_day, home, away in dates:
        offset, matches = windows[first_day // unit]
        series.append((teams[home], teams[away], start + timedelta(days=offset), matches))
    return series
