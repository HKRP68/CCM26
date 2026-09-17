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
              categories=("Batsman", "Wicket Keeper", "All-rounder", "Bowler"),
              versions=("Base",)):
    """A synthetic catalogue: ``per_rating`` cards of every category at every
    rating in every version, each with a distinct name.

    A card is a base card when it has no ``parent_player_id``, which is what the
    real table looks like — so anything not versioned "Base" here also gets a
    parent, as the catalogue's variant rows do."""
    pool, pid = [], 1
    for version in versions:
        is_base = version.casefold() in ("", "base")
        for category in categories:
            for rating in ratings:
                for copy in range(per_rating):
                    pool.append({
                        "id": pid,
                        "name": f"{category[:3]}{rating}_{copy}_{version}",
                        "version": version,
                        "parent_player_id": None if is_base else 1,
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

    def test_every_slot_rating_is_drawn_from_inside_the_band(self):
        for seed in range(20):
            rng = random.Random(seed)
            ratings = cdraft_service.slot_ratings(top=99, bottom=84, rng=rng)
            with self.subTest(seed=seed):
                self.assertEqual(len(ratings), 11)
                self.assertTrue(all(84 <= r <= 99 for r in ratings), ratings)

    def test_the_ratings_are_random_not_a_ladder(self):
        """The reported bug: the two settings are a min and a max, so slot 1
        must not always be the ceiling and slot 11 must not always be the
        floor — which is exactly what stepping evenly between them produced."""
        firsts, lasts = set(), set()
        for seed in range(40):
            ratings = cdraft_service.slot_ratings(top=99, bottom=84,
                                                  rng=random.Random(seed))
            firsts.add(ratings[0])
            lasts.add(ratings[-1])
        self.assertGreater(len(firsts), 1, "slot 1 is pinned to one rating")
        self.assertGreater(len(lasts), 1, "slot 11 is pinned to one rating")
        self.assertNotEqual(firsts, {99}, "slot 1 is always the maximum")
        self.assertNotEqual(lasts, {84}, "slot 11 is always the minimum")

    def test_a_backwards_band_still_deals(self):
        ratings = cdraft_service.slot_ratings(top=80, bottom=90,
                                              rng=random.Random(1))
        self.assertTrue(all(80 <= r <= 90 for r in ratings), ratings)

    def test_a_single_point_band_deals_that_rating(self):
        ratings = cdraft_service.slot_ratings(top=85, bottom=85,
                                              rng=random.Random(1))
        self.assertEqual(ratings, [85] * 11)


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

    def test_the_pair_spread_setting_reaches_the_deal(self):
        """At 0 a slot may only offer two players on the same OVR."""
        pool = make_pool()
        for spread in (0, 1, 3):
            slots = cdraft_service.build_slots(pool=pool, seed=6, spread=spread)
            for index, slot in enumerate(slots):
                first, second = slot["cards"]
                with self.subTest(spread=spread, slot=index + 1):
                    self.assertLessEqual(
                        abs(first["rating"] - second["rating"]), spread)

    def test_the_search_band_comes_from_the_settings_not_the_draws(self):
        """The targets are random and unsorted, so the lowest and highest of
        them are not the band's ends. Using them would quietly shrink the
        search to whatever happened to land in slots 1 and 11."""
        # Nothing inside 78-88 at all; everything sits at 95+. A band that came
        # from the draws could not reach it, and the deal would fail.
        pool = make_pool(ratings=range(95, 99))
        slots = cdraft_service.build_slots(pool=pool, seed=4, top=88, bottom=78)
        self.assertEqual(len(slots), 11)
        self.assertTrue(all(95 <= c["rating"] <= 98
                            for s in slots for c in s["cards"]))

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


class AllowedVersionTests(unittest.TestCase):
    """The tick list an admin sets on the website / with /cdraftset."""

    def test_no_list_allows_everything(self):
        card = {"name": "X", "version": "TOTY", "parent_player_id": 9}
        self.assertTrue(cdraft_service.version_allowed(card, None))
        self.assertTrue(cdraft_service.version_allowed(card, []))

    def test_an_exact_label_matches_case_insensitively(self):
        card = {"name": "X", "version": "IPL 2026", "parent_player_id": 9}
        self.assertTrue(cdraft_service.version_allowed(card, ["ipl 2026"]))
        self.assertTrue(cdraft_service.version_allowed(card, ["IPL 2026"]))
        self.assertFalse(cdraft_service.version_allowed(card, ["IPL 2025"]))

    def test_base_covers_every_way_a_base_card_is_written(self):
        """A base card may carry the label, an empty string, or nothing at all
        — what actually marks it is having no parent card."""
        for version in ("Base", "base", "", None):
            card = {"name": "X", "version": version, "parent_player_id": None}
            with self.subTest(version=version):
                self.assertTrue(cdraft_service.is_base_card(card))
                self.assertTrue(cdraft_service.version_allowed(card, ["Base"]))
                self.assertFalse(cdraft_service.version_allowed(card, ["Legend"]))

    def test_a_card_with_a_parent_is_never_a_base_card(self):
        card = {"name": "X", "version": "", "parent_player_id": 42}
        self.assertFalse(cdraft_service.is_base_card(card))
        self.assertFalse(cdraft_service.version_allowed(card, ["Base"]))

    def test_the_codec_round_trips_and_folds_duplicates(self):
        raw = cdraft_service.dump_allowed_versions(["Base", "base", "Legend"])
        self.assertEqual(cdraft_service.parse_allowed_versions(raw),
                         ["Base", "Legend"])

    def test_an_unset_or_empty_list_means_every_version(self):
        for raw in (None, "", "   ", "[]", []):
            with self.subTest(raw=raw):
                self.assertIsNone(cdraft_service.parse_allowed_versions(raw))
        self.assertIsNone(cdraft_service.dump_allowed_versions([]))

    def test_a_comma_separated_value_typed_by_hand_still_works(self):
        self.assertEqual(
            cdraft_service.parse_allowed_versions(" Base , Legend "),
            ["Base", "Legend"])

    def test_unreadable_json_falls_back_to_allowing_everything(self):
        """A tick list matching no card is a mode that can never start — the
        worse of the two answers to a corrupt value."""
        self.assertIsNone(cdraft_service.parse_allowed_versions('["Base"'))
        self.assertIsNone(cdraft_service.parse_allowed_versions("{oops}"))


class VersionFilteredDraftTests(unittest.TestCase):

    def setUp(self):
        self.pool = make_pool(versions=("Base", "Legend", "TOTY"))

    def test_only_ticked_versions_are_ever_dealt(self):
        for seed in range(8):
            slots = cdraft_service.build_slots(
                pool=self.pool, seed=seed, versions=["Base", "Legend"])
            dealt = {c["version"] for slot in slots for c in slot["cards"]}
            with self.subTest(seed=seed):
                self.assertEqual(dealt, {"Base", "Legend"})

    def test_a_single_ticked_version_still_fills_all_eleven_slots(self):
        slots = cdraft_service.build_slots(pool=self.pool, seed=3,
                                           versions=["TOTY"])
        self.assertEqual(len(slots), 11)
        self.assertTrue(all(c["version"] == "TOTY"
                            for slot in slots for c in slot["cards"]))

    def test_the_version_list_is_never_relaxed_to_fill_a_slot(self):
        """Keepers exist in the catalogue, but not in the ticked version. The
        draft refuses rather than reaching for an untickled edition — a squad
        with no keeper could not field a legal XI."""
        pool = (make_pool(categories=("Batsman", "All-rounder", "Bowler"),
                          versions=("Legend",))
                + make_pool(categories=("Wicket Keeper",), versions=("TOTY",)))
        with self.assertRaises(cdraft_service.CdraftPoolError) as caught:
            cdraft_service.build_slots(pool=pool, seed=1, versions=["Legend"])
        self.assertIn("Wicket Keeper", str(caught.exception))
        self.assertIn("Legend", str(caught.exception))
        # ...and the same pool deals fine once that edition is ticked too.
        self.assertEqual(len(cdraft_service.build_slots(
            pool=pool, seed=1, versions=["Legend", "TOTY"])), 11)

    def test_a_thin_rating_borrows_from_inside_the_band_before_leaving_it(self):
        """The floor is a floor, not just slot 11's target. With nothing in the
        80s, slot 11 reaches UP into the allowed band rather than down to the
        70-rated cards that sit below the admin's floor."""
        pool = (make_pool(ratings=range(86, 90), versions=("Base",))
                + make_pool(ratings=range(70, 74), versions=("Base",)))
        slots = cdraft_service.build_slots(pool=pool, seed=2, top=88, bottom=80)
        dealt = [c["rating"] for slot in slots for c in slot["cards"]]
        self.assertTrue(all(r >= 80 for r in dealt),
                        f"dealt below the floor: {sorted(dealt)[:4]}")

    def test_it_leaves_the_band_only_when_the_band_is_empty(self):
        """Nothing at all inside 80-88: rather than fail, it reaches outside."""
        pool = make_pool(ratings=range(70, 76), versions=("Base",))
        slots = cdraft_service.build_slots(pool=pool, seed=2, top=88, bottom=80)
        self.assertEqual(len(slots), 11)
        self.assertTrue(all(70 <= c["rating"] <= 75
                            for slot in slots for c in slot["cards"]))


class FeasibilityTests(unittest.TestCase):
    """What warns the admin before a group finds out."""

    def test_the_requirement_is_two_different_players_per_slot(self):
        self.assertEqual(cdraft_service.ROLE_REQUIREMENTS, {
            cdraft_service.ROLE_BATSMAN: 8,
            cdraft_service.ROLE_KEEPER: 2,
            cdraft_service.ROLE_ALLROUNDER: 4,
            cdraft_service.ROLE_BOWLER: 8,
        })

    def test_a_healthy_pool_reports_no_shortfall(self):
        feasibility = cdraft_service.pool_feasibility(make_pool(), None)
        self.assertEqual(cdraft_service.feasibility_shortfalls(feasibility), [])

    def test_versions_narrow_what_counts(self):
        pool = make_pool(ratings=range(80, 82), per_rating=1,
                         versions=("Base", "Legend"))
        both = cdraft_service.pool_feasibility(pool, None)
        base_only = cdraft_service.pool_feasibility(pool, ["Base"])
        self.assertEqual(both[cdraft_service.ROLE_KEEPER][0], 4)
        self.assertEqual(base_only[cdraft_service.ROLE_KEEPER][0], 2)

    def test_five_cards_of_one_cricketer_are_one_usable_pick(self):
        """The deal de-dupes by name, so the count has to as well."""
        pool = [{"id": i, "name": "MS Dhoni", "version": v,
                 "parent_player_id": None if v == "Base" else 1,
                 "category": "Wicket Keeper", "rating": 88}
                for i, v in enumerate(("Base", "Gold", "Icon", "TOTY", "Prime"),
                                      start=1)]
        feasibility = cdraft_service.pool_feasibility(pool, None)
        self.assertEqual(feasibility[cdraft_service.ROLE_KEEPER], (1, 2))
        self.assertIn(cdraft_service.ROLE_KEEPER,
                      [r for r, _h, _n in
                       cdraft_service.feasibility_shortfalls(feasibility)])


class SettingsTests(unittest.TestCase):
    """load_settings reads GameConfig and falls back to the env defaults."""

    def test_an_empty_config_gives_the_env_defaults(self):
        settings = cdraft_service.load_settings({})
        self.assertEqual(settings["rating_min"], cdraft_service.RATING_BOTTOM)
        self.assertEqual(settings["rating_max"], cdraft_service.RATING_TOP)
        self.assertEqual(settings["pair_spread"], cdraft_service.PAIR_SPREAD)
        self.assertIsNone(settings["versions"])

    def test_stored_values_win(self):
        settings = cdraft_service.load_settings({
            "cdraft_rating_min": 80, "cdraft_rating_max": 92,
            "cdraft_pair_spread": 2,
            "cdraft_versions_json": '["Base", "Legend"]'})
        self.assertEqual(settings["rating_min"], 80)
        self.assertEqual(settings["rating_max"], 92)
        self.assertEqual(settings["pair_spread"], 2)
        self.assertEqual(settings["versions"], ["Base", "Legend"])

    def test_the_pair_spread_is_clamped(self):
        """It is the one mechanic that makes the two squads provably fair, so a
        typed 50 must not quietly become a slot that hands somebody the better
        card."""
        self.assertEqual(
            cdraft_service.load_settings({"cdraft_pair_spread": 50})["pair_spread"],
            cdraft_service.SPREAD_LIMIT_HIGH)
        self.assertEqual(
            cdraft_service.load_settings({"cdraft_pair_spread": -3})["pair_spread"],
            cdraft_service.SPREAD_LIMIT_LOW)

    def test_a_backwards_range_is_repaired_not_rejected(self):
        settings = cdraft_service.load_settings({
            "cdraft_rating_min": 92, "cdraft_rating_max": 80})
        self.assertEqual((settings["rating_min"], settings["rating_max"]),
                         (80, 92))

    def test_out_of_range_and_junk_values_are_survivable(self):
        settings = cdraft_service.load_settings({
            "cdraft_rating_min": -5, "cdraft_rating_max": 900})
        self.assertEqual(settings["rating_min"],
                         cdraft_service.RATING_FLOOR_LIMIT)
        self.assertEqual(settings["rating_max"],
                         cdraft_service.RATING_CEILING_LIMIT)
        junk = cdraft_service.load_settings({"cdraft_rating_min": "eighty"})
        self.assertEqual(junk["rating_min"], cdraft_service.RATING_BOTTOM)

    def test_the_summary_names_the_pool(self):
        self.assertEqual(
            cdraft_service.settings_summary(
                {"rating_min": 80, "rating_max": 90,
                 "versions": ["Base", "Legend"]}),
            "80–90 OVR · Base, Legend")
        self.assertEqual(
            cdraft_service.settings_summary(
                {"rating_min": 78, "rating_max": 88, "versions": None}),
            "78–88 OVR · all versions")


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


class ButtonOwnershipTests(unittest.TestCase):
    """Every button this mode posts must survive the global owner guard.

    ``services/button_access.py`` blocks any inline button pressed by somebody
    other than the user whose update posted the message, unless the callback
    prefix is listed as shared. /cdraft shipped without that listing, and the
    guest's Join tap was refused — the mode could not start at all.

    Rather than restate a prefix list that can drift, this walks the keyboards
    the flow actually builds. A button added anywhere in it then fails here
    instead of shipping unpressable.
    """

    @staticmethod
    def _callback_data(markup):
        return [button.callback_data
                for row in markup.inline_keyboard for button in row
                if getattr(button, "callback_data", None)]

    def _assert_all_shared(self, markup, what):
        from services.button_access import is_shared_callback_data
        data = self._callback_data(markup)
        self.assertTrue(data, f"{what} has no callback buttons to check")
        for value in data:
            with self.subTest(what=what, callback_data=value):
                self.assertTrue(
                    is_shared_callback_data(value),
                    f"{what} button {value!r} is not in "
                    f"SHARED_CALLBACK_PREFIXES — the other captain's tap on it "
                    f"will be refused with 'This button is not for you'")

    def test_the_lobby_and_slot_cards_are_pressable_by_both_captains(self):
        from handlers import cdraft as module
        slots = cdraft_service.build_slots(pool=make_pool(), seed=1)
        self._assert_all_shared(module._lobby_keyboard(123456), "the lobby")
        for index, slot in enumerate(slots):
            self._assert_all_shared(module._slot_keyboard(123456, index, slot),
                                    f"slot {index + 1}")

    def test_the_challenge_league_leg_is_pressable_by_both_captains(self):
        """Everything from the pitch step on, which /cdraft hands over to."""
        from handlers import challenge
        draft = {"draft_id": 123456, "host_team": "@a XI",
                 "target_team": "@b XI", "vs_bot": False}
        players = [SimpleNamespace(id=i, name=f"P{i}") for i in range(1, 12)]
        cases = {
            "the pitch picker": challenge._pitch_keyboard(123456, allow_deny=False),
            "the XI prompt": challenge._challenge_xi_keyboard(123456, draft),
            "the XI picker": challenge._challenge_xi_player_keyboard(
                123456, "host", players, [], saved_available=True,
                saved_label="✅ Use draft order"),
            "the XI picker, mid-selection": challenge._challenge_xi_player_keyboard(
                123456, "host", players, [p.id for p in players]),
            "the confirmed XI view": challenge._challenge_xi_postselect_keyboard(
                123456, "host", confirmed=True),
            "the unconfirmed XI view": challenge._challenge_xi_postselect_keyboard(
                123456, "host", confirmed=False),
            "the match-ready card": challenge._challenge_start_match_keyboard(123456),
        }
        for what, markup in cases.items():
            self._assert_all_shared(markup, what)

    def test_a_slot_card_carries_a_way_out_of_the_draft(self):
        from handlers import cdraft as module
        slot = cdraft_service.build_slots(pool=make_pool(), seed=1)[0]
        data = self._callback_data(module._slot_keyboard(123456, 0, slot))
        self.assertEqual(sum(1 for d in data if d.startswith("cdc_")), 1)
        self.assertEqual(sum(1 for d in data if d.startswith("cdp_")), 2)


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

    # STRANGER is a third person in the group who is not in the draft. Once the
    # buttons are shared with the owner guard, these handlers are the only thing
    # keeping them out, so every one of them is checked against this id.
    HOST_TG, GUEST_TG, STRANGER_TG, CHAT = 111, 222, 333, -100

    def setUp(self):
        from handlers import cdraft as module
        self.module = module
        self.context = FakeContext()
        users = {self.HOST_TG: _fake_user(self.HOST_TG, 1),
                 self.GUEST_TG: _fake_user(self.GUEST_TG, 2),
                 self.STRANGER_TG: _fake_user(self.STRANGER_TG, 3)}
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
            # The admin's pool settings normally come from GameConfig; pin them
            # so these tests never depend on ambient database state.
            patch.object(cdraft_service, "load_settings",
                         lambda config=None: {"rating_min": 78,
                                              "rating_max": 88,
                                              "pair_spread": 1,
                                              "versions": None}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    async def _open_lobby(self, invite_tg=None):
        """Open a lobby. ``invite_tg`` sends /cdraft as a reply to that person,
        which is how a draft is opened for one named player."""
        reply_to = None
        if invite_tg is not None:
            reply_to = SimpleNamespace(from_user=SimpleNamespace(
                id=invite_tg, username=f"u{invite_tg}",
                first_name=f"U{invite_tg}", is_bot=False))
        update = SimpleNamespace(
            effective_message=FakeMessage(self.CHAT, reply_to=reply_to),
            effective_chat=SimpleNamespace(id=self.CHAT, type="supergroup"),
            effective_user=SimpleNamespace(id=self.HOST_TG, username="u111",
                                           first_name="U111", is_bot=False))
        await self.module.cdraft_handler(update, self.context)
        return update

    async def _cancel(self, tg_id):
        draft = self._draft()
        query = FakeQuery(f"cdc_{draft['draft_id']}", tg_id, self.CHAT)
        await self.module.cdraft_cancel_callback(
            SimpleNamespace(callback_query=query), self.context)
        return query

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
        # Both captains see what they will be drafting from before anyone joins.
        self.assertIn("78–88 OVR", update.effective_message.replies[0])
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
        # Teams are named after the captains, with a short code the live match
        # header can show as-is.
        self.assertEqual(draft["target_team"], "U222 Draft XI")
        self.assertEqual(draft["team_codes"]["U222 Draft XI"], "U222")
        self.assertEqual(draft["host_team"], "U111 Draft XI")
        self.assertEqual(draft["team_codes"]["U111 Draft XI"], "U111")
        self.assertIn("Slot 1/11", self.context.bot.sent[-1].text)
        # Exactly the slot's two cards are offered, plus the way out.
        data = [b.callback_data
                for row in self.context.bot.sent[-1].reply_markup.inline_keyboard
                for b in row]
        self.assertEqual(sum(1 for d in data if d.startswith("cdp_")), 2)
        self.assertEqual(sum(1 for d in data if d.startswith("cdc_")), 1)
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
        query = await self._cancel(self.HOST_TG)
        self.assertIn("cancelled", query.edits[-1])
        self.assertNotIn(_challenge_draft_chat_key(self.CHAT), self.context.bot_data)

    async def test_only_the_host_can_cancel_the_lobby(self):
        await self._open_lobby()
        for presser in (self.GUEST_TG, self.STRANGER_TG):
            with self.subTest(presser=presser):
                query = await self._cancel(presser)
                self.assertTrue(query.answers[-1][1])
                self.assertEqual(self._draft()["turn"], "join")

    async def test_either_captain_can_cancel_a_draft_in_progress(self):
        from handlers.challenge import _challenge_draft_chat_key
        for canceller in (self.HOST_TG, self.GUEST_TG):
            with self.subTest(canceller=canceller):
                self.context = FakeContext()
                await self._open_lobby()
                await self._join()
                await self._pick(self.HOST_TG)      # a slot or two in
                query = await self._cancel(canceller)
                self.assertIn("cancelled", query.edits[-1].lower())
                # The chat is free again and the draft is gone.
                self.assertNotIn(_challenge_draft_chat_key(self.CHAT),
                                 self.context.bot_data)
                self.assertFalse([k for k in self.context.bot_data
                                  if k.startswith("challenge_team_draft_")])
                # ...and the pick clock went with it.
                self.assertFalse(self.context.job_queue.jobs)

    async def test_a_bystander_cannot_cancel_a_draft_in_progress(self):
        await self._open_lobby()
        await self._join()
        query = await self._cancel(self.STRANGER_TG)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(self._draft()["turn"], "draft")

    async def test_cancelling_says_it_once_not_twice(self):
        """The slot card the button sits on is edited; a second chat message
        saying the same thing would just be noise."""
        await self._open_lobby()
        await self._join()
        before = len(self.context.bot.sent)
        await self._cancel(self.GUEST_TG)
        self.assertEqual(len(self.context.bot.sent), before)

    async def test_the_draft_cannot_be_cancelled_once_the_match_is_being_set_up(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        state = draft["cdraft"]
        tg_for = {"host": self.HOST_TG, "target": self.GUEST_TG}
        while not cdraft_service.is_complete(state):
            await self._pick(tg_for[cdraft_service.current_side(state)])
        self.assertEqual(draft["turn"], "complete")
        query = await self._cancel(self.HOST_TG)
        self.assertTrue(query.answers[-1][1])
        self.assertIn("already being set up", query.answers[-1][0])

    async def test_a_bystander_cannot_make_a_pick(self):
        """Once the owner guard shares these buttons, this check is the only
        thing keeping the rest of the group off the draft."""
        await self._open_lobby()
        await self._join()
        state = self._draft()["cdraft"]
        query = await self._pick(self.STRANGER_TG)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(state["index"], 0)
        self.assertEqual(state["squads"]["host"], [])
        self.assertEqual(state["squads"]["target"], [])

    async def test_an_invited_lobby_admits_only_the_invitee(self):
        await self._open_lobby(invite_tg=self.GUEST_TG)
        query = await self._join(tg_id=self.STRANGER_TG)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(self._draft()["turn"], "join")
        await self._join(tg_id=self.GUEST_TG)
        self.assertEqual(self._draft()["turn"], "draft")

    async def test_an_open_lobby_admits_the_first_taker(self):
        await self._open_lobby()
        await self._join(tg_id=self.STRANGER_TG)
        draft = self._draft()
        self.assertEqual(draft["turn"], "draft")
        self.assertEqual(draft["target_tg_id"], self.STRANGER_TG)
        # ...and the seat is then taken.
        query = await self._join(tg_id=self.GUEST_TG)
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(draft["target_tg_id"], self.STRANGER_TG)

    # ── A live draft must never be torn down underneath its captains ──
    #
    # The regression: captains reported "This draft is no longer active." part
    # way through picking. The abandoned-setup backstop (CHALLENGE_DRAFT_EXPIRE,
    # 10 minutes) is armed with the lobby, and eleven slots at a minute each
    # outlast it — so anything that leaves it running, or lets it fire on a
    # draft that is mid-pick, kills a draft both captains are still playing and
    # leaves every slot button reporting the draft as gone.

    async def test_the_setup_backstop_is_dropped_once_picking_starts(self):
        from handlers.cdraft import _lobby_job_name
        await self._open_lobby()
        draft_id = self._draft()["draft_id"]
        self.assertTrue(self.context.job_queue.get_jobs_by_name(
            _lobby_job_name(draft_id)), "the lobby arms the backstop")
        await self._join()
        self.assertFalse(self.context.job_queue.get_jobs_by_name(
            _lobby_job_name(draft_id)),
            "picking has its own clock — the backstop must go")

    async def test_every_slot_re_drops_the_backstop(self):
        """Belt and braces: a backstop that somehow survived the join must not
        be left ticking over a draft that is being played out."""
        from handlers.cdraft import _lobby_job_name
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        # Re-arm it behind the handlers' back, as a missed cancel would.
        self.context.job_queue.run_once(
            lambda ctx: None, 600, name=_lobby_job_name(draft["draft_id"]),
            data={})
        await self._pick(self.HOST_TG)
        self.assertFalse(self.context.job_queue.get_jobs_by_name(
            _lobby_job_name(draft["draft_id"])))

    async def test_the_backstop_refuses_to_kill_a_draft_that_is_still_picking(self):
        from handlers.challenge import (
            _challenge_team_draft_key, _expire_challenge_draft,
        )
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        job = SimpleNamespace(data={"draft_id": draft["draft_id"],
                                    "chat_id": self.CHAT, "message_id": 1})
        await _expire_challenge_draft(
            SimpleNamespace(job=job, bot=self.context.bot,
                            bot_data=self.context.bot_data,
                            job_queue=self.context.job_queue))
        self.assertIn(_challenge_team_draft_key(draft["draft_id"]),
                      self.context.bot_data,
                      "a draft mid-pick is not an abandoned setup")
        # ...and the picking carries on as if nothing happened.
        query = await self._pick(self.HOST_TG)
        self.assertEqual(draft["cdraft"]["index"], 1)
        self.assertNotIn(self.module.DRAFT_GONE_MESSAGE,
                         [text for text, _alert in query.answers])

    async def test_the_backstop_still_frees_an_abandoned_lobby(self):
        """The guard is for picking only — an unjoined lobby must still expire."""
        from handlers.challenge import (
            _challenge_team_draft_key, _expire_challenge_draft,
        )
        await self._open_lobby()
        draft = self._draft()
        job = SimpleNamespace(data={"draft_id": draft["draft_id"],
                                    "chat_id": self.CHAT, "message_id": 1})
        await _expire_challenge_draft(
            SimpleNamespace(job=job, bot=self.context.bot,
                            bot_data=self.context.bot_data,
                            job_queue=self.context.job_queue))
        self.assertNotIn(_challenge_team_draft_key(draft["draft_id"]),
                         self.context.bot_data)

    # ── What a stale button says ──

    async def test_a_pick_button_pressed_after_the_picking_says_so(self):
        """The draft is alive and on the pitch step — not "no longer active"."""
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        state = draft["cdraft"]
        tg_for = {"host": self.HOST_TG, "target": self.GUEST_TG}
        # Keep a button from the last slot before it is picked away.
        while not cdraft_service.is_complete(state):
            stale = (f"cdp_{draft['draft_id']}_{state['index']}"
                     f"_{cdraft_service.current_slot(state)['cards'][0]['id']}")
            await self._pick(tg_for[cdraft_service.current_side(state)])
        self.assertEqual(draft["turn"], "complete")
        query = FakeQuery(stale, self.HOST_TG, self.CHAT)
        await self.module.cdraft_pick_callback(
            SimpleNamespace(callback_query=query), self.context)
        self.assertEqual(query.answers[-1][0],
                         self.module.PICKING_OVER_MESSAGE)

    async def test_a_button_on_a_draft_that_is_gone_says_what_to_do_next(self):
        await self._open_lobby()
        await self._join()
        draft = self._draft()
        stale = (f"cdp_{draft['draft_id']}_0"
                 f"_{cdraft_service.current_slot(draft['cdraft'])['cards'][0]['id']}")
        await self._cancel(self.HOST_TG)
        for data in (stale, f"cdj_{draft['draft_id']}",
                     f"cdc_{draft['draft_id']}"):
            query = FakeQuery(data, self.HOST_TG, self.CHAT)
            handler = {"cdp": self.module.cdraft_pick_callback,
                       "cdj": self.module.cdraft_join_callback,
                       "cdc": self.module.cdraft_cancel_callback}[data[:3]]
            await handler(SimpleNamespace(callback_query=query), self.context)
            self.assertEqual(query.answers[-1][0],
                             self.module.DRAFT_GONE_MESSAGE, data)
            self.assertIn("/cdraft", query.answers[-1][0])

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
