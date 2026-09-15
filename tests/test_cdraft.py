"""Challenge Draft (/cdraft) — the draft rules, and the promise they exist for.

The load-bearing claim of this mode is that a drafted squad is a legal Playing
XI *by construction*: whichever card each captain takes, and whatever the other
captain does, both sides end up on a shape both of the game's rulebooks accept.
``test_every_draft_produces_two_legal_xis`` is that claim, checked over many
random drafts and both rulebooks.
"""

import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import cdraft_service, xi_rules


def make_pool(ratings=range(70, 96), per_rating=4,
              categories=("Batsman", "Wicket Keeper", "All-rounder", "Bowler")):
    """A synthetic catalogue: ``per_rating`` cards of every category at every
    rating, each with a distinct name."""
    pool, pid = [], 1
    for category in categories:
        for rating in ratings:
            for copy in range(per_rating):
                pool.append({
                    "id": pid,
                    "name": f"{category[:3]}{rating}_{copy}",
                    "country": "India",
                    "category": category,
                    "rating": rating,
                    "bat_rating": rating - 2,
                    "bowl_rating": rating - 5,
                    "bat_hand": "Right",
                    "bowl_hand": "Right",
                    "bowl_style": "Fast",
                })
                pid += 1
    return pool


def play_out(slots, seed=0, chooser=None):
    """Run a whole draft to completion. ``chooser(slot, side)`` picks a card;
    the default picks at random, which is the point — no strategy may produce
    an illegal squad."""
    rng = random.Random(seed)
    chooser = chooser or (lambda slot, side: rng.choice(slot["cards"]))
    state = cdraft_service.new_state(slots)
    while not cdraft_service.is_complete(state):
        side = cdraft_service.current_side(state)
        slot = cdraft_service.current_slot(state)
        cdraft_service.apply_pick(state, state["index"], side,
                                  chooser(slot, side)["id"])
    return state


class SlotTemplateTests(unittest.TestCase):

    def test_the_template_is_four_one_two_four_over_eleven_slots(self):
        self.assertEqual(cdraft_service.SLOT_COUNT, 11)
        counts = {role: cdraft_service.SLOT_ROLES.count(role)
                  for role in set(cdraft_service.SLOT_ROLES)}
        self.assertEqual(counts, {
            cdraft_service.ROLE_BATSMAN: 4,
            cdraft_service.ROLE_KEEPER: 1,
            cdraft_service.ROLE_ALLROUNDER: 2,
            cdraft_service.ROLE_BOWLER: 4,
        })

    def test_no_role_is_stranded_at_one_end_of_the_rating_ladder(self):
        """Batsmen and bowlers must span the ladder, or every bowler in the game
        would be rated below every batsman."""
        for role in (cdraft_service.ROLE_BATSMAN, cdraft_service.ROLE_BOWLER):
            positions = [i for i, r in enumerate(cdraft_service.SLOT_ROLES)
                         if r == role]
            self.assertLess(min(positions), 3, f"{role} starts too late")
            self.assertGreater(max(positions), 7, f"{role} ends too early")

    def test_the_pick_order_is_a_snake_that_splits_eleven_picks_six_five(self):
        order = cdraft_service.PICK_ORDER
        self.assertEqual(len(order), cdraft_service.SLOT_COUNT)
        self.assertEqual(order[0], "host", "the host who opened it picks first")
        self.assertEqual(order[-1], "target", "the guest gets the last pick")
        self.assertEqual(sorted([order.count("host"), order.count("target")]),
                         [5, 6])
        # A snake, not strict alternation: somebody picks twice in a row.
        self.assertTrue(any(a == b for a, b in zip(order, order[1:])))

    def test_the_rating_ladder_descends_from_top_to_bottom(self):
        ratings = cdraft_service.slot_ratings(top=90, bottom=80)
        self.assertEqual(ratings[0], 90)
        self.assertEqual(ratings[-1], 80)
        self.assertEqual(ratings, sorted(ratings, reverse=True))


