"""The trait seed and the shared market, against real rows in a real database.

``_seed_traits`` runs on every single boot against the live database, which
makes it the riskiest code in this change: it now UPDATES existing rows rather
than only inserting missing ones. The tests below pin what it is and isn't
allowed to touch — in particular that an admin's ``is_active`` toggle and price
override survive the next deploy, which is the whole reason those two fields are
excluded.

The second half buys from a real ``GlobalTraitMarket`` row through the real
service, so the unlimited-stock rule is checked against SQLAlchemy's own
UPDATE...WHERE rather than a fake.

Two things here are deliberate, and both are about not depending on which other
test modules ran first:

  * The real ``database`` / ``models`` / market modules are captured at IMPORT
    time — during collection, before any other module's fixtures run — and
    every test goes through those same objects. Several suites in this
    repository install a synthetic stub at ``sys.modules["models"]`` and never
    put the real one back, so resolving the name later can hand you a module
    with no ``Trait`` on it at all.
  * ``setUpModule`` then repairs ``sys.modules`` for the duration, because the
    production code under test resolves ``from models import Trait`` at call
    time and would pick up that same stub. Whatever was there is restored
    afterwards, so this module leaves the process exactly as it found it.

No ``DATABASE_URL`` swapping either: a throwaway engine is bound to the real
``Base``'s metadata and ``SessionLocal`` points at it for the duration.
"""

import os
import sys
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Captured during collection, while these names still resolve to the real
# modules. Do not re-resolve them inside a test.
import database as db
import models as mdl  # registers the tables on db.Base
from services import global_market as market

_TMP = None
_ENGINE = None
_SESSION_FACTORY = None
_REAL_SESSION_LOCAL = None
_SAVED_MODULES = {}
_PATCHED_NAMES = ("database", "models")


def setUpModule():
    """Point the seed at a throwaway sqlite file, without touching imports."""
    global _TMP, _ENGINE, _SESSION_FACTORY, _REAL_SESSION_LOCAL, _SAVED_MODULES

    # Undo any stub another suite left behind, for the duration of this module.
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _PATCHED_NAMES}
    sys.modules["database"] = db
    sys.modules["models"] = mdl

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    _ENGINE = create_engine(f"sqlite:///{_TMP.name}")
    db.Base.metadata.create_all(bind=_ENGINE)
    _SESSION_FACTORY = sessionmaker(bind=_ENGINE, autoflush=False,
                                    autocommit=False)

    # ``_seed_traits`` opens its own session via the module-level factory, so
    # this is the one thing that has to be redirected.
    _REAL_SESSION_LOCAL = db.SessionLocal
    db.SessionLocal = _SESSION_FACTORY


def tearDownModule():
    if _REAL_SESSION_LOCAL is not None:
        db.SessionLocal = _REAL_SESSION_LOCAL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


def _session():
    return _SESSION_FACTORY()


class SeedTests(unittest.TestCase):
    def setUp(self):
        session = _session()
        try:
            session.query(mdl.Trait).delete()
            session.commit()
        finally:
            session.close()

    def test_the_whole_catalogue_lands_in_the_database(self):
        from services.trait_service import TRAIT_DEFINITIONS

        db._seed_traits()
        session = _session()
        try:
            rows = session.query(mdl.Trait).all()
            self.assertEqual(len(rows), len(TRAIT_DEFINITIONS))
            by_key = {t.effect_key: t for t in rows}
            for definition in TRAIT_DEFINITIONS:
                row = by_key[definition["effect_key"]]
                self.assertEqual(row.name, definition["name"])
                self.assertEqual(row.category, definition["category"])
                self.assertEqual(row.rarity, definition["rarity"])
                self.assertTrue(row.is_active)
        finally:
            session.close()

    def test_seeding_twice_changes_nothing(self):

        db._seed_traits()
        session = _session()
        try:
            before = {(t.id, t.name, t.rarity) for t in session.query(mdl.Trait).all()}
        finally:
            session.close()

        db._seed_traits()
        session = _session()
        try:
            after = {(t.id, t.name, t.rarity) for t in session.query(mdl.Trait).all()}
        finally:
            session.close()
        self.assertEqual(before, after)

    def test_a_retuned_trait_reaches_an_existing_database(self):
        """The reason the seed updates instead of only inserting."""

        db._seed_traits()
        session = _session()
        try:
            row = session.query(mdl.Trait).filter(
                mdl.Trait.effect_key == "bat_finisher").first()
            row.description = "stale text from an older build"
            row.rarity = "common"
            session.commit()
        finally:
            session.close()

        db._seed_traits()
        session = _session()
        try:
            row = session.query(mdl.Trait).filter(
                mdl.Trait.effect_key == "bat_finisher").first()
            self.assertNotIn("stale", row.description)
            self.assertEqual(row.rarity, "rare")
        finally:
            session.close()

    def test_the_admin_switch_and_price_override_survive_a_deploy(self):
        """Two fields the seed must never write, or the website is a lie."""

        db._seed_traits()
        session = _session()
        try:
            row = session.query(mdl.Trait).filter(
                mdl.Trait.effect_key == "bat_anchor").first()
            row.is_active = False
            row.base_price = 4242
            session.commit()
            trait_id = row.id
        finally:
            session.close()

        db._seed_traits()
        session = _session()
        try:
            row = session.query(mdl.Trait).get(trait_id)
            self.assertFalse(row.is_active)
            self.assertEqual(row.base_price, 4242)
        finally:
            session.close()

    def test_a_renamed_trait_is_matched_on_its_effect_key(self):
        """Renaming in the catalogue must update, not insert a duplicate."""

        db._seed_traits()
        session = _session()
        try:
            row = session.query(mdl.Trait).filter(
                mdl.Trait.effect_key == "bowl_yorker").first()
            row.name = "Old Yorker Name"
            session.commit()
            trait_id = row.id
            total = session.query(mdl.Trait).count()
        finally:
            session.close()

        db._seed_traits()
        session = _session()
        try:
            self.assertEqual(session.query(mdl.Trait).count(), total)
            self.assertEqual(session.query(mdl.Trait).get(trait_id).name,
                             "Yorker Specialist")
        finally:
            session.close()


