import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, User
from telegram.error import Forbidden

from handlers import wordchase as wc
from services.community_answer_service import key


class WordBankTests(unittest.TestCase):
    def test_both_pools_are_populated_and_answers_are_unique(self):
        self.assertIn("Smriti Mandhana", wc._WORD_BANK["cricket"])
        self.assertIn("Team Chemistry", wc._WORD_BANK["bot"])
        answers = [key(answer) for words in wc._WORD_BANK.values() for answer in words]
        self.assertNotIn("", answers)
        self.assertEqual(len(answers), len(set(answers)))

    def test_deck_uses_each_answer_once_before_reshuffling(self):
        answers = ["Yorker", "Cover Drive", "Googly"]
        game = {"decks": {"cricket": []}}
        with patch.dict(wc._WORD_BANK, cricket=answers):
            drawn = [wc._draw_word(game)[1] for _ in answers]
            self.assertCountEqual(drawn, answers)
            self.assertNotEqual(wc._draw_word(game)[1], drawn[-1])


class WordChaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(wc.GAMES, clear=True))
        self.enterContext(patch.dict(wc._WORD_BANK, {
            "cricket": ["MS Dhoni", "Hat-trick", "Googly"],
            "bot": ["Team Chemistry", "Lucky 7"],
        }))
        self.enterContext(patch.object(wc.random, "shuffle"))
        self.chat = Chat(-100123, "supergroup", title="Cricket <Club>")
        self.host = User(1, "Host", False)
        self.guesser = User(2, "A & B", False)
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), args=[])

    def update(self, user, text=None):
        return SimpleNamespace(
            effective_chat=self.chat, effective_user=user,
            message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        )

    async def start(self, mode="cricket"):
        self.context.args = [mode] if mode else []
        update = self.update(self.host)
        await wc.wordchase_handler(update, self.context)
        return update

    def callback(self, user, data):
        update = self.update(user)
        update.callback_query = SimpleNamespace(
            from_user=user, data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        return update

    @staticmethod
    def end_button(update):
        args = update.message.reply_text.await_args
        return args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    async def test_modes_and_round_hint_keep_the_answer_private(self):
        update = await self.start("bot")
        game = wc.GAMES[self.chat.id]
        self.assertEqual(set(game["decks"]), {"bot"})
        public = update.message.reply_text.await_args.args[0]
        private = self.context.bot.send_message.await_args.args[1]
        self.assertIn("CCM26 Bot", public)
        self.assertIn("5 · 1", public)
        self.assertNotIn("Lucky 7", public)
        self.assertIn("Lucky 7", private)
        self.assertIn("Cricket &lt;Club&gt;", private)

        await wc.wordchase_end_handler(self.update(self.host), self.context)
        await self.start("")
        self.assertEqual(set(wc.GAMES[self.chat.id]["decks"]), {"cricket", "bot"})

    async def test_one_correct_guess_scores_while_the_next_dm_is_pending(self):
        await self.start()
        first = self.update(self.guesser, "googly!")
        other_user = User(3, "Other guesser", False)
        second = self.update(other_user, "Googly")
        future = self.update(other_user, "Hat trick")
        dm_started = asyncio.Event()
        release_dm = asyncio.Event()

        async def send_dm(*args, **kwargs):
            dm_started.set()
            await release_dm.wait()

        self.context.bot.send_message.side_effect = send_dm
        turn = asyncio.create_task(wc.wordchase_message_handler(first, self.context))
        try:
            await asyncio.wait_for(dm_started.wait(), timeout=1)
            await wc.wordchase_message_handler(second, self.context)
            await wc.wordchase_message_handler(future, self.context)
        finally:
            release_dm.set()
            await turn

        game = wc.GAMES[self.chat.id]
        self.assertEqual(game["scores"], {self.guesser.id: ("A &amp; B", 1)})
        self.assertEqual(game["host_id"], self.guesser.id)
        self.assertEqual(game["word"], "Hat-trick")
        self.assertTrue(game["guessing_enabled"])
        second.message.reply_text.assert_not_awaited()
        future.message.reply_text.assert_not_awaited()

    async def test_dm_failure_keeps_the_host_and_invalidates_old_end_button(self):
        start = await self.start()
        guess = self.update(self.guesser, "gOOgly.")
        self.context.bot.send_message.side_effect = [Forbidden("Start needed"), None]
        await wc.wordchase_message_handler(guess, self.context)

        game = wc.GAMES[self.chat.id]
        self.assertEqual(game["host_id"], self.host.id)
        self.assertEqual(game["round"], 2)
        self.assertEqual(game["word"], "MS Dhoni")
        self.assertIn("tap Start", guess.message.reply_text.await_args.args[0])

        stale = self.callback(self.host, self.end_button(start))
        await wc.wordchase_end_callback(stale, self.context)
        stale.callback_query.edit_message_text.assert_not_awaited()
        self.assertIn(self.chat.id, wc.GAMES)

        current_data = self.end_button(guess)
        wrong_user = self.callback(self.guesser, current_data)
        await wc.wordchase_end_callback(wrong_user, self.context)
        wrong_user.callback_query.edit_message_text.assert_not_awaited()
        self.assertIn(self.chat.id, wc.GAMES)

        current = self.callback(self.host, current_data)
        await wc.wordchase_end_callback(current, self.context)
        end = current.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("A &amp; B · <b>1</b>", end)
        self.assertNotIn(self.chat.id, wc.GAMES)

    async def test_initial_dm_failure_clears_the_game(self):
        self.context.bot.send_message.side_effect = Forbidden("Start needed")
        update = await self.start()
        self.assertIn("tap Start", update.message.reply_text.await_args.args[0])
        self.assertNotIn(self.chat.id, wc.GAMES)
