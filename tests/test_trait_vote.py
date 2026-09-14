"""The pre-toss Trait Vote: two captains decide whether traits play at all.

What these pin, in the order a match meets them:

  • the rule — Yes+Yes plays with traits, No+No without, and a split defers to
    the weaker XI (with the operator's alternatives, and the dead-level tie,
    behaving as documented);
  • the secret ballot — a first answer never appears on the prompt, because a
    revealed answer turns the second captain's vote into a reply;
  • who may answer, and how often;
  • the question not being asked when it decides nothing (two untraited squads)
    or must not be asked at all (an official fixture, a practice match);
  • that "off" actually reaches the ball — ``handlers.match._ball_traits`` is
    the one gate every /wpm delivery passes, and /letsplay strips its inline
    traits at launch instead;
  • that "off" also takes the ⚡ Trait Boost off the card, because leaving it on
    would hand the trait-heavy squad a rating lead in a match that was agreed to
    be played without traits;
  • and that both prompts survive the owner-guard, which would otherwise let the
    captain who did not trigger the message vote and block the other one.
"""

import asyncio
import unittest
from unittest import mock

import config
from handlers import letsplay
from handlers import match as match_mod
from services import button_access
from services import trait_vote_service as tvs


def _trait(level=3, name="Finisher", emoji="🔥"):
    return {"effect_key": "bat_finisher", "level": level,
            "display_name": name, "emoji": emoji, "category": "Batting"}


def _xi(ratings, traits_on_first=None):
    xi = [{"roster_id": i + 1, "name": f"P{i + 1}", "rating": r, "traits": []}
          for i, r in enumerate(ratings)]
    if traits_on_first:
        xi[0]["traits"] = list(traits_on_first)
    return xi


# ──────────────────────────────────────────────────────────────────────
# The rule
# ──────────────────────────────────────────────────────────────────────

class ResolveTests(unittest.TestCase):
    def test_both_yes_plays_with_traits(self):
        out = tvs.resolve("yes", "yes", 80, 80)
        self.assertTrue(out.enabled)
        self.assertEqual(out.outcome, "both_yes")
        self.assertIsNone(out.decided_by)

    def test_both_no_plays_without_them(self):
        out = tvs.resolve("no", "no", 80, 80)
        self.assertFalse(out.enabled)
        self.assertEqual(out.outcome, "both_no")

    def test_a_split_defers_to_the_weaker_xi(self):
        # Host is stronger and wants traits; the weaker guest said no.
        out = tvs.resolve("yes", "no", 86.0, 80.0)
        self.assertFalse(out.enabled)
        self.assertEqual((out.outcome, out.decided_by), ("split", "guest"))

    def test_the_weaker_xi_can_also_ask_FOR_traits(self):
        # The rule is "the underdog decides", not "the underdog turns them off".
        out = tvs.resolve("no", "yes", 86.0, 80.0)
        self.assertTrue(out.enabled)
        self.assertEqual(out.decided_by, "guest")

    def test_two_dead_level_xis_fall_back_to_the_default(self):
        out = tvs.resolve("yes", "no", 82.0, 82.0)
        self.assertEqual(out.decided_by, "default")
        self.assertIs(out.enabled, tvs.default_vote() == tvs.VOTE_YES)

    def test_a_missing_answer_counts_as_the_default_and_never_turns_traits_off(self):
        # Silence can only ever agree with traits, so nobody can turn them off
        # by refusing to answer.
        self.assertTrue(tvs.resolve(None, "yes", 80, 80).enabled)
        self.assertTrue(tvs.resolve(None, None, 80, 80).enabled)
        self.assertEqual(tvs.resolve(None, "no", 90, 80).decided_by, "guest")

    def test_junk_answers_read_as_no_answer_rather_than_raising(self):
        self.assertIsNone(tvs.normalize_vote("maybe"))
        self.assertIsNone(tvs.normalize_vote(None))
        self.assertEqual(tvs.normalize_vote("YES"), tvs.VOTE_YES)
        self.assertEqual(tvs.normalize_vote(False), tvs.VOTE_NO)

    def test_the_operator_can_settle_splits_a_different_way(self):
        with mock.patch.object(config, "TRAIT_VOTE_SPLIT_RULE", "traits_on"):
            self.assertTrue(tvs.resolve("yes", "no", 90, 80).enabled)
        with mock.patch.object(config, "TRAIT_VOTE_SPLIT_RULE", "traits_off"):
            self.assertFalse(tvs.resolve("yes", "no", 80, 90).enabled)
        with mock.patch.object(config, "TRAIT_VOTE_SPLIT_RULE", "nonsense"):
            # Unknown keys must not break a live lobby — they read as underdog.
            self.assertEqual(tvs.resolve("yes", "no", 90, 80).decided_by, "guest")

    def test_an_agreed_vote_ignores_the_split_rule_entirely(self):
        with mock.patch.object(config, "TRAIT_VOTE_SPLIT_RULE", "traits_off"):
            self.assertTrue(tvs.resolve("yes", "yes", 90, 10).enabled)


