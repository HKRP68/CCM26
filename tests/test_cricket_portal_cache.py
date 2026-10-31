from types import SimpleNamespace
from unittest.mock import Mock, patch
from services import cricket_portal_cache as cache


def scope():
    cache.invalidate()
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url='sqlite:///portal-test'))
    return session, SimpleNamespace(id=21), SimpleNamespace(id=3)


def test_historical_responses_reuse_cache_but_callers_cannot_mutate_it():
    args = scope()
    build = Mock(return_value={'rows': [{'runs': 50}]})
    with patch('services.live_records._source_signature', return_value=(1,)):
        first = cache.read(*args, 'explorer', {}, build)
        first['rows'][0]['runs'] = 99
        second = cache.read(*args, 'explorer', {}, build)
    assert second['rows'][0]['runs'] == 50
    assert build.call_count == 1


def test_completion_and_committed_corrections_invalidate_history():
    args = scope()
    build = Mock(side_effect=[{'runs': 50}, {'runs': 70}, {'runs': 90}])
    with patch('services.live_records._source_signature', side_effect=[(1,), (2,), (2,)]):
        assert cache.read(*args, 'explorer', {}, build)['runs'] == 50
        assert cache.read(*args, 'explorer', {}, build)['runs'] == 70
        cache.invalidate()
        assert cache.read(*args, 'explorer', {}, build)['runs'] == 90


def test_filters_are_part_of_cache_scope():
    args = scope()
    build = Mock(side_effect=[{'runs': 50}, {'runs': 10}])
    with patch('services.live_records._source_signature', return_value=(1,)):
        cache.read(*args, 'explorer', {'pitch': 'Green'}, build)
        assert cache.read(*args, 'explorer', {'pitch': 'Dusty'}, build)['runs'] == 10
    assert build.call_count == 2
