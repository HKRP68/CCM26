"""Rating rules — "at least N players rated X or lower".

One rule shape shared by the Franchise Auction (a squad must end up holding at
least N players at or under a rating) and the Challenge League tournament (a
Playing XI must field at least N of them). It exists so a competition can keep
the cheap end of the pool in play: without it every side buys, or picks, the
top of the ratings and nothing else.

Stored as JSON on the owning row::

    [{"max_rating": 83, "min_players": 4}, {"max_rating": 80, "min_players": 2}]

Several rules may stand at once and each is checked on its own. They nest
naturally — a player rated 79 counts toward both "≤83" and "≤80" — so there is
no need to sum them.

No database and no Telegram here, so every function is directly testable.
"""

import json

MIN_CAP = 1
MAX_CAP = 99


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize(rules, *, cap_players=None):
    """Clean a list of rule dicts into canonical form.

    Rows with a rating outside 1–99 or a count under 1 are dropped (a count of
    0 is "no rule", not a rule). Two rows with the same cap keep the larger
    count. ``cap_players`` clamps every count — a rule asking for more players
    than a squad or an XI can hold is only ever unsatisfiable.
    Returns the rules sorted by cap, highest first.
    """
    merged = {}
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        cap = _as_int(rule.get("max_rating"))
        need = _as_int(rule.get("min_players"))
        if cap is None or need is None or need < 1:
            continue
        if not MIN_CAP <= cap <= MAX_CAP:
            continue
        if cap_players is not None:
            need = min(need, max(0, int(cap_players)))
            if need < 1:
                continue
        merged[cap] = max(need, merged.get(cap, 0))
    return [{"max_rating": cap, "min_players": need}
            for cap, need in sorted(merged.items(), reverse=True)]


def parse(text):
    """Read stored JSON into canonical rules; anything malformed reads as none."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return normalize(data)


def dump(rules):
    """Serialise rules for storage, or ``None`` when there are none."""
    rules = normalize(rules)
    return json.dumps(rules, separators=(",", ":")) if rules else None


def describe(rule):
    """``"Min 4 players rated ≤83"``."""
    need = int(rule["min_players"])
    noun = "player" if need == 1 else "players"
    return f"Min {need} {noun} rated ≤{int(rule['max_rating'])}"


def count_at_or_below(ratings, cap):
    """How many of ``ratings`` are at or under ``cap``. Unknown ratings don't count."""
    return sum(1 for r in ratings if r is not None and int(r) <= int(cap))


def progress(rules, ratings):
    """``[(rule, have)]`` — how far a squad is toward each rule."""
    ratings = list(ratings)
    return [(rule, count_at_or_below(ratings, rule["max_rating"])) for rule in rules]


def shortfalls(rules, ratings, slots_left):
    """Rules a squad can no longer meet with ``slots_left`` more signings.

    ``ratings`` is the squad *including* the signing being checked. Returns a
    list of ``(rule, owed)`` for every rule whose outstanding count is larger
    than the room left to fill it.
    """
    ratings = list(ratings)
    out = []
    for rule in rules:
        owed = rule["min_players"] - count_at_or_below(ratings, rule["max_rating"])
        if owed > max(0, int(slots_left)):
            out.append((rule, owed))
    return out


def xi_error(rules, ratings):
    """``""`` when a Playing XI meets every rule, else the first failure message."""
    ratings = list(ratings)
    for rule in rules:
        have = count_at_or_below(ratings, rule["max_rating"])
        if have < rule["min_players"]:
            return (f"{describe(rule)} required in XI (you have {have})")
    return ""


def parse_command(args):
    """Parse ``/…ratingrule`` arguments.

    Returns ``(action, cap, need)`` where action is ``"list"``, ``"set"``,
    ``"off"`` or ``"clear"``; raises ``ValueError`` with a usage hint otherwise.
    """
    args = [a.strip() for a in (args or ()) if a and a.strip()]
    if not args:
        return "list", None, None
    head = args[0].lower().lstrip("≤<=")
    if head in ("clear", "none", "reset"):
        return "clear", None, None
    if head in ("off", "remove", "del", "delete"):
        if len(args) < 2 or _as_int(args[1].lstrip("≤<=")) is None:
            raise ValueError("Name the rating to remove, e.g. off 83")
        return "off", _as_int(args[1].lstrip("≤<=")), None
    cap = _as_int(head)
    need = _as_int(args[1]) if len(args) > 1 else None
    if cap is None or need is None:
        raise ValueError("Give a rating and a count, e.g. 83 4")
    if not MIN_CAP <= cap <= MAX_CAP:
        raise ValueError(f"Rating must be {MIN_CAP}–{MAX_CAP}.")
    if need < 0:
        raise ValueError("The count can't be negative.")
    return ("off", cap, None) if need == 0 else ("set", cap, need)


def apply_command(rules, action, cap, need):
    """Return the rule list after a parsed command (``list`` returns it unchanged)."""
    rules = [dict(r) for r in rules or ()]
    if action == "clear":
        return []
    if action == "off":
        return [r for r in rules if r["max_rating"] != cap]
    if action == "set":
        rules = [r for r in rules if r["max_rating"] != cap]
        rules.append({"max_rating": cap, "min_players": need})
        return normalize(rules)
    return rules