class XiHelperTests(unittest.TestCase):
    def test_strength_counts_the_trait_boost(self):
        bare = _xi([80] * 11)
        kitted = _xi([80] * 11, traits_on_first=[_trait(5)])
        self.assertEqual(tvs.team_strength(bare), 80.0)
        self.assertGreater(tvs.team_strength(kitted), tvs.team_strength(bare))

    def test_has_any_traits_sees_one_trait_anywhere_in_the_xi(self):
        self.assertFalse(tvs.has_any_traits(_xi([80] * 11)))
        self.assertTrue(tvs.has_any_traits(_xi([80] * 11, [_trait(1)])))

    def test_strip_traits_leaves_the_callers_own_xi_alone(self):
        original = _xi([80] * 11, [_trait(4)])
        stripped = tvs.strip_traits(original)
        self.assertEqual(stripped[0]["traits"], [])
        self.assertTrue(original[0]["traits"], "the source XI was mutated")
        self.assertEqual(tvs.team_strength(stripped), 80.0)

    def test_a_state_with_no_flag_plays_with_traits(self):
        self.assertTrue(tvs.traits_enabled({}))
        self.assertTrue(tvs.traits_enabled(None))
        self.assertTrue(tvs.traits_enabled({"traits_enabled": True}))
        self.assertFalse(tvs.traits_enabled({"traits_enabled": False}))


class RenderTests(unittest.TestCase):
    def test_the_prompt_never_reveals_an_answer_that_is_already_in(self):
        text = tvs.prompt_text("Host", "Guest", 84, 80,
                               host_voted=True, guest_voted=False)
        self.assertIn("Host: ✅ locked in", text)
        self.assertIn("Guest: ⏳ waiting", text)
        for leak in ("Host: Yes", "Host: No", "Host voted yes", "Host voted no"):
            self.assertNotIn(leak, text)

    def test_the_result_reveals_both_answers_and_says_who_decided(self):
        out = tvs.resolve("yes", "no", 86, 80)
        text = tvs.result_text(out, "Host", "Guest", "yes", "no")
        self.assertIn("TRAITS: OFF", text)
        self.assertIn("Guest", text)
        self.assertIn("lower-rated", text)
        self.assertIn("Host: Yes", text)
        self.assertIn("Guest: No", text)

    def test_the_off_result_says_nothing_was_unequipped(self):
        text = tvs.result_text(tvs.resolve("no", "no", 80, 80), "Host", "Guest",
                               "no", "no")
        self.assertIn("next match", text)

    def test_the_status_line_reads_both_ways(self):
        self.assertIn("ON", tvs.status_line(True))
        self.assertIn("OFF", tvs.status_line(False))


# ──────────────────────────────────────────────────────────────────────
# "Off" where it has to bite: the ball, and the card
# ──────────────────────────────────────────────────────────────────────

class BallGateTests(unittest.TestCase):
    """``handlers.match._ball_traits`` — the one gate every /wpm delivery takes
    (chat flow, Mini App and vsbot auto-play all call into ``_calc``)."""

    def test_an_ordinary_match_still_carries_its_traits(self):
        player = {"roster_id": -7, "traits": [_trait(2)]}
        self.assertEqual(match_mod._ball_traits({}, player), [_trait(2)])

    def test_a_no_traits_match_carries_none_even_for_inline_traits(self):
        player = {"roster_id": -7, "traits": [_trait(2)]}
        self.assertEqual(match_mod._ball_traits({"traits_enabled": False}, player), [])

    def test_a_no_traits_match_never_even_asks_the_database(self):
        state = {"traits_enabled": False}
        with mock.patch.object(match_mod, "_get_roster_traits",
                               side_effect=AssertionError("looked traits up anyway")):
            self.assertEqual(match_mod._ball_traits(state, {"roster_id": 42}), [])

    def test_the_flag_reads_the_same_way_everywhere(self):
        self.assertTrue(match_mod.traits_enabled_for({}))
        self.assertFalse(match_mod.traits_enabled_for({"traits_enabled": False}))


class _Entry:
    def __init__(self, roster_id):
        self.id = roster_id


