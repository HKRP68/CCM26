"""Tournament objectives from committed official innings."""

from decimal import Decimal, InvalidOperation
from math import ceil
import json
import re

from services.event_studio_rules import FORMATS, integer

METRICS = {
    "runs": "Batter runs", "boundaries": "Bat boundaries", "fours": "Bat fours",
    "sixes": "Bat sixes", "fifties": "Fifties (50–99)", "centuries": "Centuries (100+)",
    "highest_score": "Highest individual score", "wickets": "Bowler-credited wickets",
    "team_dismissals": "Team dismissals", "three_wicket_hauls": "Three-wicket hauls",
    "five_wicket_hauls": "Five-wicket hauls", "maidens": "Maidens", "wins": "Wins",
    "played_matches": "Completed matches", "consecutive_wins": "Consecutive wins",
    "successful_chases": "Successful chases", "successful_defenses": "Successful defenses",
    "aggregate_strike_rate": "Aggregate strike rate", "aggregate_economy": "Aggregate economy",
    "max_partnership": "Highest partnership", "pace_wickets": "Pace wickets",
    "spin_wickets": "Spin wickets", "captain_runs": "Captain runs", "captain_wickets": "Captain wickets",
    "bowling_dot_balls": "Bowling dot balls", "hat_tricks": "Hat-tricks",
    "powerplay_runs": "Powerplay batter runs", "powerplay_wickets": "Powerplay bowler wickets",
    "death_runs": "Death-over batter runs", "death_wickets": "Death-over bowler wickets",
    "qualification": "Play a qualifying playoff fixture", "champion": "Win the final",
}
RATES = {"aggregate_strike_rate": ("runs", "bat_balls", 100),
         "aggregate_economy": ("conceded", "bowl_balls", 6)}
MAXIMA = {"highest_score", "max_partnership", "qualification", "champion"}
PRESETS = {"easy": .65, "balanced": 1, "hard": 1.25, "extreme": 1.55, "custom": None}
STAGES = ("league", "playoff", "survival")
FILTERS = {"runs", "wickets", "team_dismissals"}
PHASES = ("powerplay", "middle", "death")
ROLES = ("batter", "keeper", "allrounder", "bowler")
ROLE_METRICS = {"runs", "boundaries", "fours", "sixes", "fifties", "centuries", "highest_score",
                "wickets", "maidens", "three_wicket_hauls", "five_wicket_hauls",
                "aggregate_strike_rate", "aggregate_economy"}


def tournament(config):
    return config.get("objective_scope") == "tournament" and config["kind"] == "live"


def active_rows(config):
    rows = config.get("tournament_objectives", [])
    ladder = config.get("objective_ladders")
    if not ladder:
        return rows
    from services.event_objective_rules import parse_objectives
    stages = parse_objectives(json.dumps(ladder), match_format=config["format"], star_cap=120)
    aliases = {"dots": "bowling_dot_balls", "win_streak": "consecutive_wins",
               "chases": "successful_chases", "defenses": "successful_defenses"}
    expanded = []
    for objective in stages:
        metric = aliases.get(objective.metric, objective.metric)
        for stage in objective.stages:
            expanded.append({"id": stage.id, "metric": metric, "target": stage.target, "stars": stage.stars,
                             "title": f"{METRICS[metric]} · {stage.id.replace('_', ' ').title()}",
                             "locked": stage.locked,
                             "stages": ["playoff"] if objective.optional_playoffs else ["league", "survival"]})
    return [*rows, *expanded]



