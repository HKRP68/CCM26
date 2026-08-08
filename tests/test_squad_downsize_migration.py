"""Coverage for the one-time squad + trait downsizing migration.

When MAX_ROSTER dropped 25 → 19 and TRAIT_MAX_PER_SQUAD (18) was introduced,
existing accounts were suddenly over both caps and stuck. ``trim_roster_to_cap``
and ``trim_squad_traits_to_cap`` bring them back in line, refunding at BUY price
rather than the lossy sell price because a forced release is not a sale.

These tests pin the selection rules (lowest rating / lowest level first), the
Career Player exemptions, the refund amounts, and re-run idempotency.
"""

import os
import sys
import tempfile
import unittest


class SquadDownsizeMigrationTest(unittest.TestCase):
    # squad_downsize_service is listed because the sibling
    # tests/test_squad_limits_button.py evicts it and rebinds it to its own
    # temporary database. Without it here, whichever file runs second can pick
    # up a service module bound to the other one's engine.
    _MODULE_NAMES = ("database", "models", "config", "services.roster_service",
                     "services.trait_service", "handlers.release",
                     "services.squad_downsize_service")

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

    _next_tg = [9000]
    _next_pid = [1000]

    def _make_user(self, session, coins=0, gems=0):
        from models import User
        self._next_tg[0] += 1
        user = User(telegram_id=self._next_tg[0], first_name="Cap",
                    roster_count=0, total_coins=coins, total_gems=gems)
        session.add(user)
        session.flush()
        return user

    def _add_card(self, session, user, rating, is_career=False):
        from models import Player, UserRoster
        self._next_pid[0] += 1
        pid = self._next_pid[0]
        player = Player(id=pid, name=f"P{pid}", rating=rating,
                        category="Batsman", country="India",
                        bat_hand="Right", bowl_hand="Right",
                        bowl_style="Medium", is_career=is_career,
                        career_owner_user_id=user.id if is_career else None)
        session.add(player)
        session.flush()
        entry = UserRoster(user_id=user.id, player_id=player.id,
                           order_position=(user.roster_count or 0) + 1)
        session.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
        session.flush()
        return entry, player

    def _make_trait(self, session, name, rarity="common", category="Batting"):
        from models import Trait
        trait = Trait(name=name, category=category, description="d",
                      emoji="✨", effect_key=f"k_{name}", rarity=rarity)
        session.add(trait)
        session.flush()
        return trait

    def _equip(self, session, user, entry, trait, level=1):
        from models import PlayerTrait
        pt = PlayerTrait(user_id=user.id, roster_id=entry.id,
                         trait_id=trait.id, level=level)
        session.add(pt)
        session.flush()
        return pt

    # ── roster trim ─────────────────────────────────────────────────────

    def test_trims_to_cap_dropping_lowest_rated_and_refunding_buy_price(self):
        from config import MAX_ROSTER, get_buy_value, get_sell_value
        from database import get_session
        from services.roster_service import trim_roster_to_cap, get_roster_count

        session = get_session()
        try:
            user = self._make_user(session)
            ratings = list(range(60, 60 + 25))  # 25 cards, 60..84
            for r in ratings:
                self._add_card(session, user, r)
            session.commit()

            result = trim_roster_to_cap(session, user)
            session.commit()

            self.assertEqual(get_roster_count(session, user.id), MAX_ROSTER)
            self.assertEqual(result["count"], 25 - MAX_ROSTER)

            # The six lowest ratings went, the rest stayed.
            dropped = sorted(r["rating"] for r in result["released"])
            self.assertEqual(dropped, ratings[:25 - MAX_ROSTER])

            expected = sum(get_buy_value(r) for r in dropped)
            self.assertEqual(result["coins"], expected)
            self.assertEqual(user.total_coins, expected)
            # Buy price, not the lossy sell price — that's the whole point.
            self.assertGreater(expected, sum(get_sell_value(r) for r in dropped))
        finally:
            session.close()

    def test_career_player_survives_even_as_the_lowest_rated_card(self):
        from config import MAX_ROSTER
        from database import get_session
        from models import UserRoster
        from services.roster_service import trim_roster_to_cap

        session = get_session()
        try:
            user = self._make_user(session)
            career_entry, _career_player = self._add_card(session, user, 50,
                                                          is_career=True)
            for r in range(70, 70 + 24):
                self._add_card(session, user, r)
            session.commit()

            trim_roster_to_cap(session, user)
            session.commit()

            survivors = (session.query(UserRoster)
                         .filter(UserRoster.user_id == user.id).all())
            self.assertEqual(len(survivors), MAX_ROSTER)
            self.assertIn(career_entry.id, [s.id for s in survivors])
        finally:
            session.close()

    def test_trim_clears_captain_pointer_and_leaves_no_undo(self):
        from database import get_session
        from models import PendingUndo
        from services.roster_service import trim_roster_to_cap

        session = get_session()
        try:
            user = self._make_user(session)
            entries = [self._add_card(session, user, r)[0]
                       for r in range(60, 60 + 25)]
            # Captain the worst card, so the trim is guaranteed to drop it.
            user.captain_roster_id = entries[0].id
            session.commit()

            trim_roster_to_cap(session, user)
            session.commit()

            self.assertIsNone(user.captain_roster_id)
            undo = (session.query(PendingUndo)
                    .filter(PendingUndo.user_id == user.id).first())
            self.assertIsNone(undo, "a forced release must not be undoable")
        finally:
            session.close()

    def test_trim_roster_is_a_no_op_when_already_under_cap(self):
        from database import get_session
        from services.roster_service import trim_roster_to_cap

        session = get_session()
        try:
            user = self._make_user(session)
            for r in range(70, 75):
                self._add_card(session, user, r)
            session.commit()

            result = trim_roster_to_cap(session, user)
            self.assertEqual(result, {"released": [], "coins": 0, "count": 0})
            self.assertEqual(user.total_coins, 0)
        finally:
            session.close()

    # ── trait trim ──────────────────────────────────────────────────────

    def test_trims_traits_lowest_level_first_and_refunds_buy_value(self):
        from config import TRAIT_MAX_PER_SQUAD, trait_buy_value
        from database import get_session
        from services.trait_service import (trim_squad_traits_to_cap,
                                            count_squad_traits)

        session = get_session()
        try:
            user = self._make_user(session)
            # 20 cards each carrying one trait; levels 1..20 capped at 5.
            levels = []
            for i in range(20):
                entry, _ = self._add_card(session, user, 80)
                trait = self._make_trait(session, f"T{user.id}_{i}")
                level = min(5, (i % 5) + 1)
                levels.append(level)
                self._equip(session, user, entry, trait, level=level)
            session.commit()

            result = trim_squad_traits_to_cap(session, user)
            session.commit()

            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
            self.assertEqual(result["count"], 20 - TRAIT_MAX_PER_SQUAD)
            # Lowest levels went first.
            self.assertEqual(sorted(r["level"] for r in result["removed"]),
                             sorted(levels)[:20 - TRAIT_MAX_PER_SQUAD])
            expected = sum(trait_buy_value(r["level"], r["rarity"])
                           for r in result["removed"])
            self.assertEqual(result["gems"], expected)
            self.assertEqual(user.total_gems, expected)
        finally:
            session.close()

    def test_career_traits_are_exempt_from_the_squad_budget(self):
        from config import TRAIT_MAX_PER_SQUAD, TRAIT_MAX_PER_PLAYER
        from database import get_session
        from models import PlayerTrait
        from services.trait_service import (trim_squad_traits_to_cap,
                                            count_squad_traits)

        session = get_session()
        try:
            user = self._make_user(session)
            career_entry, _ = self._add_card(session, user, 90, is_career=True)
            # Career card carries 3 max-level traits — the most valuable on the
            # account, and still untouchable.
            for i in range(TRAIT_MAX_PER_PLAYER):
                trait = self._make_trait(session, f"C{user.id}_{i}",
                                         rarity="elite")
                self._equip(session, user, career_entry, trait, level=5)
            # Squad sits exactly on the cap, so nothing should be refunded.
            for i in range(TRAIT_MAX_PER_SQUAD):
                entry, _ = self._add_card(session, user, 80)
                trait = self._make_trait(session, f"S{user.id}_{i}")
                self._equip(session, user, entry, trait, level=1)
            session.commit()

            result = trim_squad_traits_to_cap(session, user)
            session.commit()

            self.assertEqual(result["count"], 0)
            self.assertEqual(user.total_gems, 0)
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
            # All 21 rows survive: 18 squad + 3 career.
            total = (session.query(PlayerTrait)
                     .filter(PlayerTrait.user_id == user.id).count())
            self.assertEqual(total, TRAIT_MAX_PER_SQUAD + TRAIT_MAX_PER_PLAYER)
        finally:
            session.close()

    # ── end-to-end ──────────────────────────────────────────────────────

    def test_downsize_user_is_idempotent(self):
        from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
        from database import get_session
        from services.squad_downsize_service import downsize_user
        from services.roster_service import get_roster_count
        from services.trait_service import count_squad_traits

        session = get_session()
        try:
            user = self._make_user(session)
            for i in range(25):
                entry, _ = self._add_card(session, user, 60 + i)
                trait = self._make_trait(session, f"M{user.id}_{i}")
                self._equip(session, user, entry, trait, level=(i % 5) + 1)
            session.commit()

            first = downsize_user(session, user)
            session.commit()
            self.assertEqual(get_roster_count(session, user.id), MAX_ROSTER)
            self.assertLessEqual(count_squad_traits(session, user.id),
                                 TRAIT_MAX_PER_SQUAD)
            self.assertGreater(first["cards"], 0)

            coins_after, gems_after = user.total_coins, user.total_gems

            second = downsize_user(session, user)
            session.commit()
            self.assertEqual(second, {"cards": 0, "coins": 0,
                                      "traits": 0, "gems": 0})
            self.assertEqual(user.total_coins, coins_after)
            self.assertEqual(user.total_gems, gems_after)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