class BuildSlotsTests(unittest.TestCase):

    def setUp(self):
        self.pool = make_pool()
        self.slots = cdraft_service.build_slots(pool=self.pool, seed=11)

    def test_every_slot_deals_two_cards_of_that_slots_role(self):
        self.assertEqual(len(self.slots), 11)
        for index, slot in enumerate(self.slots):
            self.assertEqual(len(slot["cards"]), 2, f"slot {index + 1}")
            self.assertEqual(slot["role"], cdraft_service.SLOT_ROLES[index])
            for card in slot["cards"]:
                self.assertEqual(
                    cdraft_service.normalise_role(card["category"]),
                    slot["role"], f"slot {index + 1} dealt the wrong role")

    def test_the_two_cards_in_a_pair_are_within_the_configured_spread(self):
        for index, slot in enumerate(self.slots):
            first, second = slot["cards"]
            self.assertLessEqual(
                abs(first["rating"] - second["rating"]),
                cdraft_service.PAIR_SPREAD,
                f"slot {index + 1} dealt an unfair pair")

    def test_no_cricketer_is_dealt_twice_in_one_draft(self):
        names = [c["name"].casefold()
                 for slot in self.slots for c in slot["cards"]]
        self.assertEqual(len(names), len(set(names)))

    def test_versions_of_one_cricketer_count_as_the_same_player(self):
        """The catalogue carries several cards per real cricketer. Dealing two of
        them would put the same player on the field twice, so the de-dupe is by
        name rather than by id."""
        pool = []
        for pid, version in enumerate(("Base", "Gold", "Icon", "Legend"), start=1):
            pool.append({"id": pid, "name": "MS Dhoni", "country": "India",
                         "category": "Wicket Keeper", "rating": 88,
                         "bat_rating": 86, "bowl_rating": 10,
                         "bat_hand": "Right", "bowl_hand": "Right",
                         "bowl_style": ""})
        with self.assertRaises(cdraft_service.CdraftPoolError):
            cdraft_service.build_slots(
                pool=pool, seed=1,
                top=88, bottom=88)

    def test_a_thin_band_widens_rather_than_giving_up(self):
        """Nothing at the ladder's ratings, plenty far below it: the search
        widens until it finds a pair instead of failing."""
        pool = make_pool(ratings=range(60, 64))
        slots = cdraft_service.build_slots(pool=pool, seed=3, top=88, bottom=78)
        self.assertEqual(len(slots), 11)
        for slot in slots:
            self.assertTrue(all(60 <= c["rating"] <= 63 for c in slot["cards"]))

    def test_a_role_the_pool_cannot_supply_refuses_rather_than_substituting(self):
        """A squad with no keeper cannot field a legal XI, so a missing role is
        a hard failure — never quietly swapped for another role."""
        pool = make_pool(categories=("Batsman", "All-rounder", "Bowler"))
        with self.assertRaises(cdraft_service.CdraftPoolError) as caught:
            cdraft_service.build_slots(pool=pool, seed=5)
        self.assertIn("Wicket Keeper", str(caught.exception))

    def test_career_cards_never_reach_the_pool(self):
        """``build_slots`` reads the shared player cache, which applies
        ``player_service.not_career`` when it loads. This pins the dependency so
        a future refactor cannot quietly start dealing somebody's career card."""
        import inspect
        from services import player_cache
        self.assertIn("not_career", inspect.getsource(player_cache._refresh))


