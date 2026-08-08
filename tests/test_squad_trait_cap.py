"""Coverage for the squad-wide trait cap (TRAIT_MAX_PER_SQUAD).

Trait limits used to be purely per roster slot (3 per player), so a captain
could kit out every card they owned. A squad-wide budget of 18 now sits on top
of that — with the Career Player exempt, carrying its own 3 slots, so a fully
kitted account holds 21 equipped traits.

These tests pin the cap, the career exemption, and that a replace (which is
net-zero on the count) still works while sitting exactly on the cap.
"""

import os
import sys
import tempfile
import unittest


class SquadTraitCapTest(unittest.TestCase):
    _MODULE_NAMES = ("database", "models", "config", "services.trait_service")

    @classmethod
    def setUpClass(cls):
        cls._prev_database_url = os.environ.get("DATABASE_URL")
        cls._saved_modules = {name: sys.modules.get(name) for name in cls._MODULE_NAMES}
        for name in cls._MODULE_NAMES:
            sys.modules.pop(name, None)

        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmp.name}"

        from database import Base, engine
        import models  # noqa: F401  (registers the tables on Base)

        cls.engine = engine
        Base.metadata.create_all(bind=engine)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.engine.dispose()
        except Exception:
            pass
        if cls._prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls._prev_database_url
        for name, module in cls._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        try:
            os.unlink(cls._tmp.name)
        except OSError:
            pass

    # ── fixtures ────────────────────────────────────────────────────────

    _next_tg = [7000]
    _next_pid = [5000]
    _next_trait = [0]

    def _make_user(self, session, gems=0):
        from models import User
        self._next_tg[0] += 1
        user = User(telegram_id=self._next_tg[0], first_name="Cap",
                    roster_count=0, total_coins=0, total_gems=gems)
        session.add(user)
        session.flush()
        return user

    def _add_card(self, session, user, is_career=False):
        from models import Player, UserRoster
        self._next_pid[0] += 1
        pid = self._next_pid[0]
        player = Player(id=pid, name=f"P{pid}", rating=85, category="Batsman",
                        country="India", bat_hand="Right", bowl_hand="Right",
                        bowl_style="Medium", is_career=is_career,
                        career_owner_user_id=user.id if is_career else None)
        session.add(player)
        session.flush()
        entry = UserRoster(user_id=user.id, player_id=player.id,
                           order_position=(user.roster_count or 0) + 1)
        session.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
        session.flush()
        return entry

    def _stock_inventory(self, session, user, category="Batting"):
        """Add one fresh trait to the user's inventory and return the row."""
        from models import Trait, TraitInventory
        self._next_trait[0] += 1
        n = self._next_trait[0]
        trait = Trait(name=f"Trait{n}", category=category, description="d",
                      emoji="✨", effect_key=f"eff_{n}", rarity="common")
        session.add(trait)
        session.flush()
        inv = TraitInventory(user_id=user.id, trait_id=trait.id, level=1)
        session.add(inv)
        session.flush()
        return inv

    def _fill_squad_to_cap(self, session, user):
        """Equip exactly TRAIT_MAX_PER_SQUAD traits across non-career cards."""
        from config import TRAIT_MAX_PER_SQUAD
        from services.trait_service import apply_trait_to_player
        for _ in range(TRAIT_MAX_PER_SQUAD):
            entry = self._add_card(session, user)
            inv = self._stock_inventory(session, user)
            ok, msg = apply_trait_to_player(session, user, inv.id, entry.id)
            self.assertTrue(ok, msg)
        session.flush()

    # ── tests ───────────────────────────────────────────────────────────

    def test_applying_the_nineteenth_squad_trait_is_refused(self):
        from config import TRAIT_MAX_PER_SQUAD
        from database import get_session
        from services.trait_service import (apply_trait_to_player,
                                            count_squad_traits)
        session = get_session()
        try:
            user = self._make_user(session)
            self._fill_squad_to_cap(session, user)
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)

            entry = self._add_card(session, user)
            inv = self._stock_inventory(session, user)
            ok, msg = apply_trait_to_player(session, user, inv.id, entry.id)

            self.assertFalse(ok)
            self.assertIn(str(TRAIT_MAX_PER_SQUAD), msg)
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
        finally:
            session.rollback()
            session.close()

    def test_career_player_keeps_its_own_three_slots_at_the_cap(self):
        from config import TRAIT_MAX_PER_SQUAD, TRAIT_MAX_PER_PLAYER
        from models import PlayerTrait
        from database import get_session
        from services.trait_service import (apply_trait_to_player,
                                            count_squad_traits)
        session = get_session()
        try:
            user = self._make_user(session)
            self._fill_squad_to_cap(session, user)
            career = self._add_card(session, user, is_career=True)

            # All three career slots fill even though the squad is at its cap.
            for i, category in enumerate(("Batting", "Bowling", "Fielding")):
                inv = self._stock_inventory(session, user, category=category)
                ok, msg = apply_trait_to_player(session, user, inv.id, career.id)
                self.assertTrue(ok, f"career slot {i + 1}: {msg}")

            # A 4th is refused by the per-player rule, not the squad budget.
            inv = self._stock_inventory(session, user, category="Mental")
            ok, msg = apply_trait_to_player(session, user, inv.id, career.id)
            self.assertFalse(ok)
            self.assertIn(str(TRAIT_MAX_PER_PLAYER), msg)

            # Career traits never count toward the squad budget.
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
            total = (session.query(PlayerTrait)
                     .filter(PlayerTrait.user_id == user.id).count())
            self.assertEqual(total, TRAIT_MAX_PER_SQUAD + TRAIT_MAX_PER_PLAYER)
        finally:
            session.rollback()
            session.close()

    def test_replace_still_works_while_sitting_exactly_on_the_cap(self):
        from config import TRAIT_MAX_PER_SQUAD, TRAIT_REPLACE_COST
        from models import PlayerTrait
        from database import get_session
        from services.trait_service import (replace_trait_on_player,
                                            count_squad_traits)
        session = get_session()
        try:
            user = self._make_user(session, gems=TRAIT_REPLACE_COST * 2)
            self._fill_squad_to_cap(session, user)

            victim = (session.query(PlayerTrait)
                      .filter(PlayerTrait.user_id == user.id).first())
            inv = self._stock_inventory(session, user)

            ok, msg = replace_trait_on_player(session, user, victim.id, inv.id)
            self.assertTrue(ok, msg)
            # A replace is net-zero on the count, so the cap is untouched.
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
        finally:
            session.rollback()
            session.close()

    def test_removing_a_trait_frees_a_squad_slot(self):
        from config import TRAIT_MAX_PER_SQUAD
        from models import PlayerTrait
        from database import get_session
        from services.trait_service import (apply_trait_to_player,
                                            remove_trait_from_player,
                                            count_squad_traits)
        session = get_session()
        try:
            user = self._make_user(session)
            self._fill_squad_to_cap(session, user)

            victim = (session.query(PlayerTrait)
                      .filter(PlayerTrait.user_id == user.id).first())
            ok, _ = remove_trait_from_player(session, user, victim.id)
            self.assertTrue(ok)
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD - 1)

            entry = self._add_card(session, user)
            inv = self._stock_inventory(session, user)
            ok, msg = apply_trait_to_player(session, user, inv.id, entry.id)
            self.assertTrue(ok, msg)
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
        finally:
            session.rollback()
            session.close()

    def test_budget_payload_reports_used_max_and_full(self):
        from config import TRAIT_MAX_PER_SQUAD
        from database import get_session
        from services.trait_service import squad_trait_budget
        session = get_session()
        try:
            user = self._make_user(session)
            self.assertEqual(squad_trait_budget(session, user.id),
                             {"used": 0, "max": TRAIT_MAX_PER_SQUAD,
                              "full": False})

            self._fill_squad_to_cap(session, user)
            self.assertEqual(squad_trait_budget(session, user.id),
                             {"used": TRAIT_MAX_PER_SQUAD,
                              "max": TRAIT_MAX_PER_SQUAD, "full": True})
        finally:
            session.rollback()
            session.close()

    def test_one_users_traits_never_count_against_another(self):
        from database import get_session
        from services.trait_service import count_squad_traits
        session = get_session()
        try:
            loaded = self._make_user(session)
            self._fill_squad_to_cap(session, loaded)
            fresh = self._make_user(session)
            self.assertEqual(count_squad_traits(session, fresh.id), 0)
        finally:
            session.rollback()
            session.close()


if __name__ == "__main__":
    unittest.main()
