"""/impact, and where the Impact Player button lives.

The regression this file exists for: `/impact` never worked. `impact_handler`
bound the ``(match_id, state)`` tuple returned by `_find_cipl_match_in_chat` to
a single name, so the truthiness check passed and the code then looked up a
match id that cannot exist — every `/impact`, in every chat, answered "No live
over-by-over match in this chat". The callbacks were tested; the command was
not, which is exactly how it shipped.

Also pinned here:
  * the button rides on the bowler prompt and both approach prompts, beside
    View Match, so a captain never has to remember the command;
  * whoever taps the shared button gets their OWN side's picker;
  * `/impact` is usable through the whole approach-selection sequence, which is
    the window captains actually care about.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import handlers.cipl_play as cp
from services import cipl_match
from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH, A_COMPLETED,
)

BAT_TG, BOWL_TG, STRANGER_TG = 11, 22, 999
CHAT = -100


def _p(rid, name, category="Batsman", bat=70, bowl=40):
    return {"roster_id": rid, "name": name, "category": category,
            "rating": max(bat, bowl), "bat_rating": bat, "bowl_rating": bowl}


def _state():
    state = cipl_match.build_cipl_state(
        match_id=7, overs=20, bat_user_id=1, bowl_user_id=2,
        bat_user_tg=BAT_TG, bowl_user_tg=BOWL_TG,
        bat_xi=[_p(i, f"B{i}", bat=80 - i) for i in range(1, 12)],
        bowl_xi=[_p(100 + i, f"W{i}", "Bowler", bowl=80 - i) for i in range(1, 12)],
        bat_team_name="Alpha", bowl_team_name="Beta", chat_id=CHAT,
        bat_bench=[_p(50, "SuperSub", bat=95)],
        bowl_bench=[_p(150, "BenchPace", "Bowler", bowl=90)])
    state.update({"current_over": 8, "striker_idx": 2,
                  "non_striker_idx": 3, "next_batsman_idx": 4})
    state["bat_stats"] = {str(i): {"runs": 0, "balls": 0, "out": i <= 2}
                          for i in range(1, 12)}
    return state


class _Recorder:
    """Captures what the handler said, and any keyboard it said it with."""

    def __init__(self):
        self.events = []

    def bot(self):
        rec = self

        class _Bot:
            async def send_message(self, chat_id, text, **kw):
                rec.events.append(("send", text, kw.get("reply_markup")))
                return SimpleNamespace(message_id=len(rec.events))

            async def edit_message_text(self, *a, **kw):
                return None

            async def delete_message(self, *a, **kw):
                return None
        return _Bot()

    def message(self):
        rec = self

        class _Msg:
            async def reply_text(self, text, **kw):
                rec.events.append(("reply", text, kw.get("reply_markup")))
        return _Msg()

    @property
    def last_text(self):
        return self.events[-1][1] if self.events else ""

    @property
    def last_kind(self):
        return self.events[-1][0] if self.events else None


def _run_impact(state, tg_id, next_action=A_PICK_CIPL_BOWLER, found=True):
    rec = _Recorder()
    ctx = SimpleNamespace(bot=rec.bot(), bot_data={}, job_queue=None)
    update = SimpleNamespace(
        effective_message=rec.message(),
        effective_chat=SimpleNamespace(id=CHAT),
        effective_user=SimpleNamespace(id=tg_id))

    async def _gs(c, m):
        return state

    async def _ss(c, m, s, next_action=None, last_prompt_msg_id=None):
        return None

    async def _na(c, m):
        return next_action

    async def _find(c, cid):
        return (7, state) if found else (None, None)

    with patch.object(cp, "_gs", _gs), patch.object(cp, "_ss", _ss), \
         patch.object(cp, "_get_next_action", _na), \
         patch.object(cp, "_find_cipl_match_in_chat", _find):
        asyncio.run(cp.impact_handler(update, ctx))
    return rec


class ImpactCommandTests(unittest.TestCase):
    def test_it_opens_the_picker_instead_of_claiming_there_is_no_match(self):
        # THE regression. Before the fix this said "No live over-by-over match".
        rec = _run_impact(_state(), BAT_TG)
        self.assertEqual(rec.last_kind, "send")
        self.assertIn("Impact Player", rec.last_text)
        self.assertIn("Step 1 of 3", rec.last_text)

    def test_it_opens_the_picker_at_every_approach_step(self):
        for action in (A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH,
                       A_PICK_BAT_APPROACH):
            rec = _run_impact(_state(), BAT_TG, next_action=action)
            self.assertIn("Step 1 of 3", rec.last_text,
                          f"/impact should work at {action}")

    def test_the_picker_lists_that_captains_own_players(self):
        rec = _run_impact(_state(), BAT_TG)
        labels = [b.text for row in rec.events[-1][2].inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("B") for l in labels), labels)
        self.assertFalse(any(l.startswith("W") for l in labels), labels)

    def test_the_bowling_captain_gets_the_bowling_side(self):
        rec = _run_impact(_state(), BOWL_TG)
        labels = [b.text for row in rec.events[-1][2].inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("W") for l in labels), labels)
        self.assertFalse(any(l.startswith("B") for l in labels), labels)

    def test_a_non_captain_is_turned_away(self):
        rec = _run_impact(_state(), STRANGER_TG)
        self.assertEqual(rec.last_kind, "reply")
        self.assertIn("captains", rec.last_text)

    def test_no_match_in_the_chat_says_so(self):
        rec = _run_impact(_state(), BAT_TG, found=False)
        self.assertEqual(rec.last_kind, "reply")
        self.assertIn("No live over-by-over match", rec.last_text)

    def test_a_side_that_already_swapped_is_told_so(self):
        from services import impact_player
        state = _state()
        impact_player.cipl_use(state, 1, 50, 9, A_PICK_CIPL_BOWLER)
        rec = _run_impact(state, BAT_TG)
        self.assertEqual(rec.last_kind, "reply")
        self.assertIn("already used", rec.last_text.lower())


class UnavailableTextTests(unittest.TestCase):
    def test_mid_over_says_what_to_wait_for(self):
        opts = {"used": False, "legal_break": None, "message": "generic"}
        text = cp._impact_unavailable_text(opts, "SOMETHING_ELSE")
        self.assertIn("over is being bowled", text)

    def test_a_finished_match_says_so(self):
        opts = {"used": False, "legal_break": None, "message": "generic"}
        self.assertIn("over", cp._impact_unavailable_text(opts, A_COMPLETED).lower())

    def test_a_concrete_reason_is_passed_through(self):
        opts = {"used": True, "legal_break": None,
                "message": "Impact Player already used."}
        self.assertEqual(cp._impact_unavailable_text(opts, A_PICK_CIPL_BOWLER),
                         "Impact Player already used.")


class ButtonPlacementTests(unittest.TestCase):
    """The button must ride beside View Match on every prompt, not only the
    over summary — that message is deleted the moment the next over starts."""

    def _labels(self, rows):
        return [b.text for row in (rows or []) for b in row]

    def test_it_sits_on_the_same_row_as_view_match(self):
        # _miniapp_row needs a configured Mini App host, which the test env has
        # no reason to carry — stand one in so there is a row to share.
        from telegram import InlineKeyboardButton
        fake = [[InlineKeyboardButton("📊 View Match", callback_data="noop")]]
        with patch.object(cp, "_miniapp_row", lambda s: fake):
            rows = cp._view_and_impact_rows(_state(), 7)
        row_with_impact = next(r for r in rows
                               if any("Impact" in b.text for b in r))
        self.assertTrue(any("View Match" in b.text for b in row_with_impact),
                        "Impact Player should share the View Match row")
        self.assertEqual(len(rows), 1, "it should not add a second row")

    def test_every_prompt_carries_it(self):
        # _with_view_match is what _new_action_message and _edit_action_message
        # run for the bowler prompt and both approach prompts.
        labels = self._labels(cp._with_view_match(_state(), [])) 
        self.assertIn("🔄 Impact Player", labels)

    def test_the_over_summary_carries_it(self):
        labels = self._labels(cp._between_overs_row(_state(), 7))
        self.assertIn("🔄 Impact Player", labels)

    def test_it_disappears_once_both_sides_have_used_theirs(self):
        state = _state()
        state["impact_players"] = {"usage": {"1": {"used": True},
                                             "2": {"used": True}}}
        self.assertIsNone(cp._impact_button(state, 7))

    def test_it_stays_while_one_side_still_has_theirs(self):
        state = _state()
        state["impact_players"] = {"usage": {"1": {"used": True},
                                             "2": {"used": False}}}
        self.assertIsNotNone(cp._impact_button(state, 7))

    def test_it_survives_a_missing_mini_app_row(self):
        # No Mini App host configured — the button must still appear, on a row
        # of its own, rather than vanishing with the View Match row.
        with patch.object(cp, "_miniapp_row", lambda s: None):
            labels = self._labels(cp._view_and_impact_rows(_state(), 7))
        self.assertEqual(labels, ["🔄 Impact Player"])


if __name__ == "__main__":
    unittest.main()
