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

    def setUp(self):
        # preview_downsize/run_downsize sweep EVERY over-cap account in the
        # shared database, so a user left behind by one test would silently
        # change what the next one sees swept. Each test cleans up after
        # itself, keeping the global scans scoped to the accounts it created.
        self._created_user_ids = []
        self.addCleanup(self._delete_created_users)

    def _delete_created_users(self):
        from database import get_session
        from models import PlayerTrait, TraitInventory, User, UserRoster
        if not self._created_user_ids:
            return
        session = get_session()
        try:
            ids = self._created_user_ids
            for model in (PlayerTrait, TraitInventory, UserRoster):
                (session.query(model)
                 .filter(model.user_id.in_(ids))
                 .delete(synchronize_session=False))
            (session.query(User).filter(User.id.in_(ids))
             .delete(synchronize_session=False))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _make_user(self, session):
        from models import User
        self._next_tg[0] += 1
        user = User(telegram_id=self._next_tg[0], first_name="Cap",
                    roster_count=0, total_coins=0, total_gems=0)
        session.add(user)
        session.flush()
        self._created_user_ids.append(user.id)
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

        from models import PlayerTrait, UserRoster
        from services.trait_service import count_squad_traits

        session = get_session()
        try:
            user = self._make_user(session)
            career = self._add_card(session, user, is_career=True)
            for _ in range(TRAIT_MAX_PER_PLAYER):
                self._equip(session, user, career, level=5)
            for _ in range(MAX_ROSTER - 1):
                self._add_card(session, user)

            # Exclude the career entry BEFORE slicing, or a career row landing
            # inside the first 18 leaves the squad one trait short and the test
            # stops checking the boundary it says it checks.
            squad = [e for e in session.query(UserRoster)
                     .filter_by(user_id=user.id).all() if e.id != career.id]
            for entry in squad[:TRAIT_MAX_PER_SQUAD]:
                self._equip(session, user, entry)
            session.commit()

            # Exactly on the squad cap, with the career card's 3 on top of it.
            self.assertEqual(count_squad_traits(session, user.id),
                             TRAIT_MAX_PER_SQUAD)
            self.assertEqual(
                session.query(PlayerTrait).filter_by(user_id=user.id).count(),
                TRAIT_MAX_PER_SQUAD + TRAIT_MAX_PER_PLAYER)
            # 21 equipped in total, but only 18 count — so this is not over cap.
            self.assertNotIn(user.id, find_over_cap_user_ids(session))
        finally:
            session.close()

    # ── preview ─────────────────────────────────────────────────────────

    def test_preview_changes_nothing_and_is_repeatable(self):
        from config import MAX_ROSTER
        from database import get_session
        from services.roster_service import get_roster_count
        from services.squad_downsize_service import preview_downsize

        from models import User

        session = get_session()
        try:
            user = self._make_user(session)
            for _ in range(MAX_ROSTER + 4):
                self._add_card(session, user)
            session.commit()
            # preview_downsize expunges as it goes (see its docstring), so hold
            # the id and re-query rather than the instance.
            user_id = user.id
            before = get_roster_count(session, user_id)
            coins_before = user.total_coins

            first = preview_downsize(session)
            second = preview_downsize(session)

            self.assertEqual(first, second, "a preview must not affect the next")
            self.assertGreater(first["cards"], 0)
            # Nothing persisted: the roster and the balance are untouched.
            self.assertEqual(get_roster_count(session, user_id), before)
            self.assertEqual(session.get(User, user_id).total_coins, coins_before)
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

    def test_a_committed_run_hands_back_everything_a_dm_needs(self):
        """on_user_done gets the chat id and the itemised detail, post-commit.

        The callback fires only after that account's changes are committed, so
        nobody can be told about a release that was rolled back.
        """
        from config import MAX_ROSTER
        from database import get_session
        from services.squad_downsize_service import (format_downsize_dm,
                                                     run_downsize)

        session = get_session()
        try:
            user = self._make_user(session)
            for i in range(MAX_ROSTER + 3):
                self._add_card(session, user, rating=60 + i)
            session.commit()
            user_id, tg_id = user.id, user.telegram_id

            seen = []
            run_downsize(session, on_user_done=lambda uid, r: seen.append((uid, r)))

            mine = [r for uid, r in seen if uid == user_id]
            self.assertEqual(len(mine), 1)
            result = mine[0]
            self.assertEqual(result["telegram_id"], tg_id)
            self.assertEqual(len(result["released"]), 3)
            self.assertTrue(all(r.get("name") for r in result["released"]))
            # ...and it composes into a message naming a real released card.
            msg = format_downsize_dm(result)
            self.assertIn(result["released"][0]["name"], msg)
        finally:
            session.close()

    def test_an_untouched_account_is_never_reported_for_a_dm(self):
        """A squad already within the limits must not be messaged."""
        from config import MAX_ROSTER
        from database import get_session
        from services.squad_downsize_service import run_downsize

        session = get_session()
        try:
            user = self._make_user(session)
            for _ in range(MAX_ROSTER - 2):
                self._add_card(session, user)
            session.commit()
            user_id = user.id

            seen = []
            run_downsize(session, on_user_done=lambda uid, r: seen.append(uid))
            self.assertNotIn(user_id, seen)
        finally:
            session.close()

    def test_one_bad_account_does_not_abort_a_preview(self):
        """A dry run must skip what a real run would skip, not die on it."""
        from unittest.mock import patch
        from config import MAX_ROSTER
        from database import get_session
        from services.squad_downsize_service import preview_downsize

        session = get_session()
        try:
            doomed = self._make_user(session)
            healthy = self._make_user(session)
            for user in (doomed, healthy):
                for _ in range(MAX_ROSTER + 2):
                    self._add_card(session, user)
            session.commit()
            doomed_id = doomed.id

            real_trim = __import__("services.squad_downsize_service",
                                   fromlist=["x"]).trim_roster_to_cap

            def explode(sess, user, *a, **kw):
                if user.id == doomed_id:
                    raise RuntimeError("boom")
                return real_trim(sess, user, *a, **kw)

            with patch("services.squad_downsize_service.trim_roster_to_cap",
                       side_effect=explode):
                totals = preview_downsize(session)

            # The bad account is counted, not fatal, and the healthy one is
            # still previewed.
            self.assertEqual(totals["failed"], 1)
            self.assertEqual(totals["users"], 1)
            self.assertGreater(totals["cards"], 0)
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
            self.src = fh.read()
        self.route = (self.src.split("def admin_squad_limits_apply():")[1]
                      .split("\n@app.route")[0])
        self.worker = (self.src.split("def _squad_limits_worker(")[1]
                       .split("\n@app.route")[0])

    def test_apply_needs_no_preview_token(self):
        """One press. The preview gate is gone on purpose.

        A preview runs the entire sweep and throws it away, so requiring one
        made every apply cost twice what it needed to.
        """
        self.assertNotIn("confirm_token", self.route)
        self.assertNotIn("_SQUAD_TOKEN_KEY", self.src)

    def test_the_request_does_not_do_the_work(self):
        # At ~28 queries per over-cap account the sweep can outlast the web
        # server's request timeout. A timed-out apply is the worst outcome
        # available: the browser errors while the work keeps committing.
        self.assertNotIn("run_downsize", self.route)
        self.assertIn("threading.Thread", self.route)
        self.assertIn("_squad_limits_worker", self.route)

    def test_the_single_flight_claim_is_atomic(self):
        """Checking "already running?" and claiming must share one lock hold.

        Two `with` blocks would reintroduce exactly the race the session-cookie
        token had — both requests read "idle", both start a sweep.
        """
        head = self.route[:self.route.index("threading.Thread")]
        acquire = head.index("with _SQUAD_JOB_LOCK")
        check = head.index('_SQUAD_JOB.get("state") == "running"')
        claim = head.index('state="running"')
        self.assertLess(acquire, check)
        self.assertLess(check, claim)
        # ...and no second acquisition between them.
        self.assertEqual(head[acquire:claim].count("with _SQUAD_JOB_LOCK"), 1)

    def test_a_busy_sweep_is_refused_rather_than_doubled(self):
        head = self.route[:self.route.index("threading.Thread")]
        self.assertIn("already running", head)
        self.assertIn("return redirect", head)

    def test_the_worker_owns_its_session_and_never_raises(self):
        # The request that started it returned long ago, so it cannot borrow
        # that Session; and a thread dying silently would leave the panel
        # reading "running" forever.
        self.assertIn("get_session()", self.worker)
        self.assertIn("db.close()", self.worker)
        self.assertIn('state="error"', self.worker)

    def test_progress_counts_only_committed_accounts(self):
        # on_user_done fires after each account's commit, so the number on
        # screen can never be ahead of what is actually saved.
        self.assertIn("on_user_done=_on_user", self.worker)
        self.assertIn('_SQUAD_JOB["done_users"]', self.worker)

    def test_the_audit_log_cannot_fail_the_committed_sweep(self):
        tail = self.worker[self.worker.index("run_downsize("):]
        self.assertIn("log_admin", tail)
        self.assertIn("squad limits audit log failed after apply", tail)



