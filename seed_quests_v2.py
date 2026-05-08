"""Bulk-import quest definitions from the provided markdown table.

Run once via: `python seed_quests_v2.py`. Idempotent — won't duplicate quests
that already exist (matched by name + quest_type).

For each quest we infer:
  - target_count from the description ("3 wickets" → 3)
  - event_key from keywords in the description
  - reward_points based on type (Daily=10, Monthly=50, Bonus=25 by default)

Quests whose objective the engine can't auto-track get event_key="manual" —
admin can still see them, edit them, and manually bump progress per user.
"""

import re
import logging
import sys
from datetime import datetime
from typing import List, Tuple

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# Event key inference rules. First match wins.
# ════════════════════════════════════════════════════════════════════

# (regex pattern, event_key, target_extractor) — order matters!
EVENT_RULES = [
    # ── Multi-stat (all-rounder) — match before single-stat rules
    (r"score\s+(\d+)\+?\s+runs?\s+and\s+take\s+(\d+)\+?\s+wickets?", "manual", "first"),

    # ── Hat-tricks (specific)
    (r"hat[- ]?trick", "hattrick", lambda d: 1),

    # ── Centuries
    (r"(\d+)\+?\s+centur", "hundred", "first"),
    (r"\bcentury\b", "hundred", lambda d: 1),
    (r"score\s+a\s+century", "hundred", lambda d: 1),
    (r"double\s+century", "hundred", lambda d: 1),
    (r"triple\s+century", "hundred", lambda d: 1),

    # ── Specific wicket counts in a single match (use wickets_in_match key)
    (r"take\s+(\d+)\+?\s+wickets?\s+in\s+(a\s+match|one\s+game|the\s+match|this\s+match)",
     "wickets_in_match", "first"),
    (r"take\s+(?:at\s+least\s+)?(\d+)\+?\s+wickets?\s+with\s+your\s+bowlers\s+in\s+one\s+game",
     "wickets_in_match", "first"),
    (r"take\s+(?:at\s+least\s+)?(\d+)\+?\s+wickets?\s+(in\s+a|in\s+one|with)",
     "wickets_in_match", "first"),

    # ── Cumulative wickets (period — daily/monthly aggregate)
    (r"take\s+(\d+)\+?\s+wickets", "wickets_taken", "first"),

    # ── Runs (specific run amounts in a single innings/match)
    (r"score\s+(\d+)\+?\s+(?:or\s+more\s+)?runs?\s+in\s+a\s+single\s+innings",
     "runs_in_innings", "first"),
    (r"score\s+(\d+)\+?\s+(?:or\s+more\s+)?runs?\s+in\s+(?:a\s+single|one)\s+(t20\s+)?match",
     "runs_in_innings", "first"),
    (r"score\s+50\s+or\s+more\s+runs", "fifty", lambda d: 1),
    (r"half[- ]?century", "fifty", lambda d: 1),
    (r"quick\s+fifty", "fifty", lambda d: 1),

    # ── Total runs in a period
    (r"score\s+([\d,]+)\+?\s+runs?\s+(in\s+the\s+month|across|in\s+t20|this\s+month)", "runs_scored", "first"),

    # ── Sixes
    (r"hit\s+(\d+)\+?\s+sixes?\s+(in\s+a\s+single|in\s+one\s+innings|in\s+one\s+match)",
     "sixes_in_match", "first"),
    (r"hit\s+(\d+)\+?\s+sixes", "sixes_hit", "first"),
    (r"(\d+)\+?\s+sixes\s+in\s+(a\s+day|the\s+entire\s+month|t20\s+matches\s+this\s+month)",
     "sixes_hit", "first"),

    # ── Boundaries
    (r"hit\s+(\d+)\+?\s+(?:or\s+more\s+)?boundaries?\s+\(.+?\)\s+in\s+(a\s+single|one)",
     "boundaries_in_match", "first"),
    (r"(\d+)\+?\s+boundaries\s+\(.+?\)\s+in\s+t20", "boundaries_hit", "first"),
    (r"hit\s+(\d+)\+?\s+(?:or\s+more\s+)?boundaries", "boundaries_hit", "first"),

    # ── Wins / matches
    (r"win\s+(\d+)\+?\s+t?20?\s*matches?", "match_won", "first"),
    (r"win\s+(\d+)\+?\s+matches", "match_won", "first"),
    (r"win\s+a\s+match", "match_won", lambda d: 1),
    (r"win\s+any\s+(?:online|practice|\w+)\s+match", "match_won", lambda d: 1),
    (r"win\s+a\s+super\s+over", "manual", lambda d: 1),

    # ── Untracked: chases, powerplay specifics, death over specifics, etc.
    (r"chase|powerplay|death\s+over|yorker|maiden|catch|run[- ]out|economy|strike\s+rate|not\s+out|opener|middle[- ]order|super\s+over|fielding|finisher",
     "manual", lambda d: 1),

    # ── Team-bound fallback — manual since we'd need team-specific tracking
    (r"\bteam\b", "manual", lambda d: 1),
]


