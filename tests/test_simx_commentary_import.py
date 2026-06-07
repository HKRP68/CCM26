"""Tests for the SimCricketX commentary pack converter (import_simx_commentary).

Covers the pure convert_pack mapping only (no DB): key remapping, keeper/fielder
split, placeholder normalisation, and dropping of unsupported placeholders.
"""
import json
import os

from import_simx_commentary import convert_pack
from services.commentary_service import list_event_keys


def _sample_pack():
    return {
        "events": {
            "boundary_four": [{"text": "{batter} drives {bowler} for four."}],
            "boundary_six": [{"text": "{batter} launches it for six!"}],
            "single": [{"text": "{batter} taps it for one."}],
            "double": [{"text": "Two more for {batter}."}],
            "wide": [{"text": "Down leg from {bowler}, wide."}],
            "noball": [{"text": "{bowler} oversteps."}],
            "byes": [{"text": "Beats everyone, byes."}],
            "legbyes": [{"text": "Off the pad, leg byes."}],
            "wicket_run_out": [{"text": "{batter} is short of the crease!"}],
            "wicket_caught": [
                {"text": "Edged through to the keeper, gone!"},
                {"text": "Skied it, the fielder at long-on takes it."},
            ],
            "totally_unknown_key": [{"text": "should be ignored"}],
        },
        "narratives": {
            "milestone_50": [{"text": "Fifty for {batter}!"}],
            "milestone_100": [{"text": "Hundred for {batter}!"}],
            "collapse_wicket": [{"text": "{team} are falling apart!"}],
        },
    }


def test_key_remapping():
    out = convert_pack(_sample_pack())
    assert "four" in out and "six" in out
    assert out["one"] == ["{batsman} taps it for one."]
    assert out["two"] == ["Two more for {batsman}."]
    assert out["no_ball"] == ["{bowler} oversteps."]
    assert out["wicket_runOut"][0].endswith("crease!")
    # byes + legbyes both fold into 'extras'
    assert len(out["extras"]) == 2
    # unknown source key is dropped
    assert "totally_unknown_key" not in out


def test_caught_keeper_fielder_split():
    out = convert_pack(_sample_pack())
    assert any("keeper" in t.lower() for t in out["wicket_caught_keeper"])
    assert any("fielder" in t.lower() for t in out["wicket_caught_fielder"])
    assert "keeper" not in out["wicket_caught_fielder"][0].lower()


def test_placeholder_normalisation_and_drop():
    out = convert_pack(_sample_pack())
    # {batter} -> {batsman}
    assert "{batter}" not in out["four"][0]
    assert "{batsman}" in out["four"][0]
    # milestone narratives flow into 'milestones'
    assert len(out["milestones"]) == 2
    # narratives with unsupported {team} placeholder are dropped entirely
    flat = " ".join(line for lines in out.values() for line in lines)
    assert "{team}" not in flat


def test_all_output_keys_are_known_event_keys():
    out = convert_pack(_sample_pack())
    known = set(list_event_keys())
    assert set(out).issubset(known), f"unexpected keys: {set(out) - known}"


def test_real_pack_converts():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "data", "commentary_pack.json")
    if not os.path.exists(path):
        return  # asset optional in some checkouts
    with open(path, encoding="utf-8") as f:
        out = convert_pack(json.load(f))
    assert sum(len(v) for v in out.values()) > 100
    assert set(out).issubset(set(list_event_keys()))
