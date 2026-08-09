"""Changing the name and country on a Career Player.

The rules this locks down are the ones money and identity hang off:

  • the first change is free, the second costs 300 💎, the third 500 💎, and
    every one after that is 250 💎 more than the last
  • paid changes are only on sale while the website has opened them; the free
    one always works
  • one submission buys both halves — name *and* country for a single rung
  • a name rolled from the curated pool applies itself; a name the owner typed
    waits for approval and **the card keeps its old name and country until then**
  • rejecting or withdrawing refunds every gem *and* gives the rung back, so a
    refused name costs nobody a change
"""

import itertools
import os
import sys
import tempfile
import unittest

_TG_IDS = itertools.count(96_001)
_NAME_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.career_service",
                 "services.career_change_service", "services.career_name_service",
                 "services.config_service", "services.player_service")


def unique_name(prefix):
    """A unique career name built only from letters (names never hold digits)."""
    number = next(_NAME_SEQ)
    suffix = ""
    while True:
        number, remainder = divmod(number - 1, 26)
        suffix = chr(ord("A") + remainder) + suffix
        if number == 0:
            return f"{prefix} {suffix}"


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
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)

    from database import get_session
    from models import CareerFace, CareerNamePool
    session = get_session()
    try:
        session.add(CareerFace(slot=1, label="Face 1", sort_order=1,
                               is_active=True))
        # Two countries with enough names behind them that a roll always
        # resolves, which is what the wizard's own availability rules require.
        for country, firsts, surnames in (
                ("India", ("Arjun", "Bhuvan", "Chirag"), ("Menon", "Nair", "Oberoi")),
                ("Australia", ("Dean", "Elliot", "Finn"), ("Preston", "Quilty", "Rowe"))):
            for value in firsts:
                session.add(CareerNamePool(country=country, name_kind="first",
                                           value=value, letter=value[0],
                                           is_active=True))
            for value in surnames:
                session.add(CareerNamePool(country=country, name_kind="surname",
                                           value=value, letter=value[0],
                                           is_active=True))
        session.commit()
    finally:
        session.close()


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


class ChangeTestCase(unittest.TestCase):
    """One owner with one career player, and the website settings reset."""

    #: Overridden per test class; written to GameConfig in setUp.
    CONFIG = {}

    def setUp(self):
        from database import get_session
        from models import User
        from services import career_service as cs
        from services.config_service import save_config

        self.session = get_session()
        settings = {
            "career_change_price_2": 300,
            "career_change_price_3": 500,
            "career_change_price_step": 250,
            "career_paid_changes_open": True,
            "career_custom_names_open": True,
            "career_custom_names_need_approval": True,
            "career_name_blocklist": "",
        }
        settings.update(self.CONFIG)
        save_config(self.session, settings)

        self.user = User(telegram_id=next(_TG_IDS), username="changer",
                         total_coins=1000, total_gems=10_000, roster_count=0)
        self.session.add(self.user)
        self.session.flush()
        result = cs.create_career_player(
            self.session, self.user, name=unique_name("Change Owner"),
            country="India", bat_hand="Right", bowl_hand="Right",
            bowl_style="Fast", face_slot=1)
        self.assertTrue(result["ok"], result.get("message"))
        self.player = result["player"]
        # A name for this test alone. The whole module shares one database and
        # an applied change is committed, so a literal name reused across tests
        # would collide with the card an earlier test already renamed.
        self.custom = unique_name("Typed Name")
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def submit(self, **kwargs):
        from services import career_change_service as ccs
        result = ccs.submit_change(self.session, self.user, player=self.player,
                                   **kwargs)
        if result["ok"]:
            self.session.commit()
        else:
            self.session.rollback()
        return result


