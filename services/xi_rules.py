"""The two Playing XI rulebooks, as plain functions.

The bot needs to build legal XIs (``services.bot_xi_builder``) and the handlers
need to check the ones users pick. Both rulebooks used to live inside Telegram
handler modules, which meant anything that wanted to *apply* a rule had to
import a handler — and with it Telegram, SQLAlchemy and Pillow. They live here
instead: no imports beyond the standard library, so the rules can be reused and
unit-tested on their own.

Two rulebooks, because the game genuinely has two:

  • ROSTER XI (/playingxi, /letsplay, /lpbot) — a user's own cards.
    11 players, 3-5 Batsmen, 3-5 Bowlers, 1-2 Wicket Keepers, 1-3 All-rounders,
    the "3rd all-rounder must bowl worse than every pure Bowler" clause, and no
    two versions of the same cricketer.

  • CHALLENGE LEAGUE XI (/cipl, league commands, /ciplbot) — a league team's
    squad. 11 players, at least one Wicket Keeper, at least five bowling options
    (Bowlers + All-rounders), and the league's overseas min/max.

``handlers.lineup`` and ``handlers.challenge`` re-export these under their
existing names, so every current call site is unchanged.
"""

import json


# ════════════════════════════════════════════════════════════════════
# Roster XI (a user's own cards)
# ════════════════════════════════════════════════════════════════════

def validate_roster_xi(roster_list):
    """Validate a personal-roster Playing XI. Returns ``(valid, errors)``.

    ``roster_list`` is a list of ``(UserRoster, Player)`` pairs in batting order;
    only the ``Player`` side is read (``category``, ``bowl_rating``, ``name``,
    ``version``, ``id``, ``parent_player_id``).
    """
    if len(roster_list) < 11:
        return False, [f"Need 11 players, have {len(roster_list)}"]

    top_11 = roster_list[:11]
    errors = []

    cats = {"Batsman": [], "Wicket Keeper": [], "All-rounder": [], "Bowler": []}
    for _entry, player in top_11:
        cat = player.category
        if cat in cats:
            cats[cat].append(player)
        else:
            cats["Batsman"].append(player)

    batsmen = cats["Batsman"]
    keepers = cats["Wicket Keeper"]
    allrounders = cats["All-rounder"]
    bowlers = cats["Bowler"]

    # Min/Max checks
    if len(batsmen) < 3:
        errors.append(f"Need min 3 Batsmen (have {len(batsmen)})")
    if len(batsmen) > 5:
        errors.append(f"Max 5 Batsmen (have {len(batsmen)})")
    if len(bowlers) < 3:
        errors.append(f"Need min 3 Bowlers (have {len(bowlers)})")
    if len(bowlers) > 5:
        errors.append(f"Max 5 Bowlers (have {len(bowlers)})")
    if len(keepers) < 1:
        errors.append("Need at least 1 Wicket Keeper")
    if len(keepers) > 2:
        errors.append(f"Max 2 Wicket Keepers (have {len(keepers)})")
    if len(allrounders) < 1:
        errors.append("Need at least 1 All-rounder")
    if len(allrounders) > 3:
        errors.append(f"Max 3 All-rounders (have {len(allrounders)})")

    # 3rd ALR rule: if you have 3 all-rounders, AT LEAST ONE of them
    # must have a BOWL rating lower than all pure Bowlers' BOWL ratings
    if len(allrounders) == 3 and bowlers:
        min_bowler_bowl = min(b.bowl_rating for b in bowlers)
        # Find the weakest (lowest bowl rating) all-rounder
        weakest_alr = min(allrounders, key=lambda p: p.bowl_rating)
        if weakest_alr.bowl_rating >= min_bowler_bowl:
            errors.append(
                f"At least one All-rounder must have BOWL rating lower than all Bowlers. "
                f"Lowest Bowler BOWL: {min_bowler_bowl}. "
                f"Your weakest All-rounder ({weakest_alr.name}, BOWL {weakest_alr.bowl_rating}) "
                f"doesn't qualify."
            )

    # Duplicate-version rule: each player (regardless of which version) can
    # only appear ONCE in the XI. So if user has the Base + IPL 2026 cards
    # of the same player, only ONE of them can be in the playing XI.
    seen_base_ids = {}  # base_id -> first variant we saw
    duplicate_pairs = []
    for _entry, player in top_11:
        base_id = player.parent_player_id or player.id
        if base_id in seen_base_ids:
            duplicate_pairs.append((seen_base_ids[base_id], player))
        else:
            seen_base_ids[base_id] = player
    if duplicate_pairs:
        for first, second in duplicate_pairs:
            label_first = first.version or "Base"
            label_second = second.version or "Base"
            errors.append(
                f"Duplicate of <b>{first.name}</b> in XI: "
                f"<i>{label_first}</i> and <i>{label_second}</i>. "
                f"Pick only one version. Use /swap to swap one out."
            )

    return len(errors) == 0, errors


