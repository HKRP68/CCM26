"""The reference data, the player profiles it must not touch, and the fallback.

Covers requirements 1, 2 and 20 of the brief's test list: player records stay
byte-identical, no schema migration is implied, and a missing or corrupt
reference reverts to the legacy engine instead of inventing probabilities.
"""

import hashlib
import json
import os
import tempfile
import unittest

from engine.realism import baselines, config as rcfg
from engine.realism.outcome_distribution import OUTCOMES

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYERS = os.path.join(REPO, "data", "players.json")

# The six fields the brief allows the simulation to read, as data/players.json
# actually spells them. If this set changes, the realism engine's contract
# with the player system has changed and that is a deliberate decision, not a
# refactor.
ALLOWED_FIELDS = {"Player Name", "Category", "Batting Style", "Batting Rating",
                  "Bowling Style", "Bowling Rating", "Country", "overall all",
                  "Version"}


class PlayerProfilesUntouchedTests(unittest.TestCase):
    """§1: the player system must remain exactly as it is."""

    def test_players_json_schema_is_unchanged(self):
        with open(PLAYERS, "r", encoding="utf-8") as fh:
            players = json.load(fh)
        self.assertIsInstance(players, list)
        self.assertGreater(len(players), 1000)
        seen = set()
        for p in players:
            seen |= set(p)
        self.assertEqual(
            seen, ALLOWED_FIELDS,
            "data/players.json gained or lost a field. The realism engine must "
            "not add player attributes (see docs/realism_engine_v1.md §1).")

    def test_no_realism_field_leaked_into_player_records(self):
        with open(PLAYERS, "r", encoding="utf-8") as fh:
            blob = fh.read()
        for banned in ("form", "aggression", "pace_rating", "technique",
                       "accuracy", "power", "confidence", "fatigue",
                       "realism", "baseline"):
            self.assertNotIn('"%s"' % banned, blob,
                             "player records must carry no derived skill field")

    def test_reference_files_contain_no_player_names(self):
        """§1: historical data must be aggregate and player-independent."""
        with open(PLAYERS, "r", encoding="utf-8") as fh:
            names = [p["Player Name"] for p in json.load(fh)]
        version = rcfg.reference_version()
        root = os.path.join(rcfg.REFERENCE_ROOT, version)
        blob = ""
        for name in sorted(os.listdir(root)):
            if name.endswith(".json"):
                with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                    blob += fh.read()
        for player in names:
            self.assertNotIn(player, blob,
                             "reference data must not mention individual players")

    def test_importing_the_engine_does_not_write_player_data(self):
        """§1: no migration, and nothing writes to the roster."""
        before = hashlib.sha256(open(PLAYERS, "rb").read()).hexdigest()
        import engine.realism  # noqa: F401
        from engine.realism import BallContext, build_distribution
        build_distribution(BallContext(bat_rating=80, bowl_rating=70))
        after = hashlib.sha256(open(PLAYERS, "rb").read()).hexdigest()
        self.assertEqual(before, after)


class ReferenceValidityTests(unittest.TestCase):

    def setUp(self):
        baselines.clear_cache()
        self.doc = baselines.load(format_name="T20", reload=True)

    def test_t20_reference_loads_and_validates(self):
        self.assertIsNotNone(self.doc, "the shipped T20 reference must load")
        baselines.validate(self.doc)

    def test_every_node_distribution_is_a_distribution(self):
        for key in self.doc["nodes"]:
            dist = baselines.smoothed(self.doc, key)
            self.assertEqual(set(dist), set(OUTCOMES), key)
            for outcome, p in dist.items():
                self.assertGreaterEqual(p, 0.0, "%s/%s" % (key, outcome))
                self.assertTrue(p == p and p not in (float("inf"),),
                                "%s/%s is not finite" % (key, outcome))
            self.assertAlmostEqual(sum(dist.values()), 1.0, places=9, msg=key)

    def test_every_node_reaches_the_root(self):
        nodes = self.doc["nodes"]
        for key in nodes:
            cur, hops = key, 0
            while cur != "root":
                cur = nodes[cur]["parent"]
                hops += 1
                self.assertLess(hops, 20, "%s does not terminate" % key)

    def test_provenance_records_sample_sizes_and_honesty(self):
        """§6: store sample sizes, source metadata, coverage and version."""
        cov = baselines.coverage(self.doc)
        self.assertEqual(cov["nodes"], len(self.doc["nodes"]))
        self.assertEqual(cov["observed_nodes"] + cov["pseudo_nodes"], cov["nodes"])
        prov = cov["provenance"]
        self.assertIn(prov["mode"], ("cricsheet", "anchors"))
        self.assertIn("level_measured" if prov["mode"] == "anchors"
                      else "deliveries", prov)
        self.assertTrue(self.doc.get("model_version"))
        for node in self.doc["nodes"].values():
            self.assertIn(node["count_kind"], ("observed", "pseudo"))

    def test_anchors_mode_does_not_claim_observed_counts(self):
        """§4/§6: a prior must never be presented as a measurement."""
        prov = self.doc.get("provenance") or {}
        if prov.get("mode") != "anchors":
            self.skipTest("reference was built from ball-by-ball data")
        self.assertFalse(prov.get("all_nodes_observed"))
        self.assertFalse(prov.get("shape_measured"))
        self.assertTrue(prov.get("limitation"))
        kinds = {n["count_kind"] for n in self.doc["nodes"].values()}
        self.assertEqual(kinds, {"pseudo"})

    def test_measured_anchors_are_reproduced_by_the_baseline(self):
        """The fitted baseline must actually hit the real measured level."""
        prov = self.doc.get("provenance") or {}
        if not prov.get("level_measured"):
            self.skipTest("no measured anchor in this reference")
        from engine.realism.outcome_distribution import expected_runs
        for comp, anchor in (self.doc.get("anchors") or {}).items():
            if not anchor.get("measured"):
                continue
            dist = baselines.smoothed(self.doc, "T20/%s" % comp)
            # Wides and no-balls are 65% of Extras and consume no legal ball.
            legal = 1.0 - 0.65 * dist["Extras"]
            got_rpb = expected_runs(dist) / legal
            self.assertAlmostEqual(got_rpb, anchor["runs_per_ball_faced"],
                                   delta=0.05, msg="%s scoring level" % comp)
            self.assertAlmostEqual(dist["Wicket"] / legal,
                                   anchor["wickets_per_ball"],
                                   delta=0.004, msg="%s wicket level" % comp)

    def test_manifest_states_the_data_licence(self):
        """§4: document attribution and redistribution requirements."""
        path = baselines.manifest_path(rcfg.reference_version())
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        lic = manifest["licensing"]
        self.assertIn("ODbL", lic["cricsheet_data"])
        self.assertIn("cricsheet.org", lic["cricsheet_url"])
        self.assertIn("Derivative Database", lic["notice"])
        self.assertIn("Cricket-Intelligence-Lab", lic["code_attribution"])


