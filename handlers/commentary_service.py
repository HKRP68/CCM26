"""Live Commentary — picks a random commentary line per event.

Event keys (standard set, matches the user's .py format):
  dot, one, two, three, four, six,
  wicket_bowled, wicket_caught_fielder, wicket_caught_keeper,
  wicket_lbw, wicket_stumped,
  extras, milestones, general

The bot calls pick_commentary(event_key, batsman, bowler, fielder, keeper, runs)
to get a single rendered line. If no commentary exists for the event, returns None.

Admin can CRUD lines via the website + bulk-import a Python dict file.
"""

import logging
import random
import re
import ast

from models import CommentaryEntry

logger = logging.getLogger(__name__)


# Default fielder/keeper names used when context doesn't supply them
DEFAULT_FIELDER = "the fielder"
DEFAULT_KEEPER = "the keeper"


def pick_commentary(session, event_key, batsman="", bowler="",
                    fielder=DEFAULT_FIELDER, keeper=DEFAULT_KEEPER, runs=0):
    """Pick a random active commentary line for the given event.
    Returns formatted string, or None if no entries exist.
    """
    try:
        rows = (session.query(CommentaryEntry)
                .filter(CommentaryEntry.event_key == event_key,
                        CommentaryEntry.is_active == True).all())
        if not rows:
            return None
        # Weighted choice
        weights = [max(1, r.weight or 1) for r in rows]
        chosen = random.choices(rows, weights=weights, k=1)[0]
        text = chosen.text or ""
        # Replace placeholders. Use safe replacement (no .format because of {} in
        # admin-entered strings that may contain unescaped braces)
        text = text.replace("{batsman}", str(batsman or ""))
        text = text.replace("{bowler}", str(bowler or ""))
        text = text.replace("{fielder}", str(fielder or DEFAULT_FIELDER))
        text = text.replace("{keeper}", str(keeper or DEFAULT_KEEPER))
        text = text.replace("{runs}", str(runs or 0))
        return text.strip()
    except Exception:
        logger.exception(f"pick_commentary failed for event {event_key}")
        return None


def list_event_keys():
    """Standard set of event keys the engine fires for."""
    return [
        "dot", "one", "two", "three", "four", "six",
        "wicket_bowled", "wicket_caught_fielder", "wicket_caught_keeper",
        "wicket_lbw", "wicket_stumped", "wicket_runOut",
        "extras", "milestones", "general",
    ]


def parse_commentary_py(text):
    """Parse uploaded .py file content into {event_key: [lines, ...]}.

    Looks for a dict literal (typically named 'cricket_commentary'). Uses
    ast.literal_eval for safety — only literals are evaluated, no code runs.

    Returns: dict[event_key -> list[str]]. Raises ValueError on parse failure.
    """
    # Find any dict literal that looks like {key: [..]}
    # Strategy: try to parse the whole file as a Python module, find a Dict assignment
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise ValueError(f"Couldn't parse file as Python: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Dict):
                # Convert to literal — must be all str keys + list-of-str values
                try:
                    parsed = ast.literal_eval(value)
                except (ValueError, SyntaxError) as e:
                    continue
                if isinstance(parsed, dict):
                    out = {}
                    for k, v in parsed.items():
                        if isinstance(k, str) and isinstance(v, list):
                            out[k] = [str(x).strip() for x in v if isinstance(x, str) and x.strip()]
                    if out:
                        return out
    raise ValueError("No commentary dictionary found. Expected a top-level Python dict like cricket_commentary = {\"dot\": [...], ...}")


def bulk_import(session, parsed_dict, replace=False):
    """Import lines from a parsed dict.

    Args:
      parsed_dict: {event_key -> [lines, ...]}
      replace: if True, deletes all existing entries for affected keys first

    Returns: (added, skipped) counts.
    """
    added = 0
    skipped = 0
    if replace:
        keys = list(parsed_dict.keys())
        if keys:
            (session.query(CommentaryEntry)
             .filter(CommentaryEntry.event_key.in_(keys)).delete(synchronize_session=False))
            session.flush()

    # Build set of existing (key, text) to avoid duplicates when not replacing
    existing = set()
    if not replace:
        rows = session.query(CommentaryEntry.event_key, CommentaryEntry.text).all()
        existing = {(r[0], r[1]) for r in rows}

    for event_key, lines in parsed_dict.items():
        for line in lines:
            line = line.strip()
            if not line:
                skipped += 1
                continue
            if (not replace) and (event_key, line) in existing:
                skipped += 1
                continue
            entry = CommentaryEntry(event_key=event_key, text=line, is_active=True, weight=1)
            session.add(entry)
            added += 1

    session.flush()
    return added, skipped


def get_stats(session):
    """Returns dict of {event_key: count_active} for admin overview."""
    from sqlalchemy import func
    rows = (session.query(CommentaryEntry.event_key, func.count(CommentaryEntry.id))
            .filter(CommentaryEntry.is_active == True)
            .group_by(CommentaryEntry.event_key).all())
    return {r[0]: r[1] for r in rows}


def export_as_py(session):
    """Generate a .py file content with the current commentary."""
    rows = (session.query(CommentaryEntry)
            .filter(CommentaryEntry.is_active == True)
            .order_by(CommentaryEntry.event_key, CommentaryEntry.id).all())
    by_key = {}
    for r in rows:
        by_key.setdefault(r.event_key, []).append(r.text)

    lines = ["# Cricket commentary — exported from admin panel",
             "# Format: cricket_commentary = {event_key: [lines]}",
             "",
             "cricket_commentary = {"]
    for key in list_event_keys():
        if key not in by_key:
            continue
        lines.append(f'    "{key}": [')
        for t in by_key[key]:
            # Escape any double-quotes
            escaped = t.replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'        "{escaped}",')
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)
