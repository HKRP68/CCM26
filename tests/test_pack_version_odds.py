"""A version pack obeys its odds — and only once it has some.

Version packs used to ignore ratings completely: the pull was one uniform draw
across every card carrying the version, so a set with forty 78s and one 99 paid
out a 78 forty times in forty-one. That is a reasonable default and it is what
every pack in the wild was built around, so it has to survive untouched.

What changed is that the form can now give a version pack a weight per rating,
and when it does the pull has to honour it. The gate is the weights existing,
never the mode: a pack saved before the odds table existed still carries
whatever ``main_min_rating``/``main_max_rating`` happened to be sitting in the
form, dead metadata nothing ever read. Consult it because the mode says
"version" and a Legend card rated outside that stale band stops being pullable
overnight, on a pack nobody edited.

So these tests pin both halves:

  • **no odds saved → the pull is exactly what it always was**, right down to
    tracking the card counts rather than the ratings;
  • **odds saved → the pull follows them**, the pity timer starts applying
    (the band is real now), and a rating with no card behind it is dropped and
    the rest renormalised rather than quietly collapsing the whole curve to
    flat odds.
"""

import collections
import itertools
import os
import random
import sys
import tempfile
import unittest

_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.player_service", "services.pack_odds",
                 "services.pack_pricing", "services.pack_service")


def _sync_package_attr(name, module):
    """Keep the parent package's attribute in step with ``sys.modules``.

    ``sys.modules.pop("services.pack_service")`` is not enough on its own: the
    ``services`` package object still carries ``pack_service`` as an attribute,
    and ``from services import pack_service`` reads that attribute in
    preference to importing anything. Several test files in this suite stub
    ``models`` and import service modules against the stub; without this, one
    of those stubbed modules is handed straight to this file, bringing a
    ``Player`` class that is not the one imported here — and every query comes
    back "ambiguous column name: players.id", a very long way from the cause.
    """
    if "." not in name:
        return
    parent, _, child = name.rpartition(".")
    package = sys.modules.get(parent)
    if package is None:
        return
    if module is None:
        if hasattr(package, child):
            delattr(package, child)
    else:
        setattr(package, child, module)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)
        _sync_package_attr(name, None)

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
        _sync_package_attr(name, module)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class PullCase(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import pack_service

        self.session = get_session()
        self.packs = pack_service
        random.seed(20260917)

    def tearDown(self):
        from models import Pack, PackPurchase, Player, UnopenedPack, User, UserRoster
        self.session.rollback()
        for model in (UserRoster, UnopenedPack, PackPurchase, Player, Pack, User):
            self.session.query(model).delete()
        self.session.commit()
        self.session.close()

    def card(self, rating, version="Legend"):
        from models import Player
        n = next(_SEQ)
        row = Player(name=f"Card {n}", rating=rating, version=version,
                     category="Batsman", country="India", bat_hand="Right",
                     bowl_hand="Right", bowl_style="Fast", is_active=True)
        self.session.add(row)
        self.session.flush()
        return row

    def pack(self, **kwargs):
        from models import Pack
        row = Pack(slot_number=next(_SEQ), name="Test Pack",
                   main_filter_mode="version",
                   main_versions_json='["Legend"]',
                   main_min_rating=70, main_max_rating=99, main_count=1,
                   bonus_min_rating=74, bonus_max_rating=80, bonus_count=0)
        for key, value in kwargs.items():
            setattr(row, key, value)
        self.session.add(row)
        self.session.flush()
        return row

    def user(self):
        from models import User
        row = User(telegram_id=next(_SEQ), username=f"manager{next(_SEQ)}",
                   first_name="Test", total_coins=10_000_000,
                   roster_count=0, pack_pity_counter=0)
        self.session.add(row)
        self.session.flush()
        return row

    def spread(self, pack, draws=2000):
        """``{rating: times pulled}`` over ``draws`` main-slot pulls."""
        tally = collections.Counter()
        for _ in range(draws):
            picked = self.packs._pick_main_player(self.session, pack)
            self.assertIsNotNone(picked, "a bought pack paid out nothing")
            tally[picked.rating] += 1
        return tally


class WithoutOddsTests(PullCase):
    """Every version pack that exists today. Nothing here may move."""

    def test_the_pull_still_tracks_the_cards_not_the_ratings(self):
        for _ in range(40):
            self.card(78)
        self.card(99)
        self.session.commit()

        pack = self.pack(main_weights_json=None)
        tally = self.spread(pack)
        # Forty-one cards, one of them a 99: about one pull in forty-one.
        self.assertGreater(tally[78], 1800)
        self.assertLess(tally[99], 120)

    def test_a_stale_rating_band_is_not_consulted(self):
        """The band is whatever was in the form when the pack was saved."""
        self.card(60)     # outside main_min_rating..main_max_rating
        self.session.commit()

        pack = self.pack(main_min_rating=90, main_max_rating=99,
                         main_weights_json=None)
        self.assertEqual(self.spread(pack, draws=50)[60], 50)

    def test_the_pity_counter_never_moves(self):
        """Pity needs a rating band, and a weightless version pack has none."""
        self.card(91)
        self.card(95)
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json=None, bonus_count=0)
        user = self.user()
        for _ in range(3):
            result = self.packs.open_pack(self.session, user, pack)
            self.assertTrue(result["success"], result.get("message"))
        self.assertEqual(user.pack_pity_counter or 0, 0)

    def test_weights_that_do_not_fit_the_band_count_as_none(self):
        """The same tolerance ``_weighted_pick_rating`` has always had."""
        self.card(91)
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[60, 30]")
        self.assertFalse(self.packs._main_odds_active(pack))


