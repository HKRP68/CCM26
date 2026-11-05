"""Post-match coaching derived solely from recorded match facts.

Builds up to two turning points and one actionable tip per team. Missing
evidence is omitted; a trait activation is never presented as runs saved or
as the cause of a result. No database, randomness or simulation changes.
"""

import html
import logging
import math
import re

from engine.approach_modifiers import REPEAT_FROM
from services.impact_player import active_players

logger = logging.getLogger(__name__)


def _number(value, default=0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _wickets(fow, bpu):
    counts = {}
    for entry in fow or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 4:
            continue
        marker = str(entry[3])
        if marker.endswith("balls"):
            if not re.fullmatch(r"\d+ balls", marker):
                continue
            balls = int(_number(marker.split()[0]))
            over = math.ceil(balls / bpu)
        else:
            if not re.fullmatch(r"\d+(?:\.\d+)?", marker):
                continue
            whole, _, part = marker.partition(".")
            if _number(part) >= bpu:
                continue
            over = int(_number(whole)) + int(_number(part) > 0)
        if over > 0:
            counts[over] = counts.get(over, 0) + 1
    return counts


def _innings(state):
    bpu = 5 if state.get("ball_format") == "The100" else 6
    innings = []
    for prefix in (("inn1_", "") if state.get("innings") == 2 else ("",)):
        archived = bool(prefix)
        batting = state.get(prefix + "bat_team") if archived else state.get("bat_team_name")
        bowling = state.get(prefix + "bowl_team") if archived else state.get("bowl_team_name")
        if archived:
            batting = batting or state.get("inn1_team")
            bowling = bowling or state.get("bat_team_name")
        view = {
            "number": len(innings) + 1, "team": str(batting or ""),
            "bowling_team": str(bowling or ""),
            "team_id": state.get(prefix + "bat_team_id"),
            "bowling_team_id": state.get(prefix + "bowl_team_id"),
            "over_runs": state.get(prefix + "over_runs") or [],
            "approaches": state.get(prefix + "approach_log") or [],
            "momentum": state.get(prefix + "momentum_history") or [],
            "bat_xi": state.get(prefix + "bat_xi") or [],
            "bowl_xi": state.get(prefix + "bowl_xi") or [],
            "bat_stats": state.get(prefix + "bat_stats") or {},
            "bowl_stats": state.get(prefix + "bowl_stats") or {},
            "wickets": _wickets(state.get(prefix + "fow"), bpu),
            "bpu": bpu, "unit": "Set" if bpu == 5 else "Over",
            "total_units": max(1, int(_number(state.get("overs"), 20))),
        }
        if archived:
            if view["team_id"] is None:
                view["team_id"] = state.get("bowl_team_id")
            if view["bowling_team_id"] is None:
                view["bowling_team_id"] = state.get("bat_team_id")
        for field in ("over_runs", "approaches", "momentum", "bat_xi", "bowl_xi"):
            if not isinstance(view[field], (list, tuple)):
                view[field] = []
        for field in ("bat_xi", "bowl_xi"):
            view[field] = [player for player in view[field] if isinstance(player, dict)]
        for field in ("bat_stats", "bowl_stats"):
            if not isinstance(view[field], dict):
                view[field] = {}
        innings.append(view)
    return innings


def _turning_points(innings, chase_history):
    candidates = {}

    def add(view, over, score, detail):
        if score <= 0 or over < 1 or over > view["total_units"]:
            return
        key = (view["number"], over)
        if key not in candidates or candidates[key][0] < score:
            candidates[key] = (score, {
                "innings": view["number"], "over": over, "team": view["team"],
                "text": f"Innings {view['number']} · {view['team']} · {view['unit']} {over}: {detail}",
            })

    for view in innings:
        runs = [_number(value, None) for value in view["over_runs"]]
        known = [value for value in runs if value is not None]
        average = sum(known) / len(known) if known else 0
        for over in sorted(set(range(1, len(runs) + 1)) | set(view["wickets"])):
            value = runs[over - 1] if over <= len(runs) else None
            wickets = view["wickets"].get(over, 0)
            if value is None:
                if wickets:
                    add(view, over, 12 * wickets,
                        f"{wickets} wicket{'s' if wickets != 1 else ''} fell.")
                continue
            # Wickets and unusual overs provide a factual fallback for matches
            # that predate the momentum and chase-probability traces.
            weight = 12 * wickets + abs(value - average)
            if wickets or abs(value - average) >= 5:
                add(view, over, weight,
                    f"{value:g} runs, {wickets} wicket{'s' if wickets != 1 else ''}.")
        if view["number"] == 1:
            for i, (before, after) in enumerate(zip(view["momentum"], view["momentum"][1:]), 2):
                before, after = _number(before, None), _number(after, None)
                if before is None or after is None:
                    continue
                add(view, i, abs(after - before) * 10,
                    f"batting momentum moved from {before:+.1f} to {after:+.1f}.")

    chase = next((view for view in innings if view["number"] == 2), None)
    previous = None
    for entry in chase_history or []:
        if not isinstance(entry, dict) or not chase:
            continue
        chance = _number(entry.get("chasing"), -1)
        over = int(_number(entry.get("over")))
        if not 0 <= chance <= 100 or over <= 0:
            continue
        if previous and over == previous[0] + 1:
            add(chase, over, abs(chance - previous[1]),
                f"chase chance moved from {previous[1]:g}% to {chance:g}%.")
        previous = (over, chance)
    return [event for _, event in sorted(
        candidates.values(), key=lambda candidate: (-candidate[0],
                                                     candidate[1]["innings"],
                                                     candidate[1]["over"]))[:2]]


def _traits(state, player):
    if state.get("traits_enabled") is False:
        return set()
    traits = player.get("traits")
    if traits is None:
        traits = (state.get("_traits_cache") or {}).get(str(player.get("roster_id")), [])
    return {trait.get("effect_key") for trait in traits or [] if isinstance(trait, dict)}


def _team_tips(state, innings):
    candidates = {}

    def offer(team, priority, kind, text, team_id=None, innings_number=None):
        key = ("id", str(team_id)) if team_id is not None else ("innings", innings_number)
        if team and (key not in candidates or candidates[key][0] < priority):
            candidates[key] = (priority, {"team": team, "team_id": team_id,
                                          "kind": kind, "text": text})

    total = max(1, int(_number(state.get("overs"), 20)))
    death = max(1, total - 2)  # The trait engine's last-three-unit window.
    for view in innings:
        team = view["team"]
        logs = [entry for entry in view["approaches"] if isinstance(entry, dict)]
        reached_death = any(_number(runs, None) is not None
                            for runs in view["over_runs"][death - 1:])
        for player in active_players(view["bat_xi"]):
            traits = _traits(state, player)
            if not traits & {"bat_finisher", "elite_ice_finisher"}:
                continue
            if "bat_finisher" not in traits and view["number"] != 2:
                continue
            trait_name = "Finisher" if "bat_finisher" in traits else "Ice Finisher"
            stats = view["bat_stats"].get(str(player.get("roster_id")))
            if stats is None:
                stats = view["bat_stats"].get(player.get("roster_id"))
            if (reached_death and isinstance(stats, dict) and "balls" in stats
                    and _number(stats["balls"], -1) == 0 and not stats.get("out")):
                offer(team, 100, "unused_finisher",
                      f"{str(player.get('name') or 'Your finisher')[:80]} faced no balls although the innings "
                      f"reached the last three {view['unit'].lower()}s. Move them up the batting "
                      f"order so they have a chance to use their {trait_name} trait.",
                      view["team_id"], view["number"])

        for player in active_players(view["bowl_xi"]):
            if not _traits(state, player) & {"bowl_death", "bowl_yorker"}:
                continue
            rid = player.get("roster_id")
            spell = [entry for entry in logs if entry.get("bowler_rid") is not None
                     and str(entry["bowler_rid"]) == str(rid)]
            before = sum(_number(entry.get("balls")) for entry in spell
                         if 0 < _number(entry.get("over")) < death)
            from services.cipl_match import quota_for
            quota = quota_for({"overs": total}, player) * view["bpu"]
            if reached_death and before >= quota and not any(
                    _number(entry.get("over")) >= death for entry in spell):
                offer(view["bowling_team"], 90, "early_death_quota",
                      f"{str(player.get('name') or 'Your death bowler')[:80]} used their full bowling quota "
                      f"before the last three {view['unit'].lower()}s. Reserve at least one "
                      f"{view['unit'].lower()} for their death-over trait next match.",
                      view["bowling_team_id"], 3 - view["number"])

        for field, owner in (("bat", team), ("bowl", view["bowling_team"])):
            longest, run, last, previous_over = 0, 0, None, 0
            for entry in logs:
                pick, over = entry.get(field), int(_number(entry.get("over")))
                run = run + 1 if pick and pick == last and over == previous_over + 1 else int(bool(pick))
                longest = max(longest, run)
                last, previous_over = pick, over
            if longest >= REPEAT_FROM:
                offer(owner, 60, "repeated_approach",
                      f"You kept the same {'batting intent' if field == 'bat' else 'bowling plan'} "
                      f"for {longest} consecutive {view['unit'].lower()}s. Change it sooner "
                      "next match to stay less predictable.",
                      view["team_id"] if field == "bat" else view["bowling_team_id"],
                      view["number"] if field == "bat" else 3 - view["number"])

        stats = [value for value in view["bat_stats"].values() if isinstance(value, dict)]
        dots = sum(_number(value.get("dots")) for value in stats)
        balls = sum(_number(value.get("balls")) for value in stats)
        if balls >= 12 and dots >= 6 and dots / balls >= 0.4:
            offer(team, 50, "dot_pressure",
                  f"You faced {dots:g} dot balls from {balls:g} balls. Look for singles "
                  "and use a lower-risk batting choice when boundaries dry up.",
                  view["team_id"], view["number"])

        known_runs = [_number(runs, None) for runs in view["over_runs"]]
        pairs = [(a + b, i + 1) for i, (a, b) in enumerate(zip(known_runs, known_runs[1:]))
                 if a is not None and b is not None]
        if pairs:
            quiet_runs, start = min(pairs)
            offer(team, 1, "phase_review",
                  f"Your quietest two-{view['unit'].lower()} spell brought {quiet_runs:g} runs "
                  f"({view['unit'].lower()}s {start}–{start + 1}). Practice finding singles "
                  "through similar quiet spells next match.", view["team_id"], view["number"])
    return [entry for _priority, entry in candidates.values()]


def build_post_match_coaching(state):
    empty = {"turning_points": [], "tips": []}
    if not isinstance(state, dict):
        return empty
    try:
        innings = _innings(state)
        empty["turning_points"] = _turning_points(innings, state.get("chase_history"))
        empty["tips"] = _team_tips(state, innings)
    except Exception:
        logger.exception("could not build post-match coaching from this snapshot")
    return empty


def render_coaching_html(coaching, telegram=False):
    """Escape facts and names once for either Telegram or the offline report."""
    def esc(text, budget=None):
        raw = str(text or "")
        if not budget:
            return html.escape(raw, quote=True)
        escaped, used = [], 0
        for character in raw:
            part = html.escape(character, quote=True)
            if used + len(part) > budget:
                return "".join(escaped) + "…"
            escaped.append(part)
            used += len(part)
        return "".join(escaped)
    turns = (coaching or {}).get("turning_points") or []
    tips = (coaching or {}).get("tips") or []
    if not turns and not tips:
        return ""
    if telegram:
        lines = ["🧠 <b>Post-match coaching</b>", "<blockquote expandable>"]
        if turns:
            lines.append("<b>Turning points</b>")
            lines.extend(f"{i}. {esc(point['text'], 500)}" for i, point in enumerate(turns[:2], 1))
        if tips:
            lines.append("\n<b>Next match</b>")
            lines.extend(f"<b>{esc(tip['team'], 200)}:</b> {esc(tip['text'], 600)}" for tip in tips[:2])
        return "\n".join(lines) + "</blockquote>"
    body = []
    if turns:
        body.append("<h3>Turning points</h3><ol class='turns'>" + "".join(
            f"<li>{esc(point['text'])}</li>" for point in turns[:2]) + "</ol>")
    if tips:
        body.append("<h3>Next match</h3>" + "".join(
            f"<p><b>{esc(tip['team'])}:</b> {esc(tip['text'])}</p>" for tip in tips[:2]))
    return "".join(body)


async def send_post_match_coaching(bot, chat_id, state):
    """A coaching failure must never interrupt result delivery or cleanup."""
    try:
        message = render_coaching_html(build_post_match_coaching(state), telegram=True)
        if message:
            await bot.send_message(chat_id, message, parse_mode="HTML")
    except Exception:
        logger.exception("post-match coaching delivery failed")
