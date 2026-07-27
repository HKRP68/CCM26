"""Who played the match, and what the bot is allowed to tell you.

Three related things a scorecard has to get right:

* the bot's chosen Approach is **not** published after the over — a bot whose
  every pick is printed is a bot whose mix can be written down and countered;
* a bot-run side is **named** as one, because in a league match the bot fields a
  real franchise and "RCB won by 8 wickets" otherwise reads like a human result;
* the archived text scorecard **credits the captains**, because it is the file
  anyone reads once the match is gone, and two players fielding RCB otherwise
  produce identical archives.
"""

import unittest

from services import match_webapp_service as mws

try:
    import handlers.cipl_play as cp        # needs python-telegram-bot
    _HAVE_CP = True
except Exception:                          # pragma: no cover
    _HAVE_CP = False


def _pvp_state():
    return {
        "match_id": 42,
        "bat_team_name": "CSK", "bowl_team_name": "RCB",
        "bat_team_id": 2, "bowl_team_id": 1,
        "bat_user_tg": 222, "bowl_user_tg": 111,
        "inn1_team": "RCB",
        "user_names": {"111": "alice", "222": "Bob Singh"},
        "pitch_type": "Hard", "stadium": "Chinnaswamy",
    }


def _bot_state():
    return dict(_pvp_state(), is_bot_match=True, bot_user_id=1,
                user_names={"111": "🤖 Bot", "222": "alice"})


_INNINGS = [
    {"number": 1, "bat_team": "RCB", "runs": 186, "wickets": 4, "overs": "20.0",
     "batting": [{"name": "Kohli", "runs": 74, "balls": 48, "fours": 8,
                  "sixes": 2, "out": True, "how_out": "c Dhoni b Bumrah",
                  "sr": 154.2}],
     "bowling": [{"name": "Bumrah", "overs": "4.0", "maidens": 0, "runs": 28,
                  "wickets": 2, "econ": 7.0}]},
    {"number": 2, "bat_team": "CSK", "runs": 170, "wickets": 8, "overs": "20.0",
     "batting": [], "bowling": []},
]


