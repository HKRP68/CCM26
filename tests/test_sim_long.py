"""/sim ODI and /sim Test: long formats on the Conditions Engine.

The short /sim engine is T20-tuned and collapses sides over 50 overs, so the
long formats run on engine.sim.match through services.sim_long. These tests
pin the parsing, the cricket the engine plays (quotas, innings counts, stumps,
draws) and the handler's delivery and cooldown behaviour for both formats.
"""

import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

logging.disable(logging.CRITICAL)

import handlers.sim as sim_mod
from engine.sim import situation
from engine.sim.config import get_config
from services import sim_long
from services.match_formats import resolve_format, get_format


def _xi(tag, base):
    cats = ["Batsman"] * 4 + ["Wicket Keeper"] + ["All-rounder"] * 2 + ["Bowler"] * 4
    styles = [""] * 5 + ["Off spin", "Medium-fast", "Fast", "Fast-medium", "Leg spin", "Fast"]
    return [{"id": f"{tag}{i}", "name": f"{tag} <P{i}>", "rating": base,
             "bat_rating": base - i * 3, "bowl_rating": 30 if i < 5 else base,
             "category": c, "bowl_style": s, "bat_hand": "Right", "bowl_hand": "Right"}
            for i, (c, s) in enumerate(zip(cats, styles))]


# ── parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,key", [
    ("ODI", "ODI"), ("odi", "ODI"), ("50", "ODI"), ("One-Day", "ODI"),
    ("Test", "Test"), ("TEST", "Test"), ("5day", "Test"),
    ("T20", "T20"), ("10", "T10"),
])
def test_format_tokens_resolve(token, key):
    assert resolve_format(token) == key


def test_parse_format_long_formats_and_custom_cap():
    fmt, err = sim_mod._parse_format(["odi"])
    assert err is None and fmt["label"] == "ODI" and fmt["overs"] == 50 and fmt["long_format"]
    fmt, err = sim_mod._parse_format(["Test"])
    assert err is None and fmt["label"] == "Test" and fmt["long_format"]
    fmt, err = sim_mod._parse_format(["51"])
    assert fmt is None and "between 1 and 20" in err
    fmt, err = sim_mod._parse_format(["T20"])
    assert not fmt.get("long_format")


# ── the engine through services.sim_long ────────────────────────────────

def test_odi_plays_two_innings_within_the_laws():
    res = sim_long.simulate_long_match(_xi("A", 82), _xi("B", 80), "Alpha", "Bravo",
                                       "ODI", pitch="Even", stadium=None, seed=11)
    assert res["format"] == "ODI"
    assert len(res["innings"]) == 2
    for inn in res["innings"]:
        assert inn["legal_balls"] <= 300
        assert all(w["balls"] <= 60 for w in inn["bowling"])   # 10-over quota
    assert res["innings"][1]["target"] is not None
    assert res["potm"] and res["result"]["text"]


def test_test_match_has_stumps_and_a_valid_result():
    res = sim_long.simulate_long_match(_xi("A", 82), _xi("B", 80), "Alpha", "Bravo",
                                       "Test", pitch="Even", seed=7)
    assert res["format"] == "Test"
    assert 2 <= len(res["innings"]) <= 4
    assert res["stumps"], "a multi-day Test reports close of play"
    assert all(s["text"].startswith(f"Stumps, Day {s['day']}") for s in res["stumps"])
    assert re.search(r"won by|drawn|tied", res["result"]["text"])


def test_tests_can_be_drawn():
    """Lost time and batting out the last day make draws possible again."""
    texts = [sim_long.simulate_long_match(_xi("A", 82), _xi("B", 82), "Alpha", "Bravo",
                                          "Test", pitch="Flat", seed=s)["result"]["text"]
             for s in range(12)]
    assert any("drawn" in t for t in texts)
    assert any("won by" in t for t in texts)


def test_saving_the_match_shuts_down_shots():
    cfg = get_config()
    base = situation.Situation(fmt="Test", innings=4, over=100, batter_balls=40)
    saving = situation.evaluate(
        situation.Situation(fmt="Test", innings=4, over=100, batter_balls=40,
                            save_match=True), cfg)
    normal = situation.evaluate(base, cfg)
    assert saving.aggression_set is not None and saving.aggression_set < 40
    assert saving.wicket < normal.wicket
    assert "saving" in saving.tags


# ── rendering ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["ODI", "Test"])
def test_rendering_escapes_names_and_uses_one_quote(fmt):
    res = sim_long.simulate_long_match(_xi("A", 82), _xi("B", 80), "Team <A>", "B & Co",
                                       fmt, pitch="Even", seed=3)
    texts = ([sim_long.render_match_setup(res, "desc")]
             + [sim_long.render_long_innings_card(res, i) for i in res["innings"]]
             + [sim_long.render_stumps(res), sim_long.render_long_result(res)])
    for t in texts:
        assert "<P" not in t and "<A>" not in t and " & " not in t
        assert t.count("<blockquote expandable>") <= 1
    payload = sim_long.long_feed_payload(res, "Team <A>", "B & Co")
    assert payload["format"] == fmt
    assert all(inn["commentary"] for inn in payload["innings"])