# ════════════════════════════════════════════════════════════════════
# Challenge League XI (a league team's squad)
# ════════════════════════════════════════════════════════════════════

def challenge_player_details(player):
    """The ``details_json`` blob on a ChallengePlayer, as a dict ({} if absent)."""
    raw = getattr(player, "details_json", None) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def challenge_player_category(player):
    """Normalised role for a ChallengePlayer.

    Admin-entered roles vary a lot ("WK", "wicket-keeper batter", "allrounder"),
    so they are folded onto the four canonical categories the engine uses.
    """
    data = challenge_player_details(player)
    value = (data.get("category") or data.get("Category")
             or data.get("role") or data.get("Role") or "")
    value = str(value).strip()
    low = value.lower().replace("-", " ")
    if low in ("wk", "keeper", "wicketkeeper", "wicket keeper",
               "wicket keeper batter", "wicket keeper batsman"):
        return "Wicket Keeper"
    if low in ("all rounder", "allrounder", "all round", "alr", "all-rounder"):
        return "All-rounder"
    if low in ("bowler", "bowl"):
        return "Bowler"
    if low in ("batsman", "batter", "bat"):
        return "Batsman"
    return value or "Player"


def challenge_player_rating(player):
    """Best-effort overall rating from details_json; None when unavailable.

    Rating is not a column on ChallengePlayer — it lives inside details_json —
    so this degrades gracefully (returns None) rather than raising when the blob
    has no rating, keeping the numbered-roster render robust.
    """
    data = challenge_player_details(player)
    for key in ("rating", "Rating", "overall", "Overall", "OVR", "ovr"):
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            return round(float(value))
        except (TypeError, ValueError):
            # Non-numeric ratings are rendered into HTML messages, so escape any
            # markup-like characters from this admin-supplied details_json value.
            from html import escape
            return escape(str(value))
    return None


def challenge_numeric_rating(player):
    """``challenge_player_rating`` coerced to a float, 0.0 when non-numeric.

    Selection maths needs a number it can sort by; the display helper above may
    hand back an escaped string for admin-entered junk.
    """
    try:
        return float(challenge_player_rating(player) or 0)
    except (TypeError, ValueError):
        return 0.0


def _challenge_detail_number(player, keys, default=0.0):
    details = challenge_player_details(player)
    for key in keys:
        value = details.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def challenge_bat_rating(player):
    """Batting rating for a ChallengePlayer, falling back to the overall."""
    return _challenge_detail_number(
        player, ("bat_rating", "batting_rating", "bat"),
        default=challenge_numeric_rating(player))


def challenge_bowl_rating(player):
    """Bowling rating for a ChallengePlayer, 0 when the blob doesn't carry one."""
    return _challenge_detail_number(
        player, ("bowl_rating", "bowling_rating", "bowl"), default=0.0)


def challenge_is_wicket_keeper(player):
    # Deliberately loose: an admin role that didn't match a normalisation rule
    # (e.g. "Wicketkeeper Batter") still comes through here as a keeper.
    category = challenge_player_category(player).lower()
    return "wicket" in category or category == "wk"


def challenge_is_bowling_option(player):
    category = challenge_player_category(player).lower().replace("-", " ")
    return ("bowler" in category or "all rounder" in category
            or "allrounder" in category)


def challenge_is_overseas(player):
    """True when the challenge player is flagged overseas.

    Prefers the ``is_overseas`` column (set by the admin toggle) and falls back to
    the ``is_overseas`` key inside ``details_json`` for rows that predate the column.
    """
    flag = getattr(player, "is_overseas", None)
    if flag is not None:
        return bool(flag)
    return bool(challenge_player_details(player).get("is_overseas"))


def validate_challenge_xi(players, min_overseas=0, max_overseas=11):
    """Validate a Challenge League Playing XI. Returns ``(valid, error)``.

    Unlike the roster rulebook this reports a single error string, because the
    XI picker surfaces it in a Telegram alert popup.
    """
    if len(players) != 11:
        return False, "Select exactly 11 players."
    if not any(challenge_is_wicket_keeper(player) for player in players):
        return False, "Wicket Keeper is Must"
    bowling_options = sum(1 for player in players
                          if challenge_is_bowling_option(player))
    if bowling_options < 5:
        return False, "At least 5 Bowling Option Must (Bowlers + Allrounders)"
    overseas = sum(1 for player in players if challenge_is_overseas(player))
    if overseas > max_overseas:
        return False, f"Max {max_overseas} overseas ✈️ allowed in XI (you have {overseas})"
    if overseas < min_overseas:
        return False, f"Min {min_overseas} overseas ✈️ required in XI (you have {overseas})"
    return True, ""
