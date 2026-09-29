"""One line of commentary per ball, coloured by the conditions that produced it."""

_RUNS = {
    0: ["defended back to the bowler", "beaten outside off", "straight to the fielder",
        "left alone", "dot ball — good length, no room"],
    1: ["worked away for a single", "nudged into the gap, one run", "pushed to mid-on for one",
        "dropped at his feet and a quick single"],
    2: ["driven into the gap, they come back for two", "flicked fine, two runs",
        "good running — two"],
    3: ["chased down just short of the rope — three", "they run three, superb running"],
    4: ["FOUR — driven through the covers", "FOUR — pulled in front of square",
        "FOUR — cut hard past point", "FOUR — flicked off the pads"],
    6: ["SIX — launched over long-on", "SIX — pulled into the stands",
        "SIX — cleared the front leg and deposited it", "SIX — lofted inside-out over cover"],
}

_WICKET = {
    "Bowled": "BOWLED — through the gate, stumps rearranged",
    "LBW": "LBW — trapped in front, the finger goes up",
    "Caught": "OUT — caught, the edge carried",
    "Stumped": "STUMPED — dragged out of the crease",
    "Run Out": "RUN OUT — a mix-up and a direct throw",
    "Hit Wicket": "HIT WICKET — trod on his own stumps",
}


def condition_tag(fs, bowler, result):
    """A short phrase for the dominant condition on this ball, or ''."""
    if fs is None or bowler is None:
        return ""
    if result.outfield_four:
        return "the lightning outfield does the rest"
    if result.dropped:
        return "wet ball, slippery hands" if fs.dew > 0.3 else "a costly miss"
    if bowler.is_spin:
        if fs.dew > 0.5 and result.runs >= 4:
            return "the wet ball skids on, no grip for the spinner"
        if fs["spin"] >= 1.8 and result.bucket in ("Wicket", "Dot"):
            return "ripping out of the rough"
    else:
        if fs.reverse and result.bucket == "Wicket":
            return "reverse swing, late and deadly"
        if fs["swing"] >= 1.8 and result.bucket in ("Wicket", "Dot"):
            return "hooping around corners"
        if fs["seam"] >= 1.6 and result.bucket == "Wicket":
            return "nipped off the seam"
    if result.runs == 6 and fs["six"] >= 1.2:
        return "short boundary, easy pickings"
    return ""


def line(label, bowler_name, batter_name, result, rng, fs=None, bowler=None, event=None):
    """``"15.3 Bumrah to Kohli, FOUR — driven through the covers"``."""
    if result.is_wicket:
        body = _WICKET.get(result.wicket_type, "OUT")
    elif result.extra_type:
        body = {"Wide": "wide", "No Ball": "NO BALL", "Leg Bye": "leg bye",
                "Bye": "bye"}[result.extra_type]
        if result.extra_runs > 1 and result.extra_type != "No Ball":
            body += f"s, {result.extra_runs} runs"
        if result.runs:
            body += f" and {result.runs} off the bat"
    elif result.dropped:
        body = f"DROPPED! {result.runs} run{'s' if result.runs != 1 else ''}"
    else:
        body = rng.choice(_RUNS.get(result.runs, ["played"]))
    tag = condition_tag(fs, bowler, result)
    out = f"{label} {bowler_name} to {batter_name}, {body}"
    if tag:
        out += f" ({tag})"
    if event:
        out += f" — {event}"
    return out
