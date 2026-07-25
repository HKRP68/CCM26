"""Batting-order maths for /setbo and /sbo (handlers/batting_order.py).

The order the user saves here is the line-up /letsplay walks out with, so the
swap / reorder / bench-substitution rules are pinned down explicitly.
"""

from types import SimpleNamespace

from handlers.batting_order import (
    XI_SIZE, apply_moves, apply_full_order, auto_order, has_custom_order)


def _pair(rid, name, bat, rating=None):
    entry = SimpleNamespace(id=rid)
    player = SimpleNamespace(id=rid, name=name, bat_rating=bat,
                             rating=rating if rating is not None else bat,
                             category="Batsman", bowl_style="Fast")
    return entry, player


XI = list(range(1, 12))          # roster ids 1..11 in batting order
BENCH = [12, 13, 14]             # displayed as 12, 13, 14


# ── swaps ────────────────────────────────────────────────────────────

def test_swap_two_batting_slots():
    new, changes, err = apply_moves(XI, BENCH, [2, 11])
    assert err is None
    assert new[1] == 11 and new[10] == 2
    assert changes == [(2, 11)]
    # everything else untouched
    assert new[0] == 1 and new[5] == 6


def test_multiple_swaps_apply_left_to_right():
    new, changes, err = apply_moves(XI, BENCH, [1, 2, 1, 3])
    assert err is None
    # after 1↔2: [2,1,3,...]; after 1↔3: [3,1,2,...]
    assert new[:3] == [3, 1, 2]
    assert changes == [(1, 2), (1, 3)]


def test_swapping_a_slot_with_itself_is_rejected():
    new, _c, err = apply_moves(XI, BENCH, [4, 4])
    assert new is None and "does nothing" in err


def test_odd_number_of_arguments_is_rejected():
    new, _c, err = apply_moves(XI, BENCH, [2, 11, 3])
    assert new is None and "pairs" in err


def test_first_number_must_be_an_xi_slot():
    new, _c, err = apply_moves(XI, BENCH, [13, 2])
    assert new is None and "1-11" in err


# ── bench substitution ───────────────────────────────────────────────

def test_bench_player_takes_the_batting_slot():
    new, changes, err = apply_moves(XI, BENCH, [2, 13])
    assert err is None
    assert new[1] == 13          # bench number 13 → second bench entry, rid 13
    assert 2 not in new          # the dropped player left the XI
    assert changes == [(2, 13)]


def test_bench_number_out_of_range_is_rejected():
    new, _c, err = apply_moves(XI, BENCH, [2, 99])
    assert new is None and "bench player" in err


def test_no_bench_gives_a_clearer_message():
    new, _c, err = apply_moves(XI, [], [2, 12])
    assert new is None and "no bench players" in err


def test_same_bench_player_cannot_come_in_twice():
    new, _c, err = apply_moves(XI, BENCH, [2, 13, 4, 13])
    assert new is None and "already in your XI" in err


# ── full reorder ─────────────────────────────────────────────────────

def test_full_reorder_takes_a_permutation():
    order = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    new, err = apply_full_order(XI, order)
    assert err is None
    assert new == [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def test_full_reorder_rejects_repeats():
    order = [1, 1] + list(range(3, 12))
    new, err = apply_full_order(XI, order)
    assert new is None and "exactly once" in err


# ── auto order ───────────────────────────────────────────────────────

def test_auto_order_sorts_by_batting_rating_high_to_low():
    pairs = [_pair(1, "Tail", 30), _pair(2, "Star", 92), _pair(3, "Mid", 61)]
    ordered = auto_order(pairs)
    assert [p.name for _e, p in ordered] == ["Star", "Mid", "Tail"]


def test_auto_order_breaks_ties_on_overall_rating():
    pairs = [_pair(1, "A", 70, rating=80), _pair(2, "B", 70, rating=90)]
    ordered = auto_order(pairs)
    assert [p.name for _e, p in ordered] == ["B", "A"]


# ── custom-order flag ────────────────────────────────────────────────

def test_custom_order_flag():
    assert has_custom_order(SimpleNamespace(batting_order_set_at=None)) is False
    assert has_custom_order(SimpleNamespace(batting_order_set_at=object())) is True
    assert has_custom_order(SimpleNamespace()) is False


def test_xi_size_is_eleven():
    assert XI_SIZE == 11


# ── /letsplay honours a saved order ──────────────────────────────────

def _legal_xi_pairs():
    """11 pairs that satisfy validate_xi: 4 BAT, 1 WK, 2 ALR, 4 BOWL, with the
    weakest all-rounder bowling worse than every pure bowler."""
    spec = [("Bat1", "Batsman", 90, 10), ("Bat2", "Batsman", 85, 10),
            ("Bat3", "Batsman", 80, 10), ("Bat4", "Batsman", 75, 10),
            ("Keep", "Wicket Keeper", 70, 10),
            ("Alr1", "All-rounder", 65, 40), ("Alr2", "All-rounder", 60, 45),
            ("Bwl1", "Bowler", 30, 88), ("Bwl2", "Bowler", 25, 86),
            ("Bwl3", "Bowler", 20, 84), ("Bwl4", "Bowler", 15, 82)]
    out = []
    for i, (name, cat, bat, bowl) in enumerate(spec, start=1):
        entry = SimpleNamespace(id=i)
        player = SimpleNamespace(id=i, name=name, category=cat, bat_rating=bat,
                                 bowl_rating=bowl, rating=max(bat, bowl),
                                 bowl_style="Fast", parent_player_id=None,
                                 version=None)
        out.append((entry, player))
    return out


def _xi_names(session, custom, roster):
    """Run letsplay's XI builder with the custom-order flag forced either way."""
    import handlers.letsplay as lp
    orig_roster, orig_flag = lp._get_ordered_roster, lp._has_custom_batting_order
    lp._get_ordered_roster = lambda _s, _uid: roster
    lp._has_custom_batting_order = lambda _s, _uid: custom
    try:
        xi, _bench, errors = lp._xi_bench_for_side(session, 1)
    finally:
        lp._get_ordered_roster = orig_roster
        lp._has_custom_batting_order = orig_flag
    assert errors == [], errors
    return [p.name for _e, p in xi]


def test_letsplay_uses_the_saved_batting_order():
    # Roster order deliberately NOT rating-sorted: the tail opens.
    roster = _legal_xi_pairs()
    roster = roster[::-1]
    names = _xi_names(None, custom=True, roster=roster)
    assert names[0] == "Bwl4"           # saved order kept verbatim
    assert names == [p.name for _e, p in roster]


def test_letsplay_auto_sorts_when_no_order_was_saved():
    roster = _legal_xi_pairs()[::-1]
    names = _xi_names(None, custom=False, roster=roster)
    assert names[0] == "Bat1"           # highest batting rating opens
    assert names[-1] == "Bwl4"