class WithOddsTests(PullCase):
    """A pack saved through the odds table."""

    def test_the_pull_follows_the_odds(self):
        for _ in range(40):
            self.card(91)
        self.card(95)
        self.session.commit()

        # 91-95, everything on the 95 — the opposite of what the card counts
        # would give, which is the point.
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[1, 0, 0, 0, 99]")
        tally = self.spread(pack, draws=1000)
        self.assertGreater(tally[95], 900)
        self.assertLess(tally[91], 100)

    def test_a_rating_with_no_card_is_dropped_and_the_rest_renormalised(self):
        """Not collapsed to flat odds, which is what the old widen did.

        Rolling a rating the version cannot fill used to fall through to a
        fallback that ignores the weights entirely — the pack paid an even
        spread while the admin read a curve.
        """
        self.card(91)
        self.card(95)
        self.session.commit()

        # Most of the weight sits on 93, which no card carries.
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[10, 0, 80, 0, 90]")
        tally = self.spread(pack, draws=2000)
        self.assertEqual(tally[93], 0)
        # 10 against 90 among what is left, not 50/50.
        self.assertLess(tally[91], 400)
        self.assertGreater(tally[95], 1600)

    def test_a_forced_rating_reaches_a_version_pack(self):
        """The pity timer's guaranteed pull, which version mode used to ignore."""
        self.card(91)
        self.card(95)
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[99, 0, 0, 0, 1]")
        for _ in range(20):
            picked = self.packs._pick_main_player(self.session, pack,
                                                  force_rating=95)
            self.assertEqual(picked.rating, 95)

    def test_the_pity_counter_starts_moving(self):
        """The band is real now, so a run of low pulls counts towards a top one."""
        self.card(91)
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[100, 0, 0, 0, 0]", bonus_count=0)
        user = self.user()
        for expected in (1, 2, 3):
            result = self.packs.open_pack(self.session, user, pack)
            self.assertTrue(result["success"], result.get("message"))
            self.assertEqual(user.pack_pity_counter, expected)

    def test_an_emptied_band_falls_back_rather_than_paying_nothing(self):
        """Cards get retired after the band was written from them."""
        self.card(60)     # the version's only card, now outside the band
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[60, 10, 10, 10, 10]")
        picked = self.packs._pick_main_player(self.session, pack)
        self.assertIsNotNone(picked, "the pack opened empty")
        self.assertEqual(picked.rating, 60)

    def test_the_pool_count_stops_promising_cards_out_of_reach(self):
        self.card(91)
        self.card(99)     # in the version, outside the band the odds cover
        self.session.commit()
        pack = self.pack(main_min_rating=91, main_max_rating=95,
                         main_weights_json="[60, 10, 10, 10, 10]")
        self.assertEqual(self.packs.count_main_pool(self.session, pack), 1)

        plain = self.pack(main_min_rating=91, main_max_rating=95,
                          main_weights_json=None)
        self.assertEqual(self.packs.count_main_pool(self.session, plain), 2)


class BothModeTests(PullCase):
    def test_an_empty_top_rating_no_longer_flattens_the_curve(self):
        for _ in range(5):
            self.card(85)
        self.card(86)
        self.session.commit()
        pack = self.pack(main_filter_mode="both",
                         main_min_rating=85, main_max_rating=87,
                         main_weights_json="[20, 80, 50]")
        tally = self.spread(pack, draws=2000)
        self.assertEqual(tally[87], 0, "nothing sits at 87")
        # 20 against 80 between the two that can be pulled.
        self.assertLess(tally[85], 600)
        self.assertGreater(tally[86], 1400)

    def test_it_still_pays_out_when_the_whole_band_is_empty(self):
        self.card(70)
        self.session.commit()
        pack = self.pack(main_filter_mode="both",
                         main_min_rating=85, main_max_rating=87,
                         main_weights_json="[20, 80, 50]")
        self.assertIsNone(self.packs._pick_main_player(self.session, pack),
                          "'both' has never widened past its own band")


if __name__ == "__main__":
    unittest.main()
