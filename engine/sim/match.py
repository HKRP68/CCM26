"""Standalone match simulator: T20, ODI and Test on the Conditions Engine.

    from engine.sim.match import MatchSetup, simulate_match
    result = simulate_match(MatchSetup(fmt="ODI", team1=..., team2=..., seed=7))

Same setup + same seed → the same match, ball for ball. Everything the match
does is driven by ``config/sim_engine.json``; this module is only the loop:
toss, innings, overs, sessions, rain, new balls, follow-on, declarations.
"""

from dataclasses import dataclass
from typing import List, Optional

from engine import pitch_registry
from engine.sim import (ball_aging, commentary, drama, factors, outcome,
                        rain_dls, situation, stadium as stadium_mod, weather as weather_mod)
from engine.sim.config import get_config
from engine.sim.models import Conditions, MatchTime, Stadium, Weather
from engine.sim.player_adapter import to_batter, to_bowler
from engine.sim.rng import SimRng
from engine.sim.time_of_day import dew_intensity


@dataclass
class Team:
    name: str
    players: List[dict]          # engine player dicts, in batting order
    short: str = ""


@dataclass
class MatchSetup:
    fmt: str = "T20"                        # T20 | ODI | Test
    team1: Optional[Team] = None
    team2: Optional[Team] = None
    pitch: Optional[str] = None             # None → the stadium's typical pitch
    stadium: Optional[Stadium] = None
    weather: Optional[Weather] = None       # None → from the stadium's climate
    time: Optional[MatchTime] = None        # None → format default
    grass: Optional[str] = None
    seed: Optional[object] = None
    overs: Optional[int] = None             # override the format's overs
    commentary: bool = True


# ── per-innings bookkeeping ─────────────────────────────────────────────