class DownsizeDmTest(unittest.TestCase):
    """The DM a captain gets after the Apply button rewrites their squad.

    ``format_downsize_dm`` takes a plain result dict, so these need no database
    — which is the point: the message can be checked without seeding an
    account, and the composer stays a pure function that both entry points and
    the tests can call the same way.
    """

    def _result(self, **over):
        base = {"cards": 2, "coins": 3400, "traits": 1, "gems": 250,
                "telegram_id": 555,
                "released": [{"name": "Foo Bar", "rating": 61, "value": 1700},
                             {"name": "Baz Qux", "rating": 62, "value": 1700}],
                "removed": [{"name": "Power Hitter", "level": 2,
                             "rarity": "common", "gems": 250}]}
        base.update(over)
        return base

    def test_silent_when_the_account_lost_nothing(self):
        """No message for a squad that was already legal."""
        from services.squad_downsize_service import format_downsize_dm
        self.assertIsNone(format_downsize_dm(self._result(released=[],
                                                          removed=[])))

    def test_names_what_left_and_what_it_paid_back(self):
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result())
        self.assertIn("Foo Bar", msg)
        self.assertIn("Baz Qux", msg)
        self.assertIn("Power Hitter", msg)
        self.assertIn("3,400", msg)   # coins refunded
        self.assertIn("250", msg)     # gems refunded

    def test_states_the_caps_and_the_career_exemption(self):
        from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result())
        self.assertIn(str(MAX_ROSTER), msg)
        self.assertIn(str(TRAIT_MAX_PER_SQUAD), msg)
        self.assertIn("Career", msg)

    def test_does_not_claim_the_career_player_is_outside_the_roster_cap(self):
        """It is exempt from release, NOT from the count.

        trim_roster_to_cap computes ``over = len(rows) - cap`` across every
        roster row, career included, so a captain with 19 cards one of which is
        their Career Player is exactly at the cap. Telling them the career slot
        is free would have them expecting 20.
        """
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result())
        self.assertNotIn("exempt from both", msg)
        self.assertIn("takes one of those slots", msg)

    def test_does_not_claim_the_inventory_was_untouched(self):
        # Released cards hand their traits BACK to inventory, so "untouched"
        # would contradict the line above it. Nothing is ever removed, though.
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result())
        self.assertIn("Nothing was removed from your trait inventory", msg)

    def test_promises_the_trait_inventory_was_untouched(self):
        # The fear a message like this creates, answered explicitly — and the
        # answer is true: only equipped traits are ever candidates.
        from services.squad_downsize_service import format_downsize_dm
        self.assertIn("inventory", format_downsize_dm(self._result()).lower())

    def test_a_roster_only_trim_does_not_claim_traits_were_removed(self):
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result(removed=[], traits=0, gems=0))
        self.assertIn("Players released", msg)
        self.assertNotIn("Traits removed", msg)

    def test_player_names_are_html_escaped(self):
        # parse_mode=HTML: an unescaped name is a broken message at best.
        from services.squad_downsize_service import format_downsize_dm
        msg = format_downsize_dm(self._result(
            released=[{"name": "<b>hax</b> & co", "rating": 60, "value": 1}]))
        self.assertNotIn("<b>hax</b>", msg)
        self.assertIn("&lt;b&gt;hax&lt;/b&gt; &amp; co", msg)

    def test_long_lists_are_collapsed_to_stay_under_telegrams_limit(self):
        from services.squad_downsize_service import format_downsize_dm
        released = [{"name": f"P{i}", "rating": 60, "value": 100}
                    for i in range(30)]
        msg = format_downsize_dm(self._result(released=released, cards=30))
        self.assertIn("more", msg)
        self.assertLess(len(msg), 4096, "Telegram rejects longer messages")
        # The count in the heading is still the true one.
        self.assertIn("Players released (30)", msg)


if __name__ == "__main__":
    unittest.main()