class _Player:
    def __init__(self, name, rating, category="Batsman"):
        self.name = name
        self.rating = rating
        self.category = category
        self.id = rating
        self.bat_rating = rating
        self.bowl_rating = rating - 10
        self.bowl_style = "Medium Pacer"
        self.bowl_hand = "Right"
        self.bat_hand = "Right"
        self.country = "IND"
        self.version = "Base"


def _pairs(ratings):
    return [(_Entry(i + 1), _Player(f"P{i + 1}", r)) for i, r in enumerate(ratings)]


class LetsPlayLaunchTests(unittest.TestCase):
    def test_engine_dicts_are_built_without_traits_when_the_vote_said_no(self):
        with mock.patch.object(letsplay, "_roster_traits",
                               side_effect=AssertionError("loaded traits anyway")):
            xi = letsplay._xi_to_engine(None, _pairs([80] * 11), with_traits=False)
        self.assertEqual(len(xi), 11)
        self.assertTrue(all(p["traits"] == [] for p in xi))

    def test_engine_dicts_still_carry_traits_by_default(self):
        with mock.patch.object(letsplay, "_roster_traits",
                               return_value=[_trait(1)]) as loader:
            xi = letsplay._xi_to_engine(None, _pairs([80] * 11))
        self.assertTrue(loader.called)
        self.assertEqual(xi[0]["traits"], [_trait(1)])


class XiCardTests(unittest.TestCase):
    """The Playing XI card has to agree with the match it is introducing."""

    def _draft(self, **extra):
        draft = {"invite_id": 1, "chat_id": -100, "pitch_type": "Hard",
                 "host": {"tg_id": 1, "name": "Host", "user_id": 1},
                 "guest": {"tg_id": 2, "name": "Guest", "user_id": 2}}
        draft.update(extra)
        return draft

    def test_a_no_traits_match_shows_no_badges_and_no_boost(self):
        with mock.patch.object(letsplay, "_side_trait_map",
                               return_value={1: [_trait(5)]}):
            text = letsplay._show_xi_text(
                self._draft(traits_enabled=False), _pairs([80] * 11),
                _pairs([80] * 11), session=object())
        self.assertIn("Traits:", text)
        self.assertIn("OFF", text)
        self.assertNotIn("⚡", text)

    def test_a_with_traits_match_still_shows_the_boost_it_always_did(self):
        with mock.patch.object(letsplay, "_side_trait_map",
                               return_value={1: [_trait(5)]}):
            text = letsplay._show_xi_text(
                self._draft(traits_enabled=True), _pairs([80] * 11),
                _pairs([80] * 11), session=object())
        self.assertIn("⚡", text)

    def test_a_match_that_never_voted_reads_exactly_as_before(self):
        with mock.patch.object(letsplay, "_side_trait_map", return_value={}):
            text = letsplay._show_xi_text(
                self._draft(), _pairs([80] * 11), _pairs([80] * 11),
                session=object())
        self.assertNotIn("Traits:", text)


# ──────────────────────────────────────────────────────────────────────
# The /letsplay prompt, driven end to end
# ──────────────────────────────────────────────────────────────────────

HOST_TG, GUEST_TG, BYSTANDER_TG = 111, 222, 333


class _Bot:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return mock.Mock(message_id=900 + len(self.sent))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)
        return mock.Mock(message_id=kw.get("message_id"))


class _Ctx:
    def __init__(self, bot_data=None):
        self.bot_data = bot_data if bot_data is not None else {}
        self.job_queue = None
        self.bot = _Bot()


class _Query:
    def __init__(self, tg_id, data, ctx):
        self.from_user = mock.Mock(id=tg_id)
        self.data = data
        self.message = mock.Mock(chat_id=-100, message_id=901)
        self._ctx = ctx
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text or "")

    async def edit_message_text(self, text, **kw):
        self._ctx.bot.edits.append(text)


def _update(query):
    return mock.Mock(callback_query=query)


def _lp_draft(ctx, strengths=(84.0, 80.0)):
    draft = {
        "invite_id": 1, "chat_id": -100, "status": "traitvote",
        "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
        "guest": {"tg_id": GUEST_TG, "name": "Guest", "user_id": 2},
        "trait_vote": {"host": None, "guest": None,
                       "host_strength": strengths[0],
                       "guest_strength": strengths[1]},
        "trait_vote_msg_id": 901,
    }
    ctx.bot_data["lp_1"] = draft
    return draft


