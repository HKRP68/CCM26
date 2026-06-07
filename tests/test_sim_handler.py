"""Tests for /sim handler team assembly helpers."""
import logging
from types import SimpleNamespace

logging.disable(logging.CRITICAL)

from services.sim_team import append_distinct_base_players, base_player_id


def _player(player_id, parent_id=None):
    return SimpleNamespace(id=player_id, parent_player_id=parent_id)


def test_append_distinct_base_players_filters_versions_and_backfills_to_eleven():
    base = _player(1)
    variant = _player(2, parent_id=1)
    initial = [base, variant, _player(3)]
    backfill = [_player(4), _player(5), _player(6), _player(7), _player(8),
                _player(9), _player(10), _player(11), _player(12), _player(13)]

    rows = append_distinct_base_players([], initial)
    assert [base_player_id(p) for p in rows] == [1, 3]

    append_distinct_base_players(rows, backfill)

    base_ids = [base_player_id(p) for p in rows]
    assert len(rows) == 11
    assert len(base_ids) == len(set(base_ids))
