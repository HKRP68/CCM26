"""World Cup ODI counting and provenance for the Cricsheet reference CLI."""

from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
from statistics import mean, pstdev
from zipfile import ZipFile

from engine.realism.context import chase_bucket, wickets_bucket
from engine.realism.phases import phase_for_ball

LICENCE = "Open Data Commons Attribution License (ODC-By) 1.0"
ATTRIBUTION = "Contains information from Cricsheet, made available under ODC-By 1.0."
EXTRA_TYPES = ("wides", "noballs", "byes", "legbyes", "penalty")
NON_DISMISSALS = {"retired hurt", "retired not out"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quantiles(values):
    if not values:
        return {}
    samples = sorted(values)
    result = {}
    for p in (5, 10, 25, 50, 75, 90, 95):
        lo, frac = divmod((len(samples) - 1) * p, 100)
        result[f"p{p}"] = round(samples[lo] * (1 - frac / 100)
                                + samples[min(lo + 1, len(samples) - 1)] * frac / 100, 3)
    return result


def classify(delivery, free_hit=False):
    """One primary outcome and an independent accounting view of the ball."""
    ex = delivery.get("extras") or {}
    runs = delivery["runs"]
    illegal = bool(ex.get("wides") or ex.get("noballs"))
    extra = next((name for name in EXTRA_TYPES if ex.get(name)), None)
    dismissals = [w for w in delivery.get("wickets", []) if w["kind"] not in NON_DISMISSALS]
    if free_hit or extra == "noballs":
        permitted = {"run out", "obstructing the field", "hit the ball twice"}
    elif extra == "wides":
        permitted = {"run out", "stumped", "hit wicket", "obstructing the field"}
    else:
        permitted = None
    if permitted is not None and any(w["kind"] not in permitted for w in dismissals):
        raise ValueError(f"Illegal dismissal on {extra or 'free hit'}: {dismissals}")
    bat_runs = runs["batter"]
    primary = ("Extras" if extra else "Wicket" if dismissals else
               ("Dot", "Single", "Double", "Three", "Four", "Four", "Six")[min(6, bat_runs)])
    return primary, not illegal, extra, dismissals


class Sample:
    def __init__(self):
        self.counts = defaultdict(Counter)
        self.metrics = defaultdict(Counter)
        self.overs = defaultdict(Counter)
        self.extras = defaultdict(Counter)
        self.innings = defaultdict(list)
        self.chases = defaultdict(list)
        self.match_ids = []
        self.dates = []
        self.fingerprints = defaultdict(list)

    def count_match(self, match_id, doc):
        from tools.build_realism_reference import _match_competition

        info = doc["info"]
        competition = _match_competition(info, {})
        self.match_ids.append(match_id)
        self.dates.extend(info["dates"])
        target = None
        for number, innings in enumerate((i for i in doc["innings"] if not i.get("super_over")), 1):
            legal = score = wickets = 0
            free_hit = False
            faced, batter_scores = Counter(), Counter()
            bands = set()
            fingerprint = []
            ik = f"inn{number}"
            for over in innings["overs"]:
                phase = phase_for_ball(legal, 300, "ODI")
                over_start, score_start = legal, score
                for ball in over["deliveries"]:
                    phase = phase_for_ball(legal, 300, "ODI")
                    primary, is_legal, extra, outs = classify(ball, free_hit)
                    parts = ["ODI", competition, phase, ik, wickets_bucket(wickets)]
                    if target is not None:
                        band = chase_bucket((target - score) * 6 / (300 - legal), "ODI")
                        bands.add(band)
                        parts.append(band)
                    self.counts["root"][primary] += 1
                    for depth in range(1, len(parts) + 1):
                        self.counts["/".join(parts[:depth])][primary] += 1

                    runs = ball["runs"]
                    stats_groups = ["all", phase, ik, f"{phase}/{ik}",
                                    f"overs_{legal // 30 * 5 + 1}_{legal // 30 * 5 + 5}",
                                    f"faced_{faced[ball['batter']] // 15 * 15}"]
                    for key in stats_groups:
                        self.metrics[key].update({
                            "deliveries": 1, "legal_balls": int(is_legal),
                            "batting_runs": runs["batter"], "runs": runs["total"],
                            "extras": runs["extras"], "wickets": len(outs),
                            "boundaries": int(runs["batter"] in (4, 6) and not runs.get("non_boundary")),
                            "balls_faced": int(extra != "wides"), primary: 1,
                            "free_hits": int(free_hit), "illegal_wickets": len(outs) if not is_legal else 0,
                        })
                    if extra:
                        # A conditional joint event, kept separate from primary counts.
                        dismissal = outs[0]["kind"] if outs else ""
                        variant = (extra, runs["extras"], runs["batter"],
                                   int((ball.get("extras") or {}).get("byes", 0)
                                       + (ball.get("extras") or {}).get("legbyes", 0)),
                                   dismissal, int(bool(outs and outs[0]["player_out"] != ball["batter"])))
                        self.extras[phase][variant] += 1
                        self.extras["all"][variant] += 1
                    score += runs["total"]
                    wickets += len(outs)
                    legal += is_legal
                    faced[ball["batter"]] += extra != "wides"
                    batter_scores[ball["batter"]] += runs["batter"]
                    free_hit = extra == "noballs" or (free_hit and extra == "wides")
                    if is_legal and not extra and len(fingerprint) < 48:
                        fingerprint.append((legal, "W" if outs else str(runs["batter"])))
                if legal - over_start == 6:
                    self.overs[phase][score - score_start] += 1
                    self.overs["all"][score - score_start] += 1
            self.fingerprints[match_id].append(dict(fingerprint))
            self.innings[ik].append({
                "match_id": match_id, "runs": score, "wickets": wickets, "balls": legal,
                "centuries": sum(r >= 100 for r in batter_scores.values()),
                "double_centuries": sum(r >= 200 for r in batter_scores.values()),
                "early_end": legal < 300,
            })
            if number == 2:
                for band in bands | {"all"}:
                    self.chases[band].append("won" if score >= target
                                             else "tie" if score == target - 1 else "lost")
            else:
                target = score + 1

    def report(self):
        metrics = {}
        for key, row in self.metrics.items():
            d, legal = row["deliveries"], row["legal_balls"]
            metrics[key] = dict(row, runs_per_over=6 * row["runs"] / max(1, legal),
                                runs_per_legal_ball=row["runs"] / max(1, legal),
                                batting_runs_per_ball=row["batting_runs"] / max(1, row["balls_faced"]),
                                boundary_share=row["boundaries"] / d, wicket_rate=row["wickets"] / d,
                                dot_share=row["Dot"] / d, extras_per_delivery=row["extras"] / d)
        summaries = {}
        for key, rows in self.innings.items():
            totals = [r["runs"] for r in rows]
            summaries[key] = {
                "n": len(rows), "mean": mean(totals), "sd": pstdev(totals),
                "quantiles": quantiles(totals), "wickets_mean": mean(r["wickets"] for r in rows),
                **{field: sum(r[field] for r in rows)
                   for field in ("centuries", "double_centuries", "early_end")},
                "350_plus": sum(r >= 350 for r in totals), "400_plus": sum(r >= 400 for r in totals),
                "innings": rows,
            }
        return {
            "match_ids": self.match_ids,
            "dates": [min(self.dates), max(self.dates)] if self.dates else [],
            "metrics": metrics, "innings": summaries,
            "chases": {band: dict(Counter(results), n=len(results))
                       for band, results in self.chases.items()},
            "nodes": {key: {"counts": dict(count), "n": count.total(),
                            "probabilities": {o: n / count.total() for o, n in count.items()}}
                      for key, count in self.counts.items()},
            "completed_over_runs": {phase: dict(hist) for phase, hist in self.overs.items()},
        }

    def extra_variants(self):
        return {phase: [list(event) + [n] for event, n in sorted(rows.items())]
                for phase, rows in self.extras.items()}


def audit_commentary(directory, fingerprints):
    """Cross-check delivery positions without imputing missing ICC metadata."""
    directory = Path(directory)
    source_hash = hashlib.sha256()
    matches, markers, discarded = [], Counter(), Counter()
    for file in sorted(directory.glob("*.csv")):
        source_hash.update(file.name.encode())
        source_hash.update(file.read_bytes())
        with file.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["Ball", "Runs", "Commentary"]:
                raise ValueError(f"Unexpected ICC commentary schema: {file}")
            # The scrape is newest first; a decrease marks an innings reset.
            innings, current, previous = [], {}, 0
            for row in reversed(list(reader)):
                marker = row["Runs"]
                markers[marker] += 1
                position = re.fullmatch(r"(\d{1,2})\.([1-6])", row["Ball"] or "")
                if not position:
                    discarded["no_regulation_ball_position"] += 1
                    continue
                ball = int(position[1]) * 6 + int(position[2])
                if ball < previous:
                    innings.append(current)
                    current = {}
                previous = ball
                if marker in {"•", "0", "1", "2", "3", "4", "5", "6", "W"}:
                    current[ball] = "0" if marker == "•" else marker
                else:
                    discarded["unmapped_for_fingerprint"] += 1
            innings.append(current)

        candidates = []
        for match_id, official in fingerprints.items():
            pairs = [(expected, observed) for left, right in zip(official, innings)
                     for pos, expected in left.items() if (observed := right.get(pos)) is not None]
            if len(pairs) >= 40:
                agreement = sum(expected == observed for expected, observed in pairs)
                if agreement / len(pairs) >= 0.85:
                    candidates.append((match_id, agreement, len(pairs)))
        matches.append({"icc_id": file.stem, "innings_segments": len(innings),
                        "matches": candidates})
    if not matches:
        raise ValueError(f"No ICC commentary CSVs in {directory}")
    return {
        "name": "ppai22/cwc-2019-ball-by-ball-dataset",
        "data_sha256_of_sorted_names_and_bytes": source_hash.hexdigest(),
        "files": len(matches), "commentary_rows": markers.total(), "run_markers": dict(markers),
        "crosswalk": matches, "fingerprint_exclusions": dict(discarded),
        "files_with_unique_cricsheet_match": sum(len(m["matches"]) == 1 for m in matches),
        "limitations": ("ICC scraped commentary is used to cross-check the structured 2019 data. "
                        "It has no match metadata, target revisions, or explicit dismissal fields. "
                        "Missing or ambiguous events are not imputed into the measured reference."),
    }


def build(paths, min_year, max_year, holdout_from, development_fold=None):
    train, holdout = Sample(), Sample()
    seen, sources = set(), []
    excluded = {key: [] for key in ("date_window", "format_gender_or_team_type", "duplicate",
                                    "reduced_or_adjusted", "incomplete", "super_overs")}
    for path in map(Path, paths):
        with ZipFile(path) as archive:
            files = [n for n in sorted(archive.namelist()) if n.endswith(".json")]
            sources.append({"filename": path.name, "sha256": digest(path), "json_documents": len(files)})
            for name in files:
                doc = json.loads(archive.read(name))
                info = doc["info"]
                match_id = Path(name).stem
                year = int(info["dates"][0][:4])
                innings = doc.get("innings", [])
                regulation = [i for i in innings if not i.get("super_over")]
                if min_year and year < min_year or max_year and year > max_year:
                    excluded["date_window"].append(match_id)
                elif info.get("match_type") != "ODI" or info.get("gender") != "male" or info.get("team_type") != "international":
                    excluded["format_gender_or_team_type"].append(match_id)
                elif match_id in seen:
                    excluded["duplicate"].append(match_id)
                elif (info.get("overs") != 50 or info.get("balls_per_over") != 6
                      or info.get("outcome", {}).get("method")
                      or any(i.get("target", {}).get("overs", 50) != 50 for i in regulation)):
                    excluded["reduced_or_adjusted"].append(match_id)
                elif (len(regulation) != 2 or any(not i.get("overs") for i in regulation)
                      or info.get("outcome", {}).get("result") in {"no result", "abandoned"}):
                    excluded["incomplete"].append(match_id)
                else:
                    seen.add(match_id)
                    excluded["super_overs"].extend([match_id] * (len(innings) - len(regulation)))
                    is_holdout = bool(holdout_from and year >= holdout_from)
                    if development_fold is not None and year == 2019:
                        is_holdout = int(hashlib.sha256(match_id.encode()).hexdigest(), 16) % 5 == development_fold
                    (holdout if is_holdout else train).count_match(match_id, doc)
    return train, holdout, {"sources": sources, "exclusions": dict(excluded),
                            "exclusion_counts": {key: len(ids) for key, ids in excluded.items()}}
