"""The batting order saved with /sbo, used everywhere gameplay happens.

``services.batting_order_service`` is the single place that decides whose order
an XI bats in, so these tests pin down both halves of that decision — a saved
order is used verbatim, no saved order falls back to batting rating — and then
follow the ``order_locked`` marker into the three places that used to make the
call for themselves: the auto-sim (/sim), the Mini App setup screen (/wpm) and
the Quick Match innings distribution.
"""

import logging
import random
from types import SimpleNamespace

logging.disable(logging.CRITICAL)

from services import batting_order_service as bos


# ════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════

def _dict_xi(prefix="P", ratings=(90, 30, 70, 40, 85, 55, 20, 65, 45, 75, 35)):
    """11 engine player dicts whose batting ratings are deliberately jumbled, so
    "kept as saved" and "sorted by rating" can never be confused."""
    cats = (["Batsman"] * 4 + ["Wicket Keeper"] + ["All-rounder"] * 2
            + ["Bowler"] * 4)
    return [{
        "roster_id": i + 1, "player_id": i + 1, "name": f"{prefix}{i + 1}",
        "rating": 70, "bat_rating": r, "bowl_rating": 60,
        "category": cats[i], "bowl_style": "Fast",
        "bowl_hand": "Right", "bat_hand": "Right",
    } for i, r in enumerate(ratings)]


def _pair(rid, name, bat, rating=None):
    entry = SimpleNamespace(id=rid)
    player = SimpleNamespace(id=rid, name=name, bat_rating=bat,
                             rating=rating if rating is not None else bat,
                             category="Batsman", bowl_style="Fast")
    return entry, player


class _FakeSession:
    """Stands in for a DB session for ``user_has_custom_order``."""

    def __init__(self, saved_at):
        self._saved_at = saved_at

    def query(self, *_a, **_kw):
        return self

    def filter(self, *_a, **_kw):
        return self

    def first(self):
        return (self._saved_at,)


# ════════════════════════════════════════════════════════════════════
# The two states
# ════════════════════════════════════════════════════════════════════

def test_saved_order_is_used_verbatim_and_marked():
    xi = _dict_xi()
    ordered = bos.order_dicts(xi, custom=True)
    assert [p["name"] for p in ordered] == [p["name"] for p in xi]
    assert bos.is_order_locked(ordered)


def test_no_saved_order_falls_back_to_batting_rating():
    xi = _dict_xi()
    ordered = bos.order_dicts(xi, custom=False)
    brs = [p["bat_rating"] for p in ordered]
    assert brs == sorted(brs, reverse=True)
    assert not bos.is_order_locked(ordered)


def test_ordering_does_not_mutate_the_caller_s_list():
    xi = _dict_xi()
    before = [dict(p) for p in xi]
    bos.order_dicts(xi, custom=True)
    assert xi == before


def test_pairs_follow_the_same_two_states():
    pairs = [_pair(1, "Tail", 30), _pair(2, "Star", 92), _pair(3, "Mid", 61)]
    assert [p.name for _e, p in bos.order_pairs(pairs, True)] == \
        ["Tail", "Star", "Mid"]
    assert [p.name for _e, p in bos.order_pairs(pairs, False)] == \
        ["Star", "Mid", "Tail"]


def test_flag_reads_from_the_user_row_and_the_db():
    assert bos.has_custom_order(SimpleNamespace(batting_order_set_at=None)) is False
    assert bos.has_custom_order(SimpleNamespace(batting_order_set_at=object())) is True
    assert bos.has_custom_order(SimpleNamespace()) is False
    assert bos.user_has_custom_order(_FakeSession(object()), 1) is True
    assert bos.user_has_custom_order(_FakeSession(None), 1) is False


def test_an_empty_or_unmarked_xi_is_not_treated_as_saved():
    assert bos.is_order_locked([]) is False
    assert bos.is_order_locked(None) is False
    assert bos.is_order_locked(_dict_xi()) is False


# ════════════════════════════════════════════════════════════════════
# Openers and the next player down
# ════════════════════════════════════════════════════════════════════

def test_openers_are_slots_one_and_two():
    xi = bos.order_dicts(_dict_xi(), custom=True)
    op1, op2 = bos.openers_from_order(xi)
    assert (op1["name"], op2["name"]) == ("P1", "P2")


def test_openers_need_two_players():
    assert bos.openers_from_order([]) == (None, None)
    assert bos.openers_from_order(_dict_xi()[:1]) == (None, None)


def test_next_batsman_is_the_next_one_down_the_order():
    xi = _dict_xi()
    # 1 and 2 opened; 2 is out, so 3 walks in.
    stats = {2: {"out": True}}
    assert bos.next_batsman_index(xi, stats, 0, 1) == 2


def test_next_batsman_skips_everyone_already_out():
    xi = _dict_xi()
    # 1 still in at the crease; 2, 3 and 4 are gone. Next is 5 (index 4).
    stats = {2: {"out": True}, 3: {"out": True}, 4: {"out": True}}
    assert bos.next_batsman_index(xi, stats, 0, 2) == 4


def test_next_batsman_tolerates_string_keyed_stats():
    """Match state round-trips through JSON, so stat keys come back as strings."""
    xi = _dict_xi()
    stats = {"2": {"out": True}, "3": {"out": True}}
    assert bos.next_batsman_index(xi, stats, 0, 1) == 3


