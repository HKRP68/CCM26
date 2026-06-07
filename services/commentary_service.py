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


# ═══════════════════════════════════════════════════════════════════════
# BUILT-IN FALLBACK COMMENTARY (UnderCover /cric voice)
# ═══════════════════════════════════════════════════════════════════════
# Used when the admin DB has no active lines for an event. This guarantees
# cinematic commentary out-of-the-box; admins can still override per event.
FALLBACK_LINES = {
    "dot": [
        "{bowler} keeps it tight — {batsman} can't get it away. Dot ball.",
        "Beaten! {batsman} pushes at it but finds only the fielder. No run.",
        "Solid defence from {batsman}. Watchful stuff.",
    ],
    "one": [
        "Tucked away by {batsman} for a quick single.",
        "{batsman} works it into the gap and scampers through for one.",
    ],
    "two": [
        "Driven into the outfield — {batsman} comes back for the second.",
        "Good running between the wickets, {batsman} picks up a couple.",
    ],
    "three": [
        "Into the gap and they're running hard — three to {batsman}!",
        "{batsman} finds the deep fielder and turns it into three.",
    ],
    "four": [
        "FOUR! {batsman} times the {bowler} delivery beautifully through the gap!",
        "Cracked away! {batsman} finds the rope with a gorgeous stroke. FOUR runs!",
        "That's racing away! {batsman} pierces the field and beats the dive. FOUR!",
    ],
    "six": [
        "🚀 MASSIVE! {batsman} stands tall and launches {bowler} into the stands for SIX!",
        "💥 That is HUGE! {batsman} clears the rope with ease off {bowler}. Maximum!",
        "Smacked! {batsman} picks up the length early and deposits it for SIX!",
    ],
    "wicket_bowled": [
        "🎯 CLEAN BOWLED! {bowler} produces a peach — {batsman}'s stumps are shattered!",
        "Timber! {bowler} sneaks through the gate and {batsman} has to go!",
    ],
    "wicket_lbw": [
        "🦵 That's plumb! {bowler} traps {batsman} dead in front. LBW — given!",
        "Up goes the finger! {batsman} is caught on the crease, {bowler} strikes LBW!",
    ],
    "wicket_caught_fielder": [
        "🙌 Caught! {batsman} skies it and {fielder} settles under it off {bowler}.",
        "Straight down the throat! {batsman} picks out {fielder}. {bowler} celebrates!",
    ],
    "wicket_caught_keeper": [
        "🧤 Edged and taken! {keeper} pouches it cleanly off {bowler}. {batsman} walks.",
        "Thin nick! {keeper} does the rest. {bowler} has his man, {batsman} is gone!",
    ],
    "wicket_stumped": [
        "⚡ Stumped! {batsman} is out of the crease and {keeper} is lightning off {bowler}!",
        "Done by the flight! {keeper} whips the bails — {batsman} is stranded!",
    ],
    "wicket_runOut": [
        "🏃 RUN OUT! Disastrous mix-up and {batsman} is short of the crease!",
        "Direct hit! {batsman} never made the ground. Brilliant fielding!",
    ],
    "extras": [
        "Strays down the side — that'll be an extra.",
        "Loose from {bowler}, and the batting side cashes in with an extra.",
    ],
    "wide": [
        "↔️ Wide! {bowler} drifts too far across and the umpire signals it.",
        "Down the leg side from {bowler} — wided by the umpire.",
    ],
    "no_ball": [
        "🚫 NO-BALL! {bowler} oversteps — and it's a FREE HIT coming up!",
        "Overstepped! {bowler} gives away a no-ball and the batting side a free hit.",
    ],
    "free_hit": [
        "🎉 FREE HIT and {batsman} makes it count — that's dispatched!",
        "No fear on the free hit — {batsman} swings hard off {bowler} and cashes in!",
    ],
    "mystery": [
        "🌀 MYSTERY BALL does the trick! {bowler} bamboozles {batsman} completely!",
        "Out of the back of the hand! {batsman} has no clue and {bowler} strikes!",
    ],
    "milestones": [
        "What an innings from {batsman} — the crowd is on its feet!",
    ],
    "general": [
        "{bowler} runs in to {batsman}...",
    ],
}


def _render(text, batsman, bowler, fielder, keeper, runs):
    """Substitute placeholders safely (no str.format — admin lines may have {})."""
    text = (text or "")
    text = text.replace("{batsman}", str(batsman or ""))
    text = text.replace("{bowler}", str(bowler or ""))
    text = text.replace("{fielder}", str(fielder or DEFAULT_FIELDER))
    text = text.replace("{keeper}", str(keeper or DEFAULT_KEEPER))
    text = text.replace("{runs}", str(runs or 0))
    return text.strip()


def _render_random_line(lines, batsman, bowler, fielder, keeper, runs):
    if not lines:
        return None
    return _render(random.choice(lines), batsman, bowler, fielder, keeper, runs)


def _fallback_commentary(event_key, batsman, bowler, fielder, keeper, runs):
    return _render_random_line(
        FALLBACK_LINES.get(event_key), batsman, bowler, fielder, keeper, runs)


def build_commentary_picker(session):
    """Return a fast per-ball commentary picker for a full simulated match.

    /sim can call commentary hundreds of times in one handler run. The older
    path queried CommentaryEntry once per delivery, which made bot replies slow
    on chats that used longer formats. This helper snapshots all active
    commentary rows once, then returns a tiny in-memory picker with the same
    fallback behavior as pick_commentary().
    """
    lines_by_event = {}
    try:
        rows = (session.query(CommentaryEntry.event_key, CommentaryEntry.text,
                              CommentaryEntry.weight)
                .filter(CommentaryEntry.is_active == True).all())
        for event_key, text, weight in rows:
            if not text:
                continue
            lines_by_event.setdefault(event_key, []).extend(
                [text] * max(1, int(weight or 1)))
    except Exception:
        logger.exception("build_commentary_picker failed")
        lines_by_event = {}

    def picker(event_key, batsman="", bowler="", fielder=DEFAULT_FIELDER,
               keeper=DEFAULT_KEEPER, runs=0):
        line = _render_random_line(
            lines_by_event.get(event_key), batsman, bowler, fielder, keeper, runs)
        if line:
            return line
        return _fallback_commentary(event_key, batsman, bowler, fielder, keeper, runs)

    return picker


def pick_commentary(session, event_key, batsman="", bowler="",
                    fielder=DEFAULT_FIELDER, keeper=DEFAULT_KEEPER, runs=0):
    """Pick a random active commentary line for the given event.

    Prefers admin-configured lines; falls back to the built-in cinematic bank
    so commentary is never empty for a known event. Returns a formatted string,
    or None only when nothing is available at all.
    """
    try:
        rows = (session.query(CommentaryEntry)
                .filter(CommentaryEntry.event_key == event_key,
                        CommentaryEntry.is_active == True).all())
        if rows:
            weights = [max(1, r.weight or 1) for r in rows]
            chosen = random.choices(rows, weights=weights, k=1)[0]
            return _render(chosen.text, batsman, bowler, fielder, keeper, runs)
    except Exception:
        logger.exception(f"pick_commentary failed for event {event_key}")

    return _fallback_commentary(event_key, batsman, bowler, fielder, keeper, runs)


def list_event_keys():
    """Standard set of event keys the engine fires for."""
    return [
        "dot", "one", "two", "three", "four", "six",
        "wicket_bowled", "wicket_caught_fielder", "wicket_caught_keeper",
        "wicket_lbw", "wicket_stumped", "wicket_runOut",
        "extras", "wide", "no_ball", "free_hit", "mystery",
        "milestones", "general",
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