class PickTests(unittest.TestCase):

    def setUp(self):
        self.slots = cdraft_service.build_slots(pool=make_pool(), seed=2)
        self.state = cdraft_service.new_state(self.slots)

    def test_the_card_you_do_not_take_goes_to_your_opponent(self):
        slot = cdraft_service.current_slot(self.state)
        side = cdraft_service.current_side(self.state)
        taken, given = slot["cards"]
        chosen, other = cdraft_service.apply_pick(
            self.state, 0, side, taken["id"])
        self.assertEqual(chosen["id"], taken["id"])
        self.assertEqual(other["id"], given["id"])
        self.assertEqual(self.state["squads"][side], [taken])
        self.assertEqual(
            self.state["squads"][cdraft_service.other_side(side)], [given])

    def test_the_captain_who_is_not_on_the_clock_is_refused(self):
        side = cdraft_service.current_side(self.state)
        with self.assertRaises(ValueError):
            cdraft_service.apply_pick(
                self.state, 0, cdraft_service.other_side(side),
                self.slots[0]["cards"][0]["id"])

    def test_a_stale_button_from_another_slot_is_refused(self):
        side = cdraft_service.current_side(self.state)
        with self.assertRaises(ValueError):
            cdraft_service.apply_pick(self.state, 4, side,
                                      self.slots[4]["cards"][0]["id"])

    def test_a_card_from_a_different_slot_is_refused(self):
        side = cdraft_service.current_side(self.state)
        with self.assertRaises(ValueError):
            cdraft_service.apply_pick(self.state, 0, side,
                                      self.slots[3]["cards"][0]["id"])

    def test_picking_past_the_last_slot_is_refused(self):
        state = play_out(self.slots)
        self.assertTrue(cdraft_service.is_complete(state))
        self.assertIsNone(cdraft_service.current_side(state))
        with self.assertRaises(ValueError):
            cdraft_service.apply_pick(state, 11, "host", 1)

    def test_an_auto_pick_takes_the_stronger_card(self):
        slot = {"role": cdraft_service.ROLE_BATSMAN, "cards": [
            {"id": 1, "name": "A", "rating": 84, "bat_rating": 90,
             "bowl_rating": 10},
            {"id": 2, "name": "B", "rating": 85, "bat_rating": 80,
             "bowl_rating": 10}]}
        self.assertEqual(cdraft_service.auto_pick_id(slot), 2)

    def test_an_auto_pick_breaks_a_tie_on_the_rating_the_role_is_for(self):
        cards = [
            {"id": 1, "name": "A", "rating": 85, "bat_rating": 88,
             "bowl_rating": 60},
            {"id": 2, "name": "B", "rating": 85, "bat_rating": 70,
             "bowl_rating": 84}]
        self.assertEqual(
            cdraft_service.auto_pick_id(
                {"role": cdraft_service.ROLE_BATSMAN, "cards": cards}), 1)
        self.assertEqual(
            cdraft_service.auto_pick_id(
                {"role": cdraft_service.ROLE_BOWLER, "cards": cards}), 2)

    def test_auto_picks_only_count_while_they_are_consecutive(self):
        side = cdraft_service.current_side(self.state)
        cdraft_service.apply_pick(self.state, 0, side,
                                  self.slots[0]["cards"][0]["id"], auto=True)
        self.assertEqual(self.state["auto_streak"][side], 1)
        # The same side's next pick is a real one, which clears their streak.
        while cdraft_service.current_side(self.state) != side:
            other = cdraft_service.current_side(self.state)
            cdraft_service.apply_pick(
                self.state, self.state["index"], other,
                cdraft_service.current_slot(self.state)["cards"][0]["id"])
        cdraft_service.apply_pick(
            self.state, self.state["index"], side,
            cdraft_service.current_slot(self.state)["cards"][0]["id"])
        self.assertEqual(self.state["auto_streak"][side], 0)
        self.assertIsNone(cdraft_service.walked_away(self.state))

    def test_a_side_that_misses_enough_picks_in_a_row_is_reported(self):
        state = cdraft_service.new_state(self.slots)
        missed = 0
        while not cdraft_service.is_complete(state):
            side = cdraft_service.current_side(state)
            slot = cdraft_service.current_slot(state)
            auto = side == "host"
            cdraft_service.apply_pick(state, state["index"], side,
                                      slot["cards"][0]["id"], auto=auto)
            missed += 1 if auto else 0
            if missed >= cdraft_service.MAX_AUTO_STREAK:
                break
        self.assertEqual(cdraft_service.walked_away(state), "host")


