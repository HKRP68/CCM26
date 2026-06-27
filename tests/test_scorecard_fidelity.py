"""Tests for scorecard fidelity helpers (services.scorecard_card).

Only the pure extras-breakdown helper is exercised here; PIL is stubbed so the
module imports without the Pillow dependency installed.
"""

import sys
import types
import unittest


def _load_scorecard_with_pil_stub():
    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        pil.Image = types.SimpleNamespace(new=lambda *a, **k: None)
        pil.ImageDraw = types.SimpleNamespace(Draw=lambda *a, **k: None)
        pil.ImageFont = types.SimpleNamespace(truetype=lambda *a, **k: None,
                                              load_default=lambda *a, **k: None)
        sys.modules["PIL"] = pil
    sys.modules.pop("services.scorecard_card", None)
    from services.scorecard_card import _extras_breakdown
    return _extras_breakdown


_extras_breakdown = _load_scorecard_with_pil_stub()


class ExtrasBreakdownTests(unittest.TestCase):
    def test_shows_only_nonzero_categories(self):
        self.assertEqual(
            _extras_breakdown({"wd": 5, "nb": 1, "b": 0, "lb": 3, "total": 9}),
            "wd 5 · nb 1 · lb 3")

    def test_all_zero_falls_back_to_generic_label(self):
        self.assertEqual(
            _extras_breakdown({"wd": 0, "nb": 0, "b": 0, "lb": 0, "total": 0}),
            "wides, no-balls, byes")

    def test_handles_missing_keys_and_none(self):
        self.assertEqual(_extras_breakdown({}), "wides, no-balls, byes")
        self.assertEqual(_extras_breakdown(None), "wides, no-balls, byes")
        self.assertEqual(_extras_breakdown({"wd": None, "nb": 2}), "nb 2")

    def test_preserves_category_order(self):
        self.assertEqual(
            _extras_breakdown({"lb": 1, "b": 2, "nb": 3, "wd": 4}),
            "wd 4 · nb 3 · b 2 · lb 1")


if __name__ == "__main__":
    unittest.main()
