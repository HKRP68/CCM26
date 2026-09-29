"""The ball gets old: shine, hardness and roughness, and what each buys.

* overs 0-10 (new): swing x1.6, seam x1.4, pace x1.1 — the window is longer
  on a humid day (``weather.humidity.swingWindowExtraOvers``)
* overs 10-25: conventional swing decays from x1.0 to x0.7
* overs 25-40: shine < 40 on a dry, abrasive surface lets a quick, skilled
  bowler reverse it (x1.8); roughness > 50 gives spin x1.3
* overs 40+: hardness < 30 → pace x0.85, spin x1.5, bat speed x1.05

``advance`` is pure (returns a new :class:`BallState`), and :func:`ball_at`
rebuilds a ball of any age from scratch, which is what the live hook uses.
"""

from dataclasses import replace

from engine.sim.models import BallState


def new_ball(color="White", number=1):
    return BallState(overs_old=0.0, shine=100.0, hardness=100.0, roughness=0.0,
                     color=color, number=number)


def _abrasion(pitch, cfg):
    return float(cfg["ballAging"]["abrasivePitches"].get(pitch, 1.0))


def advance(ball, pitch, cfg, overs=1.0, dew=0.0):
    """The ball after ``overs`` more overs on ``pitch`` (dew restores shine)."""
    d = cfg["ballAging"]["decayPerOver"]
    ab = _abrasion(pitch, cfg)
    restore = cfg["timeOfDay"]["eveningNight"]["shineRestorePerOver"] * dew
    return replace(
        ball,
        overs_old=min(90.0, ball.overs_old + overs),
        shine=max(0.0, min(100.0, ball.shine - (d["shine"] * ab - restore) * overs)),
        hardness=max(0.0, ball.hardness - d["hardness"] * ab * overs),
        roughness=min(100.0, ball.roughness + d["roughness"] * ab * overs),
    )


def ball_at(overs_old, pitch, cfg, color="White", dew=0.0):
    return advance(new_ball(color), pitch, cfg, overs=max(0.0, overs_old), dew=dew)


def reverse_available(ball, pitch, cfg):
    o = cfg["ballAging"]["old"]
    return (ball.overs_old >= cfg["ballAging"]["mid"]["untilOver"]
            and ball.shine < o["reverseShineBelow"]
            and pitch in o["reversePitches"])


def bowler_can_reverse(bowler, cfg):
    o = cfg["ballAging"]["old"]
    return (bowler is not None and not bowler.is_spin
            and bowler.pace >= o["reverseMinPace"]
            and bowler.swing >= o["reverseMinSkill"])


def apply(fs, cond, ball, cfg, bowler=None):
    if ball is None:
        return
    a = cfg["ballAging"]
    o = ball.overs_old
    window = a["new"]["untilOver"] + fs.swing_window_extra
    mid_end = a["mid"]["untilOver"]

    if o < window:
        fs.mul("swing", a["new"]["swing"], key="new_ball",
               text="The new ball swung and seamed")
        fs.mul("seam", a["new"]["seam"])
        fs.mul("pace", a["new"]["pace"])
    elif o < mid_end:
        t = (o - window) / max(1.0, mid_end - window)
        m = a["mid"]
        fs.mul("swing", m["swingStart"] + (m["swingEnd"] - m["swingStart"]) * t)
        fs.mul("seam", m["seam"])
    else:
        fs.mul("swing", a["old"]["swing"])

    if reverse_available(ball, cond.pitch, cfg):
        fs.reverse = float(a["old"]["reverseSwing"])
        if bowler_can_reverse(bowler, cfg):
            fs.note("reverse_swing", "swing", fs.reverse,
                    "The old ball reversed for the quicks")

    if o >= mid_end and ball.roughness > a["old"]["roughnessSpinAbove"]:
        fs.mul("spin", a["old"]["roughnessSpin"], key="rough_ball",
               text="A scuffed ball gripped for the spinners")

    if o >= a["old"]["untilOver"] and ball.hardness < a["veryOld"]["hardnessBelow"]:
        v = a["veryOld"]
        fs.mul("pace", v["pace"], key="soft_ball",
               text="A soft old ball — the quicks lost their bite, the spinners took over")
        fs.mul("spin", v["spin"])
        fs.mul("bat_speed", v["batSpeed"])


def needs_new_ball(ball, fmt, cfg):
    """Test: the new ball is due every ``newBall.testEvery`` overs."""
    return fmt == "Test" and ball.overs_old >= cfg["ballAging"]["newBall"]["testEvery"]
