import asyncio
import sys
import types
from types import SimpleNamespace

# Keep the heartbeat import independent from optional app/runtime deps that are
# not installed in the test environment.
telegram_mod = types.ModuleType("telegram")
telegram_ext_mod = types.ModuleType("telegram.ext")
telegram_ext_mod.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
sys.modules.setdefault("telegram", telegram_mod)
sys.modules.setdefault("telegram.ext", telegram_ext_mod)

from services.match_heartbeat import _auto_decide

A_PICK_DELIVERY = "PICK_DELIVERY"
A_PICK_NEW_BOWLER = "PICK_NEW_BOWLER"


def _player(roster_id, name, bowl_rating):
    return {
        "roster_id": roster_id,
        "name": name,
        "bowl_rating": bowl_rating,
    }


class _FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        })


def test_timeout_auto_bowler_ignores_inactive_impact_replacement(monkeypatch):
    saved = []
    rendered = []

    def fake_save_state(context, match_id, state, next_action=None):
        saved.append({
            "match_id": match_id,
            "state": state.copy(),
            "next_action": next_action,
        })

    async def fake_render_screen(context, match_id):
        rendered.append(match_id)

    fake_state_store = types.ModuleType("services.match_state_store")
    fake_state_store.save_state = fake_save_state
    fake_state_store.A_PICK_DELIVERY = A_PICK_DELIVERY
    fake_state_store.A_PICK_LENGTH = "PICK_LENGTH"
    fake_state_store.A_PICK_SHOT = "PICK_SHOT"
    fake_state_store.A_PICK_NEW_BATSMAN = "PICK_NEW_BATSMAN"
    fake_state_store.A_PICK_NEW_BOWLER = A_PICK_NEW_BOWLER
    fake_state_store.A_PICK_OPENERS = "PICK_OPENERS"
    fake_state_store.A_PICK_OPENING_BOWLER = "PICK_OPENING_BOWLER"
    monkeypatch.setitem(sys.modules, "services.match_state_store", fake_state_store)

    fake_match = types.ModuleType("handlers.match")
    fake_match.render_screen = fake_render_screen
    monkeypatch.setitem(sys.modules, "handlers.match", fake_match)

    replaced_bowler = {
        **_player(201, "Replaced Bowler", 99),
        "active": False,
        "impact_replaced": True,
    }
    impact_bowler = {
        **_player(202, "Impact Bowler", 80),
        "active": True,
        "impact_replacement": True,
    }
    previous_bowler = _player(203, "Previous Bowler", 70)
    state = {
        "chat_id": 123,
        "bowl_xi": [replaced_bowler, impact_bowler, previous_bowler],
        "prev_bowler_rid": previous_bowler["roster_id"],
        "bowl_stats": {
            201: {"overs_done": 0},
            202: {"overs_done": 0},
            203: {"overs_done": 0},
        },
        "overs": 5,
    }
    context = SimpleNamespace(bot=_FakeBot())

    asyncio.run(_auto_decide(context, 99, state, A_PICK_NEW_BOWLER))

    assert state["current_bowler"]["roster_id"] == impact_bowler["roster_id"]
    assert saved[0]["next_action"] == A_PICK_DELIVERY
    assert saved[0]["state"]["current_bowler"]["roster_id"] == impact_bowler["roster_id"]
    assert rendered == [99]
