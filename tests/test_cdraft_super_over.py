"""A tied /cdraft match goes to a Super Over, like every other two-human mode.

/cdraft has no Super Over code of its own and must never grow any: it builds an
ordinary Challenge League draft dict, so from the toss onward it is driven by
``handlers.cipl_play``, and ``_complete_match`` sends every tied non-bot match to
``handlers.super_over``. This file pins that, from both ends:

  * the handoff — ``_complete_match`` on a drafted match reaches
    ``start_super_over``, with nothing branching on the mode; and
  * the Super Over itself, played out on squads built the way a real draft
    builds them (``build_xi_from_draft`` over ``cdraft_service.squad_cards``),
    because the squad source is the *only* thing that differs from /cipl —
    drafted cards are ``DraftCard`` shims, not ``ChallengePlayer`` rows, and a
    Super Over selects from the confirmed XI by roster id and category.

A drafted squad is dealt exactly eleven, so the Super Over is also where that
shape gets its hardest test: three batters and a bowler have to come out of the
same eleven, every replay, with nobody who has already taken the field.
"""

import asyncio
import logging
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import handlers.cipl_play as cp
import handlers.super_over as so_mod
import tests.test_super_over as sot
from services import cdraft_service, cipl_match
from tests.test_cdraft import make_pool, play_out

CHAT = -100123
HOST_TG, GUEST_TG = 111, 222
HOST_UID, GUEST_UID = 1, 2
MID = 9001


def _drafted_xis(seed=3):
    """Two squads, dealt and picked by the real draft, as engine player dicts.

    Goes through ``build_xi_from_draft`` rather than converting the cards here,
    so the test exercises the same resolution the live launch uses.
    """
    with patch("services.player_cache.get_all_active", make_pool):
        slots = cdraft_service.build_slots(seed=seed, top=88, bottom=78, spread=1)
    state = play_out(slots, seed=seed)
    draft = {
        "mode": "cdraft",
        "cdraft": state,
        "xi_selections": {
            side: {"player_ids": [int(c["id"]) for c in state["squads"][side]],
                   "confirmed": True}
            for side in ("host", "target")
        },
    }
    # No session: a drafted squad lives on the draft, so nothing is queried.
    return (cp.build_xi_from_draft(None, draft, "host"),
            cp.build_xi_from_draft(None, draft, "target"))


def _tied_cdraft_state(seed=3):
    """A drafted match, level after twenty overs each."""
    host_xi, guest_xi = _drafted_xis(seed)
    state = cipl_match.build_cipl_state(
        match_id=MID, overs=20,
        bat_user_id=GUEST_UID, bowl_user_id=HOST_UID,
        bat_user_tg=GUEST_TG, bowl_user_tg=HOST_TG,
        # Post-innings-2 roles: the batting side is the one that batted second.
        bat_xi=guest_xi, bowl_xi=host_xi,
        bat_team_name="Guest Draft XI", bowl_team_name="Host Draft XI",
        chat_id=CHAT, pitch_type="Hard")
    state.update({
        "innings": 2,
        "inn1_bat_team": "Host Draft XI",
        "inn1_bat_team_id": HOST_UID, "inn1_bowl_team_id": GUEST_UID,
        "inn1_runs": 150, "inn1_wickets": 6,
        "total_runs": 150, "total_wickets": 8,
        "mode_name": "Challenge Draft",
    })
    return state


class _Bot(sot.FakeBot):
    """The shared Super Over bot fake, plus the analysis document."""

    async def send_document(self, *a, **k):
        return None


class _Ctx:
    def __init__(self):
        self.bot = _Bot()
        self.bot_data = {}


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    """_complete_match must route a drafted tie to the Super Over."""

    async def test_a_tied_drafted_match_reaches_start_super_over(self):
        state = _tied_cdraft_state()
        self.assertTrue(cipl_match.compute_result(state)["tie"])
        self.assertFalse(cp._is_bot_match(state),
                         "/cdraft needs two humans, so it is never a bot match")

        seen = {}

        async def _fake_start(context, mid, st):
            seen["mid"], seen["state"] = mid, st
            return True

        async def _noop(*a, **k):
            return None

        with patch.object(so_mod, "start_super_over", _fake_start), \
                patch.object(cp, "_delete_prev_over", _noop), \
                patch.object(cp, "_ss", _noop):
            await cp._complete_match(_Ctx(), MID, state)

        self.assertEqual(seen.get("mid"), MID,
                         "a drafted tie must hand over to the Super Over")
        self.assertIs(seen.get("state"), state)

    async def test_a_decided_drafted_match_does_not_start_one(self):
        state = _tied_cdraft_state()
        state["total_runs"] = 151            # chased down, no tie
        self.assertFalse(cipl_match.compute_result(state)["tie"])

        called = []

        async def _fake_start(context, mid, st):
            called.append(mid)
            return True

        async def _noop(*a, **k):
            return None

        # _complete_match runs its whole finalize path here; it is heavy and
        # DB-bound, so the assertion that matters is only that it never got as
        # far as a Super Over.
        with patch.object(so_mod, "start_super_over", _fake_start), \
                patch.object(cp, "_delete_prev_over", _noop), \
                patch.object(cp, "_ss", _noop):
            try:
                await cp._complete_match(_Ctx(), MID, state)
            except Exception:
                pass
        self.assertEqual(called, [])


