"""Convert all manual quests to auto-tracked ones.

Strategy per category:
- powerplay/death-over: drop the over-window qualifier, keep run/wkt target
- yorker: drop the yorker qualifier, keep wickets-in-match
- super-over: DELETE (not in engine)
- catch / run-out / fielding: DELETE (not tracked)
- maiden: keep, use new maiden_over event
- chase: use new chase_won event
- economy: use new economy_under_X events
- not-out: use new not_out_innings event
- strike-rate: DELETE (not tracked per innings)
- team: DELETE (we don't filter by team)
- win-rate %: DELETE (gameable)
- position-specific: DELETE
- allrounder (runs+wkts in same match): use new allrounder_match event
- no-extras / clean spell: use new clean_spell event

Run via admin button or `python convert_manual_quests.py`.
"""

import re
import logging

logger = logging.getLogger(__name__)


def _classify(desc: str) -> str:
    d = desc.lower()
    if "powerplay" in d or "first 6 overs" in d or "first six overs" in d: return "powerplay"
    if ("death over" in d or "last 5 overs" in d or "last five overs" in d
        or "overs 16" in d or "16-20" in d or "16–20" in d): return "death"
    if "yorker" in d: return "yorker"
    if "super over" in d: return "super_over"
    if "catch" in d: return "catch"
    if "run-out" in d or "run out" in d: return "runout"
    if "maiden" in d: return "maiden"
    if "chase" in d or "chasing" in d: return "chase"
    if "strike rate" in d or " sr " in d or "sr+" in d or "sr " in d: return "strike_rate"
    if "not out" in d or "not-out" in d: return "not_out"
    if ("opener" in d or "middle-order" in d or "middle order" in d
        or "position 3" in d or "position 4" in d or "position 5" in d): return "position"
    if re.search(r"runs?\s+and\s+take\s+\d+\+?\s+wickets", d) or \
       re.search(r"runs?\s+and\s+\d+\+?\s+wickets", d): return "allrounder"
    if "team" in d: return "team"
    if "economy" in d: return "economy"
    if "win rate" in d or re.search(r"\d+%\s+win", d) or re.search(r"win\s+rate\s+\d+", d): return "winrate"
    if "no-ball" in d or "no ball" in d or "0 wides" in d or "no wides" in d or "no extras" in d: return "no_extras"
    if re.match(r"^win\s+a\s+match", d) or "win any" in d.split(".")[0]: return "win_simple"
    # Single-match generic stat catch
    if re.search(r"hit\s+\d+\s+or\s+more\s+sixes", d) or re.search(r"hit\s+\d+\+?\s+sixes\s+in\s+a", d): return "sixes"
    if re.search(r"\d+\+?\s+innings", d): return "consistent_innings"
    if re.search(r"highest\s+run-scorer", d) or re.search(r"highest\s+wicket-taker", d): return "best_in_match"
    return "other"


def _extract_first_int(text: str) -> int:
    m = re.search(r"(\d{1,4})", text.replace(",", ""))
    return int(m.group(1)) if m else 1


