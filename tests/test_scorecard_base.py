"""The standard broadcast design stays readable and upgrades archived photos."""

import copy
import io
from itertools import combinations
from unittest.mock import patch

import pytest
from PIL import Image

from services import scorecard_card as card
from services.match_summary_card import CANVAS_H, CANVAS_W, generate_match_summary
from test_scorecard_themes import _cached_card, _summary_payload, panel  # noqa: F401


def _batting():
    return dict(team_name="Alpha", opponent_name="Bravo", total_runs=432,
                total_wickets=10, overs_str="20.0",
                batsmen_rows=[dict(name="Opener", runs=123, balls=54, fours=10,
                                   sixes=5, strike_rate=227.78, dismissal="not out", status="not_out")],
                fall_of_wickets=[], extras_dict={"total": 7, "wd": 7})


def _bowling():
    return dict(team_name="Bravo", opponent_name="Alpha", opp_score=1000,
                opp_wickets=10, opp_overs="20.0", fall_of_wickets=[],
                bowlers_rows=[dict(name="Bowler", overs="4.0", dots=10,
                                  runs_conceded=28, wickets=4, economy=7.0)])


def _image(png):
    assert png and png.startswith(b"\x89PNG")
    return Image.open(io.BytesIO(png))


@pytest.mark.parametrize("kind", ["summary", "batting", "bowling"])
def test_normal_and_default_use_the_same_broadcast_design(kind):
    fn, payload, size = {
        "summary": (generate_match_summary, _summary_payload(), (CANVAS_W, CANVAS_H)),
        "batting": (card.generate_batting_scorecard, _batting(), (1600, 1024)),
        "bowling": (card.generate_bowling_scorecard, _bowling(), (1600, 900)),
    }[kind]
    original = copy.deepcopy(payload)
    normal = fn(**payload, theme="normal")
    assert _image(normal).size == size
    assert fn(**payload) == normal
    assert fn(**payload, theme="unknown") == normal
    assert payload == original


@pytest.mark.parametrize("kind", ["batting", "bowling"])
@pytest.mark.parametrize("size_adjustment", [0, 80])
def test_statistics_do_not_overlap_even_with_large_fonts_and_long_labels(kind, size_adjustment):
    captured = []
    real = card._draw_stat_text
    def capture(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append((args[3], str(args[4]), args[5], result))
        return result

    value_key = "total_value" if kind == "batting" else "opp_score_value"
    label_key = "total_label" if kind == "batting" else "opp_score_label"
    settings = {kind: {
        value_key: {"size": size_adjustment},
        label_key: {"text": "TOTAL RUNS / WICKETS", "size": size_adjustment},
    }}
    fn = card.generate_batting_scorecard if kind == "batting" else card.generate_bowling_scorecard
    payload = _batting() if kind == "batting" else _bowling()
    with patch.object(card, "_draw_stat_text", side_effect=capture):
        png = fn(**payload, text_settings=settings)
    image = _image(png)
    assert len(captured) == (16 if kind == "batting" else 8)
    assert any(key == value_key and text == ("432/10" if kind == "batting" else "1000/10")
               for key, text, _, _ in captured)
    for _, _, (x, y, width, height), bounds in captured:
        assert x - 1 <= bounds[0] <= bounds[2] <= x + width + 1
        assert y - 1 <= bounds[1] <= bounds[3] <= y + height + 1
        assert 0 <= bounds[1] < bounds[3] <= image.height
    for first, second in combinations(captured, 2):
        a, b = first[3], second[3]
        overlap_x = min(a[2], b[2]) - max(a[0], b[0])
        overlap_y = min(a[3], b[3]) - max(a[1], b[1])
        assert overlap_x <= 0 or overlap_y <= 0, (first, second)


def test_startup_refreshes_old_photo_ids_once_and_keeps_match_history(panel, monkeypatch):
    _cached_card(panel)
    monkeypatch.setattr(panel.database, "engine", panel.engine)
    panel.database._migrate_scorecard_broadcast_cache()
    with panel.sessions() as session:
        row = session.query(panel.models.MatchScorecardImage).one()
        assert row.file_id is None
        assert row.payload_json == '{"runs":144}'
        assert row.caption == "Match 17" and row.delivered is True
        row.file_id = "new-broadcast-photo"
        session.commit()
    panel.database._migrate_scorecard_broadcast_cache()
    with panel.sessions() as session:
        assert session.query(panel.models.MatchScorecardImage).one().file_id == "new-broadcast-photo"
    assert panel.config.get_scorecard_theme() == "normal"
