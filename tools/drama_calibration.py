"""Drama activation + budget calibration (Codex Prompt 05, §9, §10, §11).

Runs Balanced matches and measures the quantities the config numbers control, so
they can be tuned against evidence rather than guessed:

  * clutch activations per innings (target: a handful, not every over);
  * rare-volatility activations per innings (target: ~0.3, i.e. 0.015/over);
  * the momentum state distribution (|momentum| mean / p95 / max, bounded 0.75);
  * how often the shared budget's safety limit actually binds (scale < 1).

    python -m tools.drama_calibration --n 40 --pitch Even

Ratings are shifted only on in-memory copies; data/players.json is never touched.
"""

import argparse
import os
import statistics

from tools import pitch_calibration as pc


def _instrument():
    """Monkeypatch the DramaEngine to tally activations and budget use."""
    from engine.drama import engine as deng
    tally = {"clutch": 0, "rare": 0, "innings": 0, "scale_lt1": 0, "deliveries": 0,
             "momentum_abs": []}

    orig_over_start = deng.DramaEngine.on_over_start
    orig_before = deng.DramaEngine.before_delivery
    orig_new_innings = deng.DramaEngine.new_innings

    def on_over_start(self, ctx, high_pressure=False):
        c0 = self.state["clutch"]["used_batting"] + self.state["clutch"]["used_bowling"]
        r0 = self.state["rare"]["used"]
        orig_over_start(self, ctx, high_pressure)
        tally["clutch"] += (self.state["clutch"]["used_batting"]
                            + self.state["clutch"]["used_bowling"]) - c0
        tally["rare"] += self.state["rare"]["used"] - r0

    def before_delivery(self, ctx, base):
        out = orig_before(self, ctx, base)
        if self.overlay_active:
            tally["deliveries"] += 1
            tally["momentum_abs"].append(abs(self.state["momentum"]))
            if self.state["last_diag"].get("scale", 1.0) < 0.999:
                tally["scale_lt1"] += 1
        return out

    def new_innings(self, innings):
        orig_new_innings(self, innings)
        tally["innings"] += 1

    deng.DramaEngine.on_over_start = on_over_start
    deng.DramaEngine.before_delivery = before_delivery
    deng.DramaEngine.new_innings = new_innings

    def restore():
        deng.DramaEngine.on_over_start = orig_over_start
        deng.DramaEngine.before_delivery = orig_before
        deng.DramaEngine.new_innings = orig_new_innings

    return tally, restore


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--overs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=4242)
    args = ap.parse_args(argv)

    os.environ["SIM_REALISM_ENABLED"] = "1"
    os.environ["SIM_DRAMA_PRESET"] = "BALANCED"
    os.environ["SIM_DRAMA_ENABLED"] = "1"
    from engine.realism import config as rcfg
    from engine.drama import config as dcfg
    rcfg.clear_cache(); dcfg.clear_cache()

    tally, restore = _instrument()
    try:
        from services.sim_match import simulate_match
        for i in range(args.n):
            a = pc.make_xi("A", args.seed * 1000 + i)
            b = pc.make_xi("B", args.seed * 1000 + 500 + i)
            with pc._muted_logging():
                simulate_match(a, b, args.overs, args.pitch, "A", "B",
                               seed=args.seed * 7919 + i)
    finally:
        restore()
        for k in ("SIM_DRAMA_ENABLED", "SIM_DRAMA_PRESET"):
            os.environ.pop(k, None)

    innings = max(1, tally["innings"])
    mom = tally["momentum_abs"] or [0.0]
    deliveries = max(1, tally["deliveries"])
    p95 = sorted(mom)[int(0.95 * (len(mom) - 1))]
    print("\nDrama calibration — %d matches, pitch %s, %d overs\n"
          % (args.n, args.pitch, args.overs))
    print("innings simulated          : %d" % tally["innings"])
    print("clutch activations / innings: %.3f  (cap 1 per side)"
          % (tally["clutch"] / innings))
    print("rare activations / innings  : %.3f  (target ~0.30 at 0.015/over)"
          % (tally["rare"] / innings))
    print("momentum |state|  mean/p95/max: %.3f / %.3f / %.3f  (bound 0.75)"
          % (statistics.mean(mom), p95, max(mom)))
    print("budget safety-limit binds   : %.1f%% of deliveries"
          % (100.0 * tally["scale_lt1"] / deliveries))
    print()


if __name__ == "__main__":
    main()
