"""Read-only swap previews and legal bench suggestions for the bot and Mini App."""

import hashlib
import json
from html import escape

from services import chemistry, xi_rules


def _config(config):
    rules = chemistry.variety_rules(config)
    return {"chemistry_variety_target": rules["target"],
            "chemistry_variety_versions_json": rules["versions"]}


def roster_token(roster, config=None):
    """Identify the roster and scoring rules a person actually previewed."""
    fields = ("id", "parent_player_id", "name", "country", "category", "version",
              "rating", "bat_rating", "bowl_rating")
    payload = {
        "rules": _config(config),
        "roster": [[entry.id, getattr(entry, "order_position", None),
                    [getattr(player, field, None) for field in fields]]
                   for entry, player in roster],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _summary(players, config):
    if len(players) < chemistry.XI_SIZE:
        return None
    report = chemistry.calculate_role_report(players, config=config)
    keys = ("total", "total_max", "raw_points", "diversity", "diversity_countries",
            "variety", "variety_types", "variety_target")
    summary = {key: report[key] for key in keys}
    summary["roles"] = [{key: role[key] for key in
                         ("label", "category", "bonus", "max_bonus")}
                        for role in report["roles"]]
    summary["average_rating"] = round(sum(float(player.rating or 0)
                                          for player in players) / len(players), 1)
    return summary


def _card(roster, index):
    entry, player = roster[index]
    return {"roster_id": entry.id, "player_id": player.id,
            "position": index + 1, "name": player.name,
            "category": player.category, "country": player.country,
            "version": player.version or "Base", "rating": player.rating}


def summarize_xi(roster, config=None):
    return _summary([player for _, player in list(roster)[:chemistry.XI_SIZE]], _config(config))


def preview_swap(roster, first_id, second_id, config=None):
    """Simulate exchanging two owned roster rows without changing either row.

    Manual editing may leave an unfinished XI; expose its validation warnings.
    Recommendations below only include completely legal resulting XIs.
    """
    roster = list(roster)
    by_id = {entry.id: index for index, (entry, _) in enumerate(roster)}
    if first_id == second_id or first_id not in by_id or second_id not in by_id:
        raise ValueError("Choose two different players from your current roster.")
    first, second = by_id[first_id], by_id[second_id]
    if first >= chemistry.XI_SIZE > second:
        first, second = second, first
    config = _config(config)
    changed = list(roster)
    changed[first], changed[second] = changed[second], changed[first]
    before = _summary([player for _, player in roster[:chemistry.XI_SIZE]], config)
    after = _summary([player for _, player in changed[:chemistry.XI_SIZE]], config)
    _, errors = xi_rules.validate_roster_xi(changed)
    _, country_errors = chemistry.validate_country_rule(
        [player for _, player in changed[:chemistry.XI_SIZE]])
    # xi_rules also serves Telegram HTML. The public planner payload is plain text.
    errors = [error.replace("<b>", "").replace("</b>", "")
                   .replace("<i>", "").replace("</i>", "")
              for error in errors + country_errors]
    outgoing, incoming = _card(roster, first), _card(roster, second)
    token = hashlib.sha256(json.dumps([
        roster_token(roster, config), sorted([first_id, second_id])
    ]).encode()).hexdigest()
    return {"outgoing": outgoing, "incoming": incoming,
            "before": before, "after": after,
            "delta": after["total"] - before["total"] if before and after else None,
            "rating_delta": (incoming["rating"] or 0) - (outgoing["rating"] or 0),
            "membership_changed": first < chemistry.XI_SIZE <= second,
            "valid": not errors, "warnings": errors,
            "preview_token": token}


def recommend_bench_swaps(roster, limit=3, config=None):
    """The best legal swap for each bench card, ranked by chemistry then OVR.

    Show rating tradeoffs, never apply a recommendation automatically. Only
    positive displayed chemistry gains qualify, including cross-role swaps
    when the resulting XI satisfies all roster and country rules.
    """
    roster = list(roster)
    if len(roster) <= chemistry.XI_SIZE or limit <= 0:
        return []
    config = _config(config)
    suggestions = []
    for bench_entry, _ in roster[chemistry.XI_SIZE:]:
        choices = []
        for xi_entry, _ in roster[:chemistry.XI_SIZE]:
            preview = preview_swap(roster, xi_entry.id, bench_entry.id, config)
            if preview["valid"] and preview["delta"] > 0:
                choices.append(preview)
        if choices:
            suggestions.append(max(choices, key=lambda item: (
                item["delta"], item["after"]["raw_points"], item["rating_delta"],
                -item["outgoing"]["position"])))
    return sorted(suggestions, key=lambda item: (
        -item["delta"], -item["after"]["raw_points"], -item["rating_delta"],
        item["incoming"]["position"], item["outgoing"]["position"]))[:limit]


def apply_swap(roster, first_id, second_id):
    """Change positions only after the caller checks ownership, lock and token."""
    roster = list(roster)
    by_id = {entry.id: entry for entry, _ in roster}
    if first_id == second_id or first_id not in by_id or second_id not in by_id:
        raise ValueError("These players are no longer in your roster.")
    # Reads use contiguous roster order, including on older rows with gaps.
    for position, (entry, _) in enumerate(roster, 1):
        entry.order_position = position
    first, second = by_id[first_id], by_id[second_id]
    first.order_position, second.order_position = second.order_position, first.order_position


def render_swap_preview(preview):
    """Telegram HTML; the Mini App renders the same structured preview."""
    outgoing, incoming = preview["outgoing"], preview["incoming"]
    lines = ["🧪 <b>SWAP PREVIEW</b>",
             f"{escape(outgoing['name'])} ({escape(outgoing['version'])}) ↔ "
             f"{escape(incoming['name'])} ({escape(incoming['version'])})", ""]
    before, after = preview["before"], preview["after"]
    if before and after:
        lines.append(f"Chemistry: <b>{before['total']} → {after['total']}/100 "
                     f"({preview['delta']:+d})</b>")
        lines.append(f"Card Variety: {before['variety']} → {after['variety']}/10 "
                     f"({before['variety_types']} → {after['variety_types']} types)")
        lines.append(f"Country Diversity: {before['diversity']} → {after['diversity']}/10")
        lines.append(f"Average OVR: {before['average_rating']:g} → {after['average_rating']:g}")
        changes = [f"{old['label']} +{old['bonus']} → +{new['bonus']}"
                   for old, new in zip(before["roles"], after["roles"])
                   if old["bonus"] != new["bonus"]]
        if changes:
            lines.append("Unit boosts: " + "; ".join(changes))
    else:
        lines.append("A full XI is needed to calculate chemistry.")
    if not preview["membership_changed"]:
        lines.append("This only changes roster order; XI chemistry stays the same.")
    if preview["warnings"]:
        lines.append("\n⚠️ <b>XI still needs attention before a match:</b>")
        lines.extend(f"• {escape(warning)}" for warning in preview["warnings"][:4])
    lines.append("\nConfirm to apply this swap, or cancel to keep your XI.")
    return "\n".join(lines)


def render_recommendations(suggestions):
    """A compact HTML section for /cmuchem and /chemhelp."""
    if not suggestions:
        return "No legal bench swap improves your chemistry right now."
    lines = ["💡 <b>Bench cards that improve chemistry</b>"]
    for index, preview in enumerate(suggestions, 1):
        outgoing, incoming = preview["outgoing"], preview["incoming"]
        lines.append(
            f"{index}. {escape(incoming['name'])} ({escape(incoming['version'])}) "
            f"for {escape(outgoing['name'])}: <b>+{preview['delta']} CHEM</b> "
            f"({preview['before']['total']} → {preview['after']['total']}/100), "
            f"OVR {preview['rating_delta']:+g}")
    lines.append("Tap a suggestion to preview it before making any change.")
    return "\n".join(lines)