class PriceLadderTests(unittest.TestCase):
    """The ladder itself, with no database in the way."""

    def conf(self, **overrides):
        from services import career_change_service as ccs
        base = {"price_2": 300, "price_3": 500, "step": 250,
                "paid_open": True, "custom_open": True,
                "needs_approval": True, "blocklist": ""}
        base.update(overrides)
        return base

    def test_the_first_change_is_free(self):
        from services import career_change_service as ccs
        self.assertEqual(ccs.change_cost(1, self.conf()), 0)

    def test_the_published_ladder(self):
        from services import career_change_service as ccs
        conf = self.conf()
        self.assertEqual([ccs.change_cost(i, conf) for i in range(1, 7)],
                         [0, 300, 500, 750, 1000, 1250])

    def test_the_website_can_retune_every_rung(self):
        from services import career_change_service as ccs
        conf = self.conf(price_2=100, price_3=200, step=50)
        self.assertEqual([ccs.change_cost(i, conf) for i in range(1, 6)],
                         [0, 100, 200, 250, 300])


class FreeChangeTests(ChangeTestCase):
    def test_the_first_change_costs_nothing_and_applies_at_once(self):
        before = self.user.total_gems
        result = self.submit(country="Australia")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertTrue(result["applied"])
        self.assertEqual(result["cost"], 0)
        self.assertEqual(self.user.total_gems, before)
        self.assertEqual(self.player.country, "Australia")
        self.assertEqual(self.player.career_changes_used, 1)

    def test_a_pool_name_applies_without_review(self):
        from services import career_change_service as ccs
        rolled = ccs.roll_pool_name(self.session, "India")
        self.assertIsNotNone(rolled)
        result = self.submit(name=rolled, source=ccs.SOURCE_POOL)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertTrue(result["applied"])
        self.assertEqual(self.player.name, rolled)
        self.assertIsNone(ccs.pending_request(self.session, self.user.id))

    def test_name_and_country_together_cost_one_rung(self):
        from services import career_change_service as ccs
        rolled = ccs.roll_pool_name(self.session, "Australia")
        result = self.submit(name=rolled, country="Australia",
                             source=ccs.SOURCE_POOL)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.player.career_changes_used, 1)
        self.assertEqual((self.player.name, self.player.country),
                         (rolled, "Australia"))

    def test_asking_for_what_you_already_have_is_not_a_change(self):
        result = self.submit(country="India")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unchanged")
        self.assertEqual(self.player.career_changes_used, 0)


class PaidChangeTests(ChangeTestCase):
    def test_the_second_change_costs_the_second_rung(self):
        self.submit(country="Australia")             # the free one
        before = self.user.total_gems
        result = self.submit(country="India")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["cost"], 300)
        self.assertEqual(self.user.total_gems, before - 300)
        self.assertEqual(self.player.career_changes_used, 2)

    def test_the_ladder_keeps_climbing(self):
        costs = []
        for country in ("Australia", "India", "Australia", "India"):
            result = self.submit(country=country)
            self.assertTrue(result["ok"], result.get("message"))
            costs.append(result["cost"])
        self.assertEqual(costs, [0, 300, 500, 750])

    def test_a_change_nobody_can_afford_is_refused_and_charges_nothing(self):
        self.submit(country="Australia")
        self.user.total_gems = 10
        self.session.commit()
        result = self.submit(country="India")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_gems")
        self.assertEqual(self.user.total_gems, 10)
        self.assertEqual(self.player.country, "Australia")
        self.assertEqual(self.player.career_changes_used, 1)


class ClosedPaidChangeTests(ChangeTestCase):
    CONFIG = {"career_paid_changes_open": False}

    def test_the_free_change_works_even_when_paid_ones_are_shut(self):
        result = self.submit(country="Australia")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.player.country, "Australia")

    def test_the_second_change_is_refused_until_the_website_opens_it(self):
        self.submit(country="Australia")
        before = self.user.total_gems
        result = self.submit(country="India")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "closed")
        self.assertEqual(self.user.total_gems, before)
        self.assertEqual(self.player.country, "Australia")

    def test_a_granted_free_change_still_works_while_paid_ones_are_shut(self):
        from services import career_change_service as ccs
        self.submit(country="Australia")
        ccs.grant_free_change(self.session, self.player)
        self.session.commit()

        result = self.submit(country="India")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["cost"], 0)
        self.assertTrue(result["used_free_grant"])
        self.assertEqual(self.player.career_free_changes, 0)
        # A gift never moves the ladder: the next paid change is still the 2nd.
        self.assertEqual(self.player.career_changes_used, 1)


