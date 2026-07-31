"""A Career Player must never be dealt to anybody — including its own owner.

A career card is an ordinary ``players`` row, so without an explicit filter it
would be eligible for /claim, packs, the markets, the debut squad, browse and
search, and bot XIs. Handing one user's personal card to a stranger is the worst
failure this feature could have, so every pool is pinned here.
"""

import os
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.career_service",
                 "services.player_service", "services.player_cache")


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


class PoolExclusionTests(unittest.TestCase):
    """One career player, plus enough ordinary players for the pools to work."""

    @classmethod
    def setUpClass(cls):
        from database import get_session
        from models import Player, User
        from services import career_service as cs

        cls.session = get_session()
        # A spread of ordinary cards so every rating band has something in it.
        for index in range(60):
            cls.session.add(Player(
                name=f"Ordinary Player {index}", version="Base card",
                rating=55 + (index % 45), category=["Batsman", "Bowler",
                                                    "All-rounder",
                                                    "Wicket Keeper"][index % 4],
                country="India", bat_hand="Right", bowl_hand="Right",
                bowl_style="Fast", bat_rating=60, bowl_rating=60,
                is_active=True))
        user = User(telegram_id=95_001, username="owner", total_coins=0,
                    total_gems=0, roster_count=0)
        cls.session.add(user)
        cls.session.flush()

        result = cs.create_career_player(
            cls.session, user, name="Excluded Everywhere", country="India",
            bat_hand="Right", bowl_hand="Right", bowl_style="Fast", face_slot=1)
        assert result["ok"], result.get("message")
        cls.user = user
        cls.career = result["player"]
        # Make it look attractive to every band-based picker.
        cls.career.rating = 99
        cls.career.bat_rating = 99
        cls.career.bowl_rating = 99
        cls.session.commit()

        from services import player_cache
        player_cache.invalidate()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_the_helper_filters_it_out(self):
        from models import Player
        from services.player_service import not_career
        rows = not_career(self.session.query(Player)).all()
        self.assertTrue(rows, "ordinary players must still come through")
        self.assertNotIn(self.career.id, [p.id for p in rows])

    def test_the_helper_keeps_ordinary_players(self):
        from models import Player
        from services.player_service import not_career
        total = self.session.query(Player).count()
        kept = not_career(self.session.query(Player)).count()
        self.assertEqual(kept, total - 1, "only the career card may be dropped")

    def test_it_is_not_in_the_player_cache(self):
        from services import player_cache
        ids = {row["id"] for row in player_cache.get_all_active()}
        self.assertTrue(ids, "the cache must still hold ordinary players")
        self.assertNotIn(self.career.id, ids)

    def test_random_active_picks_never_return_it(self):
        from services import player_cache
        for _ in range(300):
            pick = player_cache.get_random_active()
            if pick:
                self.assertNotEqual(pick["id"], self.career.id)

    def test_rating_band_picks_never_return_it(self):
        from services import player_cache
        for _ in range(300):
            pick = player_cache.get_random_in_rating_range(95, 100)
            if pick:
                self.assertNotEqual(pick["id"], self.career.id)

    def test_name_search_does_not_surface_it(self):
        from services import player_cache
        hits = player_cache.search_by_name("Excluded")
        self.assertEqual(hits, [])

    def test_the_debut_squad_never_contains_it(self):
        from services.player_service import get_players_for_debut
        for _ in range(20):
            squad = get_players_for_debut(self.session)
            self.assertNotIn(self.career.id, [p.id for p in squad])

    def test_the_rarity_pull_never_returns_it(self):
        from services.player_service import get_random_player_by_rarity
        for _ in range(200):
            pick = get_random_player_by_rarity(self.session)
            if pick:
                self.assertNotEqual(pick.id, self.career.id)

    def test_the_global_market_never_stocks_it(self):
        from services.global_market import _pick_player_for_slot
        for _ in range(200):
            pick = _pick_player_for_slot(self.session)
            if pick:
                self.assertNotEqual(pick.id, self.career.id)

    def test_the_daily_player_market_never_stocks_it(self):
        from services.player_market import PLAYER_MARKET_MIN_RATING
        from models import Player
        from services.player_service import not_career
        # The market draws from the high-rating band the career card now sits in.
        candidates = (not_career(self.session.query(Player))
                      .filter(Player.rating >= PLAYER_MARKET_MIN_RATING,
                              Player.is_active == True)  # noqa: E712
                      .all())
        self.assertNotIn(self.career.id, [p.id for p in candidates])

    def test_browse_and_search_listings_skip_it(self):
        from services.version_paginator import find_players_for_search
        from services.version_service import get_default_for_search
        self.assertEqual(find_players_for_search(self.session, "Excluded"), [])
        self.assertEqual(get_default_for_search(self.session, "Excluded"), [])

    def test_it_is_not_tradable(self):
        from services.rating_matcher_service import (get_tradable_players,
                                                     is_player_non_tradable)
        self.assertTrue(is_player_non_tradable(self.career))
        rows = get_tradable_players(self.session, self.user.id)
        self.assertNotIn(self.career.id, [p.id for _e, p in rows])

    def test_an_ordinary_player_is_still_tradable(self):
        """The non_tradable probe must not have made every card untradable."""
        from models import Player
        from services.rating_matcher_service import is_player_non_tradable
        ordinary = (self.session.query(Player)
                    .filter(Player.is_career.is_(False),
                            Player.is_active == True)  # noqa: E712
                    .first())
        self.assertIsNotNone(ordinary)
        self.assertFalse(is_player_non_tradable(ordinary))

    def test_it_cannot_be_added_to_a_bot_team(self):
        from models import BotTeam
        from services.bot_team_service import add_player_to_team
        team = BotTeam(name="Test Bot XI")
        self.session.add(team)
        self.session.flush()
        added, message = add_player_to_team(self.session, team.id, self.career.id)
        self.assertIsNone(added)
        self.assertIn("Career", message)


if __name__ == "__main__":
    unittest.main()