class LetsPlayVoteFlowTests(unittest.TestCase):
    def _vote(self, ctx, tg_id, answer):
        asyncio.run(letsplay.letsplay_traits_callback(
            _update(_Query(tg_id, f"lp_traits_1_{answer}", ctx)), ctx))

    def test_two_answers_settle_the_vote_and_move_on_to_the_pitch(self):
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        self._vote(ctx, HOST_TG, "yes")
        self.assertEqual(draft["status"], "traitvote", "settled on one answer")
        self._vote(ctx, GUEST_TG, "no")
        self.assertEqual(draft["status"], "pitch")
        # Guest fields the weaker XI, so their "no" stands.
        self.assertFalse(draft["traits_enabled"])
        self.assertTrue(any("Pitch Selection" in t for t in ctx.bot.sent))

    def test_the_first_answer_is_not_shown_to_the_second_captain(self):
        ctx = _Ctx()
        _lp_draft(ctx)
        self._vote(ctx, HOST_TG, "no")
        prompt = ctx.bot.edits[-1]
        self.assertIn("locked in", prompt)
        self.assertNotIn("TRAITS: OFF", prompt)

    def test_a_captain_gets_one_answer_only(self):
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        self._vote(ctx, HOST_TG, "yes")
        self._vote(ctx, HOST_TG, "no")
        self.assertEqual(draft["trait_vote"]["host"], "yes")

    def test_nobody_else_in_the_chat_may_vote(self):
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        self._vote(ctx, BYSTANDER_TG, "no")
        self.assertIsNone(draft["trait_vote"]["host"])
        self.assertIsNone(draft["trait_vote"]["guest"])

    def test_a_vote_nobody_finished_settles_on_the_defaults(self):
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        self._vote(ctx, HOST_TG, "no")
        ctx.job = mock.Mock(data={"invite_id": 1})
        asyncio.run(letsplay._on_trait_vote_timeout(ctx))
        # The guest never answered, so they count as YES; the guest is also the
        # weaker XI, so their default answer stands.
        self.assertTrue(draft["traits_enabled"])
        self.assertEqual(draft["status"], "pitch")

    def test_an_already_settled_vote_cannot_be_voted_on_again(self):
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        self._vote(ctx, HOST_TG, "yes")
        self._vote(ctx, GUEST_TG, "yes")
        before = draft["traits_enabled"]
        self._vote(ctx, GUEST_TG, "no")
        self.assertEqual(draft["traits_enabled"], before)


    def test_settling_the_same_vote_twice_does_not_prompt_twice(self):
        # Two simultaneous taps can leave both handlers holding a complete vote.
        ctx = _Ctx()
        draft = _lp_draft(ctx)
        draft["trait_vote"]["host"] = "yes"
        draft["trait_vote"]["guest"] = "yes"
        asyncio.run(letsplay._settle_trait_vote(ctx, draft))
        asyncio.run(letsplay._settle_trait_vote(ctx, draft))
        self.assertEqual(sum("Pitch Selection" in t for t in ctx.bot.sent), 1)


class LetsPlayPromptSkipTests(unittest.TestCase):
    def test_two_untraited_squads_are_never_asked(self):
        ctx = _Ctx()
        draft = {"invite_id": 1, "chat_id": -100, "status": "traitvote",
                 "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
                 "guest": {"tg_id": GUEST_TG, "name": "Guest", "user_id": 2}}
        with mock.patch.object(letsplay, "get_session", return_value=mock.Mock()), \
             mock.patch.object(letsplay, "_draft_side_xi", return_value=_xi([80] * 11)):
            asked = asyncio.run(letsplay._prompt_trait_vote(ctx, draft))
        self.assertFalse(asked)
        self.assertNotIn("trait_vote", draft)

    def test_one_traited_squad_is_enough_to_hold_the_vote(self):
        ctx = _Ctx()
        draft = {"invite_id": 1, "chat_id": -100, "status": "traitvote",
                 "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
                 "guest": {"tg_id": GUEST_TG, "name": "Guest", "user_id": 2}}
        kitted = _xi([80] * 11, [_trait(3)])
        with mock.patch.object(letsplay, "get_session", return_value=mock.Mock()), \
             mock.patch.object(letsplay, "_draft_side_xi",
                               side_effect=[kitted, _xi([80] * 11)]):
            asked = asyncio.run(letsplay._prompt_trait_vote(ctx, draft))
        self.assertTrue(asked)
        self.assertIn("trait_vote", draft)

    def test_an_official_fixture_keeps_the_tournaments_rules(self):
        ctx = _Ctx()
        draft = {"invite_id": 1, "chat_id": -100, "status": "traitvote",
                 "lpt": {"tournament_id": 5},
                 "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
                 "guest": {"tg_id": GUEST_TG, "name": "Guest", "user_id": 2}}
        self.assertFalse(asyncio.run(letsplay._prompt_trait_vote(ctx, draft)))

    def test_a_practice_match_against_the_bot_is_not_a_negotiation(self):
        ctx = _Ctx()
        draft = {"invite_id": 1, "chat_id": -100, "status": "traitvote",
                 "vs_bot": True,
                 "host": {"tg_id": HOST_TG, "name": "Host", "user_id": 1},
                 "guest": {"tg_id": GUEST_TG, "name": "Bot", "user_id": 2}}
        self.assertFalse(asyncio.run(letsplay._prompt_trait_vote(ctx, draft)))


