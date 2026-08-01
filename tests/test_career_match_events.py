"""Every Career Player match event, fired from a real match state.

The weekly quest catalogue is only as good as the events behind it: a quest on
an event that never fires is a quest nobody can clear, and because the career
streak needs *every* assigned quest cleared, one of those poisons the jackpot
for whoever draws it. So each event gets a match played through
``track_user_match_quests`` and its progress checked.

The states here use the same shape both finalizes pass — ``inn1_*`` for the
first innings and the bare keys for the second — so "batting second" really is
the chase, and a bowler's figures really are one spell.
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG_IDS = itertools.count(99_401)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.quest_service",
                 "services.career_service")


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
    import models  # noqa: F401

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)

    from database import get_session
    from models import CareerFace
    session = get_session()
    try:
        session.add(CareerFace(slot=1, label="Face 1", sort_order=1,
                               is_active=True))
        session.commit()
    finally:
        session.close()


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


class CareerEventTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Quest, User, UserQuestProgress, UserRoster
        from services import career_service as cs

        self.session = get_session()
        self.session.query(UserQuestProgress).delete()
        self.session.query(Quest).delete()

        self.user = User(telegram_id=next(_TG_IDS), username="events",
                         total_coins=0, total_gems=0, roster_count=0)
        self.session.add(self.user)
        self.session.flush()
        created = cs.create_career_player(
            self.session, self.user, name=f"Eventful {self.user.id}",
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertTrue(created["ok"], created.get("message"))
        self.player = created["player"]
        self.session.flush()
        self.rid = (self.session.query(UserRoster)
                    .filter(UserRoster.user_id == self.user.id,
                            UserRoster.player_id == self.player.id)
                    .first().id)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ─────────────────────────────────────────────────────────

    def _quest(self, event_key, target=1):
        """An assigned career quest on one event, big enough to see progress."""
        from models import Quest, UserQuestProgress
        from services.quest_service import weekly_period_key
        quest = Quest(name=f"Q {event_key}", description=event_key,
                      quest_type="weekly", event_key=event_key,
                      target_count=target, reward_points=100, reward_coins=0,
                      reward_gems=15, is_active=True, career_only=True,
                      emoji="🎖", sort_order=0)
        self.session.add(quest)
        self.session.flush()
        self.session.add(UserQuestProgress(
            user_id=self.user.id, quest_id=quest.id,
            period_key=weekly_period_key(), progress=0, completed=False,
            claimed=False, assigned=True))
        self.session.flush()
        return quest

    def _xi(self):
        return [{"roster_id": self.rid, "name": self.player.name,
                 "player_id": self.player.id}]

    def _play(self, *, bat_first=None, bat_second=None, bowl=None,
              is_winner=False, potm=False):
        """Run one match through the tracker and flush."""
        from services.quest_service import track_user_match_quests
        state = {}
        if bat_first is not None:
            state["inn1_bat_xi"] = self._xi()
            state["inn1_bat_stats"] = {self.rid: bat_first}
        if bat_second is not None:
            state["bat_xi"] = self._xi()
            state["bat_stats"] = {self.rid: bat_second}
        if bowl is not None:
            state["bowl_xi"] = self._xi()
            state["bowl_stats"] = {self.rid: bowl}
        if potm:
            state["potm_player_id"] = self.player.id
            state["potm_owner_user_id"] = self.user.id
        track_user_match_quests(self.session, state, self.user, is_winner,
                                False, self.user.id if is_winner else None)
        self.session.flush()

    def _progress(self, quest):
        from models import UserQuestProgress
        return (self.session.query(UserQuestProgress)
                .filter(UserQuestProgress.user_id == self.user.id,
                        UserQuestProgress.quest_id == quest.id)
                .first().progress)

    @staticmethod
    def _bat(runs=0, balls=0, fours=0, sixes=0, out=True):
        return {"runs": runs, "balls": balls, "fours": fours, "sixes": sixes,
                "out": out}

    @staticmethod
    def _bowl(balls=0, runs=0, wickets=0, dots=0, maidens=0, hattrick=False):
        return {"balls": balls, "runs": runs, "wickets": wickets, "dots": dots,
                "maidens": maidens, "hattrick": hattrick}

    # ── appearance ──────────────────────────────────────────────────────

    def test_match_played_and_won(self):
        played = self._quest("career_match_played", 5)
        won = self._quest("career_match_won", 5)
        self._play(bat_first=self._bat(runs=10, balls=8), is_winner=True)
        self._play(bat_first=self._bat(runs=10, balls=8), is_winner=False)
        self.assertEqual(self._progress(played), 2)
        self.assertEqual(self._progress(won), 1)

    def test_potm_only_counts_for_the_career_card(self):
        quest = self._quest("career_potm", 3)
        self._play(bat_first=self._bat(runs=60, balls=40), potm=True)
        self.assertEqual(self._progress(quest), 1)
        self._play(bat_first=self._bat(runs=60, balls=40), potm=False)
        self.assertEqual(self._progress(quest), 1)

    # ── batting ─────────────────────────────────────────────────────────

    def test_runs_fours_sixes_and_balls_faced_accumulate(self):
        runs = self._quest("career_runs_scored", 500)
        fours = self._quest("career_fours_hit", 50)
        sixes = self._quest("career_sixes_hit", 50)
        balls = self._quest("career_balls_faced", 500)
        self._play(bat_first=self._bat(runs=64, balls=40, fours=6, sixes=4))
        self._play(bat_first=self._bat(runs=30, balls=25, fours=3, sixes=1))
        self.assertEqual(self._progress(runs), 94)
        self.assertEqual(self._progress(fours), 9)
        self.assertEqual(self._progress(sixes), 5)
        self.assertEqual(self._progress(balls), 65)

    def test_boundary_runs_are_four_a_four_and_six_a_six(self):
        quest = self._quest("career_boundary_runs", 500)
        self._play(bat_first=self._bat(runs=64, balls=40, fours=6, sixes=4))
        self.assertEqual(self._progress(quest), 6 * 4 + 4 * 6)

    def test_chase_runs_only_count_the_second_innings(self):
        quest = self._quest("career_chase_runs", 500)
        self._play(bat_first=self._bat(runs=70, balls=50))
        self.assertEqual(self._progress(quest), 0, "batting first is not a chase")
        self._play(bat_second=self._bat(runs=45, balls=30))
        self.assertEqual(self._progress(quest), 45)

    def test_quickfire_runs_need_both_pace_and_a_real_innings(self):
        quest = self._quest("career_quickfire_runs", 500)
        # 40 off 20 is a strike rate of 200 over enough balls — counts in full.
        self._play(bat_first=self._bat(runs=40, balls=20))
        self.assertEqual(self._progress(quest), 40)
        # 60 off 60 is a strike rate of 100 — contributes nothing.
        self._play(bat_first=self._bat(runs=60, balls=60))
        self.assertEqual(self._progress(quest), 40)
        # 12 off 5 is quick but too short a cameo to say anything.
        self._play(bat_first=self._bat(runs=12, balls=5))
        self.assertEqual(self._progress(quest), 40)
        # Exactly 150 over exactly 10 balls is the boundary case, and counts.
        self._play(bat_first=self._bat(runs=15, balls=10))
        self.assertEqual(self._progress(quest), 55)

    def test_innings_milestones_fire_once_each(self):
        fifty = self._quest("career_fifty", 5)
        hundred = self._quest("career_hundred", 5)
        twenty_five = self._quest("career_score_25_plus", 5)
        seventy_five = self._quest("career_score_75_plus", 5)
        self._play(bat_first=self._bat(runs=120, balls=70))
        self._play(bat_first=self._bat(runs=80, balls=50))
        self._play(bat_first=self._bat(runs=55, balls=40))
        self._play(bat_first=self._bat(runs=30, balls=25))
        self._play(bat_first=self._bat(runs=10, balls=9))
        self.assertEqual(self._progress(hundred), 1)
        self.assertEqual(self._progress(fifty), 2, "80 and 55; the 120 is a ton")
        self.assertEqual(self._progress(seventy_five), 2, "120 and 80")
        self.assertEqual(self._progress(twenty_five), 4)

    def test_not_out_needs_a_ball_faced(self):
        quest = self._quest("career_not_out", 5)
        self._play(bat_first=self._bat(runs=20, balls=15, out=False))
        self.assertEqual(self._progress(quest), 1)
        self._play(bat_first=self._bat(runs=20, balls=15, out=True))
        self.assertEqual(self._progress(quest), 1)
        # Did not bat: no balls faced, so not an unbeaten innings.
        self._play(bat_first=self._bat(runs=0, balls=0, out=False))
        self.assertEqual(self._progress(quest), 1)

    # ── bowling ─────────────────────────────────────────────────────────

    def test_wickets_dots_maidens_and_balls_accumulate(self):
        wickets = self._quest("career_wickets_taken", 50)
        dots = self._quest("career_dot_balls", 200)
        maidens = self._quest("career_maiden_overs", 20)
        balls = self._quest("career_balls_bowled", 500)
        self._play(bowl=self._bowl(balls=24, runs=20, wickets=2, dots=9,
                                   maidens=1))
        self._play(bowl=self._bowl(balls=18, runs=30, wickets=1, dots=4))
        self.assertEqual(self._progress(wickets), 3)
        self.assertEqual(self._progress(dots), 13)
        self.assertEqual(self._progress(maidens), 1)
        self.assertEqual(self._progress(balls), 42)

    def test_three_and_five_wicket_hauls(self):
        three = self._quest("career_three_fer", 5)
        five = self._quest("career_five_fer", 5)
        self._play(bowl=self._bowl(balls=24, runs=30, wickets=5))
        self.assertEqual((self._progress(three), self._progress(five)), (1, 1))
        self._play(bowl=self._bowl(balls=24, runs=30, wickets=3))
        self.assertEqual((self._progress(three), self._progress(five)), (2, 1))
        self._play(bowl=self._bowl(balls=24, runs=30, wickets=2))
        self.assertEqual((self._progress(three), self._progress(five)), (2, 1))

    def test_hattrick(self):
        quest = self._quest("career_hattrick", 3)
        self._play(bowl=self._bowl(balls=24, runs=20, wickets=3, hattrick=True))
        self.assertEqual(self._progress(quest), 1)

    def test_an_economy_spell_needs_four_overs_and_under_six_an_over(self):
        quest = self._quest("career_economy_spell", 5)
        # 4 overs for 20 — economy 5.0, and long enough to mean something.
        self._play(bowl=self._bowl(balls=24, runs=20))
        self.assertEqual(self._progress(quest), 1)
        # 4 overs for 24 — exactly 6.00, which is not "under 6".
        self._play(bowl=self._bowl(balls=24, runs=24))
        self.assertEqual(self._progress(quest), 1)
        # 2 overs for 4 — miserly, but too short to count as a spell.
        self._play(bowl=self._bowl(balls=12, runs=4))
        self.assertEqual(self._progress(quest), 1)

    # ── all-round ───────────────────────────────────────────────────────

    def test_an_allrounder_match_needs_both_halves_in_one_match(self):
        quest = self._quest("career_allrounder_match", 5)
        self._play(bat_first=self._bat(runs=40, balls=25),
                   bowl=self._bowl(balls=24, runs=30, wickets=3))
        self.assertEqual(self._progress(quest), 1)
        # Runs but no wickets, then wickets but no runs — neither qualifies.
        self._play(bat_first=self._bat(runs=60, balls=40))
        self._play(bowl=self._bowl(balls=24, runs=30, wickets=4))
        self.assertEqual(self._progress(quest), 1)

    # ── isolation ───────────────────────────────────────────────────────

    def test_another_users_card_never_moves_this_users_quests(self):
        from models import User, UserRoster
        from services import career_service as cs
        from services.quest_service import track_user_match_quests

        quest = self._quest("career_runs_scored", 500)
        other = User(telegram_id=next(_TG_IDS), username="other",
                     total_coins=0, total_gems=0, roster_count=0)
        self.session.add(other)
        self.session.flush()
        made = cs.create_career_player(
            self.session, other, name=f"Rival {other.id}", country="India",
            bat_hand="Right", bowl_hand="Right", bowl_style="Fast",
            face_slot=1)
        self.assertTrue(made["ok"], made.get("message"))
        self.session.flush()
        other_rid = (self.session.query(UserRoster)
                     .filter(UserRoster.user_id == other.id,
                             UserRoster.player_id == made["player"].id)
                     .first().id)

        state = {"inn1_bat_xi": [{"roster_id": other_rid,
                                  "name": made["player"].name,
                                  "player_id": made["player"].id}],
                 "inn1_bat_stats": {other_rid: self._bat(runs=99, balls=50)}}
        track_user_match_quests(self.session, state, other, True, False,
                                other.id)
        self.session.flush()
        self.assertEqual(self._progress(quest), 0)

    def test_a_match_the_career_card_sat_out_fires_nothing(self):
        from models import Player, UserRoster
        from services.quest_service import track_user_match_quests

        quest = self._quest("career_match_played", 5)
        bench = Player(name=f"Squaddie {self.user.id}", version="Base card",
                       rating=75, category="Batsman", country="India",
                       bat_hand="Right", bowl_hand="Right", bowl_style="Fast")
        self.session.add(bench)
        self.session.flush()
        entry = UserRoster(user_id=self.user.id, player_id=bench.id,
                           order_position=2)
        self.session.add(entry)
        self.session.flush()

        state = {"inn1_bat_xi": [{"roster_id": entry.id, "name": bench.name,
                                  "player_id": bench.id}],
                 "inn1_bat_stats": {entry.id: self._bat(runs=70, balls=45)}}
        track_user_match_quests(self.session, state, self.user, True, False,
                                self.user.id)
        self.session.flush()
        self.assertEqual(self._progress(quest), 0)


if __name__ == "__main__":
    unittest.main()
