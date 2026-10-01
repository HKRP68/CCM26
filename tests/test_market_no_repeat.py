"""The shared player market does not keep re-listing the same players.

What these pin down:

  • a player listed by a reroll is kept out of the next rerolls for the
    configured window (4 days by default), so consecutive markets are disjoint
    whenever the pool is big enough
  • once the window has passed, the player is eligible again
  • a pool too small to fill the market without repeats still fills it — with
    the players unseen the longest coming back first
  • a window of 0 switches the rule off
  • a hand-listed card counts as listed
"""

import os
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.config_service",
                 "services.global_market", "services.player_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

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
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class _P:
    def __init__(self, pid):
        self.id = pid


class PickTests(unittest.TestCase):
    """The pure picker, no database."""

    def setUp(self):
        from services import global_market as gm
        self.gm = gm
        self.pool = [_P(i) for i in range(1, 21)]

    def test_recent_players_are_skipped_when_the_pool_allows(self):
        recent = {i: datetime.utcnow() for i in range(1, 15)}
        for seed in range(20):
            picks = self.gm.pick_market_players(self.pool, 6, recent,
                                                rng=random.Random(seed))
            self.assertEqual(sorted(p.id for p in picks), list(range(15, 21)))

    def test_a_short_pool_brings_back_the_longest_unseen_first(self):
        now = datetime.utcnow()
        # 18 recent: player 1 is the oldest listing, 18 the newest.
        recent = {i: now - timedelta(hours=100 - i) for i in range(1, 19)}
        picks = self.gm.pick_market_players(self.pool, 6, recent,
                                            rng=random.Random(3))
        ids = {p.id for p in picks}
        self.assertEqual(len(ids), 6)
        self.assertTrue({19, 20} <= ids)          # the fresh ones, always
        self.assertEqual(ids - {19, 20}, {1, 2, 3, 4})  # then the oldest

    def test_never_more_than_the_pool(self):
        picks = self.gm.pick_market_players(self.pool[:3], 6, {})
        self.assertEqual(len(picks), 3)


class RerollTests(unittest.TestCase):
    POOL = 30
    SLOTS = 6

    def setUp(self):
        from database import get_session
        from models import (GameConfig, GlobalPlayerMarket, MarketListingHistory,
                            Player)
        from services import config_service, global_market
        self.gm = global_market
        self.cs = config_service
        self.session = get_session()
        s = self.session
        s.query(GlobalPlayerMarket).delete()
        s.query(MarketListingHistory).delete()
        s.query(Player).delete()
        s.query(GameConfig).delete()
        for i in range(self.POOL):
            s.add(Player(name=f"P{i}", rating=90, category="Batsman",
                         country="India", bat_hand="Right", bowl_hand="Right",
                         bowl_style="Medium", is_active=True))
        s.add(GameConfig(market_default_slots=self.SLOTS, market_min_rating=87,
                         market_repeat_cooldown_days=4))
        s.commit()
        self.cs._CACHE["data"] = None

    def tearDown(self):
        self.session.rollback()
        self.session.close()
        self.cs._CACHE["data"] = None

    def listed(self):
        return {r.player_id for r in self.gm.list_player_market(self.session)}

    def reroll(self):
        n = self.gm.reroll_player_market(self.session)
        self.session.commit()
        self.assertEqual(n, self.SLOTS)
        return self.listed()

    def age_history(self, days):
        from models import MarketListingHistory
        for row in self.session.query(MarketListingHistory).all():
            row.listed_at = row.listed_at - timedelta(days=days)
        self.session.commit()

    def test_back_to_back_markets_share_nobody(self):
        seen = set()
        for _ in range(self.POOL // self.SLOTS):
            listed = self.reroll()
            self.assertFalse(listed & seen, "a player came straight back")
            seen |= listed
        self.assertEqual(len(seen), self.POOL)

    def test_the_window_passing_makes_a_player_eligible_again(self):
        first = self.reroll()
        self.age_history(5)
        # Exclude everyone else so the only fresh candidates are the first six.
        from models import Player
        for p in self.session.query(Player).filter(~Player.id.in_(first)).all():
            p.is_active = False
        self.session.commit()
        self.assertEqual(self.reroll(), first)

    def test_a_small_pool_still_fills_the_market(self):
        from models import Player
        keep = [p.id for p in self.session.query(Player).limit(8).all()]
        for p in self.session.query(Player).filter(~Player.id.in_(keep)).all():
            p.is_active = False
        self.session.commit()
        first = self.reroll()
        second = self.reroll()
        self.assertEqual(len(second), self.SLOTS)
        # The two never-listed players are in, every time.
        self.assertTrue(set(keep) - first <= second)

    def test_zero_switches_the_rule_off(self):
        from models import GameConfig
        self.session.query(GameConfig).first().market_repeat_cooldown_days = 0
        self.session.commit()
        self.cs._CACHE["data"] = None
        from models import MarketListingHistory
        self.reroll()
        # History is still written (so turning it back on has data to use).
        self.assertEqual(self.session.query(MarketListingHistory).count(),
                         self.SLOTS)

    def test_a_hand_listed_card_counts_as_listed(self):
        from models import MarketListingHistory, Player
        p = self.session.query(Player).first()
        ok, _slot = self.gm.add_player_to_market(self.session, p.id)
        self.session.commit()
        self.assertTrue(ok)
        self.assertEqual(self.session.query(MarketListingHistory)
                         .filter_by(player_id=p.id).count(), 1)
        self.assertNotIn(p.id, self.reroll())


if __name__ == "__main__":
    unittest.main()
