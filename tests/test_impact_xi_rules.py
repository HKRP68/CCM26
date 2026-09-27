"""Impact Player must not break the Challenge League Playing XI rules.

The XI picker holds a league / tournament XI to: a Wicket Keeper, five bowling
options, the overseas min/max and a tournament's "min N players rated ≤X".
An Impact swap is a change to that XI, so it is held to the same rules —
otherwise a captain could pick a legal XI and then swap it into an illegal one.
"""

import unittest

from services import impact_player, cipl_match, xi_rules
from services.match_state_store import A_PICK_CIPL_BOWLER


def _p(rid, name, category="Batsman", rating=80, overseas=False, bowl=40):
    return {"roster_id": rid, "name": name, "category": category,
            "rating": rating, "card_rating": rating, "bat_rating": rating,
            "bowl_rating": bowl, "is_overseas": overseas}


def _xi():
    """A legal XI: 1 WK, 5 bowling options, 2 overseas, three rated ≤83."""
    return [
        _p(1, "Keeper", "Wicket Keeper", 85),
        _p(2, "Bat1", "Batsman", 90, overseas=True),
        _p(3, "Bat2", "Batsman", 88),
        _p(4, "Bat3", "Batsman", 82),
        _p(5, "Bat4", "Batsman", 86),
        _p(6, "Bat5", "Batsman", 87),
        _p(7, "AR1", "All-rounder", 84, overseas=True, bowl=80),
        _p(8, "Bowl1", "Bowler", 81, bowl=85),
        _p(9, "Bowl2", "Bowler", 80, bowl=84),
        _p(10, "Bowl3", "Bowler", 88, bowl=86),
        _p(11, "Bowl4", "Bowler", 89, bowl=87),
    ]


def _state(bench, rules=True, **rule_over):
    other = [_p(100 + i, f"O{i}", "Bowler", 80, bowl=80) for i in range(1, 12)]
    state = cipl_match.build_cipl_state(
        match_id=1, overs=20,
        bat_user_id=1, bowl_user_id=2, bat_user_tg=11, bowl_user_tg=22,
        bat_xi=other, bowl_xi=_xi(),
        bat_team_name="Alpha", bowl_team_name="Beta", chat_id=-100,
        bat_bench=[], bowl_bench=bench)
    if rules:
        state["xi_rules"] = {"min_overseas": 1, "max_overseas": 2,
                             "rating_rules": [{"max_rating": 83,
                                               "min_players": 3}]}
        state["xi_rules"].update(rule_over)
    return state


def _use(state, in_rid, out_rid):
    return impact_player.cipl_use(state, 2, in_rid, out_rid, A_PICK_CIPL_BOWLER)


class DictValidatorTests(unittest.TestCase):
    def test_legal_xi_passes(self):
        ok, err = xi_rules.validate_challenge_xi_dicts(
            _xi(), 1, 2, [{"max_rating": 83, "min_players": 3}])
        self.assertTrue(ok, err)

    def test_each_rule_fails(self):
        xi = _xi()
        self.assertIn("Wicket Keeper", xi_rules.validate_challenge_xi_dicts(
            [p for p in xi if p["roster_id"] != 1])[1])
        self.assertIn("Bowling", xi_rules.validate_challenge_xi_dicts(
            [p for p in xi if p["roster_id"] != 8])[1])
        self.assertIn("Max 1 overseas",
                      xi_rules.validate_challenge_xi_dicts(xi, 0, 1)[1])
        self.assertIn("Min 3 overseas",
                      xi_rules.validate_challenge_xi_dicts(xi, 3, 11)[1])
        self.assertIn("rated ≤83", xi_rules.validate_challenge_xi_dicts(
            xi, 0, 11, [{"max_rating": 83, "min_players": 4}])[1])

    def test_raw_admin_roles_are_recognised(self):
        xi = _xi()
        xi[0]["category"] = "WK"
        xi[6]["category"] = "allrounder"
        self.assertTrue(xi_rules.validate_challenge_xi_dicts(xi)[0])