class LegalXITests(unittest.TestCase):
    """The promise: whatever either captain does, both squads are legal XIs."""

    def _roster_pairs(self, state, side):
        """A squad in the ``(entry, player)`` shape the roster rulebook reads."""
        class _Row:
            def __init__(self, card):
                self.category = card["category"]
                self.bat_rating = card["bat_rating"]
                self.bowl_rating = card["bowl_rating"]
                self.rating = card["rating"]
                self.name = card["name"]
                self.id = card["id"]
                self.version = "Base"
                self.parent_player_id = None
        return [(None, _Row(c)) for c in state["squads"][side]]

    def test_every_draft_produces_two_legal_xis(self):
        pool = make_pool()
        for seed in range(25):
            slots = cdraft_service.build_slots(pool=pool, seed=seed)
            state = play_out(slots, seed=seed)
            for side in ("host", "target"):
                with self.subTest(seed=seed, side=side):
                    self.assertEqual(len(state["squads"][side]), 11)
                    self.assertEqual(cdraft_service.squad_shape(state, side), {
                        cdraft_service.ROLE_BATSMAN: 4,
                        cdraft_service.ROLE_KEEPER: 1,
                        cdraft_service.ROLE_ALLROUNDER: 2,
                        cdraft_service.ROLE_BOWLER: 4,
                    })
                    cards = cdraft_service.squad_cards(state, side)
                    ok, error = xi_rules.validate_challenge_xi(cards, 0, 11)
                    self.assertTrue(ok, error)
                    ok, errors = xi_rules.validate_roster_xi(
                        self._roster_pairs(state, side))
                    self.assertTrue(ok, errors)

    def test_a_greedy_captain_cannot_break_their_opponents_xi(self):
        """Always taking the best card on the board is the most adversarial
        strategy available; the opponent still fields a legal XI."""
        slots = cdraft_service.build_slots(pool=make_pool(), seed=99)
        state = play_out(slots, chooser=lambda slot, side: max(
            slot["cards"], key=lambda c: c["rating"]))
        for side in ("host", "target"):
            ok, error = xi_rules.validate_challenge_xi(
                cdraft_service.squad_cards(state, side), 0, 11)
            self.assertTrue(ok, error)

    def test_both_squads_finish_within_a_point_a_slot_of_each_other(self):
        """Equal roles at equal ratings means neither captain can be handed a
        materially stronger squad by the deal itself."""
        slots = cdraft_service.build_slots(pool=make_pool(), seed=42)
        state = play_out(slots, seed=42)
        gap = abs(cdraft_service.squad_strength(state, "host")
                  - cdraft_service.squad_strength(state, "target"))
        self.assertLessEqual(gap, cdraft_service.PAIR_SPREAD * 11)