def _infer_event_key_and_target(description: str) -> Tuple[str, int]:
    """Try to derive (event_key, target_count) from the quest description."""
    desc_lower = description.lower()
    for pattern, event_key, extractor in EVENT_RULES:
        m = re.search(pattern, desc_lower)
        if m:
            if callable(extractor):
                target = extractor(desc_lower)
            elif extractor == "first":
                # Use the first capturing group
                if m.groups():
                    raw = m.group(1).replace(",", "")
                    try:
                        target = int(raw)
                    except ValueError:
                        target = 1
                else:
                    target = 1
            else:
                target = 1
            return event_key, max(1, target)
    return "manual", 1


# ════════════════════════════════════════════════════════════════════
# Reward defaults by type
# ════════════════════════════════════════════════════════════════════

REWARD_DEFAULTS = {
    "daily":   {"points": 10, "coins": 500,  "gems": 0},
    "monthly": {"points": 50, "coins": 2000, "gems": 5},
    "bonus":   {"points": 25, "coins": 1500, "gems": 2},
}


def _emoji_for(quest_type: str, name: str) -> str:
    n = name.lower()
    if "century" in n or "100" in n: return "💯"
    if "six" in n: return "🎯"
    if "wicket" in n: return "🎳"
    if "boundary" in n or "boundaries" in n: return "💥"
    if "win" in n or "champion" in n or "victor" in n: return "🏆"
    if "powerplay" in n: return "⚡"
    if "death" in n: return "💀"
    if "all[- ]?round" in n: return "🌟"
    if "comeback" in n or "chase" in n: return "🔥"
    if "yorker" in n: return "🎯"
    if "economy" in n: return "🛡️"
    if "fielding" in n or "catch" in n: return "🧤"
    if "team" in n: return "👥"
    if "hat[- ]?trick" in n: return "🎩"
    if "maiden" in n: return "🚫"
    if "unbeaten" in n: return "🛡️"
    if quest_type == "daily": return "📅"
    if quest_type == "monthly": return "📆"
    return "✨"


def parse_markdown_quest_table(md_path: str) -> List[dict]:
    """Parse the markdown file into a list of quest dicts."""
    quests = []
    with open(md_path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) < 4:
                continue
            qtype, name, desc, reset = parts[0], parts[1], parts[2], parts[3]
            # Skip header / divider / empty rows
            if not qtype or qtype.lower() in ("quest type", "------------"):
                continue
            if qtype.lower() not in ("daily", "monthly", "bonus"):
                continue
            if not name or not desc:
                continue
            quests.append({
                "name": name,
                "description": desc,
                "quest_type": qtype.lower(),
                "reset": reset,
            })
    return quests


def import_quests(session, md_path: str, *, dry_run: bool = False) -> dict:
    """Import quests from markdown. Returns dict with counts."""
    from models import Quest

    rows = parse_markdown_quest_table(md_path)
    inserted = 0
    skipped_existing = 0
    auto_tracked = 0
    manual = 0

    # Load existing quest names to avoid duplicates (case-insensitive on name+type)
    existing_keys = set()
    for q in session.query(Quest).all():
        existing_keys.add((q.name.lower(), q.quest_type))

    for row in rows:
        event_key, target = _infer_event_key_and_target(row["description"])
        if event_key == "manual":
            manual += 1
        else:
            auto_tracked += 1

        defaults = REWARD_DEFAULTS.get(row["quest_type"], REWARD_DEFAULTS["daily"])

        # Bonus quests: remap to either daily or monthly based on the reset col
        actual_type = row["quest_type"]
        if actual_type == "bonus":
            actual_type = "monthly" if "30" in row["reset"] else "daily"

        # Dedupe key uses the REMAPPED type so re-imports stay idempotent
        # even when bonus-type quests exist in the DB.
        key = (row["name"].lower(), actual_type)
        if key in existing_keys:
            skipped_existing += 1
            # Roll back the auto/manual counter we incremented
            if event_key == "manual":
                manual -= 1
            else:
                auto_tracked -= 1
            continue

        q = Quest(
            name=row["name"],
            description=row["description"],
            quest_type=actual_type,
            event_key=event_key,
            target_count=target,
            reward_points=defaults["points"],
            reward_coins=defaults["coins"],
            reward_gems=defaults["gems"],
            is_active=True,
            emoji=_emoji_for(row["quest_type"], row["name"]),
            sort_order=inserted * 10,
            created_at=datetime.utcnow(),
        )
        if not dry_run:
            session.add(q)
            existing_keys.add(key)
        inserted += 1

    if not dry_run:
        session.commit()

    return {
        "total_in_file": len(rows),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "auto_tracked": auto_tracked,
        "manual": manual,
    }


def main():
    if len(sys.argv) > 1:
        md_path = sys.argv[1]
    else:
        md_path = "/mnt/user-data/uploads/tableConvert_com_34tukt.md"

    from database import SessionLocal, init_db
    init_db()
    session = SessionLocal()
    try:
        result = import_quests(session, md_path)
        print(f"  Total in file:      {result['total_in_file']}")
        print(f"  Inserted:           {result['inserted']}")
        print(f"  Skipped (existing): {result['skipped_existing']}")
        print(f"  Auto-tracked:       {result['auto_tracked']}")
        print(f"  Manual-only:        {result['manual']}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
