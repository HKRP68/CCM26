"""Import the SimCricketX commentary pack into the CommentaryEntry table.

The SimCricketX project ships a rich commentary pack at
``data/commentary_pack.json`` (100+ templates per event plus macro
"narratives"). This bot already has a commentary system backed by the
``CommentaryEntry`` table and served through ``services/commentary_service.py``
(``pick_commentary`` is called by the live match flow). This script converts the
SimCricketX pack into this bot's ``{event_key: [lines]}`` shape and feeds it
through the existing ``commentary_service.bulk_import`` so no parallel system is
introduced.

Run once via:  ``python import_simx_commentary.py [path/to/commentary_pack.json]``
Add ``--replace`` to wipe existing lines for the affected event keys first.
Idempotent by default — re-runs skip lines that already exist (matched by
event_key + text), exactly like the admin bulk importer.
"""

import json
import os
import re
import sys
import logging

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# SimCricketX event key  →  this bot's event key
# (this bot's keys come from services.commentary_service.list_event_keys)
# ════════════════════════════════════════════════════════════════════
EVENT_KEY_MAP = {
    "boundary_four": "four",
    "boundary_six":  "six",
    "wicket_bowled": "wicket_bowled",
    # wicket_caught is split by keyword below into keeper/fielder
    "wicket_lbw":     "wicket_lbw",
    "wicket_run_out": "wicket_runOut",
    "wicket_stumped": "wicket_stumped",
    "dot":      "dot",
    "single":   "one",
    "double":   "two",
    "three":    "three",
    "wide":     "wide",
    "noball":   "no_ball",
    # This bot only emits legbye extras, not byes; do not fold bye-only lines
    # into extras or live leg-bye balls can describe keeper byes.
    "legbyes":  "extras",
    "free_hit": "free_hit",
}

# Macro narratives that map cleanly onto a per-ball event key. Only milestone
# narratives are imported because the rest rely on {team}/{fielding_team}
# placeholders the renderer does not substitute.
NARRATIVE_KEY_MAP = {
    "milestone_50":  "milestones",
    "milestone_100": "milestones",
}

# Placeholders the renderer (_render in services.commentary_service) supports.
SUPPORTED_PLACEHOLDERS = {"{batsman}", "{bowler}", "{fielder}", "{keeper}", "{runs}"}

_PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")


def _normalise(text):
    """Map SimCricketX placeholders to this bot's and tidy whitespace."""
    text = (text or "").replace("{batter}", "{batsman}")
    return " ".join(text.split()).strip()


def _has_unsupported_placeholder(text):
    return any(p not in SUPPORTED_PLACEHOLDERS for p in _PLACEHOLDER_RE.findall(text))


def _lines_from(items):
    """Yield raw text from a list of pack entries (dicts with 'text', or str)."""
    for item in items or []:
        if isinstance(item, dict):
            yield item.get("text", "")
        elif isinstance(item, str):
            yield item


def _item_text(item):
    if isinstance(item, dict):
        return item.get("text", "")
    if isinstance(item, str):
        return item
    return ""


def _item_tags(item):
    if not isinstance(item, dict):
        return []
    return [str(tag).lower() for tag in item.get("tags") or []]


def _is_boundary_free_hit(item):
    """Return True only for free-hit templates compatible with a 4/6 result."""
    tags = set(_item_tags(item))
    if tags:
        return bool(tags & {"four", "six", "boundary", "rope", "maximum"})

    text = _item_text(item).lower()
    if any(blocked in text for blocked in (
        "dot ball", "swings and misses", "swing and miss", "can't take advantage",
        "cannot take advantage", "misses", "no run",
    )):
        return False
    return bool(re.search(r"\b(four|six|boundary|rope|maximum)\b", text))


def convert_pack(pack):
    """Convert a loaded SimCricketX commentary pack into {event_key: [lines]}.

    Pure function (no DB) so it can be unit-tested. Lines are normalised and any
    line containing a placeholder this bot can't render is dropped.
    """
    out = {}

    def add(event_key, raw_text):
        text = _normalise(raw_text)
        if not text or _has_unsupported_placeholder(text):
            return
        bucket = out.setdefault(event_key, [])
        if text not in bucket:
            bucket.append(text)

    events = pack.get("events", {})
    for src_key, items in events.items():
        if src_key == "wicket_caught":
            # Route keeper-flavoured lines to the keeper key, the rest to fielder.
            for raw in _lines_from(items):
                low = (raw or "").lower()
                target = "wicket_caught_keeper" if "keeper" in low else "wicket_caught_fielder"
                add(target, raw)
            continue
        dest = EVENT_KEY_MAP.get(src_key)
        if not dest:
            logger.info("Skipping unmapped event key: %s", src_key)
            continue
        if src_key == "free_hit":
            for item in items or []:
                if _is_boundary_free_hit(item):
                    add(dest, _item_text(item))
            continue
        for raw in _lines_from(items):
            add(dest, raw)

    narratives = pack.get("narratives", {})
    for src_key, dest in NARRATIVE_KEY_MAP.items():
        for raw in _lines_from(narratives.get(src_key)):
            add(dest, raw)

    return out


def _default_pack_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "data", "commentary_pack.json")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    args = [a for a in sys.argv[1:] if a != "--replace"]
    replace = "--replace" in sys.argv
    pack_path = args[0] if args else _default_pack_path()

    with open(pack_path, "r", encoding="utf-8") as f:
        pack = json.load(f)

    parsed = convert_pack(pack)
    total_lines = sum(len(v) for v in parsed.values())
    print(f"Converted {total_lines} lines across {len(parsed)} event keys "
          f"from {pack_path}")
    for key in sorted(parsed):
        print(f"  {key}: {len(parsed[key])}")

    from database import SessionLocal, init_db
    from services import commentary_service

    init_db()
    session = SessionLocal()
    try:
        added, skipped = commentary_service.bulk_import(session, parsed, replace=replace)
        session.commit()
        print(f"\nImport complete — added: {added}, skipped (duplicate/empty): {skipped}"
              f"{' [replace mode]' if replace else ''}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