def convert_manual_quests(session, *, dry_run: bool = False) -> dict:
    """Convert every quest with event_key='manual' to either an auto-tracked
    quest or delete it.

    Returns counts: {converted, deleted, kept_manual}
    """
    from models import Quest, UserQuestProgress
    converted = 0
    deleted = 0
    kept_manual = 0

    quests = session.query(Quest).filter(Quest.event_key == "manual").all()

    for q in quests:
        category = _classify(q.description)
        d = q.description.lower()

        action = None  # 'convert' or 'delete'
        new_event_key = None
        new_target = None
        new_desc = None  # if rewrite the description

        if category == "powerplay":
            # "Score 45+ runs in the first 6 overs" → "Score 45+ runs in a match"
            m = re.search(r"score\s+(\d+)\+?\s+runs", d)
            if m:
                runs = int(m.group(1))
                action = "convert"
                new_event_key = "runs_in_innings"
                new_target = runs
                new_desc = f"Score {runs}+ runs in a single innings."
            else:
                # Powerplay wicket/economy etc.
                if "wicket" in d:
                    m = re.search(r"(\d+)\+?\s+wickets?", d)
                    n = int(m.group(1)) if m else 2
                    action = "convert"; new_event_key = "wickets_in_match"; new_target = n
                    new_desc = f"Take {n}+ wickets in a single match."
                else:
                    action = "delete"

        elif category == "death":
            # Same approach as powerplay
            m = re.search(r"score\s+(\d+)\+?\s+runs", d)
            if m:
                runs = int(m.group(1))
                action = "convert"
                new_event_key = "runs_in_innings"
                new_target = runs
                new_desc = f"Score {runs}+ runs in a single innings."
            elif "concede fewer than" in d or "economy" in d:
                action = "convert"
                new_event_key = "economy_under_7"
                new_target = 1
                new_desc = "Bowl a 4+ over spell with economy under 7.00."
            else:
                action = "delete"

        elif category == "yorker":
            m = re.search(r"(\d+)\+?\s+wickets?", d)
            n = int(m.group(1)) if m else 2
            action = "convert"
            new_event_key = "wickets_in_match"
            new_target = n
            new_desc = f"Take {n}+ wickets in a single match."

        elif category == "super_over":
            action = "delete"

        elif category in ("catch", "runout"):
            action = "delete"

        elif category == "maiden":
            m = re.search(r"(\d+)\s+maidens?", d)
            n = int(m.group(1)) if m else 1
            action = "convert"
            new_event_key = "maiden_over"
            new_target = n
            new_desc = f"Bowl {n}+ maiden overs."

        elif category == "chase":
            m = re.search(r"(\d+)\+?\s+\w+\s+\w+", d)
            # Default 1 chase win (for "Win a match while chasing")
            # For "Win 5 matches while chasing" → 5
            m2 = re.search(r"(\d+)\s+matches?\s+while\s+chasing", d)
            if m2:
                n = int(m2.group(1))
            else:
                n = 1
            action = "convert"
            new_event_key = "chase_won"
            new_target = n
            new_desc = (f"Win {n} matches while chasing." if n > 1
                       else "Win a match while chasing (batting second).")

        elif category == "strike_rate":
            action = "delete"

        elif category == "not_out":
            m = re.search(r"score\s+(\d+)\+?\s+runs", d)
            runs = int(m.group(1)) if m else 40
            # Convert to "score X+ runs while remaining not out"
            # We track not_out_innings as a counter, so target = 1 here
            action = "convert"
            new_event_key = "not_out_innings"
            new_target = 1
            new_desc = "Remain not out in an innings (any score)."

        elif category == "position":
            action = "delete"

        elif category == "allrounder":
            # All these become "X allrounder matches" (30+ runs AND 2+ wkts)
            # If the description specifies a count of "10 matches where..." use 10
            m_count = re.search(r"achieve\s+(\d+)\s+matches", d)
            target_n = int(m_count.group(1)) if m_count else 1
            action = "convert"
            new_event_key = "allrounder_match"
            new_target = target_n
            if target_n > 1:
                new_desc = f"Achieve {target_n} matches with 30+ runs AND 2+ wickets."
            else:
                new_desc = "Score 30+ runs AND take 2+ wickets in the same match."

        elif category == "team":
            # We can't filter by team — strip the team part and treat as a
            # generic counterpart. Looking at the manual list, most "Team"
            # quests duplicate a generic quest. Just delete them all.
            action = "delete"

        elif category == "economy":
            # Map to economy_under_5/6/7 buckets based on threshold mentioned
            m_thresh = re.search(r"under\s+(\d+(?:\.\d+)?)", d)
            thresh = float(m_thresh.group(1)) if m_thresh else 7.0
            if thresh < 4.5: ek = "economy_under_4_5"
            elif thresh < 5.5: ek = "economy_under_5"
            elif thresh < 6.5: ek = "economy_under_6"
            else: ek = "economy_under_7"
            # Target = number of times achieved (most quests want it once)
            action = "convert"
            new_event_key = ek
            new_target = 1
            new_desc = f"Bowl 4+ overs with economy under {thresh:.1f}."

        elif category == "winrate":
            action = "delete"

        elif category == "no_extras":
            action = "convert"
            new_event_key = "clean_spell"
            new_target = 1
            new_desc = "Bowl a 4-over spell with 1+ maidens AND take 3+ wickets."

        elif category == "win_simple":
            action = "convert"
            new_event_key = "match_won"
            new_target = 1
        elif category == "sixes":
            m = re.search(r"(\d+)", d)
            n = int(m.group(1)) if m else 8
            action = "convert"
            new_event_key = "sixes_in_match"
            new_target = n
            new_desc = f"Hit {n}+ sixes in a single match."
        elif category == "consistent_innings":
            # "Score 50+ in 10 different innings" → cumulative fifties
            m_inn = re.search(r"(\d+)\+?\s+(?:different\s+)?\w*\s*innings", d)
            n = int(m_inn.group(1)) if m_inn else 5
            action = "convert"
            new_event_key = "fifty"
            new_target = n
            new_desc = f"Score 50+ in {n} different innings."
        elif category == "best_in_match":
            # "Be highest scorer/wicket-taker in winning match" → match_won
            action = "convert"
            new_event_key = "match_won"
            new_target = 1
            new_desc = "Win a match."
        else:
            # Catch-all: try to extract any wickets/runs/sixes pattern
            if "century" in d:
                action = "convert"; new_event_key = "hundred"; new_target = 1
            elif "hat-trick" in d or "hat trick" in d:
                action = "convert"; new_event_key = "hattrick"; new_target = 1
            else:
                kept_manual += 1
                continue

        if action == "delete":
            # Delete progress rows first
            if not dry_run:
                session.query(UserQuestProgress).filter(
                    UserQuestProgress.quest_id == q.id).delete()
                session.delete(q)
            deleted += 1
        elif action == "convert":
            if not dry_run:
                q.event_key = new_event_key
                q.target_count = max(1, new_target)
                if new_desc:
                    q.description = new_desc
                # Reset all in-progress entries to 0 since the rule changed
                session.query(UserQuestProgress).filter(
                    UserQuestProgress.quest_id == q.id,
                    UserQuestProgress.claimed == False,
                ).update({"progress": 0, "completed": False})
            converted += 1

    if not dry_run:
        session.commit()

    return {
        "converted": converted,
        "deleted": deleted,
        "kept_manual": kept_manual,
    }


def main():
    from database import SessionLocal, init_db
    init_db()
    s = SessionLocal()
    try:
        result = convert_manual_quests(s)
        print(f"Converted to auto: {result['converted']}")
        print(f"Deleted:           {result['deleted']}")
        print(f"Still manual:      {result['kept_manual']}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
