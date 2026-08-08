"""Coverage for the admin "Apply Squad & Trait Limits" button.

The button is the website equivalent of ``migrate_squad_and_trait_limits.py``,
sharing ``services.squad_downsize_service`` so the two can never drift. It is a
destructive bulk action — it releases cards and destroys traits across every
account — so it is gated on a preview: Apply carries a token that only Preview
issues, and the token is single-use.

These tests pin the candidate scan, that a preview leaves the database exactly
as it found it, that the preview's numbers match what an apply actually does,
and the ordering of the token check in the route.
"""

import os
import sys
import tempfile
import unittest


class SquadDownsizeServiceTest(unittest.TestCase):
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

    _next_tg = [3000]
    _next_pid = [20000]
    _next_trait = [0]

    def _make_user(self, session):
        from models import User
        self._next_tg[0] += 1
        user = User(telegram_id=self._next_tg[0], first_name="Cap",
                    roster_count=0, total_coins=0, total_gems=0)
        session.add(user)
        session.flush()
        return user

    def _add_card(self, session, user, rating=80, is_career=False):
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
        return entry

    def _equip(self, session, user, entry, level=1):
        from models import PlayerTrait, Trait
        self._next_trait[0] += 1
        n = self._next_trait[0]
        trait = Trait(name=f"BT{n}", category="Batting", description="d",
                      emoji="✨", effect_key=f"bt_{n}", rarity="common")
        session.add(trait)
        session.flush()
        pt = PlayerTrait(user_id=user.id, roster_id=entry.id,
                         trait_id=trait.id, level=level)
        session.add(pt)
        session.flush()
        return pt

    # ── the candidate scan ──────────────────────────────────────────────

    def test_scan_finds_users_over_either_cap_and_skips_compliant_ones(self):
        from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
        from database import get_session
        from services.squad_downsize_service import find_over_cap_user_ids

        session = get_session()
        try:
            over_roster = self._make_user(session)
            for _ in range(MAX_ROSTER + 3):
                self._add_card(session, over_roster)

            # Under the roster cap, but one trait too many.
            over_traits = self._make_user(session)
            for _ in range(TRAIT_MAX_PER_SQUAD + 1):
                entry = self._add_card(session, over_traits)
                self._equip(session, over_traits, entry)

            compliant = self._make_user(session)
            for _ in range(5):
                entry = self._add_card(session, compliant)
                self._equip(session, compliant, entry)
            session.commit()

            found = find_over_cap_user_ids(session)
            self.assertIn(over_roster.id, found)
            self.assertIn(over_traits.id, found)
            self.assertNotIn(compliant.id, found)
        finally:
            session.close()

    def test_career_traits_never_put_an_account_on_the_list(self):
        """A full squad plus a fully kitted Career Player is legal, not over."""
        from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD, TRAIT_MAX_PER_PLAYER
        from database import get_session
        from services.squad_downsize_service import find_over_cap_user_ids

        session = get_session()
        try:
            user = self._make_user(session)
            career = self._add_card(session, user, is_career=True)
            for _ in range(TRAIT_MAX_PER_PLAYER):
                self._equip(session, user, career, level=5)
            for _ in range(MAX_ROSTER - 1):
                self._add_card(session, user)
            for entry in (session.query(type(career))
                          .filter_by(user_id=user.id).all()[:TRAIT_MAX_PER_SQUAD]):
                if entry.id != career.id:
                    self._equip(session, user, entry)
            session.commit()

            # 21 equipped traits in total, but only 18 count toward the squad.
            self.assertNotIn(user.id, find_over_cap_user_ids(session))
        finally:
            session.close()

    # ── preview ─────────────────────────────────────────────────────────

    def test_preview_changes_nothing_and_is_repeatable(self):
        from config import MAX_ROSTER
        from database import get_session
        from services.roster_service import get_roster_count
        from services.squad_downsize_service import preview_downsize

        session = get_session()
        try:
            user = self._make_user(session)
            for _ in range(MAX_ROSTER + 4):
                self._add_card(session, user)
            session.commit()
            before = get_roster_count(session, user.id)
            coins_before = user.total_coins

            first = preview_downsize(session)
            second = preview_downsize(session)

            self.assertEqual(first, second, "a preview must not affect the next")
            self.assertGreater(first["cards"], 0)
            # Nothing persisted: the roster and the balance are untouched.
            self.assertEqual(get_roster_count(session, user.id), before)
            self.assertEqual(user.total_coins, coins_before)
        finally:
            session.close()

    def test_preview_numbers_match_what_apply_actually_does(self):
        """The preview runs the real thing and rolls back, so it can't drift."""
        from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
        from database import get_session
        from services.squad_downsize_service import preview_downsize, run_downsize

        session = get_session()
        try:
            user = self._make_user(session)
            for i in range(MAX_ROSTER + 5):
                entry = self._add_card(session, user, rating=60 + i)
                self._equip(session, user, entry, level=(i % 5) + 1)
            session.commit()

            user_id = user.id  # run_downsize expunges the session (see its docstring)
            predicted = preview_downsize(session)
            actual = run_downsize(session)

            self.assertEqual(predicted, actual)
            self.assertGreaterEqual(predicted["cards"], 5)
            # And the caps are genuinely satisfied afterwards.
            from services.roster_service import get_roster_count
            from services.trait_service import count_squad_traits
            self.assertLessEqual(get_roster_count(session, user_id), MAX_ROSTER)
            self.assertLessEqual(count_squad_traits(session, user_id),
                                 TRAIT_MAX_PER_SQUAD)
        finally:
            session.close()

    def test_apply_is_idempotent(self):
        from config import MAX_ROSTER
        from database import get_session
        from services.squad_downsize_service import (find_over_cap_user_ids,
                                                     run_downsize)

        session = get_session()
        try:
            user = self._make_user(session)
            for _ in range(MAX_ROSTER + 2):
                self._add_card(session, user)
            session.commit()

            first = run_downsize(session)
            self.assertGreater(first["users"], 0)
            self.assertEqual(find_over_cap_user_ids(session), [])

            second = run_downsize(session)
            self.assertEqual(second["users"], 0)
            self.assertEqual(second["cards"], 0)
            self.assertEqual(second["traits"], 0)
        finally:
            session.close()