class _Innings:
    def __init__(self, number, bat_team, bowl_team, cfg):
        self.number = number
        self.bat_team = bat_team
        self.bowl_team = bowl_team
        self.batters = [to_batter(p, cfg) for p in bat_team.players]
        self.bowlers = [to_bowler(p, cfg) for p in bowl_team.players]
        self.raw_bowl = {b.id: p for b, p in zip(self.bowlers, bowl_team.players)}
        self.runs = self.wkts = self.legal_balls = self.extras = 0
        self.striker, self.non_striker, self.next_in = 0, 1, 2
        self.bat = {b.id: {"name": b.name, "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                           "out": None, "position": i + 1}
                    for i, b in enumerate(self.batters)}
        self.bowl = {b.id: {"name": b.name, "balls": 0, "runs": 0, "wkts": 0, "maidens": 0,
                            "dots": 0, "kind": b.kind} for b in self.bowlers}
        self.stamina = {b.id: b.stamina for b in self.bowlers}
        self.spell = {}
        self.fow = []
        self.over_runs = []
        self.over_wkts = []
        self.lines = []
        self.wagon = {}
        self.partnership = 0
        self.partnerships = []
        self.last_event = None
        self.declared = False
        self.all_out = False
        self.target = None
        self.max_overs = None
        self.prev_bowler = None

    @property
    def overs_str(self):
        return f"{self.legal_balls // 6}.{self.legal_balls % 6}"

    def summary(self):
        return {
            "number": self.number, "team": self.bat_team.name,
            "bowling_team": self.bowl_team.name,
            "runs": self.runs, "wickets": self.wkts, "overs": self.overs_str,
            "legal_balls": self.legal_balls, "extras": self.extras,
            "declared": self.declared, "all_out": self.all_out,
            "target": self.target, "max_overs": self.max_overs,
            "batting": list(self.bat.values()), "bowling": [
                dict(v, overs=f"{v['balls'] // 6}.{v['balls'] % 6}")
                for v in self.bowl.values() if v["balls"]],
            "fow": self.fow, "over_runs": self.over_runs, "over_wkts": self.over_wkts,
            "partnerships": self.partnerships, "wagon": self.wagon,
            "commentary": self.lines,
        }


_ZONES = ("fine leg", "square leg", "midwicket", "long on", "long off",
          "cover", "point", "third man")


class _Match:
    def __init__(self, setup, cfg):
        self.cfg = cfg
        self.setup = setup
        self.fmt = setup.fmt if setup.fmt in ("T20", "ODI", "Test", "The100") else "T20"
        seed = setup.seed if setup.seed is not None else cfg["meta"].get("seed")
        self.rng = SimRng(seed)
        self.seed = self.rng.seed
        self.stadium = setup.stadium or stadium_mod.neutral(cfg)
        self.pitch = pitch_registry.normalise(setup.pitch or self.stadium.typical_pitch)
        wrng = self.rng.derive("weather-init")
        self.weather = setup.weather or weather_mod.from_climate(dict(self.stadium.climate), wrng)
        fcfg = cfg["formats"][self.fmt]
        self.overs = int(setup.overs or fcfg.get("overs", 0) or 0)
        self.time = setup.time or self._default_time()
        self.grass = setup.grass
        self.innings: List[_Innings] = []
        self.effects = []            # (innings, over, Effect) — relevance-filtered
        self.events = []             # turning-point candidates
        self.drama_events = []
        self.rain_events = []
        self.rained = False
        self.rain_count = 0
        self.dls_target = None
        # Test clock
        self.test_overs_used = 0.0
        self.test_hot_sessions = 0
        self.test_afternoon_sessions = 0
        self._last_session_idx = 0
        self.stumps = []             # end-of-day reports (Test)

    # ── clock ───────────────────────────────────────────────────────────
    def _default_time(self):
        rng = self.rng.derive("time")
        if self.fmt == "Test":
            dn = rng.chance(0.15)
            return MatchTime(session="Afternoon" if dn else "Morning", is_day_night=dn,
                             ball_color="Pink" if dn else "Red",
                             start_hour=14.0 if dn else 10.5)
        session = rng.choice(["Afternoon", "Evening", "Night"] if self.fmt != "ODI"
                             else ["Morning", "Afternoon", "Afternoon"])
        start = self.cfg["timeOfDay"]["sessionStartHour"][session]
        return MatchTime(session=session, is_day_night=session in ("Evening", "Night") or
                         (self.fmt == "ODI" and session == "Afternoon"),
                         ball_color="White", start_hour=start)

    def hour_at(self, inn_number, balls):
        t = self.cfg["timeOfDay"]
        if self.fmt == "Test":
            return self._test_hour()
        mpo = t["minutesPerOver"][self.fmt]
        start = self.time.start_hour
        if inn_number == 2:
            # The chase starts when the first innings actually ended, not when
            # its full quota would have — an early all-out brings the dew in later.
            inn1_overs = self.innings[0].legal_balls / 6.0 if self.innings else self.overs
            start += (inn1_overs * mpo + t["inningsBreakMinutes"][self.fmt]) / 60.0
        return start + (balls / 6.0) * mpo / 60.0

    def _test_session_info(self):
        f = self.cfg["formats"]["Test"]
        per = f["oversPerDay"] / f["sessionsPerDay"]
        idx = int(self.test_overs_used // per)
        day = idx // f["sessionsPerDay"] + 1
        return idx, day, idx % f["sessionsPerDay"], per

    def _test_hour(self):
        idx, day, s, per = self._test_session_info()
        key = "TestDN" if self.time.is_day_night else "Test"
        sess = self.cfg["timeOfDay"]["sessionHours"][key][s]
        into = self.test_overs_used - idx * per
        return sess["start"] + into * self.cfg["timeOfDay"]["minutesPerOver"]["Test"] / 60.0

    def days_played(self):
        """The day a Test finished on (1-5)."""
        f = self.cfg["formats"]["Test"]
        return min(f["days"], self._test_session_info()[1])

    def test_over(self):
        f = self.cfg["formats"]["Test"]
        return self.test_overs_used >= f["days"] * f["oversPerDay"]

    def _tick_test_clock(self, overs=1.0):
        before, _, _, _ = self._test_session_info()
        self.test_overs_used += overs
        after, _, _, _ = self._test_session_info()
        f = self.cfg["formats"]["Test"]
        for idx in range(before, after):
            s = idx % f["sessionsPerDay"]
            key = "TestDN" if self.time.is_day_night else "Test"
            name = self.cfg["timeOfDay"]["sessionHours"][key][s]["name"]
            if name == "Afternoon":
                self.test_afternoon_sessions += 1
            if weather_mod.is_hot(self.weather, self.cfg):
                self.test_hot_sessions += 1
            if s == f["sessionsPerDay"] - 1:
                self._close_day(idx // f["sessionsPerDay"] + 1)
                # overnight: the weather resets toward the ground's climate
                self.weather = weather_mod.from_climate(
                    dict(self.stadium.climate) or {}, self.rng.derive("night", idx))

    def _close_day(self, day):
        """Stumps: report the day, then lose the overs a real day never gets in.

        Slow over rates and bad light mean a Test day rarely reaches its full
        90; ``overShortfallPerDay`` burns that gap off the clock, which is
        what leaves time for a draw.
        """
        f = self.cfg["formats"]["Test"]
        if self.innings and not self.test_over():
            inn = self.innings[-1]
            self.stumps.append({"day": day, "text": self._stumps_text(day),
                                "team": inn.bat_team.name, "runs": inn.runs,
                                "wickets": inn.wkts, "innings": inn.number})
        lo, hi = (f.get("overShortfallPerDay") or [0, 0])[:2]
        if hi > 0 and day < f["days"]:
            self.test_overs_used += self.rng.derive("shortfall", day).randint(int(lo), int(hi))

    def _stumps_text(self, day):
        inn = self.innings[-1]
        state = f"{inn.runs}/{inn.wkts}"
        if inn.declared:
            state += " dec"
        elif inn.all_out:
            state = f"{inn.runs} all out"
        totals = {}
        for i in self.innings:
            totals[i.bat_team.name] = totals.get(i.bat_team.name, 0) + i.runs
        bat, bowl = inn.bat_team.name, inn.bowl_team.name
        if inn.target is not None:
            need = inn.target - inn.runs
            tail = (f"need {need} more with {10 - inn.wkts} wickets in hand"
                    if need > 0 else "target reached")
        elif len(self.innings) == 1:
            tail = f"{inn.overs_str} overs"
        else:
            diff = totals.get(bat, 0) - totals.get(bowl, 0)
            tail = (f"lead by {diff}" if diff > 0 else f"trail by {-diff}" if diff < 0
                    else "scores level")
        return f"Stumps, Day {day}: {bat} {state} ({tail})"

    def _saving_match(self, inn):
        """True when a Test 4th-innings chase is out of reach on the clock left."""
        if self.fmt != "Test" or inn.number != 4 or inn.target is None:
            return False
        sv = self.cfg["situation"].get("saveMatch")
        if not sv:
            return False
        f = self.cfg["formats"]["Test"]
        overs_left = f["days"] * f["oversPerDay"] - self.test_overs_used
        if overs_left <= 0:
            return True
        need = inn.target - inn.runs
        if overs_left < sv.get("minOversLeft", 15):
            return need > overs_left * 3.0
        return need / overs_left > sv["requiredRateAbove"]

    # ── conditions ──────────────────────────────────────────────────────
    def conditions(self, inn, over_idx, end):
        if self.fmt == "Test":
            idx, day, _, _ = self._test_session_info()
            wear = 0.0
            test_session, afternoon, hot = idx, self.test_afternoon_sessions, self.test_hot_sessions
        else:
            day, test_session, afternoon, hot = 1, 0, 0, 0
            wear = (self.cfg["pitchDecay"]["limitedOversInnings2Crack"]
                    if inn.number == 2 else 0.0)
            if weather_mod.is_hot(self.weather, self.cfg):
                wear *= self.cfg["weather"]["heat"]["crackRate"]
        return Conditions(
            pitch=self.pitch, weather=self.weather, time=self.time, stadium=self.stadium,
            fmt=self.fmt, hour=self.hour_at(inn.number, inn.legal_balls),
            innings=inn.number, over=over_idx, test_day=day, test_session=test_session,
            afternoon_sessions=afternoon, hot_sessions=hot, grass=self.grass, wear=wear,
            bowling_end=end, rained=self.rained)

    # ── bowling selection ───────────────────────────────────────────────
    def _quota_left(self, inn, b):
        if self.fmt == "Test":
            return 10 ** 6
        q = self.cfg["formats"][self.fmt]["maxBowlerOvers"]
        return q - inn.bowl[b.id]["balls"] // 6

    def _feasible(self, inn, choice, overs_left_after):
        if self.fmt == "Test" or overs_left_after <= 0:
            return True
        half = (overs_left_after + 1) // 2
        cap = 0
        for b in inn.bowlers:
            q = self._quota_left(inn, b) - (1 if b.id == choice.id else 0)
            cap += min(max(0, q), overs_left_after // 2 if b.id == choice.id else half)
        return cap >= overs_left_after

    def pick_bowler(self, inn, over_idx, end, ball):
        cfg = self.cfg
        cond = self.conditions(inn, over_idx, end)
        overs_left_after = (inn.max_overs - over_idx - 1) if inn.max_overs else 0
        death = situation.is_death_over(self.fmt, over_idx, cfg)
        spell_cap = cfg["formats"]["Test"]["maxSpellOvers"]
        best, best_score = None, -1e9
        rng = self.rng.derive("pick", inn.number, over_idx)
        for b in inn.bowlers:
            if b.id == inn.prev_bowler or self._quota_left(inn, b) <= 0:
                continue
            if not self._feasible(inn, b, overs_left_after):
                continue
            if self.fmt == "Test" and inn.spell.get(b.id, 0) >= spell_cap[b.kind]:
                continue
            fs = factors.compose(cond, ball, cfg, bowler=b)
            skill = (b.spin if b.is_spin else (b.pace + b.swing + b.seam) / 3.0)
            score = skill * outcome.bowler_help(b, fs, cfg) * (0.5 + b.accuracy / 200.0)
            score *= 0.6 + 0.4 * inn.stamina[b.id] / 100.0
            if death and b.is_death_specialist:
                score *= cfg["situation"]["death"]["specialist"]
            if self.fmt != "Test" and not death and b.is_death_specialist and overs_left_after < 8:
                score *= 0.8   # keep him for the death
            score *= 1 + rng.uniform(-0.05, 0.05)
            if score > best_score:
                best, best_score = b, score
        if best is None:  # nobody legal: fall back to anyone with quota
            pool = [b for b in inn.bowlers if self._quota_left(inn, b) > 0] or inn.bowlers
            best = max(pool, key=lambda b: (b.id != inn.prev_bowler, self._quota_left(inn, b)))
        return best

    # ── the ball loop ───────────────────────────────────────────────────
    def play_over(self, inn, over_idx, ball_holder):
        cfg = self.cfg
        end = "A" if over_idx % 2 == 0 else "B"
        ball = ball_holder[end]
        bowler = self.pick_bowler(inn, over_idx, end, ball)
        inn.prev_bowler = bowler.id
        for b in inn.bowlers:
            if b.id != bowler.id:
                inn.spell[b.id] = 0
                inn.stamina[b.id] = min(b.stamina, inn.stamina[b.id] + cfg["situation"]["fatigue"]["recoverPerOver"])
        inn.spell[bowler.id] = inn.spell.get(bowler.id, 0) + 1

        brng = self.rng.derive("over", inn.number, over_idx)
        runs_needed = (inn.target - inn.runs) if inn.target else None
        balls_left = ((inn.max_overs * 6 - inn.legal_balls) if inn.max_overs else None)
        close = drama.is_close_chase(runs_needed, balls_left) if inn.target and balls_left else False
        event = drama.roll_over(brng.derive("drama"), cfg, bowler_is_spin=bowler.is_spin,
                                close_chase=close, is_chase=bool(inn.target))
        if event:
            self.drama_events.append({"innings": inn.number, "over": over_idx + 1,
                                      "kind": event.kind, "text": event.text})
            self.events.append({"innings": inn.number, "over": over_idx + 1,
                                "score": 12 if self.fmt == "Test" else 22, "text": event.text})

        over_runs = over_wkts = legal = 0
        bowler_runs_this_over = 0
        sixes_forced = 2 if (event and event.kind == "hugeOver") else 0
        while legal < 6:
            if self._innings_done(inn):
                break
            batter = inn.batters[inn.striker]
            bstat = inn.bat[batter.id]
            saving = self._saving_match(inn)
            cond = self.conditions(inn, over_idx, end)
            fs = factors.compose(cond, ball, cfg, bowler=bowler)
            runs_needed = (inn.target - inn.runs) if inn.target else None
            balls_left = ((inn.max_overs * 6 - inn.legal_balls) if inn.max_overs else None)
            sit = situation.evaluate(situation.Situation(
                fmt=self.fmt, innings=inn.number, over=over_idx,
                runs_needed=runs_needed, balls_left=balls_left, wickets_down=inn.wkts,
                batter_balls=bstat["balls"], batting_position=bstat["position"],
                last_event=inn.last_event, bowler_death_specialist=bowler.is_death_specialist,
                is_chase=bool(inn.target) and self.fmt != "Test",
                save_match=saving), cfg)
            fat = cfg["situation"]["fatigue"]
            fatigue_acc = fat["lowStaminaAccuracy"] if inn.stamina[bowler.id] < fat["lowStaminaBelow"] else 1.0
            mults = outcome.bucket_multipliers(batter, bowler, fs, cfg, sit, fatigue_acc, self.fmt)
            if inn.target and inn.max_overs:
                bat_t, bowl_t = drama.close_finish_tilt(
                    cfg, runs_needed=runs_needed, balls_left=balls_left,
                    wickets_left=10 - inn.wkts, over=over_idx, total_overs=inn.max_overs)
                for k in ("Four", "Six"):
                    mults[k] *= bat_t / bowl_t
                mults["Wicket"] *= bowl_t / bat_t
            _, shot_q, _, _ = outcome.edges(batter, bowler, fs, sit, cfg, fatigue_acc, self.fmt)
            ball_no = legal + 1
            drop = cfg["drama"]["baseDropChance"] + fs.drop_add
            ev_text = None
            res = None
            if event and event.ball == ball_no:
                res, ev_text = self._drama_ball(event, bowler, brng)
            if res is None and sixes_forced and ball_no >= 4:
                sixes_forced -= 1
                res = outcome.BallResult("Six", runs=6)
                ev_text = event.text if event else None
            if res is None:
                res = outcome.resolve(cfg["outcome"]["base"][self._base_key()], mults, brng, cfg,
                                      bowler=bowler, fs=fs, shot_quality=shot_q, drop_chance=drop)
            self._record_effects(inn, over_idx, fs, bowler)
            label = f"{inn.legal_balls // 6}.{inn.legal_balls % 6 + 1}"
            if setup_comm(self):
                inn.lines.append(commentary.line(label, bowler.name, batter.name, res, brng,
                                                 fs=fs, bowler=bowler, event=ev_text))
            r = self._apply_result(inn, batter, bowler, res, over_idx, brng)
            over_runs += r
            if res.legal:
                legal += 1
            if res.is_wicket:
                over_wkts += 1
            if not (res.extra_type in ("Leg Bye", "Bye")):
                bowler_runs_this_over += r
        # end of over
        inn.over_runs.append(over_runs)
        inn.over_wkts.append(over_wkts)
        bs = inn.bowl[bowler.id]
        if legal == 6 and bowler_runs_this_over == 0:
            bs["maidens"] += 1
        drain = cfg["situation"]["fatigue"]["staminaPerOver"][bowler.kind]
        if weather_mod.is_hot(self.weather, cfg):
            drain *= cfg["weather"]["heat"]["staminaDrain"]
        inn.stamina[bowler.id] = max(0.0, inn.stamina[bowler.id] - drain)
        inn.striker, inn.non_striker = inn.non_striker, inn.striker
        big = {"T20": 18, "The100": 16, "ODI": 15, "Test": 12}[self.fmt]
        if over_runs >= big:
            self.events.append({"innings": inn.number, "over": over_idx + 1,
                                "score": over_runs * 1.5,
                                "text": f"{over_runs} came off over {over_idx + 1} ({bowler.name})"})
        if over_wkts >= 2:
            self.events.append({"innings": inn.number, "over": over_idx + 1,
                                "score": 25 * over_wkts,
                                "text": f"{bowler.name} struck {over_wkts} times in over {over_idx + 1}"})
        # the ball ages, the weather moves
        dew = dew_intensity(self.conditions(inn, over_idx, end), cfg)
        ball_holder[end] = ball_aging.advance(ball, self.pitch, cfg, 1.0, dew=dew)
        if not self._two_balls():
            ball_holder["A" if end == "B" else "B"] = ball_holder[end]
        self.weather = weather_mod.drift(self.weather, self.rng.derive("wx", inn.number, over_idx), cfg)
        if self.fmt == "Test":
            self._tick_test_clock(1.0)
        return over_runs, over_wkts

    def _two_balls(self):
        return self.fmt == "ODI" and self.cfg["ballAging"]["newBall"]["odiTwoBalls"]

    def _base_key(self):
        return self.fmt if self.fmt in self.cfg["outcome"]["base"] else "T20"

    def _drama_ball(self, event, bowler, rng):
        k = event.kind
        if k == "droppedCatch":
            return outcome.BallResult("Dot", runs=1, dropped=True), event.text
        if k == "directHit":
            return outcome.BallResult("Wicket", wicket_type="Run Out"), event.text
        if k == "stumping" and bowler.is_spin:
            return outcome.BallResult("Wicket", wicket_type="Stumped"), event.text
        if k == "drsOverturn":
            if rng.chance(0.5):
                return outcome.BallResult("Wicket", wicket_type="LBW"), event.text + " — given out on review"
            return outcome.BallResult("Dot", runs=0), event.text + " — reprieve for the batter"
        if k == "crunchMisfield":
            return outcome.BallResult("Four", runs=4), event.text
        if k == "rainScare":
            return None, None
        return None, None

    def _record_effects(self, inn, over_idx, fs, bowler):
        pace_ch = {"swing", "seam", "pace", "pace_wkt", "bounce"}
        spin_ch = {"spin", "spin_wkt"}
        for e in fs.effects:
            if e.channel in pace_ch and bowler.is_spin:
                continue
            if e.channel in spin_ch and not bowler.is_spin:
                continue
            if e.key == "reverse_swing" and bowler.is_spin:
                continue
            self.effects.append(e)

    def _apply_result(self, inn, batter, bowler, res, over_idx, rng):
        bstat, wstat = inn.bat[batter.id], inn.bowl[bowler.id]
        total = res.total_runs
        inn.runs += total
        inn.partnership += total
        if res.extra_type:
            inn.extras += res.extra_runs
            if res.extra_type in ("Wide", "No Ball"):
                wstat["runs"] += total
            if res.runs:
                bstat["runs"] += res.runs
        else:
            bstat["runs"] += res.runs
            wstat["runs"] += res.runs
        if res.legal:
            inn.legal_balls += 1
            wstat["balls"] += 1
            if res.extra_type != "Wide":
                bstat["balls"] += 1
            if total == 0 and not res.is_wicket:
                wstat["dots"] += 1
        elif res.extra_type == "No Ball":
            bstat["balls"] += 1
        off_bat = res.extra_type in (None, "No Ball")   # byes/leg byes are not hits
        if res.runs == 4 and off_bat:
            bstat["fours"] += 1
            inn.last_event = "bat"
        elif res.runs == 6 and off_bat:
            bstat["sixes"] += 1
            inn.last_event = "bat"
        if res.runs and off_bat:
            zone = rng.choice(_ZONES)
            inn.wagon[zone] = inn.wagon.get(zone, 0) + res.runs
        if res.dropped:
            self.events.append({"innings": inn.number, "over": over_idx + 1,
                                "score": 10 + bstat["runs"] * 0.3,
                                "text": f"{batter.name} dropped on {bstat['runs'] - res.runs}"})
        if res.is_wicket:
            inn.wkts += 1
            inn.last_event = "bowl"
            out_id = batter.id
            if res.wicket_type == "Run Out" and rng.chance(0.35):
                out_id = inn.batters[inn.non_striker].id
            ostat = inn.bat[out_id]
            if res.wicket_type != "Run Out":
                wstat["wkts"] += 1
                ostat["out"] = f"{res.wicket_type.lower()} b {bowler.name}" if res.wicket_type != "Bowled" else f"b {bowler.name}"
                if res.wicket_type == "LBW":
                    ostat["out"] = f"lbw b {bowler.name}"
                if res.wicket_type == "Caught":
                    fielders = [p.get("name") for p in inn.bowl_team.players if p.get("name")]
                    fielder = rng.choice(fielders) if fielders else "sub"
                    ostat["out"] = (f"c & b {bowler.name}" if fielder == bowler.name
                                    else f"c {fielder} b {bowler.name}")
                if res.wicket_type == "Stumped":
                    ostat["out"] = f"st b {bowler.name}"
            else:
                ostat["out"] = "run out"
            inn.fow.append({"wicket": inn.wkts, "runs": inn.runs, "over": inn.overs_str,
                            "batter": ostat["name"], "bowler": bowler.name})
            inn.partnerships.append({"wicket": inn.wkts, "runs": inn.partnership})
            big = 50 if self.fmt != "Test" else 100
            if ostat["runs"] >= (30 if self.fmt != "Test" else 60) or inn.partnership >= big:
                self.events.append({"innings": inn.number, "over": over_idx + 1,
                                    "score": ostat["runs"] + inn.partnership * 0.3,
                                    "text": f"{ostat['name']} out for {ostat['runs']} ({res.wicket_type}, {bowler.name})"
                                            + (f", breaking a {inn.partnership}-run stand" if inn.partnership >= big else "")})
            inn.partnership = 0
            if inn.wkts >= 10 or inn.next_in >= len(inn.batters):
                inn.all_out = True
            else:
                idx = inn.next_in
                inn.next_in += 1
                if out_id == batter.id:
                    inn.striker = idx
                else:
                    inn.non_striker = idx
            self._collapse_check(inn, over_idx)
        # Odd runs actually run swap ends — including off the bat from a no-ball.
        ran = res.runs + (res.extra_runs if res.extra_type in ("Leg Bye", "Bye") else 0)
        if not res.is_wicket and (res.legal or res.extra_type == "No Ball") and ran % 2 == 1:
            inn.striker, inn.non_striker = inn.non_striker, inn.striker
        return total

    def _collapse_check(self, inn, over_idx):
        if len(inn.fow) < 3:
            return
        a, c = inn.fow[-3], inn.fow[-1]
        def balls(o):
            ov, b = o.split(".")
            return int(ov) * 6 + int(b)
        if balls(c["over"]) - balls(a["over"]) <= 18 and c["runs"] - a["runs"] <= 20:
            self.events.append({"innings": inn.number, "over": over_idx + 1, "score": 45,
                                "text": f"Collapse: {inn.bat_team.name} lost wickets {a['wicket']}-{c['wicket']} "
                                        f"for {c['runs'] - a['runs']} runs (reaching {c['runs']}/{c['wicket']})"})

    def _innings_done(self, inn):
        if inn.all_out or inn.declared:
            return True
        if inn.target is not None and inn.runs >= inn.target:
            return True
        if inn.max_overs is not None and inn.legal_balls >= inn.max_overs * 6:
            return True
        if self.fmt == "Test" and self.test_over():
            return True
        return False

    # ── rain ────────────────────────────────────────────────────────────
    def _rain_check(self, inn, over_idx):
        rrng = self.rng.derive("rain", inn.number, over_idx)
        if not weather_mod.rain_starts(self.weather, rrng, self.cfg, self.rain_count, self.fmt):
            return
        self.rain_count += 1
        self.rained = True
        lost = weather_mod.overs_lost(rrng, self.fmt, self.cfg)
        if self.fmt == "Test":
            self.rain_events.append({"innings": inn.number, "over": over_idx + 1, "overs_lost": lost,
                                     "day": self._test_session_info()[1],
                                     "text": f"Rain! {lost} overs lost to the weather"})
            self._tick_test_clock(float(lost))
            self.events.append({"innings": inn.number, "over": over_idx + 1, "score": 30,
                                "text": f"Rain cost {lost} overs of play"})
            return
        min_o = self.cfg["weather"]["rain"]["minOversPerSide"][self.fmt]
        bowled = inn.legal_balls / 6.0
        lost = rain_dls.cap_overs_lost(inn.max_overs, bowled, lost, min_o)
        if lost <= 0:
            self.rain_events.append({"innings": inn.number, "over": over_idx + 1, "overs_lost": 0,
                                     "text": "A shower — covers on, no overs lost"})
            return
        if inn.number == 1:
            self._inn1_rain = (bowled, inn.wkts, lost)
            inn.max_overs -= lost
            text = f"Rain! {lost} overs lost — now {inn.max_overs} overs a side"
        else:
            first = self.innings[0]
            new_target = rain_dls.second_innings_target(
                first.runs, inn.max_overs, bowled, lost, inn.wkts)
            inn.max_overs -= lost
            inn.target = new_target
            self.dls_target = new_target
            text = (f"Rain! {lost} overs lost — DLS target revised to {new_target} "
                    f"from {inn.max_overs} overs")
        self.rain_events.append({"innings": inn.number, "over": over_idx + 1,
                                 "overs_lost": lost, "text": text})
        self.events.append({"innings": inn.number, "over": over_idx + 1, "score": 50, "text": text})

    # ── innings drivers ─────────────────────────────────────────────────
    def play_limited_innings(self, inn):
        color = self.time.ball_color
        balls = {"A": ball_aging.new_ball(color, 1),
                 "B": ball_aging.new_ball(color, 2 if self._two_balls() else 1)}
        over = 0
        while not self._innings_done(inn):
            self._rain_check(inn, over)
            if self._innings_done(inn):
                break
            self.play_over(inn, over, balls)
            over += 1

    def play_test_innings(self, inn, lead_before):
        color = self.time.ball_color
        ball = ball_aging.new_ball(color, 1)
        holder = {"A": ball, "B": ball}
        f = self.cfg["formats"]["Test"]
        over = 0
        while not self._innings_done(inn):
            self._rain_check(inn, over)
            if self._innings_done(inn):
                break
            if ball_aging.needs_new_ball(holder["A"], "Test", self.cfg):
                nb = ball_aging.new_ball(color, holder["A"].number + 1)
                holder = {"A": nb, "B": nb}
                inn.lines.append(f"{inn.overs_str} New ball taken")
            self.play_over(inn, over, holder)
            over += 1
            lead = lead_before + inn.runs
            remaining = f["days"] * f["oversPerDay"] - self.test_overs_used
            if inn.number in (1, 2) and inn.runs >= f["declareAbove"] and not inn.all_out:
                inn.declared = True
            elif inn.number == 3 and not inn.all_out and lead > 0:
                want = max(250.0, min(f["declareLeadInnings3"], remaining * 3.2))
                if lead >= want and remaining >= 45:
                    inn.declared = True
            if inn.declared:
                inn.lines.append(f"{inn.overs_str} {inn.bat_team.name} declare at {inn.runs}/{inn.wkts}")

    def toss(self):
        trng = self.rng.derive("toss")
        t1, t2 = self.setup.team1, self.setup.team2
        winner = t1 if trng.chance(0.5) else t2
        call = pitch_registry.toss_call(self.pitch, "Night" if self.time.is_day_night else None)
        if self.fmt == "Test":
            call = "bat" if self.pitch in ("Dry", "Dusty", "Flat", "Even", "Hard", "Dead") else "bowl"
        bat_first = winner if call == "bat" else (t2 if winner is t1 else t1)
        return winner, call, bat_first

    def run(self):
        t1, t2 = self.setup.team1, self.setup.team2
        winner, call, bat_first = self.toss()
        bowl_first = t2 if bat_first is t1 else t1
        self.toss_info = {"winner": winner.name, "decision": call}
        if self.fmt == "Test":
            return self._run_test(bat_first, bowl_first)
        inn1 = _Innings(1, bat_first, bowl_first, self.cfg)
        inn1.max_overs = self.overs
        self.innings.append(inn1)
        self.play_limited_innings(inn1)
        inn2 = _Innings(2, bowl_first, bat_first, self.cfg)
        inn2.max_overs = inn1.max_overs if inn1.max_overs < self.overs else self.overs
        inn2.target = inn1.runs + 1
        if inn1.max_overs < self.overs and getattr(self, "_inn1_rain", None):
            bowled, wk, lost = self._inn1_rain
            inn2.target = rain_dls.first_innings_target(
                inn1.runs, self.overs, bowled, wk, lost,
                self.cfg["formats"].get(self.fmt, {}).get("dlsG50", 245))
            self.dls_target = inn2.target
        self.innings.append(inn2)
        self.play_limited_innings(inn2)
        if inn2.runs >= inn2.target:
            left = 10 - inn2.wkts
            balls_left = inn2.max_overs * 6 - inn2.legal_balls
            result = {"winner": inn2.bat_team.name, "margin": f"{left} wicket{'s' if left != 1 else ''}",
                      "text": f"{inn2.bat_team.name} won by {left} wickets ({balls_left} balls left)"}
        elif inn2.runs == inn2.target - 1:
            result = {"winner": None, "margin": "tie", "text": "Match tied"}
        else:
            m = inn2.target - 1 - inn2.runs
            result = {"winner": inn1.bat_team.name, "margin": f"{m} run{'s' if m != 1 else ''}",
                      "text": f"{inn1.bat_team.name} won by {m} runs" + (" (DLS)" if self.dls_target else "")}
        if self.dls_target and result["winner"] == inn2.bat_team.name:
            result["text"] += " (DLS)"
        return result

    def _run_test(self, a, b):
        cfg = self.cfg["formats"]["Test"]
        inns = self.innings
        i1 = _Innings(1, a, b, self.cfg)
        inns.append(i1)
        self.play_test_innings(i1, 0)
        if self.test_over():
            return self._test_result()
        i2 = _Innings(2, b, a, self.cfg)
        inns.append(i2)
        self.play_test_innings(i2, -i1.runs)
        if self.test_over():
            return self._test_result()
        lead = i1.runs - i2.runs
        follow_on = False
        if lead >= cfg["followOn"]:
            enforce = (not cfg["followOnOptional"]) or self.rng.derive("followon").chance(cfg["followOnEnforceChance"])
            follow_on = enforce
        if follow_on:
            self.events.append({"innings": 2, "over": 0, "score": 40,
                                "text": f"{a.name} enforced the follow-on with a lead of {lead}"})
            i3 = _Innings(3, b, a, self.cfg)
            inns.append(i3)
            self.play_test_innings(i3, -lead)
            if self.test_over() and not i3.all_out:
                return self._test_result()
            need = lead - i3.runs
            if need <= 0:            # level scores still leave one run to get
                i4 = _Innings(4, a, b, self.cfg)
                i4.target = -need + 1
                inns.append(i4)
                self.play_test_innings(i4, 0)
        else:
            i3 = _Innings(3, a, b, self.cfg)
            inns.append(i3)
            self.play_test_innings(i3, lead)
            if self.test_over() and not (i3.all_out or i3.declared):
                return self._test_result()
            target = lead + i3.runs + 1
            if target <= 0:          # still behind after batting twice
                return self._test_result()
            i4 = _Innings(4, b, a, self.cfg)
            i4.target = target
            inns.append(i4)
            self.play_test_innings(i4, 0)
        return self._test_result()

    def _test_result(self):
        inns = self.innings
        totals = {}
        for i in inns:
            totals[i.bat_team.name] = totals.get(i.bat_team.name, 0) + i.runs
        names = [self.setup.team1.name, self.setup.team2.name]
        last = inns[-1]
        if len(inns) == 3 and last.all_out:
            # innings victory: the side that batted twice is still behind
            other = [n for n in names if n != last.bat_team.name][0]
            if totals[last.bat_team.name] < totals[other]:
                m = totals[other] - totals[last.bat_team.name]
                return {"winner": other, "margin": f"an innings and {m} runs",
                        "text": f"{other} won by an innings and {m} runs"}
        if len(inns) == 4 and last.target is not None:
            if last.runs >= last.target:
                w = 10 - last.wkts
                return {"winner": last.bat_team.name, "margin": f"{w} wickets",
                        "text": f"{last.bat_team.name} won by {w} wickets"}
            if last.all_out:
                m = last.target - 1 - last.runs
                if m == 0:
                    return {"winner": None, "margin": "tie", "text": "Match tied"}
                return {"winner": last.bowl_team.name, "margin": f"{m} runs",
                        "text": f"{last.bowl_team.name} won by {m} runs"}
        return {"winner": None, "margin": "draw", "text": "Match drawn"}


def setup_comm(m):
    return m.setup.commentary and m.cfg["output"]["commentary"]


def simulate_match(setup, cfg=None):
    """Play one match. Returns a JSON-serialisable result dict."""
    cfg = cfg or get_config()
    m = _Match(setup, cfg)
    result = m.run()
    from engine.sim import report
    return report.build_result(m, result)