class SmoothingTests(unittest.TestCase):
    """§6: hierarchical smoothing, and that alpha actually does something."""

    def _doc(self, child_counts, alpha):
        doc = {
            "schema_version": 1, "format": "T20", "model_version": "t",
            "default_competition": "c",
            "nodes": {
                "root": {"parent": None, "count_kind": "observed",
                         "counts": {o: (1000 if o == "Dot" else 100)
                                    for o in OUTCOMES}},
                "T20": {"parent": "root", "count_kind": "observed",
                        "counts": {o: (1000 if o == "Dot" else 100)
                                   for o in OUTCOMES}},
                "T20/c": {"parent": "T20", "count_kind": "observed",
                          "counts": child_counts},
            },
        }
        baselines.validate(doc)
        doc = baselines._prepare(doc)
        doc["_alpha"] = {"competition": alpha}
        return doc

    def test_a_thin_node_is_dominated_by_its_parent(self):
        thin = {o: (1 if o == "Six" else 0.001) for o in OUTCOMES}
        doc = self._doc(thin, alpha=1000.0)
        parent = baselines.smoothed(doc, "T20")
        child = baselines.smoothed(doc, "T20/c")
        self.assertLess(abs(child["Six"] - parent["Six"]), 0.02,
                        "a node with ~1 observation must follow its parent")

    def test_a_rich_node_stands_on_its_own(self):
        rich = {o: (100000 if o == "Six" else 100) for o in OUTCOMES}
        doc = self._doc(rich, alpha=250.0)
        child = baselines.smoothed(doc, "T20/c")
        self.assertGreater(child["Six"], 0.9,
                           "a node with 100k observations must not be smoothed away")

    def test_lookup_stops_at_the_deepest_existing_node(self):
        doc = self._doc({o: 500 for o in OUTCOMES}, alpha=250.0)
        _dist, key, meta = baselines.lookup(
            doc, ["T20", "c", "death", "inn2", "w6_plus"])
        self.assertEqual(key, "T20/c")
        self.assertEqual(meta["node"], "T20/c")


class SafeFallbackTests(unittest.TestCase):
    """§11 / test 20: a bad reference falls back, it does not fabricate."""

    def setUp(self):
        baselines.clear_cache()
        self.addCleanup(baselines.clear_cache)

    def test_missing_reference_returns_none(self):
        self.assertIsNone(baselines.load(version="does-not-exist",
                                         format_name="T20", reload=True))

    def test_corrupt_reference_returns_none_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = "broken"
            os.makedirs(os.path.join(tmp, version))
            with open(os.path.join(tmp, version, "t20_baselines.json"), "w") as fh:
                fh.write("{not json at all")
            orig = rcfg.REFERENCE_ROOT
            baselines.REFERENCE_ROOT = tmp
            try:
                self.assertIsNone(baselines.load(version=version,
                                                 format_name="T20", reload=True))
            finally:
                baselines.REFERENCE_ROOT = orig

    def test_validate_rejects_a_negative_count(self):
        doc = {"schema_version": 1, "format": "T20", "model_version": "t",
               "default_competition": "c",
               "nodes": {"root": {"parent": None, "count_kind": "observed",
                                  "counts": {o: (-1 if o == "Dot" else 10)
                                             for o in OUTCOMES}}}}
        with self.assertRaises(baselines.ReferenceError):
            baselines.validate(doc)

    def test_validate_rejects_a_parent_cycle(self):
        counts = {o: 10 for o in OUTCOMES}
        doc = {"schema_version": 1, "format": "T20", "model_version": "t",
               "default_competition": "c",
               "nodes": {
                   "root": {"parent": None, "count_kind": "observed",
                            "counts": dict(counts)},
                   "a": {"parent": "b", "count_kind": "observed",
                         "counts": dict(counts)},
                   "b": {"parent": "a", "count_kind": "observed",
                         "counts": dict(counts)}}}
        with self.assertRaises(baselines.ReferenceError):
            baselines.validate(doc)

    def test_unavailable_format_raises_so_the_caller_can_fall_back(self):
        from engine.realism.context import BallContext
        from engine.realism.outcome_distribution import (
            RealismUnavailable, build,
        )
        ctx = BallContext(format_name="Test")
        with self.assertRaises(RealismUnavailable):
            build(ctx)


if __name__ == "__main__":
    unittest.main()