class MarketStockTests(unittest.TestCase):
    """Real rows, real UPDATE...WHERE — no fakes in this half."""

    def setUp(self):
        db._seed_traits()
        session = _session()
        try:
            session.query(mdl.GlobalTraitMarket).delete()
            session.query(mdl.TraitInventory).delete()
            session.query(mdl.MarketPurchase).delete()
            session.query(mdl.User).delete()
            user = mdl.User(telegram_id=99001, username="cap",
                            total_gems=1_000_000)
            session.add(user)
            session.commit()
            self.user_id = user.id
        finally:
            session.close()

    def _list_market(self, session, num_slots=5):
        count = market.reroll_trait_market(session, num_slots=num_slots)
        session.commit()
        return count

    def test_a_fresh_market_lists_everything_with_unlimited_stock(self):
        session = _session()
        try:
            self.assertEqual(self._list_market(session), 5)
            rows = market.list_trait_market(session)
            self.assertEqual(len(rows), 5)
            for row in rows:
                self.assertEqual(row.quantity, 0)
                self.assertTrue(market.is_unlimited(row))
        finally:
            session.close()

    def test_the_same_slot_can_be_bought_over_and_over(self):
        session = _session()
        try:
            self._list_market(session)
            slot = market.list_trait_market(session)[0]
            user = session.query(mdl.User).get(self.user_id)
            spend = 0
            for i in range(25):
                ok, name = market.buy_trait(session, user, slot.slot_index)
                self.assertTrue(ok, f"buy {i} failed: {name}")
                spend += slot.final_price
            session.commit()

            owned = (session.query(mdl.TraitInventory)
                     .filter(mdl.TraitInventory.user_id == self.user_id).count())
            self.assertEqual(owned, 25)
            self.assertEqual(slot.purchased_count, 25)
            self.assertEqual(user.total_gems, 1_000_000 - spend)
        finally:
            session.close()

    def test_a_limited_slot_still_sells_out_for_real(self):
        session = _session()
        try:
            self._list_market(session)
            slot = market.list_trait_market(session)[0]
            market.update_trait_slot(session, slot.id, quantity=2)
            session.commit()

            user = session.query(mdl.User).get(self.user_id)
            self.assertTrue(market.buy_trait(session, user, slot.slot_index)[0])
            self.assertTrue(market.buy_trait(session, user, slot.slot_index)[0])
            ok, msg = market.buy_trait(session, user, slot.slot_index)
            session.commit()
            self.assertFalse(ok)
            self.assertIn("Sold out", msg)
            self.assertEqual(slot.purchased_count, 2)
        finally:
            session.close()

    def test_a_sold_out_slot_can_be_reopened_as_unlimited(self):
        session = _session()
        try:
            self._list_market(session)
            slot = market.list_trait_market(session)[0]
            market.update_trait_slot(session, slot.id, quantity=1)
            user = session.query(mdl.User).get(self.user_id)
            self.assertTrue(market.buy_trait(session, user, slot.slot_index)[0])
            self.assertFalse(market.buy_trait(session, user, slot.slot_index)[0])

            market.update_trait_slot(session, slot.id, quantity=0)
            session.commit()
            self.assertTrue(market.buy_trait(session, user, slot.slot_index)[0])
        finally:
            session.close()

    def test_the_listed_price_is_the_traits_rarity_price(self):
        from services.trait_service import trait_price_of
        session = _session()
        try:
            self._list_market(session)
            for row in market.list_trait_market(session):
                trait = session.query(mdl.Trait).get(row.trait_id)
                self.assertEqual(row.base_price, trait_price_of(trait))
                self.assertLessEqual(row.final_price, row.base_price)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