def generate_ladders(config, *, overwrite=False):
    """Place generated stages on the same settlement and star receipts as authored ladders."""
    options = config.get("ladder_generator", {})
    metrics = options.get("metrics", [])
    existing = config.get("objective_ladders")
    if not metrics or existing and not overwrite:
        return existing
    from services.event_objective_generator import LeagueFixture, generate_ladder
    from services.event_objective_rules import export_objectives, parse_objectives

    format_name = config["format"]
    stage_count = options.get("stage_count", 4)
    remaining = options.get("star_cap", 120) - sum(row["stars"] for row in config["tournament_objectives"])
    saved = {item.id: item for item in parse_objectives(json.dumps(existing), match_format=format_name, star_cap=120)} if existing else {}
    teams = [team["key"] for team in config["teams"]]
    keys = set(teams)
    fixtures = [LeagueFixture(row["key"], row["home"], row["away"]) for row in config["fixtures"]
                if row["stage"] == "league" and row["home"] in keys and row["away"] in keys]
    results = [obj for obj in saved.values() if obj.metric not in metrics or obj.optional_playoffs]
    remaining -= sum(stage.stars for obj in results for stage in obj.stages)
    for position, metric in enumerate(metrics):
        existing_row = next((row for row in saved.values() if row.metric == metric and not row.optional_playoffs), None)
        future_locked = sum(stage.stars for row in saved.values() if row.metric in metrics[position+1:]
                            and not row.optional_playoffs for stage in row.stages if stage.locked)
        lock_cost = sum(stage.stars for stage in existing_row.stages if stage.locked) if existing_row else 0
        available = remaining - future_locked
        if available < lock_cost:
            raise ValueError("Locked objective stages exceed the configured star cap.")
        budget = min(available, max(lock_cost, available // (len(metrics) - position)))
        if not budget:
            continue
        preview = generate_ladder(objective_id=existing_row.id if existing_row else metric, metric=metric,
                                  match_format=format_name, fixtures=fixtures, team_ids=teams, star_cap=budget,
                                  difficulty=options.get("preset", "Balanced"), stage_count=stage_count,
                                  existing=existing_row, regenerate=existing_row is not None)
        if preview.objective.stages:
            results.append(preview.objective)
            remaining -= preview.star_liability
    return json.loads(export_objectives(results))


def validate_generator(config):
    from services.event_objective_generator import PRESETS as LADDER_PRESETS
    from services.event_objective_rules import METRICS as LADDER_METRICS

    options = config.get("ladder_generator", {})
    if options == {}:
        return
    if not isinstance(options, dict) or options.keys() - {"metrics", "stage_count", "star_cap", "preset"}:
        raise ValueError("Use supported objective ladder generator fields.")
    metrics = options.get("metrics", [])
    if (not isinstance(metrics, list) or not 1 <= len(metrics) <= 6 or
            any(not isinstance(key, str) or key not in LADDER_METRICS for key in metrics) or
            len(metrics) != len(set(metrics))):
        raise ValueError("Choose one to six distinct ladder metrics.")
    integer(options.get("stage_count", 4), "Ladder stage count", 1, 4)
    integer(options.get("star_cap", 120), "Ladder star cap", 1, 120)
    preset = options.get("preset", "Balanced")
    if not isinstance(preset, str) or preset not in LADDER_PRESETS:
        raise ValueError("Choose an Easy, Balanced, Hard or Extreme ladder preset.")
    if not tournament(config):
        raise ValueError("Auto ladders require tournament objectives.")


def max_stars(config):
    return sum(row["stars"] for row in active_rows(config))


def guaranteed(config, stages=STAGES):
    keys = [team["key"] for team in config["teams"]]
    if config["schedule"]["preset"] == "survival":
        keys = keys[:1]
    return min((sum(key in (row["home"], row["away"]) and row["stage"] in stages
                    for row in config["fixtures"]) for key in keys), default=0)


def validate(config, *, publishing=False):
    if config.get("objective_scope") not in ("fixture", "tournament"):
        raise ValueError("Choose fixture or tournament objectives.")
    if not tournament(config):
        if config.get("tournament_objectives") or config.get("objective_ladders"):
            raise ValueError("Tournament objectives apply to Live Events.")
        return
    rows = active_rows(config)
    if not isinstance(rows, list) or len(rows) > 24:
        raise ValueError("Enter up to 24 tournament objectives.")
    seen = set()
    for row in rows:
        fields = {"id", "metric", "target", "stars", "comparator", "stages", "title", "locked", "min_balls", "phase", "role"}
        if not isinstance(row, dict) or row.keys() - fields:
            raise ValueError("Use supported objective fields. Named-player filters are unavailable.")
        key, metric = row.get("id"), row.get("metric")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", key) or key in seen:
            raise ValueError("Use distinct objective IDs (letters, digits, hyphens or underscores).")
        seen.add(key)
        if metric not in METRICS:
            raise ValueError(f"Unsupported tournament objective metric: {metric}.")
        if "phase" in row and (metric not in FILTERS or row["phase"] not in PHASES):
            raise ValueError(f"{key}: phase filters support runs, wickets and team dismissals.")
        if "role" in row and (metric not in ROLE_METRICS or row["role"] not in ROLES):
            raise ValueError(f"{key}: choose batter, keeper, allrounder or bowler for an individual metric.")
        if metric in ("qualification", "champion") and row.get("target") != 1:
            raise ValueError(f"{key}: the target must be one.")
        integer(row.get("stars"), "Objective stars", 1, 20)
        comparator = row.get("comparator", ">=")
        if comparator != ">=" and not (metric in RATES and comparator == "<="):
            raise ValueError(f"{key}: unsupported comparator.")
        if metric in RATES:
            try:
                target = Decimal(str(row.get("target")))
                if not target.is_finite() or not 0 < target <= 10000 or target.as_tuple().exponent < -2:
                    raise ValueError(f"{key}: use a positive target with up to two decimals.")
            except (InvalidOperation, TypeError) as exc:
                raise ValueError(f"{key}: enter a rate target.") from exc
            integer(row.get("min_balls"), "Rate minimum legal balls", 1, 30000)
        else:
            integer(row.get("target"), key, 1, 100000)
            if "min_balls" in row:
                raise ValueError(f"{key}: a minimum sample is only supported for rate objectives.")
        stages = row.get("stages", list(STAGES))
        if not isinstance(stages, list) or not stages or any(s not in STAGES for s in stages) or len(set(stages)) != len(stages):
            raise ValueError(f"{key}: select valid stages.")
        if type(row.get("locked", False)) is not bool or not isinstance(row.get("title", ""), str) or len(row.get("title", "")) > 100:
            raise ValueError(f"{key}: invalid title or lock.")
        if publishing:
            balls = FORMATS[config["format"]]["overs"] * 6
            from services.event_studio_rules import candidate_paths
            eligible = {f["key"] for f in config["fixtures"] if f["stage"] in stages}
            if metric in ("qualification", "champion") and not any(
                    f["stage"] == "playoff" and f["key"] != "third-place" for f in config["fixtures"]):
                raise ValueError(f"{key}: this objective requires a playoff schedule.")
            games = max((len(set(path) & eligible) for path in candidate_paths(config)), default=0)
            per_match = {"wickets": 10, "team_dismissals": 10, "played_matches": 1, "wins": 1,
                         "consecutive_wins": 1, "successful_chases": 1, "successful_defenses": 1,
                         "three_wicket_hauls": 3, "five_wicket_hauls": 2, "maidens": balls // 6}
            if not games or metric in per_match and row["target"] > games * per_match[metric]:
                raise ValueError(f"{key}: target exceeds the available fixtures or innings.")
            if metric in RATES and row["min_balls"] > games * balls:
                raise ValueError(f"{key}: minimum sample exceeds the available legal balls.")
    if max_stars(config) > 120:
        raise ValueError("Tournament objectives may award up to 120 stars.")



def estimates(config):
    """Use observed delivery frequencies only when the match adapter is measured."""
    from engine.realism import baselines, integration

    odi = config["format"] == "ODI"
    name = "Conservative format estimates"
    boundaries, wickets = (12, 5) if odi else (7, 4)
    if odi:
        from engine.realism import odi as reference
        try:
            document = reference.validate_reference(reference.new_settings())
        except reference.ODIUnavailable:
            document = None
    else:
        document = baselines.load(format_name="T20") if integration.live_for() else None
    root = (document or {}).get("nodes", {}).get(baselines.ROOT, {})
    counts = root.get("counts", {})
    sample = sum(counts.values())
    if root.get("count_kind") == "observed" and sample >= 1000:
        expected_deliveries = FORMATS[config["format"]]["overs"] * 6 * .70
        boundaries = expected_deliveries * (counts["Four"] + counts["Six"]) / sample
        wickets = min(8, expected_deliveries * counts["Wicket"] / sample)
        name = f'Observed {config["format"]} delivery reference with a conservative innings allowance'
    return name, boundaries, wickets

def generate(config):
    preset = PRESETS[config["objective_preset"]]
    existing = config["tournament_objectives"]
    if preset is None or config.get("objective_ladders"):
        return existing
    matches = guaranteed(config)
    if not matches:
        raise ValueError("Generate fixtures with directly assigned franchises before generating tournament objectives.")
    odi = config["format"] == "ODI"
    _, boundaries, wickets = estimates(config)
    targets = [("boundaries", boundaries, 3), ("wickets", wickets, 4),
               ("fifties", .85 if odi else .38, 4)]
    if (odi and matches >= 4) or matches >= 10:
        targets.append(("centuries", .18 if odi else .08, 5))
    else:
        targets.append(("played_matches", .55, 5))
    proposed = {metric: {"id": metric, "metric": metric, "stars": stars, "stages": list(STAGES),
                         "target": min(matches * 10 if metric == "wickets" else matches if metric == "played_matches"
                                       else 100000, max(1, ceil(rate * matches * preset)))}
                for metric, rate, stars in targets}
    for row in existing:
        if row.get("locked"):
            proposed[row["id"]] = row
    return list(proposed.values())


def warnings(config):
    if not tournament(config):
        return []
    rows = active_rows(config)
    source, _, _ = estimates(config)
    alerts = [f"{source} per guaranteed fixture. Fifties and centuries use conservative estimates."]
    count = guaranteed(config)
    if count < 3:
        alerts.append("Fewer than three guaranteed fixtures. High milestones may depend on a small sample.")
    if rows and all(set(row.get("stages", STAGES)) == {"playoff"} for row in rows):
        alerts.append("All objectives require access to a playoff fixture.")
    for row in rows:
        if row["metric"] == "centuries" and config["format"] == "T20":
            alerts.append(f'{row["id"]}: T20 centuries are rare.')
        if row["metric"] in ("qualification", "champion"):
            alerts.append(f'{row["id"]}: only a genuinely completed playoff fixture can earn this objective.')
        if row["metric"] == "wickets" and row["target"] > 8 * guaranteed(config):
            alerts.append(f'{row["id"]}: this target is above the conservative guaranteed-match estimate.')
    return alerts


def measure(row, contributions):
    metric = row["metric"]
    samples = [item.metrics for item in contributions if item.stage in row.get("stages", STAGES)]
    if "role" in row:
        samples = [item.get("roles", {}).get(row["role"], {}) for item in samples]
    if metric in RATES:
        numerator, denominator, multiplier = RATES[metric]
        balls = sum(p.get(denominator, 0) for p in samples)
        exact = Decimal(sum(p.get(numerator, 0) for p in samples) * multiplier) / balls if balls else Decimal(0)
        sample_balls = sum(p.get("legal_bat_balls" if metric == "aggregate_strike_rate" else denominator, 0)
                           for p in samples)
        sufficient = sample_balls >= row["min_balls"]
    elif metric == "consecutive_wins":
        best = streak = 0
        for p in samples:
            streak = streak + 1 if p["wins"] else 0
            best = max(best, streak)
        exact, sufficient = best, bool(samples)
    else:
        values = [p.get("phases", {}).get(row["phase"], {}).get(metric, 0)
                  if "phase" in row else p.get(metric, 0) for p in samples]
        exact = max(values, default=0) if metric in MAXIMA else sum(values)
        sufficient = bool(samples)
    target = Decimal(str(row["target"]))
    lower = row.get("comparator") == "<="
    met = sufficient and (exact <= target if lower else exact >= target)
    return (round(float(exact), 2) if metric in RATES else exact), met


def fixture_metrics(state, team, winner, performance, *, stage=None, final=False):
    from services.cipl_match import approach_bowling_type
    bats = [row.get("batting", {}) for row in performance]
    bowls = [row.get("bowling", {}) for row in performance]
    second = state["bat_team_id"] == team
    wins = int(winner == team)
    batting_partnerships = state.get("partnership_history" if second else "inn1_partnership_history", [])
    figures = state.get("inn1_bowl_stats" if second else "bowl_stats", {})
    attack = state.get("inn1_bowl_xi" if second else "bowl_xi", [])
    by_family = {"pace": 0, "spin": 0}
    for p in attack:
        bowling = approach_bowling_type(p)
        by_family["spin" if "spin" in str(bowling).lower() else "pace"] += figures.get(str(p["roster_id"]), {}).get("wickets", 0)
    captain = (state.get("event_captains") or {}).get(team)
    for p in state.get("bat_xi" if second else "inn1_bat_xi", []):
        if p["roster_id"] == captain:
            captain = p["player_id"]
            break
    captain_rows = [p for p in performance if p["player_id"] == captain]
    bat_runs = [p.get("runs", 0) for p in bats]
    wickets = [p.get("wickets", 0) for p in bowls]
    fours = sum(p.get("fours", 0) for p in bats)
    sixes = sum(p.get("sixes", 0) for p in bats)
    from services.xi_rules import _category_is_keeper
    roles_by_card = {}
    for side in ("bat_xi", "bowl_xi", "inn1_bat_xi", "inn1_bowl_xi"):
        for card in state.get(side, []):
            category = card.get("category")
            role = ("keeper" if _category_is_keeper(category) else "allrounder" if category == "All-rounder"
                    else "bowler" if category == "Bowler" else "batter")
            roles_by_card[card["player_id"]] = role
    role_stats = {role: {"phases": {phase: dict.fromkeys(FILTERS, 0) for phase in PHASES}} for role in ROLES}
    for player in performance:
        stats = role_stats[roles_by_card.get(player["player_id"], "batter")]
        bat, bowl = player.get("batting", {}), player.get("bowling", {})
        score, taken = bat.get("runs", 0), bowl.get("wickets", 0)
        values = {"runs": score, "fours": bat.get("fours", 0), "sixes": bat.get("sixes", 0),
                  "fifties": int(50 <= score < 100), "centuries": int(score >= 100),
                  "wickets": taken, "maidens": bowl.get("maidens", 0),
                  "three_wicket_hauls": int(taken >= 3), "five_wicket_hauls": int(taken >= 5),
                  "bat_balls": bat.get("balls", 0), "bowl_balls": bowl.get("balls", 0), "conceded": bowl.get("runs", 0)}
        values["boundaries"] = values["fours"] + values["sixes"]
        for key, count in values.items():
            stats[key] = stats.get(key, 0) + count
        stats["highest_score"] = max(stats.get("highest_score", 0), score)
    phase_stats = {name: dict.fromkeys(FILTERS, 0) for name in PHASES}
    legal_bat_balls = hat_tricks = 0
    streaks = {}
    innings = 2 if second else 1
    for delivery in state.get("record_deliveries", []):
        name = delivery["phase"]
        phase = phase_stats[name]
        if delivery["innings"] == innings:
            role = roles_by_card.get(delivery.get("batter_id"))
            if role:
                stats = role_stats[role]
                stats["legal_bat_balls"] = stats.get("legal_bat_balls", 0) + delivery["legal"]
                stats["phases"][name]["runs"] += delivery["bat_runs"]
            phase["runs"] += delivery["bat_runs"]
            legal_bat_balls += delivery["legal"]
        else:
            role = roles_by_card.get(delivery.get("bowler_id"))
            if role:
                role_stats[role]["phases"][name]["wickets"] += delivery["bowler_wicket"]
            phase["wickets"] += delivery["bowler_wicket"]
            phase["team_dismissals"] += delivery["wicket"]
            key = delivery["bid"]
            streaks[key] = streaks.get(key, 0) + 1 if delivery["legal"] and delivery["bowler_wicket"] else 0
            hat_tricks += int(streaks[key] == 3)
    return {
        "runs": sum(bat_runs), "boundaries": fours + sixes, "fours": fours, "sixes": sixes,
        "fifties": sum(50 <= runs < 100 for runs in bat_runs), "centuries": sum(runs >= 100 for runs in bat_runs),
        "highest_score": max(bat_runs, default=0), "wickets": sum(wickets),
        "team_dismissals": state["inn1_wickets"] if second else state["total_wickets"],
        "three_wicket_hauls": sum(w >= 3 for w in wickets), "five_wicket_hauls": sum(w >= 5 for w in wickets),
        "maidens": sum(p.get("maidens", 0) for p in bowls), "wins": wins, "played_matches": 1,
        "successful_chases": wins if second else 0, "successful_defenses": 0 if second else wins,
        "bat_balls": sum(p.get("balls", 0) for p in bats), "bowl_balls": sum(p.get("balls", 0) for p in bowls),
        "conceded": sum(p.get("runs", 0) for p in bowls),
        "max_partnership": max([state.get("partnership_runs", 0) if second else 0,
                                *(p["runs"] for p in batting_partnerships)]),
        "phases": phase_stats, "roles": role_stats, "powerplay_runs": phase_stats["powerplay"]["runs"],
        "powerplay_wickets": phase_stats["powerplay"]["wickets"], "death_runs": phase_stats["death"]["runs"],
        "death_wickets": phase_stats["death"]["wickets"], "legal_bat_balls": legal_bat_balls,
        "bowling_dot_balls": sum(p.get("dots", 0) for p in figures.values()), "hat_tricks": hat_tricks,
        "qualification": int(stage == "playoff"), "champion": int(final and winner == team),
        "pace_wickets": by_family["pace"], "spin_wickets": by_family["spin"],
        "captain_runs": sum(p.get("batting", {}).get("runs", 0) for p in captain_rows),
        "captain_wickets": sum(p.get("bowling", {}).get("wickets", 0) for p in captain_rows),
    }