class ApplyRouteIsGatedOnPreviewTest(unittest.TestCase):
    """Source-level checks on the Apply route's confirmation gate.

    Importing ``admin`` pulls in the whole Flask app, its login layer and a live
    database — far more than these assertions need. Same approach as
    ``tests/test_admin_destructive_confirmations.py``.
    """

    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), "..", "admin.py")
        with open(path) as fh:
            src = fh.read()
        self.route = (src.split("def admin_squad_limits_apply():")[1]
                      .split("\n@app.route")[0])

    def test_the_route_requires_a_token_issued_by_preview(self):
        self.assertIn('request.form.get("confirm_token"', self.route)
        self.assertIn("_SQUAD_TOKEN_KEY", self.route)

    def test_the_token_is_checked_before_anything_is_released(self):
        # Order matters: a mismatched token must return before run_downsize is
        # ever reached, or a bare POST would still have emptied every roster.
        check = self.route.index("supplied != expected")
        work = self.route.index("run_downsize(db)")
        self.assertLess(check, work)

    def test_a_failed_check_returns_without_doing_work(self):
        head = self.route[:self.route.index("run_downsize(db)")]
        self.assertIn("return redirect", head)

    def test_the_token_is_single_use(self):
        # Cleared before the work runs, so a double submit can't apply twice
        # even while the first request is still in flight.
        popped = self.route.index(f"session.pop(_SQUAD_TOKEN_KEY")
        work = self.route.index("run_downsize(db)")
        self.assertLess(popped, work)


if __name__ == "__main__":
    unittest.main()
