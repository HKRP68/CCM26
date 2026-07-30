"""A chat must never stay stuck on "a Lets Play match is already in progress".

The /letsplay guard is driven entirely by ``bot_data``: an invite/setup draft
(``lp_*``) or a live board (``ms_*``). Both can be orphaned — a timeout job that
never fired, a match that finished without its board being torn down — and when
that happened the chat was blocked with no way out, because /clearmatches only
ever looked at Match rows and ``ms_*`` state.

These tests pin the two escapes: the guard reaps what it finds dead, and
/clearmatches clears Lets Play drafts too.
"""

import unittest
from datetime import datetime, timedelta

from handlers import letsplay


class _Ctx:
    """Minimal PTB context: a real bot_data dict and no job queue.

    No job queue is the interesting case — it is exactly the situation where a
    draft's timeout can never fire, so only the deadline stamp saves the chat.
    """

    def __init__(self, bot_data=None):
        self.bot_data = bot_data if bot_data is not None else {}
        self.job_queue = None


class _Row:
    def __init__(self, status):
        self.status = status


class _Session:
    """Stands in for a DB session that holds exactly one Match row."""

    def __init__(self, row):
        self._row = row

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


def _draft(chat_id=-100, status="pitch", age_seconds=0, invite_id=1):
    created = datetime.utcnow() - timedelta(seconds=age_seconds)
    return {
        "invite_id": invite_id, "chat_id": chat_id, "status": status,
        "created_at": created.isoformat(),
    }


class StaleDraftTests(unittest.TestCase):
    def test_fresh_draft_blocks_the_chat(self):
        ctx = _Ctx({"lp_1": _draft()})
        self.assertTrue(letsplay._active_letsplay_in_chat(ctx, -100))
        self.assertIn("lp_1", ctx.bot_data)

    def test_draft_past_its_deadline_is_reaped_not_honoured(self):
        stale = _draft()
        stale["expires_at"] = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        ctx = _Ctx({"lp_1": stale})
        self.assertFalse(letsplay._active_letsplay_in_chat(ctx, -100))
        self.assertNotIn("lp_1", ctx.bot_data)

    def test_deadline_is_stamped_even_without_a_job_queue(self):
        ctx = _Ctx({"lp_1": _draft()})
        letsplay._rearm_setup_timeout(ctx, 1)
        self.assertIn("expires_at", ctx.bot_data["lp_1"])
        self.assertFalse(letsplay._draft_is_stale(ctx.bot_data["lp_1"]))

    def test_draft_with_no_deadline_falls_back_to_creation_time(self):
        old = _draft(age_seconds=letsplay.SETUP_TIMEOUT + letsplay.STALE_GRACE + 60)
        self.assertTrue(letsplay._draft_is_stale(old))
        self.assertFalse(letsplay._draft_is_stale(_draft(age_seconds=5)))

    def test_another_chats_stale_draft_is_left_alone(self):
        stale = _draft(chat_id=-200)
        stale["expires_at"] = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        ctx = _Ctx({"lp_1": stale})
        self.assertFalse(letsplay._active_letsplay_in_chat(ctx, -100))
        self.assertIn("lp_1", ctx.bot_data)

    def test_launched_draft_is_never_treated_as_stale(self):
        d = _draft(age_seconds=99999)
        d["match_launched"] = True
        self.assertFalse(letsplay._draft_is_stale(d))


class StrandedBoardTests(unittest.TestCase):
    def setUp(self):
        self.cleaned = []
        self._real_cleanup = letsplay.cleanup_state
        letsplay.cleanup_state = lambda ctx, mid: self.cleaned.append(mid)

    def tearDown(self):
        letsplay.cleanup_state = self._real_cleanup

    def _state(self):
        return {"chat_id": -100, "is_letsplay": True, "mode": "cipl_approach",
                "match_id": 7}

    def test_live_board_blocks_the_chat(self):
        ctx = _Ctx({"ms_7": self._state()})
        session = _Session(_Row("active"))
        self.assertTrue(letsplay._active_letsplay_in_chat(ctx, -100, session))
        self.assertEqual(self.cleaned, [])

    def test_board_whose_match_already_finished_is_torn_down(self):
        ctx = _Ctx({"ms_7": self._state()})
        session = _Session(_Row("completed"))
        self.assertFalse(letsplay._active_letsplay_in_chat(ctx, -100, session))
        self.assertEqual(self.cleaned, [7])

    def test_without_a_session_the_board_still_blocks(self):
        # No session means no way to check — stay conservative rather than
        # letting a second match start on top of a live one.
        ctx = _Ctx({"ms_7": self._state()})
        self.assertTrue(letsplay._active_letsplay_in_chat(ctx, -100))


class ClearMatchesTests(unittest.TestCase):
    def test_clear_helper_drops_only_this_chats_drafts(self):
        ctx = _Ctx({"lp_1": _draft(chat_id=-100, invite_id=1),
                    "lp_2": _draft(chat_id=-200, invite_id=2),
                    "_lp_seq": 2})
        self.assertEqual(letsplay.clear_letsplay_drafts_in_chat(ctx, -100), 1)
        self.assertNotIn("lp_1", ctx.bot_data)
        self.assertIn("lp_2", ctx.bot_data)
        self.assertEqual(ctx.bot_data["_lp_seq"], 2)

    def test_clear_helper_drops_drafts_in_any_setup_stage(self):
        ctx = _Ctx({f"lp_{i}": _draft(status=s, invite_id=i)
                    for i, s in enumerate(("pending", "pitch", "showxi", "toss"))})
        self.assertEqual(letsplay.clear_letsplay_drafts_in_chat(ctx, -100), 4)
        self.assertEqual([k for k in ctx.bot_data if k.startswith("lp_")], [])

    def test_chat_memory_teardown_clears_letsplay_drafts(self):
        from handlers import match as match_handlers
        ctx = _Ctx({"lp_1": _draft()})
        match_handlers._clear_chat_memory(ctx, -100)
        self.assertNotIn("lp_1", ctx.bot_data)

    def test_cleared_chat_can_start_a_new_letsplay(self):
        ctx = _Ctx({"lp_1": _draft()})
        self.assertTrue(letsplay._active_letsplay_in_chat(ctx, -100))
        letsplay.clear_letsplay_drafts_in_chat(ctx, -100)
        self.assertFalse(letsplay._active_letsplay_in_chat(ctx, -100))


if __name__ == "__main__":
    unittest.main()
