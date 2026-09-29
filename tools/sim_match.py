"""Play one match on the Conditions Engine and print it.

    python -m tools.sim_match --format T20 --stadium Wankhede --seed 7
    python -m tools.sim_match --format ODI --team1 India --team2 Australia \\
        --stadium "Eden Gardens" --session Afternoon --daynight --seed 42
    python -m tools.sim_match --format Test --stadium Chepauk --pitch Dusty --seed 3
    python -m tools.sim_match --team1 my_xi.json --team2 England --json out.json

Same arguments + same seed → the same match. Teams are countries from
data/players.json, or a JSON file ``{"name": ..., "players": [...]}``.
"""

import argparse
import json
import os
import sys

from engine.sim import report, stadium as stadium_mod, teams
from engine.sim.config import build, get_config
from engine.sim.match import MatchSetup, simulate_match
from engine.sim.models import MatchTime, Weather


def _team(arg, rows):
    if arg.endswith(".json") and os.path.exists(arg):
        return teams.load_team_file(arg)
    return teams.pick_xi(arg, rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Conditions Engine match simulator")
    ap.add_argument("--format", default="T20", choices=["T20", "ODI", "Test"])
    ap.add_argument("--team1", default="India")
    ap.add_argument("--team2", default="Australia")
    ap.add_argument("--stadium", default="Wankhede Stadium")
    ap.add_argument("--pitch", default=None, help="Dry/Dusty/Bouncy/Flat/Hard/Even/Green (default: the ground's)")
    ap.add_argument("--grass", default=None, choices=[None, "Heavy", "Medium", "Little", "No Grass"])
    ap.add_argument("--seed", default=None)
    ap.add_argument("--session", default=None, choices=["Morning", "Afternoon", "Evening", "Night"])
    ap.add_argument("--daynight", action="store_true")
    ap.add_argument("--ball", default=None, choices=["Red", "White", "Pink"])
    ap.add_argument("--cloud", type=float, default=None)
    ap.add_argument("--humidity", type=float, default=None)
    ap.add_argument("--temp", type=float, default=None)
    ap.add_argument("--wind", type=float, default=None)
    ap.add_argument("--wind-toward", default=None, choices=["A", "B"])
    ap.add_argument("--rain", type=float, default=None, help="rain chance %%")
    ap.add_argument("--drama", type=float, default=None, help="dramaSlider 0-100")
    ap.add_argument("--no-commentary", action="store_true")
    ap.add_argument("--no-wagon", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--json", default=None, help="also write the full result here")
    args = ap.parse_args(argv)

    cfg = get_config()
    if args.drama is not None:
        cfg = build(override={"drama": {"dramaSlider": args.drama}})

    rows = teams.load_rows()
    st = stadium_mod.find(args.stadium)
    if st is None:
        names = ", ".join(s.name for s in stadium_mod.all_stadiums())
        ap.error(f"unknown stadium {args.stadium!r}. Known: {names}")

    weather = None
    if any(v is not None for v in (args.cloud, args.humidity, args.temp, args.wind, args.rain)):
        c = dict(st.climate)
        weather = Weather(
            cloud_cover=args.cloud if args.cloud is not None else c.get("cloudCover", 30),
            humidity=args.humidity if args.humidity is not None else c.get("humidity", 55),
            temperature_c=args.temp if args.temp is not None else c.get("temperatureC", 28),
            wind_kph=args.wind if args.wind is not None else c.get("windKPH", 10),
            rain_chance=args.rain if args.rain is not None else c.get("rainChance", 10),
            wind_toward=args.wind_toward).clamped()

    time = None
    if args.session or args.daynight or args.ball:
        session = args.session or ("Night" if args.daynight and args.format != "Test" else "Afternoon")
        ball = args.ball or ("Pink" if args.format == "Test" and args.daynight
                             else "Red" if args.format == "Test" else "White")
        start = cfg["timeOfDay"]["sessionStartHour"][session]
        if args.format == "Test":
            start = 14.0 if args.daynight else 10.5
        time = MatchTime(session=session, is_day_night=args.daynight or session in ("Evening", "Night"),
                         ball_color=ball, start_hour=start)

    setup = MatchSetup(fmt=args.format, team1=_team(args.team1, rows), team2=_team(args.team2, rows),
                       pitch=args.pitch, stadium=st, weather=weather, time=time, grass=args.grass,
                       seed=args.seed, commentary=not args.no_commentary)
    res = simulate_match(setup, cfg)
    if args.summary_only:
        print(report.render_summary(res))
    else:
        print(report.render_text(res, commentary=not args.no_commentary, wagon=not args.no_wagon))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
