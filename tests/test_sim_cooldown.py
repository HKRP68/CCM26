"""/sim runs on a 4-hour cooldown, and a challenge spends both sides' clocks.

/sim simulates an entire match server-side — two innings, a summary image and a
full commentary feed — and it was free to repeat. It now sits on the same
cooldown ladder as /claim and /gspin: a base value in config.py, overridable per
command by an admin, reduced by paid tiers.

Replying to another player sims your XI against theirs. That is a challenge, so
it spends the cooldown for BOTH sides — which means a player who is still on
cooldown cannot be challenged. The clock only starts once a match has actually
been simulated, so a rejected command never costs anyone their sim.
"""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

logging.disable(logging.CRITICAL)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import SIM_COOLDOWN
import handlers.sim as sim_mod


class _Msg:
    """A Telegram message that records what the handler sent back."""

    def __init__(self, sent, reply_to=None):
        self.sent = sent
        self.reply_to_message = reply_to

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return _Msg(self.sent)

    async def edit_text(self, text, **kwargs):
        self.sent.append(text)
        return self

    async def reply_photo(self, **kwargs):
        return None

    async def reply_document(self, *a, **kwargs):
        return None


class SimCooldownTests(unittest.TestCase):
    def setUp(self):
        from database import Base
        from models import User, UserStats
        self.User, self.UserStats = User, UserStats
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        # The handler closes the session in its finally block; keep it open so
        # the test can read back what it wrote.
        self.db.close = lambda: None

        self.caller = User(telegram_id=1001, first_name="Caller", team_name="Alpha")
        self.rival = User(telegram_id=2002, first_name="Rival", team_name="Bravo")
        self.db.add_all([self.caller, self.rival])
        self.db.flush()
        self.db.commit()

        self._patched = []
        self._patch(sim_mod, "get_session", lambda: self.db)
        self._patch(sim_mod, "sync_telegram_user",
                    lambda session, tg: self.db.query(User)
                    .filter(User.telegram_id == tg.id).first())
        # Everything below the cooldown gate is heavy DB + engine work that
        # these tests are not about; stub it down to "a match happened".
        self._patch(sim_mod, "_get_ordered_roster",
                    lambda session, uid: [(None, SimpleNamespace(rating=80))] * 11)
        self._patch(sim_mod, "validate_xi", lambda roster: (True, []))
        self._patch(sim_mod, "_xi_from_roster", lambda s, uid, roster: ["xi"] * 11)
        self._patch(sim_mod, "_build_bot_xi", lambda session, avg: ["bot"] * 11)
        self._patch(sim_mod, "build_commentary_picker", lambda session: None)
        self._patch(sim_mod, "list_pitches", lambda: ["Flat"])
        self._patch(sim_mod, "get_pitch_meta", lambda p: {"description": "flat"})
        self._patch(sim_mod, "get_config", lambda: {})
        self._patch(sim_mod, "render_innings_card", lambda inn: "card")
        self._patch(sim_mod, "render_result", lambda m: "result")
        self._patch(sim_mod, "render_match_summary_image", lambda m, **k: None)
        self.simulated = []
        self._patch(sim_mod, "simulate_match", self._fake_simulate)

    def tearDown(self):
        for obj, name, original in reversed(self._patched):
            setattr(obj, name, original)
        self.db.close = lambda: None

    def _patch(self, obj, name, value):
        self._patched.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def _fake_simulate(self, *a, **k):
        self.simulated.append(True)
        blank = {"innings": 1, "batting_team": "A", "bowling_team": "B",
                 "runs": 100, "wickets": 5, "overs": "20.0"}
        return {"format": "T20", "toss": {"text": "toss"},
                "innings1": dict(blank), "innings2": dict(blank, innings=2),
                "result": {"text": "Alpha won"}, "potm": None,
                "commentary_feed": []}

    # ── helpers ──

    def _stats(self, user):
        return (self.db.query(self.UserStats)
                .filter(self.UserStats.user_id == user.id).first())

    def _stats_for(self, user):
        """Create the user's stats row, as the cooldown gate would."""
        return sim_mod._stats_row(self.db, user.id)

    def _last_sim(self, user):
        """When this user last spent a sim — None if they never have (which
        includes having no stats row at all)."""
        row = self._stats(user)
        return row.last_sim if row is not None else None

    def _run(self, caller, reply_to=None):
        """Run /sim as ``caller``, optionally replying to ``reply_to``."""
        sent = []
        reply = (_Msg(sent, None) if reply_to is None else None)
        if reply_to is not None:
            reply = _Msg(sent)
            reply.from_user = SimpleNamespace(id=reply_to.telegram_id, is_bot=False)
        message = _Msg(sent, reply_to=(reply if reply_to is not None else None))
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_user=SimpleNamespace(id=caller.telegram_id, is_bot=False))
        context = SimpleNamespace(args=[])
        asyncio.run(sim_mod.sim_handler(update, context))
        return "\n".join(sent)

    # ── the solo sim ──

    def test_a_first_sim_runs_and_starts_the_clock(self):
        out = self._run(self.caller)
        self.assertEqual(len(self.simulated), 1)
        self.assertIsNotNone(self._stats(self.caller).last_sim)
        self.assertIn("Next /sim in", out)

    def test_a_second_sim_straight_after_is_refused(self):
        self._run(self.caller)
        out = self._run(self.caller)
        self.assertEqual(len(self.simulated), 1, "the second /sim still simulated")
        self.assertIn("cooling down", out)

    def test_the_sim_is_available_again_once_the_cooldown_has_passed(self):
        self._run(self.caller)
        stats = self._stats(self.caller)
        stats.last_sim = datetime.utcnow() - timedelta(seconds=SIM_COOLDOWN + 60)
        self.db.commit()
        self._run(self.caller)
        self.assertEqual(len(self.simulated), 2)

    def test_a_rejected_sim_does_not_burn_the_cooldown(self):
        self._patch(sim_mod, "validate_xi", lambda roster: (False, ["no keeper"]))
        out = self._run(self.caller)
        self.assertEqual(self.simulated, [])
        self.assertIn("Playing XI is invalid", out)
        self.assertIsNone(self._stats(self.caller).last_sim)

    # ── the challenge sim ──

    def test_a_challenge_spends_both_players_cooldowns(self):
        out = self._run(self.caller, reply_to=self.rival)
        self.assertEqual(len(self.simulated), 1)
        self.assertIsNotNone(self._stats(self.caller).last_sim)
        self.assertIsNotNone(self._stats(self.rival).last_sim)
        self.assertIn("Next /sim for both sides in", out)

    def test_a_player_on_cooldown_cannot_be_challenged(self):
        self._run(self.rival)                      # the rival spends their sim
        self.simulated.clear()

        out = self._run(self.caller, reply_to=self.rival)
        self.assertEqual(self.simulated, [], "the challenge simulated anyway")
        self.assertIn("still on /sim cooldown", out)
        # ...and being challenged while unavailable cost the caller nothing.
        self.assertIsNone(self._stats(self.caller).last_sim)

    def test_a_challenger_on_cooldown_is_stopped_before_the_rival_is_touched(self):
        self._run(self.caller)
        self.simulated.clear()

        out = self._run(self.caller, reply_to=self.rival)
        self.assertEqual(self.simulated, [])
        self.assertIn("cooling down", out)
        # The rival was never even looked at, let alone charged.
        self.assertIsNone(self._stats(self.rival))