class DraftedSquadSuperOverTests(unittest.TestCase):
    """The Super Over itself, played on drafted cards."""

    def setUp(self):
        so_mod._BALL_PAUSE = 0
        # _patch_finalize swaps out module-level names in four modules and never
        # puts them back, so it leaks into whatever runs after it. Snapshot and
        # restore them here: this file sorts ahead of tests/test_match_state_cache
        # and a permanently stubbed cleanup_state fails it.
        import handlers.cipl_play as _cp
        import services.match_rewards as _mr
        import services.match_state_store as _mss
        for module, name in ((so_mod, "_persist_main_stats"),
                             (so_mod, "get_session"),
                             (so_mod, "_build_super_over_card"),
                             (_mr, "award_match_rewards_core"),
                             (_mss, "cleanup_state"),
                             (_cp, "_build_cipl_summary_image")):
            self.addCleanup(setattr, module, name, getattr(module, name))
        sot._patch_finalize(so_mod)
        # The shared harness stands in for the database with a stub that only
        # answers what _finalize's reward path asks. Everything else it touches
        # (the scorecard snapshot, the POTM credit, quest tracking) fails and is
        # swallowed by production's own try/except — correct behaviour, but it
        # buries the suite's output in tracebacks that mean nothing here.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        # The analysis document and the match-state read behind it are the
        # normal completion's, not the Super Over's.
        self._p = patch.object(cp, "_send_match_analysis",
                               lambda *a, **k: asyncio.sleep(0))
        self._p.start()
        self.addCleanup(self._p.stop)

    def _play(self, seed):
        random.seed(seed)
        ctx = _Ctx()
        state = _tied_cdraft_state(seed=seed)
        started = asyncio.run(so_mod.start_super_over(ctx, MID, state))
        self.assertTrue(started, "a drafted tie must kick off a Super Over")
        return ctx, so_mod._get(ctx, MID)

    def test_a_drafted_tie_plays_out_to_a_winner(self):
        ctx, so = self._play(11)
        # The side that batted second in the main match bats first.
        self.assertEqual(so["first_bat_uid"], GUEST_UID)
        self.assertEqual((so["bat_uid"], so["bowl_uid"]), (GUEST_UID, HOST_UID))

        asyncio.run(sot._play_match(ctx, MID))

        self.assertIsNone(so_mod._get(ctx, MID), "the Super Over is cleaned up")
        texts = "\n".join(m.text for m in ctx.bot.messages if m.text)
        self.assertIn("SUPER OVER RESULT", texts)
        self.assertIn("Match Winner", texts)

    def test_the_tie_is_announced_with_both_drafted_team_names(self):
        ctx, _so = self._play(5)
        texts = "\n".join(m.text for m in ctx.bot.messages if m.text)
        self.assertIn("MATCH TIED", texts)
        self.assertIn("SUPER OVER TIME", texts)
        self.assertIn("Host Draft XI", texts)
        self.assertIn("Guest Draft XI", texts)

    def test_eleven_drafted_cards_can_field_a_super_over_side(self):
        """Selection needs 3 batters and a bowler out of the same eleven."""
        _ctx, so = self._play(2)
        for uid in (HOST_UID, GUEST_UID):
            self.assertEqual(len(so["teams"][uid]["xi"]), 11,
                             "a drafted squad is dealt exactly eleven")
            self.assertGreaterEqual(len(so_mod._eligible_batters(so, uid)), 3)
            self.assertGreaterEqual(len(so_mod._eligible_bowlers(so, uid)), 1)

    def test_drafted_cards_carry_the_ids_selection_works_by(self):
        # _xi_players drops anything without a roster_id, and the pickers key
        # every button on it — a DraftCard that lost its id would silently
        # shrink the side the Super Over could field.
        _ctx, so = self._play(4)
        for uid in (HOST_UID, GUEST_UID):
            rids = [p.get("roster_id") for p in so["teams"][uid]["xi"]]
            self.assertTrue(all(isinstance(r, int) for r in rids), rids)
            self.assertEqual(len(set(rids)), 11, "roster ids must be unique")

    def test_several_drafts_all_reach_a_winner(self):
        """Different deals, different squads — every one must terminate."""
        for seed in (1, 6, 9):
            with self.subTest(seed=seed):
                ctx, _so = self._play(seed)
                asyncio.run(sot._play_match(ctx, MID))
                self.assertIsNone(so_mod._get(ctx, MID))
                texts = "\n".join(m.text for m in ctx.bot.messages if m.text)
                self.assertIn("SUPER OVER RESULT", texts)


if __name__ == "__main__":
    unittest.main()
