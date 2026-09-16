"""Who may press the Impact Player buttons.

Two different rules on two different messages, and getting either backwards is
a live-match bug rather than a cosmetic one:

  * The 🔄 button sits on the over summary / innings-break card. That message is
    sent while handling ONE captain's approach tap, so services/button_access
    registers it to that captain — and the OTHER captain would be locked out of
    their own substitution. It must be a shared prefix, with the handler
    checking the clicker.
  * The picker it opens is personal: it lists one captain's squad and spends
    their one irreversible swap. The other captain must not drive it, including
    after a restart has emptied the owner registry — which is why those buttons
    carry the owner id in the callback data instead of relying on it.
"""

import unittest
from types import SimpleNamespace

from services import button_access as ba
import handlers.cipl_play as cp

BAT_TG, BOWL_TG, STRANGER_TG = 11, 22, 999
CHAT, MSG = -100, 5


def _click(data, uid, chat=CHAT, msg=MSG):
    return SimpleNamespace(callback_query=SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid),
        message=SimpleNamespace(chat_id=chat, message_id=msg,
                                chat=SimpleNamespace(id=chat))))


class SharedEntryButtonTests(unittest.TestCase):
    """The over summary belongs to one captain; the 🔄 button on it does not."""

    def setUp(self):
        # The summary was posted while handling the batting captain's tap.
        ba.register_button_owner(CHAT, MSG, BAT_TG)

    def test_the_other_captain_is_not_locked_out_of_their_own_swap(self):
        self.assertTrue(
            ba.check_callback_owner(_click("cipl_imp_7", BOWL_TG)),
            "the bowling captain must be able to open their own picker from "
            "a summary registered to the batting captain")

    def test_the_posting_captain_can_still_use_it(self):
        self.assertTrue(ba.check_callback_owner(_click("cipl_imp_7", BAT_TG)))

    def test_it_is_declared_shared_alongside_the_other_cipl_prompts(self):
        self.assertTrue(ba.is_shared_callback_data("cipl_imp_7"))

    def test_a_stranger_reaches_the_handler_and_is_refused_there(self):
        # The guard lets them through (shared prefix); _impact_guard is what
        # actually turns them away, so the refusal is per-match, not per-message.
        self.assertTrue(ba.check_callback_owner(_click("cipl_imp_7", STRANGER_TG)))


class PickerIsPersonalTests(unittest.TestCase):
    def test_the_picker_prefixes_are_not_shared(self):
        for prefix in ("cipl_impo_", "cipl_impi_", "cipl_impp_", "cipl_impx_"):
            self.assertFalse(
                ba.is_shared_callback_data(prefix + "u11_7_9"),
                f"{prefix} must stay personal")

    def test_the_shared_entry_prefix_does_not_swallow_the_picker_prefixes(self):
        # "cipl_impo_" must not match a "cipl_imp_" startswith test.
        self.assertFalse("cipl_impo_u11_7_9".startswith("cipl_imp_"))

    def test_the_other_captain_is_blocked_even_with_an_empty_registry(self):
        # A restart empties the registry; the tag has to carry the lock alone.
        data = cp._imp_cb("cipl_impo_", BAT_TG, 7, 9)
        click = _click(data, BOWL_TG, chat=-777, msg=1)   # never registered
        self.assertIsNone(ba.get_registered_owner(-777, 1))
        self.assertFalse(ba.check_callback_owner(click))

    def test_the_owner_can_drive_their_own_picker(self):
        data = cp._imp_cb("cipl_impo_", BAT_TG, 7, 9)
        self.assertTrue(ba.check_callback_owner(_click(data, BAT_TG, -777, 1)))

    def test_being_turned_away_explains_how_to_get_your_own(self):
        data = cp._imp_cb("cipl_impp_", BAT_TG, 7, 9, 50, 4)
        msg = ba.blocked_message_for(data)
        self.assertNotEqual(msg, ba.BLOCKED_BUTTON_MESSAGE)
        self.assertIn("/impact", msg)


class CallbackDataTests(unittest.TestCase):
    def test_every_step_round_trips(self):
        cases = [
            ("cipl_impx_", (7,)),
            ("cipl_impo_", (7, 9)),
            ("cipl_impi_", (7, 9, 50)),
            ("cipl_impp_", (7, 9, 50, 4)),
        ]
        for prefix, parts in cases:
            data = cp._imp_cb(prefix, BAT_TG, *parts)
            self.assertEqual(cp._imp_parse(prefix, data, len(parts)),
                             list(parts), data)

    def test_untagged_data_from_before_this_shipped_still_parses(self):
        # A button already on screen during a deploy must not read as garbage.
        self.assertEqual(cp._imp_parse("cipl_impo_", "cipl_impo_7_9", 2), [7, 9])

    def test_malformed_data_is_rejected_rather_than_guessed(self):
        for bad in ("", "cipl_impo_", "cipl_impo_u11_7", "cipl_impo_u11_7_x",
                    "cipl_impo_u11_7_9_9", None):
            self.assertIsNone(cp._imp_parse("cipl_impo_", bad, 2), bad)

    def test_the_longest_callback_data_fits_telegrams_64_byte_limit(self):
        # A 16-digit owner id and 7-digit ids is the worst realistic case.
        data = cp._imp_cb("cipl_impp_", 1234567890123456,
                          9999999, 9999999, 9999999, 10)
        self.assertLessEqual(len(data.encode()), 64, data)


if __name__ == "__main__":
    unittest.main()
