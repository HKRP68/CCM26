"""The deciding Super Over result/card must not queue behind database work."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import handlers.super_over as SO


def test_result_and_main_card_do_not_wait_for_slow_finalization(monkeypatch):
    from services import league_records, tournament_channels, match_rewards
    from services import match_webapp_service, match_state_store, post_match

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    events = []
    loop_thread = threading.get_ident()

    class Session:
        def get(self, *args):
            return None

        def commit(self):
            finished.set()

        def rollback(self):
            pass

        def close(self):
            pass

    def slow_session():
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3), 'The event loop could not deliver the main card'
        return Session()

    async def send_message(*args, **kwargs):
        events.append('result' if 'SUPER OVER RESULT' in str(args) else 'message')
        return SimpleNamespace(message_id=1)

    async def send_main(*args, **kwargs):
        # Ensure the DB is actually stalled while the summary is delivered.
        assert await asyncio.to_thread(entered.wait, 1)
        assert 'result' in events
        assert not finished.is_set()
        events.append('main')
        release.set()
        return True

    def so_card(*args):
        events.append('super-over')
        return None

    monkeypatch.setattr(SO, 'get_session', slow_session)
    monkeypatch.setattr(SO, '_send_main_scorecard', send_main)
    monkeypatch.setattr(SO, '_build_super_over_card', so_card)
    monkeypatch.setattr(SO, '_super_over_mvp', lambda *a: (None, None))
    monkeypatch.setattr(SO, '_turning_points', lambda *a: [])
    monkeypatch.setattr(SO, '_so_scoreline', lambda *a: 'XI 10/0')
    monkeypatch.setattr(SO, '_final_reward_text', lambda *a: 'Rewards')
    monkeypatch.setattr(SO, '_super_over_miniapp_innings', lambda *a: [])
    monkeypatch.setattr(SO, '_super_over_summary', lambda *a: {})
    monkeypatch.setattr(SO, '_spectate_markup', lambda *a: None)
    monkeypatch.setattr(match_rewards, 'award_match_rewards_core', lambda *a, **kw: (0, 0, 0, 0))
    monkeypatch.setattr(match_webapp_service, 'save_final_scorecard', lambda *a, **kw: True)
    monkeypatch.setattr(match_state_store, 'cleanup_state', lambda *a: None)
    monkeypatch.setattr(post_match, 'announce', AsyncMock())
    monkeypatch.setattr(league_records, 'announce', AsyncMock())
    monkeypatch.setattr(tournament_channels, 'publish_match', AsyncMock())
    so = dict(mid=42, chat_id=-123, main={}, main_state={}, so_number=1,
              results=[], score={1: (10, 0), 2: (8, 1)}, first_bat_uid=1,
              teams={1: dict(name='A', tg=1), 2: dict(name='B', tg=2)})
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send_message),
                          bot_data={SO._so_key(42): so})
    try:
        asyncio.run(SO._finalize(ctx, 42, 1, 2))
    finally:
        release.set()
    assert events.index('result') < events.index('main') < events.index('super-over')
    assert finished.is_set()
    assert SO._so_key(42) not in ctx.bot_data
