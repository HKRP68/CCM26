"""Career streak / active-day accounting (services/match_rewards.py).

/myprofile reports "Current Streak", "Best Streak" and "Active Days". Those
counters used to move only in the chat /playmatch finalize, so every match
played through /cipl, /letsplay, a Super Over or the Mini App left them frozen.
They now live in the shared reward core; these tests pin that behaviour down.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from services.match_rewards import (
    apply_win_streak, record_active_day, record_match_result_stats)


def _user(**kw):
    base = dict(id=1, win_streak=0, best_streak=0, active_days=0,
                last_match_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeSession:
    """Minimal session: query(User).get(id) resolves from a dict."""

    def __init__(self, users):
        self._users = users

    def query(self, _model):
        return self

    def get(self, uid):
        return self._users.get(uid)


# ── streak ───────────────────────────────────────────────────────────

def test_win_extends_streak_and_best():
    u = _user(win_streak=4, best_streak=4)
    apply_win_streak(u, True)
    assert u.win_streak == 5
    assert u.best_streak == 5


def test_loss_resets_current_but_keeps_best():
    u = _user(win_streak=5, best_streak=5)
    apply_win_streak(u, False)
    assert u.win_streak == 0
    assert u.best_streak == 5


def test_best_streak_only_grows():
    u = _user(win_streak=1, best_streak=9)
    apply_win_streak(u, True)
    assert u.win_streak == 2
    assert u.best_streak == 9


# ── active days ──────────────────────────────────────────────────────

def test_first_ever_match_counts_an_active_day():
    u = _user()
    assert record_active_day(u) is True
    assert u.active_days == 1
    assert u.last_match_date is not None


def test_second_match_same_day_does_not_double_count():
    now = datetime.utcnow()
    u = _user(active_days=3, last_match_date=now - timedelta(minutes=5))
    assert record_active_day(u, now) is False
    assert u.active_days == 3
    assert u.last_match_date == now


def test_match_on_a_new_day_counts_again():
    now = datetime.utcnow()
    u = _user(active_days=3, last_match_date=now - timedelta(days=1))
    assert record_active_day(u, now) is True
    assert u.active_days == 4


# ── combined result recording ────────────────────────────────────────

def test_record_match_result_updates_both_sides():
    winner = _user(id=1, win_streak=2, best_streak=2)
    loser = _user(id=2, win_streak=7, best_streak=7)
    session = _FakeSession({1: winner, 2: loser})

    record_match_result_stats(session, 1, 2)

    assert winner.win_streak == 3 and winner.best_streak == 3
    assert winner.active_days == 1
    assert loser.win_streak == 0 and loser.best_streak == 7
    assert loser.active_days == 1


def test_tie_keeps_streaks_but_counts_the_day():
    a = _user(id=1, win_streak=3, best_streak=3)
    b = _user(id=2, win_streak=0, best_streak=5)
    session = _FakeSession({1: a, 2: b})

    record_match_result_stats(session, None, None, tie_user_ids=(1, 2))

    assert a.win_streak == 3 and b.win_streak == 0
    assert a.active_days == 1 and b.active_days == 1


def test_reward_core_moves_the_streak_counters():
    """award_match_rewards_core is what /cipl, /letsplay, the Super Over and the
    Mini App all call — the streak must move there, not only in chat matches."""
    import services.match_rewards as mr

    winner = _user(id=1, total_coins=0, total_gems=0, matches_played=0,
                   matches_won=0, matches_lost=0, win_streak=1, best_streak=1)
    loser = _user(id=2, total_coins=0, total_gems=0, matches_played=0,
                  matches_won=0, matches_lost=0, win_streak=6, best_streak=6)
    session = _FakeSession({1: winner, 2: loser})

    # Stub the config/season/event lookups the core makes lazily.
    import services.config_service as cs
    import services.activity_service as acts
    orig_cfg, orig_log = cs.get_config, acts.log_activity
    cs.get_config = lambda _s=None: {
        "match_win_coins_per_over": 10, "match_win_gems_per_over": 0,
        "match_loss_coins_per_over": 5, "match_loss_gems_per_over": 0}
    acts.log_activity = lambda *a, **k: None
    try:
        mr.award_match_rewards_core(session, 1, 2, overs=20, is_vsbot=True)
    finally:
        cs.get_config, acts.log_activity = orig_cfg, orig_log

    assert winner.matches_won == 1 and winner.win_streak == 2
    assert winner.best_streak == 2 and winner.active_days == 1
    assert loser.matches_lost == 1 and loser.win_streak == 0
    assert loser.best_streak == 6 and loser.active_days == 1
