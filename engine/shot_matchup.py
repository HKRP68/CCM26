"""Optional ball-by-ball technique choices, independent of ratings and pitch.

Inspired by the finer delivery/shot controls in Crickium. Multipliers are
deliberately small; this layer adds information the existing engine lacks,
without applying its rating, confidence or pitch advantages a second time.
No choices means an exact no-op, including the random stream.
"""

LINES = ("off", "middle", "leg")
FOOTWORK = ("front", "back", "advance")
STROKES = ("ground", "lofted")
INTENTS = ("defensive", "balanced", "attacking")
SPIN_LENGTHS = ("full", "good", "short")


def validate_choice(value, options, label):
    """Return an optional canonical key; reject malformed API input."""
    if value is None or value == "" or value == "auto":
        return None
    if not isinstance(value, str) or value not in options:
        raise ValueError(f"Invalid {label}. Choose {', '.join(options)} or auto.")
    return value


def length_family(length):
    text = str(length or "").lower()
    if "yorker" in text:
        return "yorker"
    if any(word in text for word in ("short", "bouncer", "hit the deck")):
        return "short"
    if any(word in text for word in ("back", "hard")):
        return "back"
    if "full" in text:
        return "full"
    if "good" in text:
        return "good"
    return None


def apply(probs, *, shot, length=None, is_spin=False, line=None,
          footwork=None, stroke=None, intent=None):
    """Reshape weights in place and return an explanation for the score feed.

    Unknown legacy context is neutral. Bowlers can choose a line independently
    of a batter's technique. Extras do not depend on the batter's choices.
    """
    if not any((line, footwork, stroke, intent)):
        return None
    family = length_family(length)
    name = shot.lower()
    score = 0
    reasons = []

    if footwork and family:
        fit = {
            "full": {"front": 2, "back": -2, "advance": 1},
            "good": {"front": 1, "back": 0, "advance": -1},
            "back": {"front": -1, "back": 1, "advance": -2},
            "short": {"front": -2, "back": 2, "advance": -3},
            "yorker": {"front": 1, "back": -1, "advance": -3},
        }[family].get(footwork, 0)
        score += fit
        if fit:
            reasons.append("Footwork fits the length" if fit > 0 else "Footwork exposes the batter")

    if family:
        if "drive" in name:
            score += 1 if family == "full" else (-1 if family in ("short", "yorker") else 0)
        if any(word in name for word in ("cut", "pull", "hook")):
            score += 1 if family in ("short", "back") else (-2 if family == "yorker" else -1)
    if "sweep" in name or name == "paddle":
        score += 1 if is_spin else -1

    if line:
        # Lines are relative to the batter, so left/right-handedness is not
        # applied again here. The existing handedness layer owns that effect.
        if any(word in name for word in ("cut", "off drive")):
            score += {"off": 1, "middle": -1, "leg": -2}.get(line, 0)
        elif name in ("flick", "leg glance", "on drive", "glance"):
            score += {"off": -1, "middle": 1, "leg": 1}.get(line, 0)
        probs["wide"] *= {"off": 1.35, "middle": 0.80, "leg": 1.20}.get(line, 1.0)

    score = max(-4, min(4, score))
    weights = {"dot": 1.0 - 0.025 * score, "1": 1.0 + 0.015 * score,
               "2": 1.0 + 0.015 * score, "3": 1.0,
               "4": 1.0 + 0.04 * score, "6": 1.0 + 0.035 * score,
               "W": 1.0 - 0.035 * score}
    if stroke == "ground":
        weights["6"] *= 0.65
        weights["1"] *= 1.06
        weights["W"] *= 0.94
        reasons.append("Ground stroke favours rotation and reduces aerial risk")
    elif stroke == "lofted":
        weights["6"] *= 1.18
        weights["1"] *= 0.94
        weights["W"] *= 1.12
        reasons.append("Lofted stroke trades safety for sixes")
    if intent == "defensive":
        weights["dot"] *= 1.12
        weights["4"] *= 0.88
        weights["6"] *= 0.85
        weights["W"] *= 0.92
    elif intent == "attacking":
        weights["dot"] *= 0.94
        weights["4"] *= 1.10
        weights["6"] *= 1.12
        weights["W"] *= 1.10

    # Preserve the entire non-extra mass. Batting decisions must not change
    # the chance of a wide, no-ball or leg-bye through normalisation.
    before = sum(probs[k] for k in weights)
    for key, multiplier in weights.items():
        probs[key] *= max(0.50, min(1.50, multiplier))
    after = sum(probs[k] for k in weights)
    if after > 0:
        for key in weights:
            probs[key] *= before / after
    return {"quality": "good" if score > 0 else ("poor" if score < 0 else "neutral"),
            "score": score, "line": line, "length": family,
            "footwork": footwork, "stroke": stroke, "intent": intent,
            "reasons": reasons}