class DraftCardTests(unittest.TestCase):
    """The shim has to be indistinguishable from a ChallengePlayer to every
    reader downstream of the draft."""

    def setUp(self):
        self.card = cdraft_service.DraftCard({
            "id": 77, "name": "R Jadeja", "country": "India",
            "category": "All-rounder", "rating": 87, "bat_rating": 82,
            "bowl_rating": 86, "bat_hand": "Left", "bowl_hand": "Left",
            "bowl_style": "Spin"})

    def test_the_xi_rulebook_reads_it(self):
        self.assertEqual(xi_rules.challenge_player_category(self.card),
                         "All-rounder")
        self.assertEqual(xi_rules.challenge_numeric_rating(self.card), 87.0)
        self.assertEqual(xi_rules.challenge_bat_rating(self.card), 82.0)
        self.assertEqual(xi_rules.challenge_bowl_rating(self.card), 86.0)
        self.assertTrue(xi_rules.challenge_is_bowling_option(self.card))
        self.assertFalse(xi_rules.challenge_is_wicket_keeper(self.card))
        self.assertFalse(xi_rules.challenge_is_overseas(self.card))

    def test_the_engine_conversion_reads_it(self):
        from services import cipl_match
        engine = cipl_match.cp_to_player_dict(self.card)
        self.assertEqual(engine["name"], "R Jadeja")
        self.assertEqual(engine["rating"], 87)
        self.assertEqual(engine["bat_rating"], 82)
        self.assertEqual(engine["bowl_rating"], 86)
        self.assertEqual(engine["bat_hand"], "Left")
        self.assertEqual(engine["bowl_style"], "Spin")
        # Both ids are the master card's, so Player-of-the-Match and global
        # player stats land on the real card.
        self.assertEqual(engine["roster_id"], 77)
        self.assertEqual(engine["player_id"], 77)

    def test_a_keeper_is_recognised_as_one(self):
        keeper = cdraft_service.DraftCard({
            "id": 1, "name": "K", "category": "Wicket Keeper", "rating": 85,
            "bat_rating": 84, "bowl_rating": 5})
        self.assertTrue(xi_rules.challenge_is_wicket_keeper(keeper))


class MatchHandoffTests(unittest.TestCase):
    """The drafted squad has to reach the match engine with no league behind it."""

    def test_the_engine_xi_is_built_from_the_draft_in_the_captains_order(self):
        from handlers.cipl_play import build_xi_from_draft

        slots = cdraft_service.build_slots(pool=make_pool(), seed=4)
        state = play_out(slots, seed=4)
        order = cdraft_service.squad_in_batting_order(state, "host")
        draft = {"mode": "cdraft", "cdraft": state,
                 "xi_selections": {"host": {"player_ids": order,
                                            "confirmed": True}}}
        # ``session`` is None on purpose: a /cdraft XI must not need one.
        xi = build_xi_from_draft(None, draft, "host")
        self.assertEqual(len(xi), 11)
        self.assertEqual([p["roster_id"] for p in xi], order,
                         "the XI must come back in the captain's batting order")
        by_id = {int(c["id"]): c for c in state["squads"]["host"]}
        for player in xi:
            self.assertEqual(player["name"], by_id[player["roster_id"]]["name"])
            self.assertEqual(player["rating"],
                             by_id[player["roster_id"]]["rating"])


class BattingOrderTests(unittest.TestCase):

    def test_the_draft_order_becomes_a_believable_line_up(self):
        slots = cdraft_service.build_slots(pool=make_pool(), seed=8)
        state = play_out(slots, seed=8)
        ordered_ids = cdraft_service.squad_in_batting_order(state, "host")
        self.assertEqual(len(ordered_ids), 11)
        self.assertEqual(sorted(ordered_ids),
                         sorted(int(c["id"]) for c in state["squads"]["host"]))
        by_id = {int(c["id"]): c for c in state["squads"]["host"]}
        roles = [cdraft_service.normalise_role(by_id[pid]["category"])
                 for pid in ordered_ids]
        # Specialist bowlers bat last; nobody bats after them.
        first_bowler = roles.index(cdraft_service.ROLE_BOWLER)
        self.assertTrue(all(r == cdraft_service.ROLE_BOWLER
                            for r in roles[first_bowler:]))


class FakeBot:
    """Just enough Telegram to run the handler: every message gets an id, and
    edits are recorded against it."""

    def __init__(self):
        self.sent = []
        self.edits = {}
        self._next_id = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        message = SimpleNamespace(message_id=self._next_id, chat_id=chat_id,
                                  text=text, reply_markup=kwargs.get("reply_markup"))
        self.sent.append(message)
        return message

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits[message_id] = text
        return True

    async def edit_message_caption(self, *args, **kwargs):
        return True

    async def delete_message(self, chat_id, message_id):
        return True


