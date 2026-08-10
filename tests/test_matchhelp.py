"""/matchhelp — the Approach-game guide.

Two things are worth pinning. The pages have to fit in a Telegram message, and
their numbers have to come from the engine rather than from someone's memory: a
guide that says the death overs start at 16 when the engine starts them at 17 is
worse than no guide, because a player will believe it.
"""

import asyncio
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

import handlers.matchhelp as mh

TELEGRAM_LIMIT = 4096


class PageTests(unittest.TestCase):
    def test_every_page_fits_in_one_telegram_message(self):
        for index, (key, _e, title, _b) in enumerate(mh.PAGES):
            length = len(mh._page_text(index))
            self.assertLess(length, TELEGRAM_LIMIT, f"{key} ({title}) is {length}")

    def test_every_page_renders_without_raising(self):
        for index in range(len(mh.PAGES)):
            self.assertTrue(mh._page_text(index).strip())

    def test_page_keys_are_unique(self):
        keys = [k for k, _e, _t, _b in mh.PAGES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_markup_is_balanced(self):
        """Telegram rejects the whole message on malformed HTML, so a stray tag
        loses the page rather than one word of it."""
        for index, (key, *_r) in enumerate(mh.PAGES):
            text = mh._page_text(index)
            for tag in ("b", "i"):
                self.assertEqual(text.count(f"<{tag}>"), text.count(f"</{tag}>"),
                                 f"{key}: unbalanced <{tag}>")

    def test_bare_ampersands_are_escaped(self):
        for index, (key, *_r) in enumerate(mh.PAGES):
            stripped = re.sub(r"&(amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);", "",
                              mh._page_text(index))
            self.assertNotIn("&", stripped, f"{key} has an unescaped &")


class GeneratedFromTheEngineTests(unittest.TestCase):
    """The whole point of generating these pages: they cannot drift."""

    def test_the_phase_page_uses_the_engine_windows(self):
        from services.cipl_match import DEATH_UNITS, FORMAT_SPECS
        body = mh._phase_block()
        pp = FORMAT_SPECS["T20"]["powerplay_units"]
        self.assertIn(f"overs 1-{pp}", body)
        self.assertIn(f"overs {21 - DEATH_UNITS}-20", body)

    def test_the_pitch_page_uses_the_configured_par_bands(self):
        from engine import ground_config
        body = mh._pitch_block()
        for pitch in ("Flat", "Dusty", "Green"):
            dyn = ground_config.get_scoring_dynamics(pitch)
            self.assertIn(f"par {dyn['par_low']}-{dyn['par_high']}", body, pitch)

    def test_the_pitch_page_names_every_pitch_and_its_second_innings(self):
        from engine import pitch_state
        body = mh._pitch_block()
        for pitch in ("Flat", "Hard", "Even", "Bouncy", "Dry", "Green", "Dusty"):
            self.assertIn(f"<b>{pitch}</b>", body)
            self.assertIn(pitch_state.evolution_note(pitch), body)

    def test_the_predictability_page_uses_the_real_threshold(self):
        from engine.approach_modifiers import REPEAT_FROM, REPEAT_CAP
        body = mh._predictability_block()
        self.assertIn(f"{REPEAT_FROM} overs running", body)
        self.assertIn(str(REPEAT_CAP), body)

    def test_every_approach_appears_with_its_own_label(self):
        from engine.approach_modifiers import (BATTING_APPROACHES,
                                               BOWLING_APPROACHES)
        bat, bowl = mh._batting_block(), mh._bowling_block()
        for _k, emoji, label in BATTING_APPROACHES:
            self.assertIn(label, bat)
            self.assertIn(emoji, bat)
        for _k, emoji, label in BOWLING_APPROACHES:
            self.assertIn(label, bowl)

    def test_the_momentum_page_uses_the_engine_caps(self):
        from engine.momentum import MOMENTUM_CAP
        self.assertIn(f"+{MOMENTUM_CAP:g}", mh._momentum_block())


class KeyboardTests(unittest.TestCase):
    def test_every_page_is_reachable_from_every_page(self):
        for index in range(len(mh.PAGES)):
            data = [b.callback_data
                    for row in mh._keyboard(index, 7).inline_keyboard for b in row]
            for key, *_r in mh.PAGES:
                self.assertIn(f"{mh.CB}_go_7_{key}", data, f"{key} unreachable")

    def test_the_ends_have_no_dead_navigation(self):
        first = [b.text for row in mh._keyboard(0, 7).inline_keyboard for b in row]
        last = [b.text for row in mh._keyboard(len(mh.PAGES) - 1, 7).inline_keyboard
                for b in row]
        self.assertFalse(any("Back" in t for t in first))
        self.assertFalse(any("Next" in t for t in last))

    def test_callback_data_stays_under_telegrams_64_byte_cap(self):
        for index in range(len(mh.PAGES)):
            for row in mh._keyboard(index, 10 ** 12).inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64,
                                         button.callback_data)

    def test_the_callback_round_trips_the_page_key(self):
        """The page keys must survive being split out of the callback data."""
        for key, *_r in mh.PAGES:
            data = f"{mh.CB}_go_7_{key}"
            _, _, owner, parsed = data.split("_", 3)
            self.assertEqual(owner, "7")
            self.assertEqual(parsed, key)


