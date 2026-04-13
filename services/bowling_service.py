"""Bowling service — delivery options per bowler type and hand."""

BOWLER_PROFILES = {
    "Fast": {
        "Right": {
            "variations": ["Outswing", "Reverse Swing", "Inswing", "Seam Up", "Slower"],
            "lengths": ["Hard", "Good", "Full", "Hit the Deck", "Yorker", "Bouncer"],
        },
        "Left": {
            "variations": ["Outswing", "Reverse Swing", "Inswing", "Seam Up", "Slower"],
            "lengths": ["Hard", "Good", "Full", "Hit the Deck", "Yorker", "Bouncer"],
        },
    },
    "Medium Pacer": {
        "Right": {
            "variations": ["Leg Cutter", "Off Cutter", "Slower", "Knuckle", "Cross Seam"],
            "lengths": ["Good Length", "Full Length", "Short of Length", "Back of Length", "Yorker"],
        },
        "Left": {
            "variations": ["Leg Cutter", "Off Cutter", "Slower", "Knuckle", "Cross Seam"],
            "lengths": ["Good Length", "Full Length", "Short of Length", "Back of Length", "Yorker"],
        },
    },
    "Off Spinner": {
        "Right": {"deliveries": ["Off Break", "Doosra", "Arm Ball", "Top Spinner", "Carrom Ball"]},
        "Left": {"deliveries": ["Orthodox", "Arm Ball", "Slider", "Top Spinner", "Backspinner", "Carrom Ball"]},
    },
    "Leg Spinner": {
        "Right": {"deliveries": ["Leg Break", "Googly", "Flipper", "Top Spinner", "Slider", "Surprise"]},
        "Left": {"deliveries": ["Chinaman", "Wrong'un", "Flipper", "Slider", "Teesra", "Surprise"]},
    },
}

# Map bowl_style from DB to profile key
STYLE_MAP = {
    "Fast": "Fast",
    "Medium Pacer": "Medium Pacer",
    "Off Spinner": "Off Spinner",
    "Leg Spinner": "Leg Spinner",
    "Leg-break": "Leg Spinner",
    "Off-break": "Off Spinner",
    "Left-arm fast": "Fast",
    "Right-arm fast": "Fast",
    "Left-arm medium": "Medium Pacer",
    "Right-arm medium": "Medium Pacer",
    "Left-arm orthodox": "Off Spinner",
    "Slow left-arm orthodox": "Off Spinner",
}

AVAILABLE_SHOTS = ["Drive", "Cut", "Pull", "Leg Glance", "Flick", "Sweep", "Switch Hit", "Slog", "Loft"]


def get_bowler_profile_key(bowl_style):
    """Map DB bowl_style to profile key."""
    if not bowl_style:
        return "Medium Pacer"
    low = bowl_style.lower()
    # Direct match
    if bowl_style in STYLE_MAP:
        return STYLE_MAP[bowl_style]
    # Fuzzy match
    if "fast" in low:
        return "Fast"
    if "medium" in low:
        return "Medium Pacer"
    if "off" in low or "orthodox" in low:
        return "Off Spinner"
    if "leg" in low or "chinaman" in low or "wrist" in low:
        return "Leg Spinner"
    return "Medium Pacer"


def is_spinner(bowl_style):
    key = get_bowler_profile_key(bowl_style)
    return key in ("Off Spinner", "Leg Spinner")


def is_pacer(bowl_style):
    return not is_spinner(bowl_style)


def get_delivery_options(bowl_style, bowl_hand):
    """Return delivery options for a bowler.
    For pacers: {"variations": [...], "lengths": [...], "is_spinner": False}
    For spinners: {"deliveries": [...], "is_spinner": True}
    """
    key = get_bowler_profile_key(bowl_style)
    hand = "Right" if not bowl_hand or bowl_hand.startswith("R") else "Left"

    profile = BOWLER_PROFILES.get(key, {})
    hand_profile = profile.get(hand) or profile.get("Right", {})

    if "deliveries" in hand_profile:
        return {"deliveries": hand_profile["deliveries"], "is_spinner": True}
    else:
        return {
            "variations": hand_profile.get("variations", ["Seam Up"]),
            "lengths": hand_profile.get("lengths", ["Good"]),
            "is_spinner": False,
        }