class FakeJobQueue:
    """Records scheduled jobs instead of running them, so a test can fire one
    on demand (the pick clock) and assert on the rest."""

    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, name=None, data=None, **kwargs):
        job = SimpleNamespace(callback=callback, when=when, name=name, data=data)
        job.schedule_removal = lambda j=job: self.jobs.remove(j) if j in self.jobs else None
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name]


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}
        self.job_queue = FakeJobQueue()


class FakeMessage:
    def __init__(self, chat_id, reply_to=None):
        self.chat_id = chat_id
        self.message_id = 1
        self.reply_to_message = reply_to
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies) + 500, text=text)


class FakeQuery:
    def __init__(self, data, tg_id, chat_id):
        self.data = data
        self.from_user = SimpleNamespace(id=tg_id, username=f"u{tg_id}",
                                         first_name=f"U{tg_id}", is_bot=False)
        self.message = SimpleNamespace(message_id=7, chat_id=chat_id)
        self.answers = []
        self.edits = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs.get("show_alert", False)))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def _fake_user(tg_id, uid):
    return SimpleNamespace(id=uid, telegram_id=tg_id, username=f"u{tg_id}",
                           first_name=f"U{tg_id}", team_name=None)


class CdraftFlowTests(unittest.IsolatedAsyncioTestCase):
    """The command, the join and eleven picks, driven through the real handlers."""

    HOST_TG, GUEST_TG, CHAT = 111, 222, -100

    def setUp(self):
        from handlers import cdraft as module
        self.module = module
        self.context = FakeContext()
        users = {self.HOST_TG: _fake_user(self.HOST_TG, 1),
                 self.GUEST_TG: _fake_user(self.GUEST_TG, 2)}
        self._patches = [
            patch.object(module, "get_session",
                         lambda: SimpleNamespace(close=lambda: None)),
            patch.object(module, "sync_telegram_user",
                         lambda session, tg: users.get(getattr(tg, "id", None))),
            patch.object(module, "_active_match_in_chat", lambda *a, **k: None),
            patch.object(module, "_active_cric_match_in_chat", lambda *a, **k: None),
            patch.object(module, "_active_match_for_user", lambda *a, **k: None),
            patch.object(module, "_active_cric_match_for_user", lambda *a, **k: None),
            patch.object(module, "_user_label",
                         lambda u: f"@{u.username}"),
            patch("services.player_cache.get_all_active", make_pool),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    async def _open_lobby(self):
        update = SimpleNamespace(
            effective_message=FakeMessage(self.CHAT),
            effective_chat=SimpleNamespace(id=self.CHAT, type="supergroup"),
            effective_user=SimpleNamespace(id=self.HOST_TG, username="u111",
                                           first_name="U111", is_bot=False))
        await self.module.cdraft_handler(update, self.context)
        return update

    def _draft(self):
        """The live draft dict — found under the Challenge League's own key,
        which is the whole point: /cdraft builds a league draft."""
        from handlers.challenge import _challenge_team_draft_key
        prefix = _challenge_team_draft_key("")
        key = next(k for k in self.context.bot_data if k.startswith(prefix))
        return self.context.bot_data[key]

    async def _join(self, tg_id=None):
        draft = self._draft()
        query = FakeQuery(f"cdj_{draft['draft_id']}", tg_id or self.GUEST_TG,
                          self.CHAT)
        await self.module.cdraft_join_callback(
            SimpleNamespace(callback_query=query), self.context)
        return query

    async def _pick(self, tg_id, card_index=0):
        draft = self._draft()
        state = draft["cdraft"]
        slot = cdraft_service.current_slot(state)
        query = FakeQuery(
            f"cdp_{draft['draft_id']}_{state['index']}"
            f"_{slot['cards'][card_index]['id']}", tg_id, self.CHAT)
        await self.module.cdraft_pick_callback(
            SimpleNamespace(callback_query=query), self.context)
        return query

    async def test_the_command_opens_a_lobby_and_locks_the_chat(self):
        from handlers.challenge import _challenge_draft_chat_key
        update = await self._open_lobby()
        self.assertIn("CHALLENGE DRAFT", update.effective_message.replies[0])
        draft = self._draft()
        self.assertEqual(draft["turn"], "join")
        self.assertEqual(draft["mode"], "cdraft")
        self.assertEqual(
            self.context.bot_data[_challenge_draft_chat_key(self.CHAT)],
            draft["draft_id"])

    async def test_a_private_chat_is_turned_away(self):
        message = FakeMessage(self.CHAT)
        await self.module.cdraft_handler(SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=self.CHAT, type="private"),
            effective_user=SimpleNamespace(id=self.HOST_TG, username="u111",
                                           first_name="U111", is_bot=False)),
            self.context)
        self.assertIn("two-player mode", message.replies[0])
        self.assertFalse(self.context.bot_data)

    async def test_the_host_cannot_join_their_own_draft(self):
        await self._open_lobby()
        query = await self._join(tg_id=self.HOST_TG)
        self.assertTrue(query.answers[-1][1], "should be an alert")
        self.assertEqual(self._draft()["turn"], "join")

    async def test_joining_starts_the_draft_and_posts_the_first_slot(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        self.assertEqual(draft["turn"], "draft")
        self.assertEqual(draft["target_tg_id"], self.GUEST_TG)
        self.assertEqual(draft["target_team"], "@u222 XI")
        self.assertIn("Slot 1/11", self.context.bot.sent[-1].text)
        # The host is on the clock, and only their two cards are offered.
        buttons = self.context.bot.sent[-1].reply_markup.inline_keyboard
        self.assertEqual(len(buttons), 2)
        # The abandoned-setup backstop must not be left running over an active
        # draft — eleven picks can outlast it.
        self.assertFalse(self.context.job_queue.get_jobs_by_name(
            f"cl_draft_{draft['draft_id']}"))

    async def test_the_wrong_captain_is_refused_and_the_slot_stands(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        clock = self.context.job_queue.get_jobs_by_name(
            f"cdraft_pick_{draft['draft_id']}")
        query = await self._pick(self.GUEST_TG)     # host is on the clock
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(draft["cdraft"]["index"], 0)
        # The host's clock is still the one running — a stray tap by the other
        # captain must not cancel or reset it.
        self.assertEqual(self.context.job_queue.get_jobs_by_name(
            f"cdraft_pick_{draft['draft_id']}"), clock)

    async def test_a_stale_button_from_an_earlier_slot_is_refused(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        state = draft["cdraft"]
        stale = f"cdp_{draft['draft_id']}_0_{state['slots'][0]['cards'][0]['id']}"
        await self._pick(self.HOST_TG)              # slot 0 is now done
        query = FakeQuery(stale, self.HOST_TG, self.CHAT)
        await self.module.cdraft_pick_callback(
            SimpleNamespace(callback_query=query), self.context)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(state["index"], 1, "the stale tap must not re-pick")

    async def test_a_pick_gives_the_other_card_to_the_opponent(self):
        await self._open_lobby()
        await self._join()
        state = self._draft()["cdraft"]
        taken, given = state["slots"][0]["cards"]
        await self._pick(self.HOST_TG, card_index=0)
        self.assertEqual(state["squads"]["host"], [taken])
        self.assertEqual(state["squads"]["target"], [given])
        self.assertEqual(state["index"], 1)

    async def test_a_full_draft_ends_on_the_pitch_prompt(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        state = draft["cdraft"]
        tg_for = {"host": self.HOST_TG, "target": self.GUEST_TG}
        while not cdraft_service.is_complete(state):
            await self._pick(tg_for[cdraft_service.current_side(state)])

        self.assertEqual(draft["turn"], "complete",
                         "the league flow only takes over at 'complete'")
        texts = [m.text for m in self.context.bot.sent]
        self.assertTrue(any("DRAFT COMPLETE" in t for t in texts))
        self.assertTrue(any("Choose the pitch" in t for t in texts))
        # The pitch card carries the league flow's own buttons from here.
        pitch_markup = self.context.bot.sent[-1].reply_markup
        self.assertTrue(all(
            button.callback_data.startswith(f"cl_pitch_{draft['draft_id']}_")
            for row in pitch_markup.inline_keyboard for button in row))
        # Both squads are complete, legal and evenly matched.
        for side in ("host", "target"):
            cards = cdraft_service.squad_cards(state, side)
            self.assertEqual(len(cards), 11)
            ok, error = xi_rules.validate_challenge_xi(cards, 0, 11)
            self.assertTrue(ok, error)
        # The backstop is back on now that picking is over.
        self.assertTrue(self.context.job_queue.get_jobs_by_name(
            f"cl_draft_{draft['draft_id']}"))

    async def test_a_lapsed_pick_is_auto_picked_and_the_draft_carries_on(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        state = draft["cdraft"]
        expected = cdraft_service.auto_pick_id(state["slots"][0])
        timer = self.context.job_queue.get_jobs_by_name(
            f"cdraft_pick_{draft['draft_id']}")[0]
        self.context.job = timer
        await timer.callback(SimpleNamespace(
            job=timer, bot=self.context.bot, bot_data=self.context.bot_data,
            job_queue=self.context.job_queue))
        self.assertEqual(state["index"], 1)
        self.assertEqual([c["id"] for c in state["squads"]["host"]], [expected])
        self.assertIn("Slot 2/11", self.context.bot.sent[-1].text)

    async def test_a_captain_who_misses_enough_picks_ends_the_draft(self):
        from handlers.challenge import _challenge_draft_chat_key
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        draft_id = draft["draft_id"]
        for _ in range(cdraft_service.MAX_AUTO_STREAK * 3):
            jobs = self.context.job_queue.get_jobs_by_name(f"cdraft_pick_{draft_id}")
            if not jobs:
                break
            timer = jobs[0]
            await timer.callback(SimpleNamespace(
                job=timer, bot=self.context.bot, bot_data=self.context.bot_data,
                job_queue=self.context.job_queue))
        self.assertIn("Draft cancelled", self.context.bot.sent[-1].text)
        # The chat is free again for the next challenge.
        self.assertNotIn(_challenge_draft_chat_key(self.CHAT), self.context.bot_data)

    async def test_the_host_can_cancel_a_lobby_nobody_joined(self):
        from handlers.challenge import _challenge_draft_chat_key
        await self._open_lobby()
        draft_id = self._draft()["draft_id"]
        query = FakeQuery(f"cdc_{draft_id}", self.HOST_TG, self.CHAT)
        await self.module.cdraft_cancel_callback(
            SimpleNamespace(callback_query=query), self.context)
        self.assertIn("cancelled", query.edits[-1])
        self.assertNotIn(_challenge_draft_chat_key(self.CHAT), self.context.bot_data)

    async def test_only_the_host_can_cancel(self):
        await self._open_lobby()
        draft_id = self._draft()["draft_id"]
        query = FakeQuery(f"cdc_{draft_id}", self.GUEST_TG, self.CHAT)
        await self.module.cdraft_cancel_callback(
            SimpleNamespace(callback_query=query), self.context)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(self._draft()["turn"], "join")

    async def test_a_second_challenge_cannot_open_over_a_live_draft(self):
        await self._open_lobby()
        second = FakeMessage(self.CHAT)
        await self.module.cdraft_handler(SimpleNamespace(
            effective_message=second,
            effective_chat=SimpleNamespace(id=self.CHAT, type="supergroup"),
            effective_user=SimpleNamespace(id=self.GUEST_TG, username="u222",
                                           first_name="U222", is_bot=False)),
            self.context)
        self.assertIn("already being set up", second.replies[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