class CommandTests(unittest.TestCase):
    def _run(self, args):
        update, ctx = MagicMock(), MagicMock()
        update.effective_user.id = 7
        update.message.reply_text = AsyncMock()
        ctx.args = args
        asyncio.run(mh.matchhelp_handler(update, ctx))
        return update.message.reply_text.call_args

    def test_it_opens_on_the_first_page(self):
        text = self._run([])[0][0]
        self.assertIn(mh.PAGES[0][2], text)

    def test_a_topic_argument_jumps_straight_there(self):
        text = self._run(["pitches"])[0][0]
        self.assertIn("Pitches", text)

    def test_aliases_work(self):
        self.assertIn("Approach vs approach", self._run(["approaches"])[0][0])

    def test_an_unknown_topic_falls_back_to_the_start(self):
        self.assertIn(mh.PAGES[0][2], self._run(["nonsense"])[0][0])


class OwnershipTests(unittest.TestCase):
    def test_someone_elses_buttons_do_nothing(self):
        q = MagicMock()
        q.data = f"{mh.CB}_go_7_pitches"
        q.from_user.id = 99
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        update = MagicMock(callback_query=q)
        asyncio.run(mh.matchhelp_go_callback(update, MagicMock()))
        q.edit_message_text.assert_not_called()
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))


class MenuRegistrationTests(unittest.TestCase):
    """Telegram publishes at most 100 commands per scope, and the group list was
    already over it — three commands were being truncated off the tail with only
    a log line to say so. Adding one more must not put it back over."""

    @staticmethod
    def _scopes():
        import ast
        import re as _re
        src = open("bot.py", encoding="utf-8").read()

        def frozen(name):
            m = _re.search(name + r" = frozenset\(\{(.*?)\}\)", src, _re.S)
            return set(_re.findall(r'"([a-z0-9_]+)"', m.group(1))) if m else set()

        menu = None
        for node in ast.parse(src).body:
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", None) == "BOT_MENU_COMMANDS"):
                menu = ast.literal_eval(node.value)
        names = [c for c, _d in menu]
        # DM_ONLY_COMMANDS is imported from services.dm_only rather than
        # written out in bot.py, so read it from the service the way
        # bot._menu_for_scope does — matching on bot.py's text finds nothing
        # and would count DM-only commands as published in groups.
        from services.dm_only import DM_ONLY_COMMANDS
        hidden = frozen("PRIVATE_ONLY_COMMANDS") | set(DM_ONLY_COMMANDS)
        return ([c for c in names if c not in hidden],
                [c for c in names if c not in frozen("GROUP_ONLY_COMMANDS")])

    def test_neither_menu_overflows(self):
        group, private = self._scopes()
        self.assertLessEqual(len(group), 100, f"group menu is {len(group)}")
        self.assertLessEqual(len(private), 100, f"private menu is {len(private)}")

    def test_matchhelp_is_advertised_where_matches_are_played(self):
        group, _private = self._scopes()
        self.assertIn("matchhelp", group)


if __name__ == "__main__":
    unittest.main()
