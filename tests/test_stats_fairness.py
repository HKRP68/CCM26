"""Anti stat-farming gate: a wide Team Overall gap in /letsplay and /wpm voids
career player stats. Covers the pure helpers plus the persistence early-return."""

import services.player_stats_service as stats_svc
from services.player_stats_service import (
    STATS_FAIRNESS_OVR_GAP,
    is_stat_farming_mismatch,
    overall_gap,
    persist_player_game_stats,
    team_overall,
)


def _xi(rating, n=11):
    return [{"rating": rating} for _ in range(n)]


def test_team_overall_is_average_rating_rounded():
    assert team_overall(_xi(90)) == 90
    assert team_overall([{"rating": 80}, {"rating": 85}]) == 82  # 82.5 -> 82
    assert team_overall([]) == 0
    # Tolerates missing/None ratings without blowing up.
    assert team_overall([{"rating": None}, {"rating": 88}]) == 44


def test_gap_at_threshold_is_a_mismatch():
    strong, weak = _xi(90), _xi(80)
    assert overall_gap(strong, weak) == STATS_FAIRNESS_OVR_GAP
    assert is_stat_farming_mismatch(strong, weak) is True
    # Order does not matter — the gap is absolute.
    assert is_stat_farming_mismatch(weak, strong) is True


def test_gap_below_threshold_still_counts():
    assert is_stat_farming_mismatch(_xi(90), _xi(81)) is False   # gap 9
    assert is_stat_farming_mismatch(_xi(85), _xi(85)) is False   # even sides


def test_disabled_state_persists_no_career_stats():
    """A ``stats_disabled`` match must short-circuit before any DB work — a bare
    ``object()`` session would raise if the function tried to touch it."""
    state = {
        "stats_disabled": True,
        "innings": 2,
        "bat_stats": {"1": {"runs": 50, "balls": 20}},
        "bowl_stats": {"1": {"balls": 24, "runs": 30, "wickets": 3}},
    }
    counts = persist_player_game_stats(object(), state)
    assert counts["batting"] == 0
    assert counts["bowling"] == 0
    assert counts.get("skipped") == "stats_disabled"


def test_enabled_match_is_unaffected_by_the_gate(monkeypatch):
    """Without the flag, persistence proceeds normally (batting is credited)."""
    recorded = []

    def _fake_update_batting(session, user_id, player_id, batting):
        recorded.append((user_id, player_id, batting))
        return True

    monkeypatch.setattr(stats_svc, "_update_batting", _fake_update_batting)
    monkeypatch.setattr(stats_svc, "_update_bowling", lambda *a, **k: False)

    state = {
        "innings": 2,
        "bat_team_id": 7,
        "bat_xi": [{"roster_id": 1, "player_id": 11}],
        "bat_stats": {"1": {"runs": 50, "balls": 20}},
        "inn1_stats_saved": True,
    }
    counts = persist_player_game_stats(object(), state)
    assert counts["batting"] == 1
    assert recorded == [(7, 11, {"runs": 50, "balls": 20})]
