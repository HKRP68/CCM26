"""Capped activity season scoring against a real temporary database."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap

_MODULE_NAMES = (
    "database", "models", "config", "services.activity_service",
    "services.season_service", "services.season_activity_service",
    "services.ranked_service", "services.pack_service",
    "services.quest_service", "services.config_service",
    "services.club_service", "services.daily_service",
    "services.achievement_service", "services.trait_service",
)
_SAVED = {}
_PREV = None
_PATH = None
_ENGINE = None
_TG = 990_000_000


def setUpModule():
    global _SAVED, _PREV, _PATH, _ENGINE
    _SAVED = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _PREV = os.environ.get("DATABASE_URL")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        _PATH = tmp.name
    os.environ["DATABASE_URL"] = f"sqlite:///{_PATH}"
    from database import Base, engine
    import models
    _ENGINE = engine
    Base.metadata.create_all(engine)


def tearDownModule():
    _ENGINE.dispose()
    if _PREV is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV
    _module_swap.restore(_SAVED)
    os.unlink(_PATH)


class ActivityPointsTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        self.s = get_session()

    def tearDown(self):
        self.s.rollback()
        self.s.close()

    def user(self, **kwargs):
        global _TG
        from models import User
        _TG += 1
        user = User(telegram_id=kwargs.pop("telegram_id", _TG),
                    first_name="Active manager", total_coins=1000, **kwargs)
        self.s.add(user)
        self.s.flush()
        return user

    def award(self, user, action, key, **kwargs):
        from services.season_activity_service import award_activity_points
        return award_activity_points(self.s, user.id, action, event_key=key, **kwargs)

    def test_claims_and_packs_have_small_independent_limits(self):
        user = self.user()
        self.assertEqual([self.award(user, "claim", f"claim:{n}") for n in range(5)],
                         [3, 3, 3, 0, 0])
        self.assertEqual([self.award(user, "open_pack", f"pack:{n}") for n in range(5)],
                         [2, 2, 2, 0, 0])
        self.assertEqual((user.season_points, user.season_wins), (15, 0))

    def test_player_and_trait_trades_share_one_daily_allowance(self):
        user = self.user()
        self.assertEqual(self.award(user, "trait_trade", "trait:1"), 2)
        self.assertEqual(self.award(user, "trade", "player:1"), 0)
        self.assertEqual(self.award(user, "trait_trade", "trait:2"), 0)

    def test_shared_cap_clips_the_last_award_and_blocks_further_points(self):
        from services.season_activity_service import get_activity_progress
        user = self.user()
        actions = ["claim"] * 3 + ["quest_claim"] * 3 + ["open_pack"] * 3
        actions += ["daily", "login_reward", "free_pack", "upgrade", "career_upgrade", "gspin"]
        for n, action in enumerate(actions):
            self.award(user, action, f"action:{n}")
        self.assertEqual(user.season_points, 39)
        self.assertEqual(self.award(user, "trait_apply", "trait:1"), 1)
        self.assertEqual(self.award(user, "achievement", "badge:1"), 0)
        self.assertEqual(user.season_points, 40)
        progress = get_activity_progress(self.s, user.id)
        self.assertEqual((progress["earned"], progress["remaining"]), (40, 0))

    def test_same_event_cannot_be_credited_twice_even_on_another_day(self):
        user = self.user()
        now = datetime.utcnow()
        self.assertEqual(self.award(user, "claim", "claim:123", now=now), 3)
        self.assertEqual(self.award(user, "claim", "claim:123", now=now), 0)
        self.assertEqual(self.award(user, "claim", "claim:123", now=now + timedelta(days=1)), 0)
        self.assertEqual(user.season_points, 3)

    def test_category_and_daily_allowances_reset_at_utc_midnight(self):
        from services.season_activity_service import get_activity_progress
        user = self.user()
        before = datetime.utcnow().replace(day=15, hour=23, minute=59)
        after = before + timedelta(minutes=2)
        self.assertEqual(self.award(user, "daily", "daily:1", now=before), 5)
        self.assertEqual(self.award(user, "daily", "daily:2", now=before), 0)
        self.assertEqual(self.award(user, "daily", "daily:3", now=after), 5)
        self.assertEqual(get_activity_progress(self.s, user.id, now=after)["earned"], 5)
        self.assertEqual(user.season_points, 10)

    def test_casual_results_payouts_and_admin_actions_never_award_activity_points(self):
        user = self.user()
        for action in ("match_reward", "quickmatch", "quick_match", "start", "buy_pack",
                       "admin_grant_pack", "season_reward", "ranked_season_reward",
                       "release", "undo_buy", "teamname", "not_an_activity"):
            with self.subTest(action=action):
                self.assertEqual(self.award(user, action, action), 0)
        self.assertEqual(user.season_points, 0)

    def test_system_and_banned_accounts_never_receive_activity_points(self):
        for user in (self.user(telegram_id=-123), self.user(is_banned=True)):
            self.assertEqual(self.award(user, "daily", "daily:1"), 0)
            self.assertEqual(user.season_points, 0)

    def test_activity_bonuses_never_change_ranked_rating_or_win_counters(self):
        from models import RankedRating
        from services.season_service import current_season_key
        user = self.user(season_key=current_season_key(), season_points=25, season_wins=1)
        rating = RankedRating(user_id=user.id, season_key=current_season_key(),
                              rating=1200, peak_rating=1200, career_peak=1200,
                              played=10, wins=6, losses=4, draws=0)
        self.s.add(rating)
        self.award(user, "daily", "daily:1")
        self.assertEqual((user.season_points, user.season_wins), (30, 1))
        self.assertEqual((rating.rating, rating.played, rating.wins), (1200, 10, 6))

    def test_new_season_resets_stale_points_before_awarding(self):
        from services.season_service import current_season_key
        user = self.user(season_key="2001-01", season_points=999, season_wins=20)
        self.award(user, "daily", "daily:1")
        self.assertEqual(user.season_key, current_season_key())
        self.assertEqual((user.season_points, user.season_wins), (5, 0))

    def test_activity_log_awards_points_and_keeps_the_point_breakdown(self):
        from models import ActivityLog
        from services.activity_service import log_activity
        user = self.user()
        log_activity(self.s, user.id, "daily", "Claimed daily reward")
        log_activity(self.s, user.id, "claim", "Claimed a player")
        self.assertEqual(user.season_points, 8)
        log = self.s.query(ActivityLog).filter_by(user_id=user.id, action="daily").one()
        self.assertIn("+5 season pts", log.detail)

    def test_long_pack_descriptions_fit_the_log_without_losing_the_bonus(self):
        from models import ActivityLog
        from services.activity_service import log_activity
        user = self.user()
        log_activity(self.s, user.id, "open_pack", "Many player names, " * 100)
        self.s.flush()
        log = self.s.query(ActivityLog).filter_by(user_id=user.id).one()
        self.assertLessEqual(len(log.detail), 500)
        self.assertTrue(log.detail.endswith("+2 season pts"))
        self.assertEqual(user.season_points, 2)

    def test_rolling_back_the_action_also_restores_the_activity_allowance(self):
        from models import SeasonActivityAward
        from services.activity_service import log_activity
        user = self.user()
        self.s.commit()
        log_activity(self.s, user.id, "daily", "Daily reward")
        self.assertEqual(user.season_points, 5)
        self.s.rollback()
        self.assertEqual(user.season_points, 0)
        self.assertEqual(self.s.query(SeasonActivityAward).filter_by(user_id=user.id).count(), 0)
        log_activity(self.s, user.id, "daily", "Retry after rollback")
        self.assertEqual(user.season_points, 5)

    def test_bonus_failure_preserves_the_action_and_its_log(self):
        from models import ActivityLog
        from services import activity_service, season_activity_service
        user = self.user()
        user.total_coins += 100
        with mock.patch.object(season_activity_service, "award_activity_points",
                               side_effect=RuntimeError("bonus unavailable")):
            activity_service.log_activity(self.s, user.id, "daily", "Paid daily coins")
        self.s.flush()
        self.assertEqual(user.total_coins, 1100)
        self.assertEqual(user.season_points, 0)
        self.assertEqual(self.s.query(ActivityLog).filter_by(user_id=user.id).count(), 1)

    def test_ranked_credit_preserves_points_committed_after_a_stale_user_read(self):
        from database import get_session
        from models import User
        from services.season_service import current_season_key, add_ranked_season_points
        user = self.user(season_key=current_season_key(), season_points=0, season_wins=0)
        self.s.commit()
        self.assertEqual(user.season_points, 0)  # load the old balance
        other = get_session()
        try:
            other.query(User).filter_by(id=user.id).update({User.season_points: 5})
            other.commit()
        finally:
            other.close()
        add_ranked_season_points(self.s, user, ranked_result={
            "rated": True, "players": {user.id: {"season_points": 25}}})
        self.assertEqual((user.season_points, user.season_wins), (30, 1))

    def test_season_api_reports_all_rules_and_actual_daily_progress(self):
        from services.season_service import get_season_info
        user = self.user()
        self.award(user, "daily", "daily:1")
        info = get_season_info(self.s, user)
        self.assertEqual(info["me"]["points"], 5)
        self.assertEqual(info["activity"]["daily_cap"], 40)
        self.assertEqual(info["activity"]["remaining"], 35)
        rules = {a["key"]: a for a in info["activity"]["activities"]}
        self.assertEqual(rules["daily"]["completed"], 1)
        self.assertIn("achievement", rules)
        self.assertEqual(rules["open_pack"]["daily_limit"], 3)

    def test_reading_a_stale_season_does_not_erase_a_concurrent_activity_credit(self):
        from database import get_session
        from models import User
        from services.season_service import current_season_key, get_season_info
        user = self.user(season_key="2001-01", season_points=999, season_wins=20)
        self.s.commit()
        self.assertEqual(user.season_key, "2001-01")
        other = get_session()
        try:
            other.query(User).filter_by(id=user.id).update({
                User.season_key: current_season_key(),
                User.season_points: 5, User.season_wins: 0})
            other.commit()
        finally:
            other.close()
        info = get_season_info(self.s, user)
        self.assertEqual((info["me"]["points"], info["me"]["wins"]), (5, 0))

    def test_achievement_unlock_earns_a_bonus_once(self):
        from services import achievement_service
        user = self.user()
        badge = {"key": "season_activity_test", "name": "Activity badge",
                 "coins": 10, "gems": 0, "check": lambda _s, _u: True}
        with mock.patch.object(achievement_service, "CATALOG", [badge]):
            self.assertEqual(len(achievement_service.check_and_award(self.s, user.id)), 1)
            self.assertEqual(achievement_service.check_and_award(self.s, user.id), [])
        self.assertEqual(user.total_coins, 1010)
        self.assertEqual((user.season_points, user.season_wins), (3, 0))

    def test_daily_claim_awards_once_despite_repeated_ad_claims(self):
        from services import daily_service, command_config_service
        from models import UserStats
        user = self.user()
        self.s.add(UserStats(user_id=user.id))
        self.s.flush()
        with mock.patch.object(command_config_service, "get_player_count", return_value=0):
            first = daily_service.claim_daily(self.s, user)
            second = daily_service.claim_daily(self.s, user)
            ad_claim = daily_service.claim_daily(self.s, user, skip_cooldown=True)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertTrue(ad_claim["ok"])
        self.assertEqual((user.season_points, user.season_wins), (5, 0))

    def test_successful_pack_open_awards_once_and_failed_reopen_awards_nothing(self):
        from models import Pack, Player, UnopenedPack
        from services import pack_service
        user = self.user()
        pack = Pack(name="Activity pack", slot_number=987, main_count=1, bonus_count=0,
                    main_min_rating=80, main_max_rating=80)
        player = Player(name="Activity player", rating=80, bat_rating=80, bowl_rating=70,
                        category="Batsman", country="India", bat_hand="Right",
                        bowl_hand="Right", bowl_style="Medium")
        self.s.add_all([pack, player])
        self.s.flush()
        inv = UnopenedPack(user_id=user.id, pack_id=pack.id, source="test")
        self.s.add(inv)
        self.s.flush()
        inventory_id = inv.id
        with mock.patch.object(pack_service, "_pick_main_player", return_value=player):
            result = pack_service.open_unopened_pack(self.s, user, inventory_id)
        self.assertTrue(result["success"])
        self.assertEqual(user.season_points, 2)
        self.assertFalse(pack_service.open_unopened_pack(self.s, user, inventory_id)["success"])
        self.assertEqual(user.season_points, 2)

    def test_applying_a_trait_earns_points_but_failed_reapplication_does_not(self):
        from models import Player, UserRoster, Trait, TraitInventory
        from services.trait_service import apply_trait_to_player
        user = self.user()
        player = Player(name="Trait activity player", rating=80, category="Batsman",
                        country="India", bat_hand="Right", bowl_hand="Right", bowl_style="Medium")
        trait = Trait(name="Activity trait", category="Batting", description="Test trait",
                      emoji="✨", effect_key="activity_test", rarity="common")
        self.s.add_all([player, trait])
        self.s.flush()
        roster = UserRoster(user_id=user.id, player_id=player.id, order_position=1)
        inv = TraitInventory(user_id=user.id, trait_id=trait.id, level=1)
        self.s.add_all([roster, inv])
        self.s.flush()
        inventory_id = inv.id
        self.assertTrue(apply_trait_to_player(self.s, user, inventory_id, roster.id)[0])
        self.assertEqual(user.season_points, 2)
        self.assertFalse(apply_trait_to_player(self.s, user, inventory_id, roster.id)[0])
        self.assertEqual((user.season_points, user.season_wins), (2, 0))


if __name__ == "__main__":
    unittest.main()