# ──────────────────────────────────────────────────────────────────────
# The /wpm prompt, driven end to end
# ──────────────────────────────────────────────────────────────────────

def _cric_lobby(ctx, strengths=(84.0, 80.0)):
    lobby = {
        "host_user_id": 1, "host_tg_id": HOST_TG, "host_label": "Host",
        "guest_user_id": 2, "guest_tg_id": GUEST_TG, "guest_label": "Guest",
        "overs": 5, "lobby_msg_id": 901,
        "trait_vote": {"host": None, "guest": None,
                       "host_strength": strengths[0],
                       "guest_strength": strengths[1]},
    }
    ctx.bot_data[match_mod._cric_lobby_key(-100)] = lobby
    return lobby


class WpmVoteFlowTests(unittest.TestCase):
    def _vote(self, ctx, tg_id, answer):
        asyncio.run(match_mod.cric_traits_callback(
            _update(_Query(tg_id, f"cric_traits:{answer}", ctx)), ctx))

    def test_two_answers_settle_the_vote_and_the_toss_follows(self):
        ctx = _Ctx()
        lobby = _cric_lobby(ctx)
        self._vote(ctx, HOST_TG, "no")
        self.assertNotIn("traits_enabled", lobby)
        self._vote(ctx, GUEST_TG, "no")
        self.assertFalse(lobby["traits_enabled"])
        self.assertNotIn("trait_vote", lobby)
        self.assertTrue(any("TOSS" in t for t in ctx.bot.edits))
        self.assertTrue(any("TRAITS: OFF" in t for t in ctx.bot.edits))

    def test_a_split_hands_the_call_to_the_weaker_xi(self):
        ctx = _Ctx()
        lobby = _cric_lobby(ctx, strengths=(90.0, 80.0))
        self._vote(ctx, HOST_TG, "yes")
        self._vote(ctx, GUEST_TG, "no")
        self.assertFalse(lobby["traits_enabled"])

    def test_only_the_two_captains_may_vote(self):
        ctx = _Ctx()
        lobby = _cric_lobby(ctx)
        self._vote(ctx, BYSTANDER_TG, "yes")
        self.assertIsNone(lobby["trait_vote"]["host"])

    def test_a_vote_nobody_finished_settles_on_the_defaults(self):
        ctx = _Ctx()
        lobby = _cric_lobby(ctx)
        ctx.job = mock.Mock(data={"chat_id": -100})
        asyncio.run(match_mod._expire_trait_vote(ctx))
        self.assertTrue(lobby["traits_enabled"])
        self.assertTrue(any("TOSS" in t for t in ctx.bot.edits))

    def test_settling_the_same_vote_twice_does_not_post_the_toss_twice(self):
        ctx = _Ctx()
        lobby = _cric_lobby(ctx)
        lobby["trait_vote"]["host"] = "yes"
        lobby["trait_vote"]["guest"] = "yes"
        asyncio.run(match_mod._settle_trait_vote(ctx, -100, lobby))
        asyncio.run(match_mod._settle_trait_vote(ctx, -100, lobby))
        self.assertEqual(sum("TOSS" in t for t in ctx.bot.edits), 1)

    def test_an_expiry_for_a_lobby_that_is_gone_does_nothing(self):
        ctx = _Ctx()
        ctx.job = mock.Mock(data={"chat_id": -100})
        asyncio.run(match_mod._expire_trait_vote(ctx))
        self.assertEqual(ctx.bot.edits, [])


class ButtonOwnershipTests(unittest.TestCase):
    """Both prompts are posted while handling ONE captain's tap, so without
    these prefixes the owner guard would silently block the other captain — the
    vote could then never be completed by anyone but its trigger."""

    def test_both_vote_prefixes_are_shared(self):
        self.assertTrue(button_access.is_shared_callback_data("cric_traits:yes"))
        self.assertTrue(button_access.is_shared_callback_data("lp_traits_1_no"))


if __name__ == "__main__":
    unittest.main()