class ImpactSwapTests(unittest.TestCase):
    def test_cannot_swap_out_the_only_keeper_for_a_batter(self):
        s = _state([_p(50, "SubBat", "Batsman", 80)])
        ok, msg, _ = _use(s, 50, 1)
        self.assertFalse(ok)
        self.assertIn("Wicket Keeper", msg)
        self.assertTrue(any(p["roster_id"] == 1 and impact_player.is_active(p)
                            for p in s["bowl_xi"]))

    def test_cannot_drop_below_five_bowling_options(self):
        s = _state([_p(50, "SubBat", "Batsman", 80)])
        ok, msg, _ = _use(s, 50, 10)
        self.assertFalse(ok)
        self.assertIn("Bowling", msg)

    def test_cannot_exceed_the_overseas_cap(self):
        s = _state([_p(50, "SubOS", "Batsman", 80, overseas=True)])
        ok, msg, _ = _use(s, 50, 3)
        self.assertFalse(ok)
        self.assertIn("overseas", msg)

    def test_cannot_fall_under_the_overseas_minimum(self):
        s = _state([_p(50, "SubBat", "Batsman", 80)], min_overseas=2)
        ok, msg, _ = _use(s, 50, 2)
        self.assertFalse(ok)
        self.assertIn("Min 2 overseas", msg)

    def test_cannot_break_the_rating_rule(self):
        s = _state([_p(50, "Star", "Batsman", 92)])
        ok, msg, _ = _use(s, 50, 4)       # Bat3 is rated 82
        self.assertFalse(ok)
        self.assertIn("rated ≤83", msg)

    def test_a_legal_swap_goes_through(self):
        s = _state([_p(50, "Star", "Batsman", 92)])
        ok, msg, _ = _use(s, 50, 3)       # Bat2 (88) for a 92: all rules hold
        self.assertTrue(ok, msg)

    def test_like_for_like_keeper_swap_is_allowed(self):
        s = _state([_p(50, "SubWK", "Wicket Keeper", 86)])
        ok, msg, _ = _use(s, 50, 1)
        self.assertTrue(ok, msg)

    def test_no_rules_on_state_means_no_check(self):
        s = _state([_p(50, "SubBat", "Batsman", 80)], rules=False)
        ok, msg, _ = _use(s, 50, 1)
        self.assertTrue(ok, msg)


class OptionsFilterTests(unittest.TestCase):
    def test_options_only_offer_legal_swaps(self):
        s = _state([_p(50, "SubOS", "Batsman", 92, overseas=True)])
        opts = impact_player.cipl_options(s, 2, A_PICK_CIPL_BOWLER)
        out_ids = {p["roster_id"] for p in opts["replaceable_players"]}
        # Only an overseas batter can make way for an overseas batter.
        self.assertEqual(out_ids, {2})
        self.assertTrue(opts["can_use"])

    def test_no_legal_swap_disables_impact(self):
        # Every possible outgoing player is either rated ≤88 (rating rule),
        # the only keeper, a bowling option, or needed for the overseas min.
        s = _state([_p(50, "Star", "Batsman", 92)], min_overseas=2,
                   rating_rules=[{"max_rating": 88, "min_players": 9}])
        opts = impact_player.cipl_options(s, 2, A_PICK_CIPL_BOWLER)
        self.assertFalse(opts["can_use"])
        self.assertEqual(opts["message"], impact_player.NO_LEGAL_SWAP_MESSAGE)

    def test_bot_only_picks_legal_swaps(self):
        from services import bot_captain
        s = _state([_p(50, "SubBat", "Batsman", 95, bowl=99)])
        s.update({"innings": 2, "current_over": 1})
        pick = bot_captain.pick_impact_swap(s, 2, A_PICK_CIPL_BOWLER)
        if pick is not None:
            in_rid, out_rid, _ = pick
            incoming = next(p for p in s["bowl_bench"] if p["roster_id"] == in_rid)
            self.assertEqual(
                impact_player.cipl_swap_error(s, "bowl", out_rid, incoming), "")


if __name__ == "__main__":
    unittest.main()
