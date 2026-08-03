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