def test_summary_image_only_for_odi():
    odi = sim_long.simulate_long_match(_xi("A", 82), _xi("B", 80), "A", "B", "ODI",
                                       pitch="Even", seed=5)
    test = sim_long.simulate_long_match(_xi("A", 82), _xi("B", 80), "A", "B", "Test",
                                        pitch="Even", seed=5)
    kw = sim_long.summary_image_kwargs(odi)
    assert kw["overs_total"] == 50 and kw["inn1_team"] == odi["innings"][0]["team"]
    assert sim_long.summary_image_kwargs(test) is None


def test_player_of_the_match_prefers_the_winning_side():
    res = {"result": {"winner": "W"},
           "impact": [{"name": "loser star", "team": "L", "impact": 10.0},
                      {"name": "winner star", "team": "W", "impact": 8.8}]}
    assert sim_long.player_of_the_match(res) == ("winner star", "W")
    res["impact"][1]["impact"] = 5.0          # a loser who dominated still wins it
    assert sim_long.player_of_the_match(res) == ("loser star", "L")
    res["result"]["winner"] = None            # a draw: best in the match
    assert sim_long.player_of_the_match(res)[0] == "loser star"


# ── the handler ─────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, sent):
        self.sent = sent
        self.reply_to_message = None
        self.docs = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return _Msg(self.sent)

    async def edit_text(self, text, **kwargs):
        self.sent.append(text)
        return self

    async def reply_photo(self, **kwargs):
        return None

    async def reply_document(self, *a, **kwargs):
        self.docs.append(kwargs.get("caption"))
        return None


@pytest.fixture
def handler_env(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    from models import User, UserStats

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.close = lambda: None
    user = User(telegram_id=1001, first_name="Caller", team_name="Alpha")
    db.add(user)
    db.commit()

    monkeypatch.setattr(sim_mod, "get_session", lambda: db)
    monkeypatch.setattr(sim_mod, "sync_telegram_user",
                        lambda session, tg: db.query(User).filter(User.telegram_id == tg.id).first())
    monkeypatch.setattr(sim_mod, "_get_ordered_roster",
                        lambda session, uid: [(None, SimpleNamespace(rating=80))] * 11)
    monkeypatch.setattr(sim_mod, "validate_xi", lambda roster: (True, []))
    monkeypatch.setattr(sim_mod, "_xi_from_roster", lambda s, uid, roster: _xi("U", 82))
    monkeypatch.setattr(sim_mod, "_build_bot_xi", lambda session, avg: _xi("S", 80))
    monkeypatch.setattr(sim_mod, "build_commentary_picker", lambda session: None)
    monkeypatch.setattr(sim_mod, "list_pitches", lambda: ["Even"])
    monkeypatch.setattr(sim_mod, "get_pitch_meta", lambda p: {"description": "true"})
    monkeypatch.setattr(sim_mod, "get_config", lambda: {})
    monkeypatch.setattr(sim_long, "_pick_stadium", lambda: None)
    monkeypatch.setattr(sim_long, "render_long_summary_image", lambda res, **k: None)
    short_calls = []
    monkeypatch.setattr(sim_mod, "simulate_match", lambda *a, **k: short_calls.append(1))

    async def _no_card(*a, **k):
        return None
    from services import scorecard_delivery
    monkeypatch.setattr(scorecard_delivery, "send_potm_card", _no_card)

    def run(args):
        sent = []
        message = _Msg(sent)
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_user=SimpleNamespace(id=1001, is_bot=False),
            effective_chat=SimpleNamespace(id=1))
        context = SimpleNamespace(args=args, bot=None)
        asyncio.run(sim_mod.sim_handler(update, context))
        return sent, message.docs

    stats = lambda: db.query(UserStats).filter(UserStats.user_id == user.id).first()
    return SimpleNamespace(run=run, stats=stats, short_calls=short_calls)


@pytest.mark.parametrize("fmt,banner", [("ODI", "ODI (50 ov)"), ("Test", "Test match (5 days)")])
def test_handler_plays_a_long_format(handler_env, fmt, banner):
    sent, docs = handler_env.run([fmt])
    joined = "\n".join(sent)
    assert handler_env.short_calls == [], "long formats must not use the T20 engine"
    assert banner in joined
    assert "MATCH RESULT" in joined and "Next /sim in" in joined
    assert docs == ["📜 Ball-by-ball commentary (JSON)"]
    assert handler_env.stats().last_sim is not None
    if fmt == "Test":
        assert "CLOSE OF PLAY" in joined


def test_long_format_shares_the_sim_cooldown(handler_env):
    handler_env.run(["ODI"])
    sent, _ = handler_env.run(["Test"])
    assert "cooling down" in "\n".join(sent)
