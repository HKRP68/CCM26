"""Pure event fixture generation and validation."""

from datetime import datetime, timedelta, timezone
from random import Random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_FIXTURES = 240


def utc_instant(value, zone):
    """Convert a local or offset timestamp to the database's naive UTC."""
    try:
        stamp = datetime.fromisoformat(value)
        tz = ZoneInfo(zone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Enter an ISO timestamp and a valid IANA timezone.") from exc
    if stamp.tzinfo is None:
        earlier = stamp.replace(tzinfo=tz, fold=0)
        later = stamp.replace(tzinfo=tz, fold=1)
        if earlier.utcoffset() != later.utcoffset():
            raise ValueError("Use an explicit UTC offset for an ambiguous or skipped local time.")
        stamp = earlier
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


def utc_text(stamp):
    return stamp.replace(tzinfo=timezone.utc).isoformat()


def round_robin(keys, legs):
    ring = list(keys)
    if len(ring) % 2:
        ring.append(None)
    rounds = []
    for leg in range(legs):
        for turn in range(len(ring) - 1):
            pairings = []
            for idx in range(len(ring) // 2):
                first, second = ring[idx], ring[-idx - 1]
                if first is not None and second is not None:
                    pairings.append((second, first) if (leg + turn + idx) % 2 else (first, second))
            rounds.append(pairings)
            ring = ring[:1] + ring[-1:] + ring[1:-1]
    return rounds


def _append_round(rows, games, stage, playoff_keys=None):
    round_number = rows[-1]["round"] + 1 if rows else 1
    for idx, (home, away) in enumerate(games):
        key = playoff_keys[idx] if playoff_keys else f"r{round_number}-{idx + 1}"
        rows.append(dict(key=key, home=home, away=away, round=round_number,
                         stage=stage, source="generated"))


def _knockout(rows, entries, third_place):
    size = 1 << (len(entries) - 1).bit_length()
    seeds = [0]
    order_size = 1
    while order_size < size:
        seeds = [seed for old in seeds for seed in (old, 2 * order_size - old - 1)]
        order_size *= 2
    places = [entries[idx] if idx < len(entries) else None for idx in seeds]
    stage = 0
    while len(places) > 1:
        games, keys, following = [], [], []
        for idx in range(0, len(places), 2):
            home, away = places[idx:idx + 2]
            if home is None or away is None:
                following.append(home or away)
                continue
            key = f"ko{stage}-{idx // 2 + 1}"
            games.append((home, away))
            keys.append(key)
            following.append(f"winner:{key}")
        if len(places) == 2 and third_place and len(entries) >= 4:
            games.append(tuple(ref.replace("winner:", "loser:") for ref in places))
            keys.append("third-place")
        _append_round(rows, games, "playoff", keys)
        places = following
        stage += 1


def generate(config):
    if config["kind"] != "live":
        return []
    keys = [team["key"] for team in config["teams"]]
    if len(keys) < 2:
        raise ValueError("Add at least two teams before generating fixtures.")
    settings = config["schedule"]
    if settings["seed"] is not None:
        Random(settings["seed"]).shuffle(keys)
    preset = settings.get("preset", "round_robin")
    rows = []
    if preset == "round_robin":
        for games in round_robin(keys, settings["legs"]):
            _append_round(rows, games, "league")
        playoffs = settings.get("playoffs")
        if playoffs == "top2":
            _append_round(rows, [("rank:1", "rank:2")], "playoff", ["final"])
        elif playoffs == "top4":
            if len(keys) < 4:
                raise ValueError("Top-four playoffs need four teams.")
            _append_round(rows, [("rank:1", "rank:2"), ("rank:3", "rank:4")],
                          "playoff", ["qualifier", "eliminator"])
            _append_round(rows, [("loser:qualifier", "winner:eliminator")],
                          "playoff", ["qualifier-2"])
            _append_round(rows, [("winner:qualifier", "winner:qualifier-2")],
                          "playoff", ["final"])
    elif preset == "series":
        if len(keys) != 2:
            raise ValueError("A bilateral series requires two teams.")
        for index in range(settings["games"]):
            pair = tuple(keys[::1 if index % 2 == 0 else -1])
            _append_round(rows, [pair], "league")
    elif preset == "groups":
        groups = {key: settings["groups"][key] for key in sorted(settings["groups"])}
        qualifiers = settings["qualify_per_group"]
        allocated = [team for membership in groups.values() for team in membership]
        if (len(groups) < 2 or sorted(allocated) != sorted(keys)
                or any(len(membership) < max(2, qualifiers) for membership in groups.values())):
            raise ValueError("Each team must appear in one group and each group needs enough teams to qualify.")
        rounds = [round_robin(group, settings["legs"]) for group in groups.values()]
        for idx in range(max(map(len, rounds))):
            _append_round(rows, [pair for group in rounds if idx < len(group) for pair in group[idx]], "league")
        entries = [f"group_rank:{group}:{rank}" for rank in range(1, qualifiers + 1) for group in groups]
        _knockout(rows, entries, settings.get("third_place", False))
    elif preset == "survival":
        challenger = config["teams"][0]["key"]
        for opponent in keys:
            if opponent != challenger:
                _append_round(rows, [(challenger, opponent)], "survival")
    elif preset == "knockout":
        _knockout(rows, keys, settings.get("third_place", False))
    else:
        raise ValueError("Choose a supported schedule preset or enter fixtures manually.")
    if len(rows) > MAX_FIXTURES:
        raise ValueError(f"No more than {MAX_FIXTURES} fixtures are allowed.")

    zone = config["timezone"]
    start = utc_instant(config["opens_at"], zone)
    calendar = start.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    close = utc_instant(config["closes_at"], zone)
    current_round = 0
    round_usage = 0
    cap = settings.get("fixtures_per_day", 24)
    for row in rows:
        if current_round != row["round"] or round_usage >= cap:
            if current_round:
                calendar += timedelta(days=settings["round_spacing_days"])
            while (calendar.isoweekday() not in settings["playing_days"]
                   or calendar.date().isoformat() in settings["blackouts"]):
                calendar += timedelta(days=1)
                local_close = close.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).date()
                if calendar.date() > local_close:
                    raise ValueError("The playing days and blackouts leave insufficient time.")
            current_round, round_usage = row["round"], 0
        opens = utc_instant(calendar.isoformat(), zone)
        deadline = opens + timedelta(hours=settings["window_hours"])
        row.update(opens_at=utc_text(opens), deadline=utc_text(deadline), grace=settings["grace"])
        round_usage += 1
    validate(config, rows)
    return rows


def validate(config, rows):
    if config["kind"] != "live":
        if rows:
            raise ValueError("Standalone Moments and Boss Battles have no tournament fixtures.")
        return
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FIXTURES:
        raise ValueError(f"Enter 1 to {MAX_FIXTURES} fixtures.")
    zone = config["timezone"]
    opens = utc_instant(config["opens_at"], zone)
    close = utc_instant(config["closes_at"], zone)
    keys = {team["key"] for team in config["teams"]}
    ladder = config["schedule"]["preset"] == "survival"
    if ladder:
        challenger = config["teams"][0]["key"]
        if (len(rows) != len(keys) - 1 or any(row["stage"] != "survival" or row["home"] != challenger
                                             for row in rows)
                or {row["away"] for row in rows} != keys - {challenger}):
            raise ValueError("A survival ladder must play each opponent once from the first team slot.")
    elif any(row["stage"] == "survival" for row in rows):
        raise ValueError("Survival rounds require the survival schedule preset.")
    seen, occupied, league = {}, {}, []
    for row in sorted(rows, key=lambda item: (item["round"], item["key"])):
        key, round_number = row["key"], row["round"]
        if key in seen or row["home"] == row["away"]:
            raise ValueError(f"{key}: duplicate fixture or both slots use the same team.")
        start = utc_instant(row["opens_at"], zone)
        end = utc_instant(row["deadline"], zone) + timedelta(minutes=row["grace"])
        if not opens <= start < utc_instant(row["deadline"], zone) or end > close:
            raise ValueError(f"{key}: release, deadline and grace must fit the event window.")
        for team in (row["home"], row["away"]):
            if team in keys:
                intervals = occupied.setdefault(team, [])
                if any(round_number == rnd or (start < to and end > since)
                       for rnd, since, to in intervals):
                    raise ValueError(f"{key}: {team} has an overlapping fixture or round.")
                intervals.append((round_number, start, end))
                continue
            if ":" not in team or row["stage"] != "playoff":
                raise ValueError(f"{key}: unknown team slot {team}.")
            kind, dependency = team.split(":", 1)
            if kind in ("rank", "group_rank"):
                size = len(keys)
                rank = dependency
                if kind == "group_rank":
                    group, _, rank = dependency.partition(":")
                    size = len(config["schedule"]["groups"].get(group, []))
                if (not rank.isdigit() or not 1 <= int(rank) <= size
                        or not league or any(rnd >= round_number or until > start
                                            for rnd, until in league)):
                    raise ValueError(f"{key}: league ranking must resolve before this fixture.")
            elif (kind not in ("winner", "loser") or dependency not in seen
                  or seen[dependency][0] >= round_number or seen[dependency][1] > start
                  or dependency in {f["key"] for f in rows if f["stage"] == "league"}):
                raise ValueError(f"{key}: unresolved or overlapping playoff dependency {team}.")
        if row["stage"] == "league":
            league.append((round_number, end))
        seen[key] = round_number, end