class CustomNameReviewTests(ChangeTestCase):
    def test_a_typed_name_waits_and_the_old_name_keeps_running(self):
        from services import career_change_service as ccs
        was = self.player.name
        result = self.submit(name=self.custom, country="Australia",
                             source=ccs.SOURCE_CUSTOM)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertFalse(result["applied"])
        # Neither half moves while it waits — a card is never half-changed.
        self.assertEqual(self.player.name, was)
        self.assertEqual(self.player.country, "India")

        pending = ccs.pending_request(self.session, self.user.id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.new_name, self.custom)
        self.assertEqual(pending.new_country, "Australia")

    def test_approval_puts_both_halves_on_the_card(self):
        from services import career_change_service as ccs
        self.submit(name=self.custom, country="Australia",
                    source=ccs.SOURCE_CUSTOM)
        pending = ccs.pending_request(self.session, self.user.id)
        result = ccs.approve_request(self.session, pending, reviewer="admin")
        self.session.commit()

        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.player.name, self.custom)
        self.assertEqual(self.player.country, "Australia")
        self.assertEqual(pending.status, ccs.STATUS_APPROVED)

    def test_rejection_refunds_the_gems_and_hands_the_change_back(self):
        from services import career_change_service as ccs
        self.submit(country="Australia")                    # burn the free one
        before = self.user.total_gems
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        self.assertEqual(self.user.total_gems, before - 300)

        pending = ccs.pending_request(self.session, self.user.id)
        result = ccs.reject_request(self.session, pending, reviewer="admin",
                                    note="Too close to a real player")
        self.session.commit()

        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["refunded"], 300)
        self.assertEqual(self.user.total_gems, before)
        self.assertEqual(self.player.career_changes_used, 1)
        self.assertEqual(pending.review_note, "Too close to a real player")
        # And the next attempt is priced as the 2nd change all over again.
        from services.career_change_service import quote
        self.assertEqual(quote(self.session, self.user, self.player)["cost"], 300)

    def test_withdrawing_refunds_the_same_way(self):
        from services import career_change_service as ccs
        self.submit(country="Australia")
        before = self.user.total_gems
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)

        pending = ccs.pending_request(self.session, self.user.id)
        result = ccs.cancel_request(self.session, pending, by_owner=True)
        self.session.commit()
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.user.total_gems, before)
        self.assertEqual(self.player.career_changes_used, 1)

    def test_a_typed_name_dressed_up_as_a_pool_name_is_still_reviewed(self):
        """The caller's claim about where a name came from is checked, not trusted."""
        from services import career_change_service as ccs
        result = self.submit(name=self.custom, source=ccs.SOURCE_POOL)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertFalse(result["applied"])
        self.assertEqual(result["request"].name_source, ccs.SOURCE_CUSTOM)

    def test_a_real_pool_name_is_recognised_for_the_country_being_moved_to(self):
        from services import career_change_service as ccs
        rolled = ccs.roll_pool_name(self.session, "Australia")
        result = self.submit(name=rolled, country="Australia",
                             source=ccs.SOURCE_POOL)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertTrue(result["applied"])

    def test_only_one_request_can_wait_at_a_time(self):
        from services import career_change_service as ccs
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        result = self.submit(name=unique_name("Second Try"), source=ccs.SOURCE_CUSTOM)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "pending")

    def test_a_name_taken_while_it_waited_is_not_approved(self):
        from models import Player
        from services import career_change_service as ccs

        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        self.session.add(Player(name=self.custom, version="Base", rating=90,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.commit()

        pending = ccs.pending_request(self.session, self.user.id)
        result = ccs.approve_request(self.session, pending, reviewer="admin")
        self.assertFalse(result["ok"])
        # Still pending, so the admin can reject it and refund rather than
        # silently losing the request.
        self.assertEqual(pending.status, ccs.STATUS_PENDING)

    def test_deleting_the_card_closes_and_refunds_the_waiting_request(self):
        """Support deleting a card must not leave its name in the queue."""
        from services import career_change_service as ccs
        from services.career_service import delete_career_player

        self.submit(country="Australia")
        before = self.user.total_gems
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        self.assertEqual(self.user.total_gems, before - 300)

        result = delete_career_player(self.session, self.player,
                                      refund_gems=False)
        self.session.commit()
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result["change_refunded"], 300)
        self.assertEqual(self.user.total_gems, before)
        # And nothing is left waiting to block the card they build next.
        self.assertIsNone(ccs.pending_request(self.session, self.user.id))

    def test_approving_a_request_whose_card_vanished_refunds_it(self):
        """The backstop for a card that went away some other way."""
        from services import career_change_service as ccs

        self.submit(country="Australia")
        before = self.user.total_gems
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        pending = ccs.pending_request(self.session, self.user.id)
        pending.player_id = 9_999_999          # a card that isn't there
        self.session.flush()

        result = ccs.approve_request(self.session, pending, reviewer="admin")
        self.session.commit()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_player")
        self.assertEqual(result["refunded"], 300)
        self.assertEqual(self.user.total_gems, before)
        self.assertEqual(pending.status, ccs.STATUS_REJECTED)


class NameRuleTests(ChangeTestCase):
    CONFIG = {"career_name_blocklist": "slur, badword"}

    def test_a_blocked_word_is_refused_however_it_is_spaced(self):
        from services import career_change_service as ccs
        for attempt in ("Slur Man", "S.L.U.R Kapoor", "Bad-Word Singh"):
            result = self.submit(name=attempt, source=ccs.SOURCE_CUSTOM)
            self.assertFalse(result["ok"], attempt)
            self.assertEqual(result["error"], "bad_name")
        self.assertEqual(self.player.career_changes_used, 0)

    def test_digits_and_emoji_are_refused(self):
        from services import career_change_service as ccs
        for attempt in ("Player 1", "Ravi 🏏", "___"):
            result = self.submit(name=attempt, source=ccs.SOURCE_CUSTOM)
            self.assertFalse(result["ok"], attempt)

    def test_a_name_another_player_carries_is_refused(self):
        from models import Player
        from services import career_change_service as ccs
        self.session.add(Player(name="Virat Kohli", version="Base", rating=90,
                                category="Batsman", country="India",
                                bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast"))
        self.session.commit()
        result = self.submit(name="virat kohli", source=ccs.SOURCE_CUSTOM)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_name")

    def test_a_country_outside_the_pool_is_refused(self):
        result = self.submit(country="Neverland")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_country")


class CustomNamesClosedTests(ChangeTestCase):
    CONFIG = {"career_custom_names_open": False}

    def test_typed_names_are_refused_while_pool_names_still_work(self):
        from services import career_change_service as ccs
        refused = self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "custom_closed")

        rolled = ccs.roll_pool_name(self.session, "India")
        allowed = self.submit(name=rolled, source=ccs.SOURCE_POOL)
        self.assertTrue(allowed["ok"], allowed.get("message"))


class ApprovalOffTests(ChangeTestCase):
    CONFIG = {"career_custom_names_need_approval": False}

    def test_a_typed_name_lands_immediately_when_review_is_off(self):
        from services import career_change_service as ccs
        result = self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        self.assertTrue(result["ok"], result.get("message"))
        self.assertTrue(result["applied"])
        self.assertEqual(self.player.name, self.custom)
        self.assertIsNone(ccs.pending_request(self.session, self.user.id))


class QuoteTests(ChangeTestCase):
    def test_the_quote_walks_the_ladder_with_the_card(self):
        from services.career_change_service import quote
        first = quote(self.session, self.user, self.player)
        self.assertTrue(first["is_free"])
        self.assertTrue(first["can_change"])
        self.assertEqual(first["next_index"], 1)

        self.submit(country="Australia")
        second = quote(self.session, self.user, self.player)
        self.assertEqual(second["cost"], 300)
        self.assertEqual(second["next_index"], 2)
        self.assertEqual([c for _, c in second["next_costs"]], [300, 500, 750])

    def test_a_waiting_request_blocks_the_next_change(self):
        from services import career_change_service as ccs
        from services.career_change_service import quote
        self.submit(name=self.custom, source=ccs.SOURCE_CUSTOM)
        blocked = quote(self.session, self.user, self.player)
        self.assertFalse(blocked["can_change"])
        self.assertEqual(blocked["blocked"], "pending")
        self.assertEqual(blocked["pending"]["new_name"], self.custom)


if __name__ == "__main__":
    unittest.main()
