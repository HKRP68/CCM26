"""The /rank, /ranked, /rivalry and /predict cards render (pure, no DB)."""

import unittest
from types import SimpleNamespace as NS


def _user(uid=1, name="Asha"):
    return NS(id=uid, first_name=name, team_name=None, username=None)


class UnknownTargetTests(unittest.TestCase):
    def test_named_unknown_player_is_reported_not_swapped_for_the_caller(self):
        import asyncio
        from unittest import mock
        import handlers.ranked as hr

        class _S:
            def query(self, *_a): return self
            def filter(self, *_a): return self
            def first(self): return NS(id=1, first_name="Me")
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass

        for handler in (hr.rank_handler, hr.rivalry_handler):
            msg = NS(reply_text=mock.AsyncMock(), reply_to_message=None)
            update = NS(message=msg, effective_user=NS(id=5))
            with mock.patch.object(hr, "get_session", return_value=_S()), \
                    mock.patch.object(hr, "resolve_command_target",
                                      return_value=(None, "not_found")):
                asyncio.run(handler(update, NS(args=["@ghost"])))
            self.assertIn("couldn't find", msg.reply_text.call_args.args[0])


class RankCardTests(unittest.TestCase):
    def test_unplayed(self):
        from handlers.ranked import render_rank_card
        text = render_rank_card(_user(), None, None)
        self.assertIn("Bronze", text)
        self.assertIn("No ranked matches", text)

    def test_played(self):
        from handlers.ranked import render_rank_card
        row = NS(rating=1180, played=4, wins=3, losses=1, draws=0,
                 peak_rating=1190, career_peak=1250)
        text = render_rank_card(_user(name="<b>x</b>"), row, 7)
        self.assertIn("Gold", text)
        self.assertIn("#7", text)
        self.assertIn("Placement", text)
        self.assertIn("&lt;b&gt;", text)

    def test_ladder(self):
        from handlers.ranked import render_ladder
        row = NS(rating=1460, wins=10, losses=2)
        text = render_ladder([(1, row, _user())], "2026-09", "📍 You: #40 · 1010")
        self.assertIn("🥇", text)
        self.assertIn("👑", text)
        self.assertIn("#40", text)


class RivalryCardTests(unittest.TestCase):
    def test_from_either_side(self):
        from handlers.ranked import render_rivalry
        row = NS(user_a_id=1, user_b_id=2, name="A vs B Derby", played=7,
                 a_wins=5, b_wins=2, ties=0, rounds_a=1, rounds_b=0, round_no=2,
                 round_played=2, round_a_wins=0, round_b_wins=2,
                 streak=2, streak_user_id=2)
        a, b = _user(1, "Asha"), _user(2, "Bala")
        self.assertIn("Asha <b>5</b> – <b>2</b> Bala", render_rivalry(row, a, b))
        self.assertIn("Bala <b>2</b> – <b>5</b> Asha", render_rivalry(row, b, a))


class PredictCardTests(unittest.TestCase):
    def test_card_and_keyboard(self):
        from handlers.predict import _keyboard, render_card
        m = NS(id=42)
        sides = [(1, "Kings"), (2, "Royals")]
        text = render_card(m, sides, {1: {"stake": 300, "count": 2}}, True)
        self.assertIn("Kings", text)
        self.assertIn("300", text)
        self.assertIn("100% of pool", text)
        kb = _keyboard(42, sides, True)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("pred_p_42_1_100", data)
        self.assertIn("pred_r_42", data)
        closed = _keyboard(42, sides, False)
        self.assertEqual(len(closed.inline_keyboard), 1)

    def test_pred_buttons_are_shared(self):
        from services.button_access import SHARED_CALLBACK_PREFIXES
        self.assertIn("pred_", SHARED_CALLBACK_PREFIXES)


if __name__ == "__main__":
    unittest.main()
