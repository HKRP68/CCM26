"""Double elimination fixtures expressed in canonical winner/loser references."""

FINAL = "de-final"
RESET = "de-reset"


def double_elimination(entries, *, reset):
    """Return rounds of (key, home, away), skipping all bye fixtures.

    The full upper/lower wiring retains empty slots through each round so an
    unplayed bye never creates a defeat or a phantom fixture.
    """
    size = 1 << (len(entries) - 1).bit_length()
    seeds = [0]
    while len(seeds) < size:
        seeds = [seed for old in seeds for seed in (old, 2 * len(seeds) - old - 1)]
    upper = [entries[index] if index < len(entries) else None for index in seeds]
    rounds = []

    def pair(pairs, label):
        winners, losers, games = [], [], []
        for i, (home, away) in enumerate(pairs, 1):
            if home is None or away is None:
                winners.append(home or away)
                losers.append(None)
                continue
            key = f"de-{label}-{i}"
            games.append((key, home, away))
            winners.append(f"winner:{key}")
            losers.append(f"loser:{key}")
        if games:
            rounds.append(games)
        return winners, losers

    def adjacent(slots):
        return zip(slots[::2], slots[1::2])

    upper, losses = pair(adjacent(upper), "u1")
    if size == 2:
        lower = losses
    else:
        lower, _ = pair(adjacent(losses), "l1")
        step = 2
        while len(upper) > 1:
            upper, losses = pair(adjacent(upper), f"u{step}")
            lower, _ = pair(zip(lower, reversed(losses)), f"l{step}a")
            if len(upper) > 1:
                lower, _ = pair(adjacent(lower), f"l{step}b")
            step += 1
    rounds.append([(FINAL, upper[0], lower[0])])
    if reset:
        rounds.append([(RESET, f"loser:{FINAL}", f"winner:{FINAL}")])
    return rounds


def is_double(config):
    return config["schedule"]["preset"] == "double_elimination"


def effective_final(config, rows):
    """Return the actual championship fixture, ignoring the unused reset slot."""
    playoffs = [item for item in config["fixtures"] if item["stage"] == "playoff" and item["key"] != "third-place"]
    if not playoffs:
        return None
    if is_double(config):
        reset = next(((fixture, result) for fixture, result in rows if fixture.fixture_key == RESET), None)
        return RESET if reset and (reset[1] is None or reset[1].kind != "cancelled") else FINAL
    return max(playoffs, key=lambda item: item["round"])["key"]
