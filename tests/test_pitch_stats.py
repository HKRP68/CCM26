"""/pitchstats — what gets recorded, what gets counted, and what gets rendered.

The parts worth pinning are the ones where a wrong answer would be invisible:

  * only /letsplay and Challenge League matches count, and a practice match
    against the AI captain never does — the bot's picks would drown the human
    record and the only thing that distinguishes one is a flag on the live state
  * a chase won in 17.2 overs is rated over the balls it actually lasted, not
    over the match length, or every successful chase deflates the pitch's rate
  * the bat-first / bat-second split follows the side that batted first, which
    is NOT the side the Match row calls user1
  * recording the same match twice (a retried finalise, a Super Over finishing
    a match the main path already wrote) must not count it twice
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG = itertools.count(880_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.pitch_stats")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


def _duel(overs, phase, bat, bowl, runs, wickets=0, balls=6):
    """``overs`` identical entries of one approach-duel over."""
    return [{"over": i + 1, "phase": phase, "bat": bat, "bowl": bowl,
             "runs": runs, "wickets": wickets, "balls": balls}
            for i in range(overs)]


class PitchStatsCase(unittest.TestCase):

    def setUp(self):
        from database import get_session
        from services import pitch_stats

        self.session = get_session()
        self.ps = pitch_stats
        self.home, self.away = self._users()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _users(self):
        from models import User
        made = []
        for _ in range(2):
            tg = next(_TG)
            u = User(telegram_id=tg, username=f"u{tg}", first_name=f"P{tg}")
            self.session.add(u)
            made.append(u)
        self.session.flush()
        return made

    def _match(self, pitch="Green", match_type="letsplay", tournament_id=None,
               toss_decision="bowl", toss_winner=None, winner=None):
        from models import Match
        m = Match(user1_id=self.home.id, user2_id=self.away.id,
                  status="completed", match_type=match_type, overs=20,
                  pitch_type=pitch, tournament_id=tournament_id,
                  toss_decision=toss_decision,
                  toss_winner_id=(toss_winner.id if toss_winner else None),
                  winner_id=(winner.id if winner else None))
        self.session.add(m)
        self.session.flush()
        return m

    def _state(self, inn1_runs=160, inn1_wkts=6, inn1_balls=120,
               inn2_runs=120, inn2_wkts=10, inn2_balls=102, bot=False,
               duels=None, inn1_duels=None):
        """A finished match's live state, reduced to what the recorder reads."""
        return {
            "is_bot_match": bot,
            "overs": 20, "ball_format": "T20",
            "inn1_runs": inn1_runs, "inn1_wickets": inn1_wkts,
            "inn1_balls": inn1_balls,
            "total_runs": inn2_runs, "total_wickets": inn2_wkts,
            "current_over": inn2_balls // 6 + 1, "current_ball": inn2_balls % 6,
            "inn1_bat_team": "Home", "inn1_bowl_team": "Away",
            "inn1_bat_team_id": self.home.id, "inn1_bowl_team_id": self.away.id,
            "inn1_approach_log": inn1_duels if inn1_duels is not None
            else _duel(20, "middle", "balanced", "balanced", 8),
            "approach_log": duels if duels is not None
            else _duel(17, "middle", "rotate", "mixed", 7),
        }

    def _result(self, winner="Home", tie=False, margin_type="runs"):
        return {"tie": tie, "winner": None if tie else winner,
                "loser": None if tie else ("Away" if winner == "Home" else "Home"),
                "margin_type": margin_type, "margin": 10}

    # ── What counts ────────────────────────────────────────────────

    def test_letsplay_and_league_matches_are_recorded(self):
        for match_type, mode in (("letsplay", "letsplay"),
                                 ("cipl", "challenge"),
                                 ("challenge", "challenge")):
            m = self._match(match_type=match_type)
            row = self.ps.record_match(self.session, m, self._state(),
                                       self._result())
            self.assertIsNotNone(row, match_type)
            self.assertEqual(row.mode, mode)

    def test_other_modes_are_not_recorded(self):
        for match_type in ("cric", "playmatch", "vsbot", "wsp", "botmatch", None):
            m = self._match(match_type=match_type)
            self.assertIsNone(
                self.ps.record_match(self.session, m, self._state(),
                                     self._result()),
                f"{match_type} should not reach the pitch record")

    def test_a_practice_match_against_the_ai_is_not_recorded(self):
        """The Match row is indistinguishable — only the live state says so."""
        m = self._match(match_type="cipl")
        self.assertIsNone(self.ps.record_match(
            self.session, m, self._state(bot=True), self._result()))

    def test_an_unknown_pitch_is_not_recorded(self):
        m = self._match(pitch="Sandpit")
        self.assertIsNone(self.ps.record_match(
            self.session, m, self._state(), self._result()))

    # ── Rates ──────────────────────────────────────────────────────

    def test_rates_use_the_balls_actually_bowled(self):
        """160 in 120 plus 120 in 102 is 7.57 an over, not 7.00.

        Dividing by the match length (40 overs) would read 7.00 and make every
        successful chase look like it slowed the pitch down.
        """
        m = self._match()
        self.ps.record_match(self.session, m,
                             self._state(inn1_runs=160, inn1_balls=120,
                                         inn2_runs=120, inn2_balls=102),
                             self._result())
        self.session.flush()
        row = self.ps.overview(self.session)[0]
        self.assertEqual(row["balls"], 222)
        self.assertAlmostEqual(row["rpo"], 280 * 6 / 222, places=4)
        self.assertAlmostEqual(row["wpo"], 16 * 6 / 222, places=4)

    def test_per_six_is_none_rather_than_zero_when_nothing_was_bowled(self):
        self.assertIsNone(self.ps.per_six(0, 0))
        self.assertEqual(self.ps.per_six(0, 6), 0.0)

    # ── The bat-first / bat-second split ───────────────────────────

    def test_the_side_that_batted_first_is_credited_by_name(self):
        m = self._match()
        self.ps.record_match(self.session, m, self._state(),
                             self._result(winner="Home"))
        self.session.flush()
        row = self.ps.overview(self.session)[0]
        self.assertEqual(row["bat_first_win_pct"], 100.0)
        self.assertEqual(row["bat_second_win_pct"], 0.0)

    def test_a_chase_credits_the_side_batting_second(self):
        m = self._match()
        self.ps.record_match(self.session, m, self._state(),
                             self._result(winner="Away", margin_type="wickets"))
        self.session.flush()
        row = self.ps.overview(self.session)[0]
        self.assertEqual(row["bat_second_win_pct"], 100.0)

    def test_a_winner_id_beats_the_name_lookup(self):
        """A Super Over hands back its own winner; the id is unambiguous."""
        m = self._match()
        result = self._result(winner="Away", margin_type="wickets")
        result["winner_id"] = self.home.id          # id says the first innings won
        self.ps.record_match(self.session, m, self._state(), result)
        self.session.flush()
        self.assertEqual(self.ps.overview(self.session)[0]["bat_first_win_pct"],
                         100.0)

    def test_ties_are_excluded_from_the_split_but_counted_as_matches(self):
        m = self._match()
        self.ps.record_match(self.session, m, self._state(),
                             self._result(tie=True))
        self.session.flush()
        row = self.ps.overview(self.session)[0]
        self.assertEqual(row["matches"], 1)
        self.assertEqual(row["ties"], 1)
        self.assertIsNone(row["bat_first_win_pct"])

    def test_an_unrecognisable_winner_is_recorded_as_a_tie_not_a_guess(self):
        m = self._match()
        self.ps.record_match(self.session, m, self._state(),
                             self._result(winner="Somebody Else"))
        self.session.flush()
        self.assertEqual(self.ps.overview(self.session)[0]["ties"], 1)

    # ── Idempotency ────────────────────────────────────────────────

    def test_recording_the_same_match_twice_counts_it_once(self):
        m = self._match()
        state, result = self._state(), self._result()
        self.ps.record_match(self.session, m, state, result)
        self.session.flush()
        first = self.ps.overview(self.session)[0]
        self.ps.record_match(self.session, m, state, result)
        self.session.flush()
        again = self.ps.overview(self.session)[0]
        self.assertEqual(again["matches"], 1)
        self.assertEqual(again["balls"], first["balls"])

    def test_re_recording_does_not_inflate_the_approach_rollup(self):
        m = self._match()
        state, result = self._state(), self._result()
        self.ps.record_match(self.session, m, state, result)
        self.session.flush()
        before = {r["approach"]: r["overs"]
                  for r in self.ps.approach_table(self.session)[0]}
        self.ps.record_match(self.session, m, state, result)
        self.session.flush()
        after = {r["approach"]: r["overs"]
                 for r in self.ps.approach_table(self.session)[0]}
        self.assertEqual(before, after)
        self.assertEqual(after["balanced"], 20)

    # ── Phases and approaches ──────────────────────────────────────

    def test_phase_splits_come_from_the_duel_log(self):
        m = self._match()
        state = self._state(
            inn1_duels=(_duel(6, "powerplay", "aggressive", "aggressive", 12, 1)
                        + _duel(10, "middle", "rotate", "defensive", 6)
                        + _duel(4, "death", "ultra", "variation", 15, 2)),
            duels=_duel(6, "powerplay", "balanced", "balanced", 9))
        self.ps.record_match(self.session, m, state, self._result())
        self.session.flush()
        detail = self.ps.pitch_detail(self.session, "Green")
        self.assertEqual(detail["phases"]["powerplay"]["runs"], 6 * 12 + 6 * 9)
        self.assertEqual(detail["phases"]["death"]["wickets"], 8)
        self.assertAlmostEqual(detail["phases"]["death"]["rpo"], 15.0, places=4)

    def test_approach_table_rates_each_intent_and_plan(self):
        m = self._match()
        state = self._state(
            inn1_duels=_duel(10, "death", "ultra", "defensive", 14, 1),
            duels=_duel(10, "middle", "rotate", "mixed", 6))
        self.ps.record_match(self.session, m, state, self._result())
        self.session.flush()
        batting, bowling, pairs = self.ps.approach_table(self.session)
        bat = {r["approach"]: r for r in batting}
        self.assertAlmostEqual(bat["ultra"]["rpo"], 14.0, places=4)
        self.assertAlmostEqual(bat["ultra"]["wpo"], 1.0, places=4)
        self.assertAlmostEqual(bat["rotate"]["rpo"], 6.0, places=4)
        # Sorted most productive first, which is what the card prints.
        self.assertEqual(batting[0]["approach"], "ultra")
        # The bowling side is sorted by economy, cheapest first.
        self.assertEqual(bowling[0]["approach"], "mixed")
        self.assertEqual(len(pairs), 2)

    def test_the_approach_table_can_be_sliced_by_pitch_and_phase(self):
        self.ps.record_match(
            self.session, self._match(pitch="Green"),
            self._state(inn1_duels=_duel(8, "death", "ultra", "mixed", 13),
                        duels=[]),
            self._result())
        self.ps.record_match(
            self.session, self._match(pitch="Dusty"),
            self._state(inn1_duels=_duel(8, "middle", "rotate", "mixed", 5),
                        duels=[]),
            self._result())
        self.session.flush()
        green = {r["approach"] for r in
                 self.ps.approach_table(self.session, pitch="Green")[0]}
        self.assertEqual(green, {"ultra"})
        death = {r["approach"] for r in
                 self.ps.approach_table(self.session, phase="death")[0]}
        self.assertEqual(death, {"ultra"})

    # ── Filters ────────────────────────────────────────────────────

    def test_mode_and_tournament_filters_narrow_the_sample(self):
        from models import Tournament
        tour = Tournament(name="T", kind="challenge", status="active",
                          format="League", overs=20, max_teams=4)
        self.session.add(tour)
        self.session.flush()

        self.ps.record_match(self.session, self._match(match_type="letsplay"),
                             self._state(), self._result())
        self.ps.record_match(
            self.session,
            self._match(match_type="cipl", tournament_id=tour.id),
            self._state(), self._result())
        self.session.flush()

        self.assertEqual(self.ps.totals(self.session)[0], 2)
        self.assertEqual(self.ps.totals(self.session, mode="letsplay")[0], 1)
        self.assertEqual(self.ps.totals(self.session, mode="challenge")[0], 1)
        self.assertEqual(self.ps.totals(self.session, tournament_only=True)[0], 1)
        self.assertEqual(
            self.ps.totals(self.session, mode="letsplay", tournament_only=True)[0],
            0)

    # ── The par-band verdict, which is the point of the overview ───

    def test_the_average_is_judged_against_the_surfaces_own_par_band(self):
        from engine import pitch_registry
        from models import PitchApproachStat, PitchMatchStat

        low, high = pitch_registry.par_band("Green")
        for score, expected in ((low - 40, "under"), ((low + high) // 2, "in"),
                                (high + 40, "over")):
            self.ps.record_match(self.session, self._match(pitch="Green"),
                                 self._state(inn1_runs=score), self._result())
            self.session.flush()
            detail = self.ps.pitch_detail(self.session, "Green")
            self.assertEqual(detail["par_verdict"], expected,
                             f"1st-innings avg {score} vs band {low}-{high}")
            # One match per case — clear it so the next average is clean.
            self.session.query(PitchMatchStat).delete()
            self.session.query(PitchApproachStat).delete()
            self.session.flush()

    # ── The toss record ────────────────────────────────────────────

    def test_the_toss_record_scores_the_call_against_the_book(self):
        from engine import pitch_registry
        book = pitch_registry.toss_call("Green")          # "bowl"
        other = "bat" if book == "bowl" else "bowl"

        # Toss winner followed the book and won.
        self.ps.record_match(
            self.session,
            self._match(pitch="Green", toss_decision=book,
                        toss_winner=self.home, winner=self.home),
            self._state(), self._result(winner="Home"))
        # Toss winner went against it and lost.
        self.ps.record_match(
            self.session,
            self._match(pitch="Green", toss_decision=other,
                        toss_winner=self.home, winner=self.away),
            self._state(), self._result(winner="Away", margin_type="wickets"))
        self.session.flush()

        toss = self.ps.pitch_detail(self.session, "Green")["toss"]
        self.assertEqual(toss["book_call"], book)
        self.assertEqual(toss["followed"], 1)
        self.assertEqual(toss["against"], 1)
        self.assertEqual(toss["followed_win_pct"], 100.0)
        self.assertEqual(toss["against_win_pct"], 0.0)
        self.assertEqual(toss["toss_winner_win_pct"], 50.0)


class RenderCase(unittest.TestCase):
    """The card renders from whatever the tables hold, including nothing."""

    def setUp(self):
        from database import get_session
        self.session = get_session()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_every_view_renders_on_an_empty_database(self):
        from models import PitchApproachStat, PitchMatchStat
        from handlers import pitchstats as h

        self.session.query(PitchMatchStat).delete()
        self.session.query(PitchApproachStat).delete()
        self.session.flush()
        for view, arg in ((h.VIEW_HOME, ""), (h.VIEW_PITCH, "Green"),
                          (h.VIEW_APPROACH, ":all")):
            text, keyboard = h._render(self.session, view, 42, h.MODE_ALL,
                                       h.SCOPE_ALL, arg)
            self.assertTrue(text.strip(), view)
            self.assertTrue(keyboard.inline_keyboard, view)

    def test_callback_data_stays_inside_telegrams_limit(self):
        from handlers import pitchstats as h
        # The longest realistic payload: the longest pitch name, the longest
        # phase, a 64-bit-ish Telegram id.
        data = h._cb(h.VIEW_APPROACH, 9_999_999_999, h.MODE_ALL, h.SCOPE_TOUR,
                     "Bouncy:powerplay")
        self.assertLessEqual(len(data.encode("utf-8")), 64, data)
        self.assertTrue(data.startswith(h.CB + h.SEP))


if __name__ == "__main__":
    unittest.main()
