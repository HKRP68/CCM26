"""Tests for the player-version upgrade cost formula.

Upgrade cost = (buy_value(target) - buy_value(current)) + 1% of that difference.
"""

from services.upgrade_service import (
    _cost_breakdown,
    _upgrade_cost,
    UPGRADE_MARKUP,
    MIN_UPGRADE_COST,
)
from config import get_buy_value


def test_markup_is_one_percent():
    assert UPGRADE_MARKUP == 0.01


def test_kohli_90_to_96_breakdown():
    # buy(96) - buy(90) = upgrade value, then +1% fee on top.
    bd = _cost_breakdown(90, 96)
    upgrade_value = get_buy_value(96) - get_buy_value(90)
    fee = round(upgrade_value * 0.01)

    assert bd["upgrade_value"] == upgrade_value
    assert bd["fee"] == fee
    assert bd["fee_percent"] == 1.0
    assert bd["cost"] == upgrade_value + fee
    assert _upgrade_cost(90, 96) == upgrade_value + fee


def test_cost_equals_value_plus_one_percent_generic():
    for cur, tgt in [(80, 85), (85, 92), (91, 99), (93, 96)]:
        upgrade_value = get_buy_value(tgt) - get_buy_value(cur)
        expected = upgrade_value + round(upgrade_value * 0.01)
        assert _upgrade_cost(cur, tgt) == max(MIN_UPGRADE_COST, expected)


def test_non_positive_difference_is_floored():
    # Same/lower rating -> no positive upgrade value, cost floored at minimum.
    assert _upgrade_cost(90, 90) == MIN_UPGRADE_COST
    assert _cost_breakdown(90, 90)["upgrade_value"] == 0
    assert _cost_breakdown(90, 85)["upgrade_value"] == 0