def test_next_batsman_is_none_when_the_innings_is_over():
    xi = _dict_xi()[:3]
    stats = {3: {"out": True}}
    assert bos.next_batsman_index(xi, stats, 0, 1) is None


# ════════════════════════════════════════════════════════════════════
# /sim — the auto-simulated match
# ════════════════════════════════════════════════════════════════════

def _sim_xi(prefix, base):
    """A legal XI for the sim engine: 4 bat, 1 keeper, 2 all-rounders, 4 bowlers."""
    cats = (["Batsman"] * 4 + ["Wicket Keeper"] + ["All-rounder"] * 2
            + ["Bowler"] * 4)
    # Batting ratings ascend down the list, so a rating sort would REVERSE it.
    return [{
        "name": f"{prefix}{i + 1}", "rating": base,
        "bat_rating": 30 + i * 5, "bowl_rating": base + 5,
        "category": c, "bowl_style": "Fast",
        "bowl_hand": "Right", "bat_hand": "Right",
    } for i, c in enumerate(cats)]


def test_sim_bats_a_saved_order_exactly_as_saved():
    from services.sim_match import simulate_innings
    random.seed(11)
    bat = bos.order_dicts(_sim_xi("H", 75), custom=True)
    inn = simulate_innings(bat, _sim_xi("A", 75), 10, "Hard", 1, "H", "A")
    assert [p["name"] for p in inn["order"]] == [p["name"] for p in bat]


def test_sim_still_sorts_an_xi_with_no_saved_order():
    from services.sim_match import simulate_innings
    random.seed(11)
    bat = _sim_xi("H", 75)
    inn = simulate_innings(bat, _sim_xi("A", 75), 10, "Hard", 1, "H", "A")
    brs = [p["bat_rating"] for p in inn["order"]]
    assert brs == sorted(brs, reverse=True)


# ════════════════════════════════════════════════════════════════════
# The Mini App setup screen
# ════════════════════════════════════════════════════════════════════

def test_miniapp_locks_openers_for_a_saved_order():
    from services.match_webapp_service import _apply_saved_batting_order
    xi = bos.order_dicts(_dict_xi(), custom=True)
    state = {"bat_xi": xi}
    assert _apply_saved_batting_order(state) is True
    assert state["openers_done"] is True
    assert [p["name"] for p in state["batting_order"]] == [p["name"] for p in xi]
    assert (state["striker_idx"], state["non_striker_idx"],
            state["next_batsman_idx"]) == (0, 1, 2)


def test_miniapp_still_asks_a_side_with_no_saved_order():
    from services.match_webapp_service import _apply_saved_batting_order
    state = {"bat_xi": _dict_xi()}
    assert _apply_saved_batting_order(state) is False
    assert not state.get("openers_done")
    assert "batting_order" not in state


def test_miniapp_never_overrides_openers_already_chosen():
    from services.match_webapp_service import _apply_saved_batting_order
    xi = bos.order_dicts(_dict_xi(), custom=True)
    state = {"bat_xi": xi, "openers_done": True, "striker_idx": 7}
    assert _apply_saved_batting_order(state) is False
    assert state["striker_idx"] == 7


def test_miniapp_lock_ignores_players_subbed_out_of_the_xi():
    from services.match_webapp_service import _apply_saved_batting_order
    xi = bos.order_dicts(_dict_xi(), custom=True)
    xi[0] = {**xi[0], "active": False}
    state = {"bat_xi": xi}
    assert _apply_saved_batting_order(state) is True
    assert state["batting_order"][0]["name"] == "P2"


# ════════════════════════════════════════════════════════════════════
# Quick Match
# ════════════════════════════════════════════════════════════════════

def test_quick_match_renumbers_positions_to_the_batting_order(monkeypatch):
    """Quick Match weights runs and balls by ``position``, so the field has to
    mean batting position — not the roster slot the player happened to land in."""
    import services.quick_match_service as qms

    roster = [(SimpleNamespace(id=i + 1, order_position=i + 1),
               SimpleNamespace(id=i + 1, name=f"P{i + 1}", rating=70,
                               bat_rating=r, bowl_rating=60,
                               category="Batsman", country="IND",
                               version="Base"))
              for i, r in enumerate((30, 90, 50))]

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def get(self, _id):
            return SimpleNamespace(captain_roster_id=None)

        def join(self, *_a, **_kw):
            return self

        def filter(self, *_a, **_kw):
            return self

        def order_by(self, *_a, **_kw):
            return self

        def all(self):
            return self._rows

    session = SimpleNamespace(query=lambda *_a, **_kw: _Q(roster))
    monkeypatch.setattr(bos, "user_has_custom_order", lambda *_a, **_kw: False)

    xi = qms.get_user_xi(session, 1)
    # No saved order → rating sort, and positions renumbered 1..n to match.
    assert [p["name"] for p in xi] == ["P2", "P3", "P1"]
    assert [p["position"] for p in xi] == [1, 2, 3]

    monkeypatch.setattr(bos, "user_has_custom_order", lambda *_a, **_kw: True)
    xi = qms.get_user_xi(session, 1)
    assert [p["name"] for p in xi] == ["P1", "P2", "P3"]
    assert [p["position"] for p in xi] == [1, 2, 3]
