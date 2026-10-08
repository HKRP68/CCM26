"""Automatic giveaways — the rotation that keeps one giveaway live.

Runs against a throwaway SQLite database: the rotation's job is to create the
right Giveaway rows in the right order, which is easiest to pin with real rows.
"""

import os
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
_MODULE_NAMES = ("database", "models", "config", "services.player_service",
                 "services.player_cache", "services.giveaway_service",
                 "services.giveaway_auto")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE
    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    from database import Base, engine
    import models  # noqa: F401
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


_CARD = dict(category="Batsman", country="India", bat_hand="Right",
             bowl_hand="Right", bowl_style="Fast", bat_rating=60, bowl_rating=60)


class AutoGiveawayTests(unittest.TestCase):

    def setUp(self):
        from database import get_session
        from models import Giveaway, GiveawayAutoConfig, GiveawayEntry, Player
        self.session = get_session()
        for model in (GiveawayEntry, Giveaway, GiveawayAutoConfig, Player):
            self.session.query(model).delete()
        # Three ordinary base cards at every rating 80-95, plus a career card at
        # each rating the ladder uses — it must never be picked.
        for rating in range(80, 96):
            for i in range(3):
                self.session.add(Player(name=f"P{rating}-{i}", version="Base card",
                                        rating=rating, is_active=True, **_CARD))
            self.session.add(Player(name=f"Career {rating}", rating=rating,
                                    is_active=True, is_career=True, **_CARD))
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _enable(self, **kw):
        from services import giveaway_auto
        cfg = giveaway_auto.get_config(self.session)
        cfg.enabled = True
        for k, v in kw.items():
            setattr(cfg, k, v)
        self.session.commit()
        return cfg

    def _finish(self, g, winner_user_ids=()):
        from models import GiveawayEntry
        g.status = "ended"
        g.winners_drawn_at = datetime.utcnow()
        for uid in winner_user_ids:
            self.session.add(GiveawayEntry(giveaway_id=g.id, user_id=uid,
                                           telegram_id=uid, is_winner=True))
        self.session.commit()

    def test_nothing_happens_while_disabled(self):
        from services.giveaway_auto import ensure_auto_giveaway
        self.assertIsNone(ensure_auto_giveaway(self.session))

    def test_one_live_at_a_time_and_three_day_default(self):
        from services.giveaway_auto import ensure_auto_giveaway
        self._enable()
        now = datetime(2026, 10, 1, 12, 0)
        g = ensure_auto_giveaway(self.session, now)
        self.assertIsNotNone(g)
        self.assertTrue(g.is_auto and g.auto_winners)
        self.assertEqual(g.end_time - g.start_time, timedelta(hours=72))
        self.assertEqual(g.status, "scheduled")
        # A second sweep while it is live creates nothing.
        self.assertIsNone(ensure_auto_giveaway(self.session, now))

    def test_prizes_rotate_and_the_ladder_climbs_slowly(self):
        from services.giveaway_auto import ensure_auto_giveaway
        self._enable(prize_cycle="player,coins,gems",
                     rating_ladder="83,84,85,86,85,86",
                     coins_amount=40000, gems_amount=30)
        seen = []
        for _ in range(9):
            g = ensure_auto_giveaway(self.session)
            self.assertIsNotNone(g)
            if g.prize_type == "player":
                seen.append(("player", g.prize_player.rating))
                self.assertFalse(g.prize_player.is_career)
            else:
                seen.append((g.prize_type, g.prize_amount))
            self._finish(g)
        self.assertEqual(seen, [
            ("player", 83), ("coins", 40000), ("gems", 30),
            ("player", 84), ("coins", 40000), ("gems", 30),
            ("player", 85), ("coins", 40000), ("gems", 30),
        ])

    def test_the_ladder_loops(self):
        from services.giveaway_auto import ensure_auto_giveaway
        self._enable(prize_cycle="player", rating_ladder="90,91")
        ratings = []
        for _ in range(4):
            g = ensure_auto_giveaway(self.session)
            ratings.append(g.prize_player.rating)
            self._finish(g)
        self.assertEqual(ratings, [90, 91, 90, 91])

    def test_a_missing_rating_falls_back_to_the_nearest(self):
        from services.giveaway_auto import ensure_auto_giveaway
        self._enable(prize_cycle="player", rating_ladder="97")
        g = ensure_auto_giveaway(self.session)
        self.assertEqual(g.prize_player.rating, 95)
        self.assertFalse(g.prize_player.is_career)

    def test_recent_auto_winners(self):
        from services.giveaway_auto import ensure_auto_giveaway
        from services.giveaway_service import recent_auto_winner_ids
        self._enable(prize_cycle="coins")
        for winners in ((1, 2), (3,), (4,)):
            g = ensure_auto_giveaway(self.session)
            self._finish(g, winners)
        # Only the last two finished automatic giveaways count.
        self.assertEqual(recent_auto_winner_ids(self.session, 2), {3, 4})

    def test_settings_parsing(self):
        from services.giveaway_auto import parse_cycle, parse_ladder
        self.assertEqual(parse_ladder(" 83, 84;85 "), [83, 84, 85])
        self.assertEqual(parse_cycle("Player, coins"), ["player", "coins"])
        for bad in ("", "83,abc", "101"):
            with self.subTest(ladder=bad), self.assertRaises(ValueError):
                parse_ladder(bad)
        with self.assertRaises(ValueError):
            parse_cycle("player,quest_points")


if __name__ == "__main__":
    unittest.main()