class SimCooldownStateTests(unittest.TestCase):
    """The cooldown value itself: base, admin override, tier reduction."""

    def setUp(self):
        from database import Base
        from models import User
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(telegram_id=7007, first_name="Sim")
        self.db.add(self.user)
        self.db.flush()

    def test_a_fresh_user_is_ready_on_the_base_cooldown(self):
        _stats, ready, remaining, cooldown = sim_mod.sim_cooldown_state(
            self.db, self.user)
        self.assertTrue(ready)
        self.assertEqual(remaining, 0)
        self.assertEqual(cooldown, SIM_COOLDOWN)
        self.assertEqual(cooldown, 4 * 3600)

    def test_a_just_used_sim_reports_the_time_left(self):
        stats, _r, _rem, _cd = sim_mod.sim_cooldown_state(self.db, self.user)
        stats.last_sim = datetime.utcnow()
        self.db.flush()
        _stats, ready, remaining, _cd = sim_mod.sim_cooldown_state(
            self.db, self.user)
        self.assertFalse(ready)
        self.assertGreater(remaining, SIM_COOLDOWN - 60)

    def test_an_admin_override_replaces_the_base_cooldown(self):
        from models import BotCommand
        self.db.add(BotCommand(command_key="sim", display_name="/sim",
                               cooldown_seconds=600))
        self.db.flush()
        _s, _r, _rem, cooldown = sim_mod.sim_cooldown_state(self.db, self.user)
        self.assertEqual(cooldown, 600)

    def test_a_paid_tier_gets_its_cooldown_back_sooner(self):
        self.user.subscription_tier = "diamond"
        self.user.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
        self.db.flush()
        _s, _r, _rem, cooldown = sim_mod.sim_cooldown_state(self.db, self.user)
        self.assertLess(cooldown, SIM_COOLDOWN)
        self.assertEqual(cooldown, 3 * 3600)       # Diamond: −15 min per hour


