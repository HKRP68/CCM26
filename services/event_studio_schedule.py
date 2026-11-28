"""Pure event fixture generation and validation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from random import Random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json

from services import event_studio_brackets as brackets

MAX_FIXTURES = 240


def _salt(config):
    """A stable seed for this event's condition draws.

    Order-insensitive on purpose: regenerating an unchanged draft must produce the
    identical schedule, and reordering the groups mapping must not re-roll anything.
    Setting ``schedule.seed`` re-rolls every fixture's ground.
    """
    material = {"seed": config["schedule"]["seed"],
                "teams": sorted(team["key"] for team in config["teams"]),
                "opens_at": config["opens_at"]}
    return sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def draw_conditions(config, key, venues, pitches, salt=None):
    """The pitch, venue and weather for one fixture, drawn reproducibly from its key."""
    from services.pitch_report import WEATHERS
    rng = Random(f"{salt or _salt(config)}|{key}")
    return {"pitch": rng.choice(pitches), "venue": rng.choice(venues),
            "weather": rng.choice(list(WEATHERS))}


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


# The league shapes the generator can write. "manual" means the admin authors the
# league rows themselves; it still accepts a generated playoff bracket.
PRESETS = ("round_robin", "one_half_round_robin", "series", "groups", "knockout",
           "double_elimination", "survival", "manual")

# The playoff brackets a league table can hand its qualified teams, as rounds of
# (key, home, away). A slot is a reference the official final standings resolve
# (rank:n) or an earlier playoff fixture does (winner:<key>, loser:<key>), so a
# bracket is stored once here and advanced from results rather than re-authored.
# "custom" generates nothing: the admin authors the playoff rows themselves and the
# generator preserves them like any other manual fixture.
PLAYOFFS = {
    "none": (0, ()),
    "top2": (2, ((("final", "rank:1", "rank:2"),),)),
    "top4_semis": (4, ((("semi-final-1", "rank:1", "rank:4"),
                        ("semi-final-2", "rank:2", "rank:3")),
                       (("final", "winner:semi-final-1", "winner:semi-final-2"),))),
    "top4": (4, ((("qualifier", "rank:1", "rank:2"),
                  ("eliminator", "rank:3", "rank:4")),
                 (("qualifier-2", "loser:qualifier", "winner:eliminator"),),
                 (("final", "winner:qualifier", "winner:qualifier-2"),))),
    "top6": (6, ((("eliminator-1", "rank:3", "rank:6"),
                  ("eliminator-2", "rank:4", "rank:5")),
                 (("semi-final-1", "rank:1", "winner:eliminator-2"),
                  ("semi-final-2", "rank:2", "winner:eliminator-1")),
                 (("final", "winner:semi-final-1", "winner:semi-final-2"),))),
    "top8": (8, ((("quarter-final-1", "rank:1", "rank:8"),
                  ("quarter-final-2", "rank:4", "rank:5"),
                  ("quarter-final-3", "rank:2", "rank:7"),
                  ("quarter-final-4", "rank:3", "rank:6")),
                 (("semi-final-1", "winner:quarter-final-1", "winner:quarter-final-2"),
                  ("semi-final-2", "winner:quarter-final-3", "winner:quarter-final-4")),
                 (("final", "winner:semi-final-1", "winner:semi-final-2"),))),
    "custom": (0, ()),
}
# The brackets whose fixtures the generator writes, in the order the builder offers them.
PLAYOFF_PRESETS = tuple(PLAYOFFS)


PLAYOFF_LABELS = {
    "none": "No", "top2": "Top two direct final", "top4_semis": "Top four semi-final",
    "top4": "Top four IPL-style", "top6": "Top six knockout", "top8": "Top eight quarter-final",
    "custom": "Custom",
}


def playoff_rounds(preset, teams):
    """The bracket for ``preset``, refusing one the table cannot seed."""
    if preset not in PLAYOFFS:
        raise ValueError("Choose a supported playoff bracket.")
    minimum, rounds = PLAYOFFS[preset]
    if rounds and teams < minimum:
        raise ValueError(f"{PLAYOFF_LABELS[preset]} playoffs need {minimum} teams.")
    return rounds


def _append_playoffs(rows, preset, teams):
    for games in playoff_rounds(preset, teams):
        _append_round(rows, [(home, away) for _, home, away in games], "playoff",
                      [key for key, _, _ in games])


EVENT_TEMPLATES = {
    "weekend": ("round_robin", "knockout"),
    "nations": ("round_robin", "groups", "knockout"),
    "legends": ("round_robin", "groups", "knockout"),
    "rivalry": ("round_robin", "series", "groups"),
    "road_to_final": ("survival",),
    "survival_league": ("survival",),
    "mystery": ("round_robin", "groups", "knockout", "double_elimination"),
    "champions": ("round_robin", "groups", "knockout", "double_elimination"),
    "underdog": ("round_robin", "groups", "knockout"),
    "dynasty": ("round_robin", "groups", "series"),
    "world_tour": ("series",),
    "double_elimination": ("double_elimination",),
}


def validate_template(config):
    from services.event_studio_rules import integer

    settings = config["schedule"]
    template = settings.get("template", "")
    integer(settings.get("rest_minutes", 60), "Minimum rest in minutes", 0, 1440)
    previous = integer(settings.get("previous_event_id", 0), "Previous event ID", 0, 2_147_483_647)
    tour = settings.get("tour", [])
    if previous and template not in ("dynasty", "champions"):
        raise ValueError("Only Dynasty and Champions League can link an earlier edition.")
    if template == "dynasty" and not previous:
        raise ValueError("Link the previous Dynasty edition.")
    if not isinstance(tour, list) or (tour and template != "world_tour"):
        raise ValueError("Country series apply only to World Tour.")
    if template == "world_tour":
        if not 2 <= len(tour) <= 8:
            raise ValueError("Enter two to eight country series for World Tour.")
        for stop in tour:
            if (not isinstance(stop, dict) or set(stop) != {"country", "games"} or
                    not isinstance(stop["country"], str) or not 1 <= len(stop["country"].strip()) <= 60):
                raise ValueError("Enter a country and game count for each World Tour stop.")
            integer(stop["games"], "World Tour games per series", 1, 7)
    if template == "":
        return
    if (not isinstance(template, str) or template not in EVENT_TEMPLATES
            or settings["preset"] not in EVENT_TEMPLATES[template]
            or config["kind"] != "live" or config["objective_scope"] != "tournament"):
        raise ValueError("Choose a schedule compatible with the Live Event template.")


def seeded_teams(config):
    keys = [team["key"] for team in config["teams"]]
    seed = config["schedule"]["seed"]
    if seed is not None:
        Random(seed).shuffle(keys)
    return keys


def generate(config, venues=None, pitches=None):
    if config["kind"] != "live":
        return []
    keys = seeded_teams(config)
    if len(keys) < 2:
        raise ValueError("Add at least two teams before generating fixtures.")
    settings = config["schedule"]
    preset = settings.get("preset", "round_robin")
    rows = []
    fixed = 0  # Leading rows that were authored, not generated, so they keep their timings.
    if preset == "round_robin":
        for games in round_robin(keys, settings["legs"]):
            _append_round(rows, games, "league")
        _append_playoffs(rows, settings.get("playoffs", "none"), len(keys))
    elif preset == "one_half_round_robin":
        # Every pair meets once, then a balanced subset meets again with home and away
        # reversed, so team totals differ by at most one. The plan comes from the
        # existing partial round-robin service rather than a second implementation.
        from services.partial_round_robin import build_schedule
        plan = build_schedule(keys, settings.get("match_target") or None)
        for stage_round in plan["rounds"]:
            _append_round(rows, stage_round["pairs"], "league")
        _append_playoffs(rows, settings.get("playoffs", "none"), len(keys))
    elif preset == "series":
        if len(keys) != 2:
            raise ValueError("A bilateral series requires two teams.")
        tour = settings.get("tour", []) if settings.get("template") == "world_tour" else []
        games = sum(stop["games"] for stop in tour) if tour else settings["games"]
        for index in range(games):
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
    elif preset == "double_elimination":
        for round_rows in brackets.double_elimination(keys, reset=settings.get("grand_final_reset", True)):
            _append_round(rows, [(home, away) for _, home, away in round_rows], "playoff",
                          [key for key, _, _ in round_rows])
    elif preset == "knockout":
        _knockout(rows, keys, settings.get("third_place", False))
    elif preset == "manual":
        # A hand-authored league can still take a generated playoff bracket. The
        # authored rows keep their own keys, slots and timings untouched; only the
        # appended bracket is written, and it is timed after the league finishes.
        if settings["playoffs"] in ("none", "custom"):
            # Nothing to generate: those rows are the admin's own, and dropping the
            # playoffs they authored to return a league-only schedule would lose work.
            raise ValueError("Choose a playoff bracket to generate from a manual schedule, "
                             "or validate the authored fixtures instead.")
        rows = sorted((deepcopy(row) for row in config["fixtures"] if row.get("stage") != "playoff"),
                      key=lambda row: (row["round"], row["key"]))
        if not rows:
            raise ValueError("Author the league fixtures before generating their playoffs.")
        fixed = len(rows)
        _append_playoffs(rows, settings["playoffs"], len(keys))
    else:
        raise ValueError("Choose a supported schedule preset or enter fixtures manually.")
    if len(rows) > MAX_FIXTURES:
        raise ValueError(f"No more than {MAX_FIXTURES} fixtures are allowed.")

    zone = config["timezone"]
    start = utc_instant(config["opens_at"], zone)
    calendar = start.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    close = utc_instant(config["closes_at"], zone)
    weekend = settings.get("template") == "weekend"
    interval = timedelta(hours=settings["window_hours"],
                         minutes=settings["grace"] + settings.get("rest_minutes", 60))
    # The weekend template folds its rest into the interval above. Everywhere else the
    # general setting pushes the next round on by whole days, keeping the time of day.
    min_rest = timedelta(minutes=0 if weekend else settings.get("min_rest_minutes", 0) or 0)
    current_round = 0
    round_usage = 0
    round_end = None  # When every fixture in the round being timed is finally settled.
    cap = settings.get("fixtures_per_day", 24)
    if fixed:
        # Resume at the event's opening time of day on the first date clear of the
        # authored league, so a generated bracket never overlaps the rows it follows.
        played = max(utc_instant(row["deadline"], zone) + timedelta(minutes=row.get("grace", 0) or 0)
                     for row in rows[:fixed])
        local = played.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
        calendar = calendar.replace(year=local.year, month=local.month, day=local.day)
        if calendar < local:
            calendar += timedelta(days=1)
        current_round, round_end = rows[fixed - 1]["round"], played
    for row in rows[fixed:]:
        if current_round != row["round"] or round_usage >= cap:
            if current_round:
                if weekend:
                    stamp = utc_instant(calendar.isoformat(), zone) + interval
                    calendar = stamp.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).replace(tzinfo=None)
                else:
                    calendar += timedelta(days=settings["round_spacing_days"])
                while round_end and utc_instant(calendar.isoformat(), zone) < round_end + min_rest:
                    calendar += timedelta(days=1)
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
        round_end = deadline + timedelta(minutes=settings["grace"])
        round_usage += 1
    if venues and pitches:
        salt = _salt(config)
        world = settings.get("template") == "world_tour"
        if world:
            from services.draft_service import country_key
            from services.tournament_stadiums import all_rows
            approved = set(venues)
            pools = {}
            for record in all_rows():
                if record.get("name") in approved:
                    pools.setdefault(country_key(record.get("country")), []).append(record["name"])
            trips = [stop["country"] for stop in settings["tour"] for _ in range(stop["games"])]
        for index, row in enumerate(rows):
            if index < fixed:
                continue  # An authored row keeps the ground it was authored with.
            options = sorted(set(pools.get(country_key(trips[index]), ()))) if world else venues
            if not options:
                raise ValueError(f"No eligible stadium in {trips[index]} for World Tour.")
            row.update(draw_conditions(config, row["key"], options, pitches, salt=salt))
    validate(config, rows)
    return rows


def validate(config, rows):
    if config["kind"] != "live":
        if rows:
            raise ValueError("Standalone Moments and Boss Battles have no tournament fixtures.")
        return
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FIXTURES:
        raise ValueError(f"Enter 1 to {MAX_FIXTURES} fixtures.")
    validate_template(config)
    zone = config["timezone"]
    opens = utc_instant(config["opens_at"], zone)
    close = utc_instant(config["closes_at"], zone)
    weekend = config["schedule"].get("template") == "weekend"
    # The weekend template packs rounds into days, so it carries its own rest figure.
    # min_rest_minutes is the general setting, and defaults to 0 so a draft saved
    # before it existed validates exactly as it did.
    rest = timedelta(minutes=max(config["schedule"].get("rest_minutes", 60) if weekend else 0,
                                 config["schedule"].get("min_rest_minutes", 0) or 0))
    if weekend and not timedelta(days=2) <= close - opens <= timedelta(days=3):
        raise ValueError("Weekend Championship must fit a two- or three-day event window.")
    if brackets.is_double(config):
        expected = {key: (home, away, round_no)
                    for round_no, group in enumerate(brackets.double_elimination(
                        seeded_teams(config), reset=config["schedule"].get("grand_final_reset", True)), 1)
                    for key, home, away in group}
        if len(rows) != len(expected) or any(
                (row["home"], row["away"], row["round"]) != expected.get(row["key"]) or row["stage"] != "playoff"
                for row in rows):
            raise ValueError("Regenerate the double-elimination bracket. Its dependencies are fixed.")
    if config["schedule"].get("template") == "world_tour":
        trips = [stop["country"] for stop in config["schedule"]["tour"] for _ in range(stop["games"])]
        if len(rows) != len(trips):
            raise ValueError("The World Tour schedule must contain its published series games.")
        from services.draft_service import country_key
        from services.tournament_stadiums import all_rows
        venue_countries = {row["name"]: country_key(row.get("country")) for row in all_rows() if row.get("name")}
        ordered = sorted(rows, key=lambda row: row["round"])
        for fixture, country in zip(ordered, trips):
            effective = config["fixture_overrides"].get(fixture["key"], {}).get("venue", fixture.get("venue"))
            if effective and venue_countries.get(effective) != country_key(country):
                raise ValueError(f'{fixture["key"]}: choose a venue in {country}.')
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
                if any(round_number == rnd or (start < to + rest and end + rest > since)
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
                        or not league or any(rnd >= round_number or until + rest > start
                                            for rnd, until in league)):
                    raise ValueError(f"{key}: league ranking must resolve before this fixture.")
            elif (kind not in ("winner", "loser") or dependency not in seen
                  or seen[dependency][0] >= round_number or seen[dependency][1] + rest > start
                  or dependency in {f["key"] for f in rows if f["stage"] == "league"}):
                raise ValueError(f"{key}: unresolved or overlapping playoff dependency {team}.")
        if row["stage"] == "league":
            league.append((round_number, end))
        seen[key] = round_number, end