class TextScorecardCreditTests(unittest.TestCase):
    def test_both_captains_are_credited(self):
        text = mws._build_text_scorecard(42, _INNINGS, "RCB won by 16 runs",
                                         state=_pvp_state())
        self.assertIn("RCB (@alice)", text)
        self.assertIn("CSK (Bob Singh)", text)

    def test_a_handle_is_only_invented_when_it_could_be_one(self):
        """A stored first name is not an @username, so it is not dressed as one."""
        credits = mws._team_credits(_pvp_state())
        self.assertEqual(credits["RCB"], "@alice")      # no spaces → a handle
        self.assertEqual(credits["CSK"], "Bob Singh")   # a name → left alone

    def test_a_bot_side_is_credited_as_the_bot(self):
        credits = mws._team_credits(_bot_state())
        self.assertEqual(credits["RCB"], mws.BOT_CREDIT)
        self.assertEqual(credits["CSK"], "@alice")

    def test_the_innings_header_does_not_shout_the_handle(self):
        text = mws._build_text_scorecard(42, _INNINGS, None, state=_pvp_state())
        self.assertIn("RCB INNINGS  —  @alice", text)
        self.assertNotIn("@ALICE", text)

    def test_the_archive_records_the_conditions(self):
        text = mws._build_text_scorecard(42, _INNINGS, None, state=_pvp_state())
        self.assertIn("Pitch: Hard", text)
        self.assertIn("Stadium: Chinnaswamy", text)

    def test_it_still_renders_with_no_state_at_all(self):
        """The live state can be gone (an old match, a crashed cleanup).

        The archive must degrade to the old, uncredited card rather than fail —
        losing the byline is a shame, losing the scorecard is a bug.
        """
        for state in (None, {}, {"user_names": None}):
            text = mws._build_text_scorecard(42, _INNINGS, "RCB won", state=state)
            self.assertIn("Match Summary: RCB vs CSK", text)
            self.assertIn("Kohli", text)
            self.assertNotIn("(@", text)

    def test_the_archive_names_the_player_of_the_match(self):
        """The on-screen card has always shown POTM; the archive never did."""
        text = mws._build_text_scorecard(
            42, _INNINGS, None, state=_pvp_state(),
            potm=("Kohli", "74(48) | 1/22", "RCB"))
        self.assertIn("Player of the Match: Kohli (74(48) | 1/22) — RCB (@alice)",
                      text)

    def test_a_partial_or_missing_potm_never_breaks_the_card(self):
        self.assertIsNone(mws._potm_line(None))
        self.assertIsNone(mws._potm_line(()))
        self.assertIsNone(mws._potm_line((None, "74(48)", "RCB")))
        self.assertEqual(mws._potm_line(("Kohli", None, None)),
                         "Player of the Match: Kohli")

    def test_scores_and_bowling_survive_the_change(self):
        text = mws._build_text_scorecard(42, _INNINGS, None, state=_pvp_state())
        self.assertIn("Total: 186/4 (20.0 Overs)", text)
        self.assertIn("Bumrah", text)
        self.assertIn("Kohli", text)


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class BotTeamTagTests(unittest.TestCase):
    def test_the_bot_s_franchise_is_named_on_the_card(self):
        state = _bot_state()
        self.assertEqual(cp.bot_team_name(state), "RCB")
        self.assertEqual(cp.with_bot_tag(state, "RCB"), "RCB (Bot)")

    def test_the_human_s_team_is_left_alone(self):
        self.assertEqual(cp.with_bot_tag(_bot_state(), "CSK"), "CSK")

    def test_a_human_match_is_never_tagged(self):
        state = _pvp_state()
        self.assertIsNone(cp.bot_team_name(state))
        for team in ("RCB", "CSK"):
            self.assertEqual(cp.with_bot_tag(state, team), team)

    def test_tagging_twice_does_not_stack(self):
        state = _bot_state()
        once = cp.with_bot_tag(state, "RCB")
        self.assertEqual(cp.with_bot_tag(state, once), once)

    def test_it_survives_a_missing_or_junk_bot_id(self):
        self.assertIsNone(cp.bot_team_name(dict(_bot_state(), bot_user_id=None)))
        self.assertIsNone(cp.bot_team_name(dict(_bot_state(), bot_user_id=999)))
        self.assertEqual(cp.with_bot_tag(_bot_state(), None), None)


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class BotPlanIsHiddenTests(unittest.TestCase):
    """The over summary must not publish what the AI captain chose."""

    def _summary_text(self, state):
        summary = {
            "over_no": 7, "over_timeline": ["1", "4", "0", "0", "6", "1"],
            "over_runs": 12, "over_wickets": 0,
            "bowler": {"name": "Bumrah", "roster_id": 1},
            "bowler_figures": "2-0-18-1", "momentum_shift": 3.0,
            "batting_approach": "ultra", "bowling_approach": "variation",
        }
        return cp._render_over_summary(state, summary)

    def _live_state(self, **extra):
        from tests.test_bot_captain import _state
        state = _state(current_over=8)
        state.update({
            "bat_team_name": "CSK", "bowl_team_name": "RCB",
            "bat_team_id": 2, "bowl_team_id": 1,
            "batting_approach": "ultra", "bowling_approach": "variation",
        })
        state.update(extra)
        return state

    def test_the_bot_s_approach_is_never_printed(self):
        state = self._live_state(is_bot_match=True, bot_user_id=1)
        text = self._summary_text(state)
        self.assertNotIn("Bot's plan", text)
        # ...and no approach label leaks by another name either.
        from services import bot_captain
        for key in bot_captain.BOWL_PLANS:
            self.assertNotIn(bot_captain.bowling_label(key), text)
        for key in bot_captain.BAT_LADDER:
            self.assertNotIn(bot_captain.batting_label(key), text)

    def test_the_over_summary_still_reports_the_over(self):
        text = self._summary_text(self._live_state(is_bot_match=True,
                                                   bot_user_id=1))
        self.assertIn("End of Over 7", text)
        self.assertIn("Bumrah", text)
        self.assertIn("12", text)

    def test_what_the_bot_has_noticed_is_still_said(self):
        """A warning is not a plan — the read note stays.

        Telling the player the bot has spotted a pattern is the mind game
        working; telling them which plan is coming is handing over the counter.
        """
        state = self._live_state(is_bot_match=True, bot_user_id=1)
        state["bot_read_note"] = "The bot has spotted three straight overs."
        text = self._summary_text(state)
        self.assertIn("spotted three straight", text)
        # ...and it is consumed, so it shows once rather than every over after.
        self.assertNotIn("bot_read_note", state)

    def test_a_human_match_never_grows_a_bot_line(self):
        text = self._summary_text(self._live_state())
        self.assertNotIn("🤖", text)
        self.assertNotIn("🧠", text)


if __name__ == "__main__":
    unittest.main()