if __name__ == "__main__":
    unittest.main()


class SimClaimRaceTests(SimCooldownTests):
    """The cooldown has to hold against two /sim commands in flight at once.

    The gate is a read; the write lands much later, after the roster reads and
    the whole simulation. The bot runs handlers concurrently
    (BOT_CONCURRENT_UPDATES, 16 by default), so both commands could pass the
    gate and each play a full match for one cooldown. claim_sim_slot closes
    that with a conditional UPDATE — whoever's statement lands first owns the
    slot.
    """

    def test_the_slot_can_only_be_claimed_once(self):
        now = datetime.utcnow()
        self._stats_for(self.caller)
        self.assertTrue(
            sim_mod.claim_sim_slot(self.db, self.caller.id, SIM_COOLDOWN, now))
        # A racing request finds the clock already running and matches no row.
        self.assertFalse(
            sim_mod.claim_sim_slot(self.db, self.caller.id, SIM_COOLDOWN, now))

    def test_the_slot_can_be_claimed_again_once_the_clock_runs_out(self):
        self._stats_for(self.caller)
        old = datetime.utcnow() - timedelta(seconds=SIM_COOLDOWN + 60)
        self.assertTrue(
            sim_mod.claim_sim_slot(self.db, self.caller.id, SIM_COOLDOWN, old))
        self.assertTrue(sim_mod.claim_sim_slot(
            self.db, self.caller.id, SIM_COOLDOWN, datetime.utcnow()))

    def test_a_challenge_that_cannot_claim_the_rival_charges_nobody(self):
        # The caller's slot is claimed first; if the rival's then fails, the
        # whole transaction must roll back — a half-claimed challenge would
        # cost the caller four hours for a match that never happened.
        real_claim = sim_mod.claim_sim_slot
        calls = []

        def claim_then_fail(session, user_id, cooldown, now):
            calls.append(user_id)
            if len(calls) == 1:
                return real_claim(session, user_id, cooldown, now)
            return False                    # the rival slipped in first

        self._patch(sim_mod, "claim_sim_slot", claim_then_fail)
        out = self._run(self.caller, reply_to=self.rival)

        self.assertEqual(self.simulated, [])
        self.assertIn("just used their sim", out)
        # Neither side is left on cooldown: the rollback took the caller's claim
        # back with the rival's.
        self.assertIsNone(self._last_sim(self.caller))
        self.assertIsNone(self._last_sim(self.rival))

    def test_a_losing_racer_is_told_rather_than_getting_a_free_match(self):
        self._patch(sim_mod, "claim_sim_slot", lambda *a, **k: False)
        out = self._run(self.caller)
        self.assertEqual(self.simulated, [])
        self.assertIn("went through on another message", out)

    def test_a_sim_that_crashes_hands_the_cooldown_back(self):
        def boom(*a, **k):
            raise RuntimeError("engine exploded")

        self._patch(sim_mod, "simulate_match", boom)
        out = self._run(self.caller)
        self.assertIn("Couldn't start the simulation", out)
        # The slot was taken before the match ran, so it has to be given back —
        # nobody loses four hours because the engine fell over.
        self.assertIsNone(self._last_sim(self.caller))

    def test_each_side_is_quoted_its_own_cooldown(self):
        # Different membership tiers mean different clocks; the caller must not
        # be told the rival gets their sim back at the caller's time.
        self.rival.subscription_tier = "diamond"
        self.rival.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
        self.db.commit()

        out = self._run(self.caller, reply_to=self.rival)
        self.assertEqual(len(self.simulated), 1)
        self.assertIn("Next /sim: you in", out)
        self.assertIn("4h", out)          # caller, free tier
        self.assertIn("3h", out)          # rival, Diamond
