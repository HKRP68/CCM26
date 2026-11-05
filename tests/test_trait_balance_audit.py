"""The audit measures production matches and restores its process state."""

import random
import unittest

from services import cipl_match, trait_engine
from tools import trait_balance as audit


class TraitAuditTests(unittest.TestCase):
    def test_loadouts_preserve_ratings_and_match_trait_budgets(self):
        for profile, expected in (("none", 0), ("batting", 5), ("bowling", 5),
                                  ("both", 10), ("bat_finisher", 5), ("bowl_death", 5)):
            with self.subTest(profile=profile):
                xi = audit.make_xi("A", 1)
                ratings = [(p["bat_rating"], p["bowl_rating"]) for p in xi]
                audit.equip(xi, profile, 5)
                self.assertEqual(sum(len(p.get("traits", [])) for p in xi), expected)
                self.assertEqual([(p["bat_rating"], p["bowl_rating"]) for p in xi], ratings)

    def test_a_mirrored_audit_is_reproducible_and_has_neutral_controls(self):
        rng_state = random.getstate()
        apply_traits = trait_engine.apply_traits
        commentary = cipl_match._COMMENTARY
        args = dict(pitches=["Even"], levels=[5], profiles=["none", "both"], n=4, seed=19)
        first = audit.collect(**args)
        self.assertEqual(random.getstate(), rng_state)
        self.assertIs(trait_engine.apply_traits, apply_traits)
        self.assertIs(cipl_match._COMMENTARY, commentary)
        self.assertEqual(first, audit.collect(**args))
        neutral, traits = first["rows"]
        self.assertEqual(neutral["win_share_pct"], 50.0)
        self.assertEqual(neutral["edge_vs_neutral_pp"], 0.0)
        self.assertEqual(traits["matches"], 4)
        self.assertTrue(traits["activation"])
        for key_role, activation in traits["activation"].items():
            key, role = key_role.split(":")
            self.assertLessEqual(activation["activated"], activation["opportunities"])
            if not trait_engine.trait_allowed(key, role):
                self.assertEqual(activation["activated"], 0)
            # A balanced five-bowler rotation must not fall back to a top-five
            # specialist batter with a batting trait in the death overs.
            if key.startswith("bat_"):
                self.assertEqual(role, "batter")
        self.assertEqual(first["simulated_matches"], 8)

    def test_invalid_parameters_fail_before_simulation(self):
        for kwargs in ({"n": 0}, {"n": 3}, {"levels": [6]}, {"pitches": ["Unknown"]},
                       {"profiles": ["unknown"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                audit.collect(**kwargs)

    def test_a_single_seed_pair_does_not_claim_an_estimated_interval(self):
        self.assertIsNone(audit.mean_interval([0.5])[1])


if __name__ == "__main__":
    unittest.main()
