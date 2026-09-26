"""Guided first session — the new-player journey after /debut.

Most players who leave do it in the first session: /debut hands out a squad
and then nothing tells them what to do with it. This module is a short
checklist of the actions that make a player come back (join the community,
claim, collect the daily, play and win a match, open a pack), each paid once,
with a finale bonus for finishing.

Progress is driven by the quest event stream. Every handler that matters
already calls ``quest_service.track_event`` with a standard event key
("claim", "daily", "vsbot_played"…), and ``track_event`` forwards those keys to
:func:`on_event` here. So no gameplay handler needed a new hook: the one
exception is joining the Official GC, which Telegram tells us about directly
(``handlers/onboarding.py`` and the group welcome handler).

State lives on ``User``:
  * ``onboarding_started_at``: stamped by /debut. NULL = an account that
    predates the journey, which is never shown it.
  * ``onboarding_steps``: comma-separated keys already completed and paid.
  * ``onboarding_done_at``: stamped when every step is done (finale paid).

Completing a step inside ``track_event`` happens on the caller's session and
cannot send Telegram messages, so the "✅ step done — next up…" card is queued
in :data:`_PENDING` and delivered by a short bot job (:func:`flush_pending`).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    key: str
    title: str          # checklist line
    how: str            # one-line "how to do it"
    command: str        # the command that does it ("" for none)
    miniapp_tab: str    # Mini App screen for the "do it" button ("" for none)
    coins: int
    gems: int
    events: tuple       # quest event keys that complete it


STEPS = (
    Step("gc", "Join the Official GC",
         "Match partners, giveaways and help from the admins live there.",
         "", "", 300, 0, ()),
    Step("claim", "Claim your first player",
         "A free player and coins, every hour.",
         "/claim", "", 200, 0, ("claim",)),
    Step("daily", "Collect your daily reward",
         "Come back every day, because the streak bonus grows.",
         "/daily", "daily", 200, 0, ("daily",)),
    Step("match", "Play your first match",
         "Practice against the bot: no opponent needed.",
         "/vsbot", "", 500, 5,
         ("match_played", "vsbot_played", "quick_match_played")),
    Step("pack", "Open a pack",
         "Free packs refill on a timer. Chase a high-rated card.",
         "", "freepack", 300, 0, ("free_pack_opened", "pack_open")),
    Step("win", "Win a match",
         "Beat the bot or a friend. Wins climb the season leaderboard.",
         "/vsbot", "", 500, 10,
         ("match_won", "vsbot_won", "quick_match_won")),
)
STEP_BY_KEY = {s.key: s for s in STEPS}
EVENT_TO_STEPS = {}
for _s in STEPS:
    for _e in _s.events:
        EVENT_TO_STEPS.setdefault(_e, []).append(_s.key)

FINALE_COINS = 1000
FINALE_GEMS = 25

# (telegram_id, step_key) completions waiting for their Telegram card.
_PENDING: deque = deque(maxlen=5000)


# ── Config ──────────────────────────────────────────────────────────

def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    try:
        from services.config_service import get_config
        return get_config()
    except Exception:
        return {}


def is_enabled(cfg=None) -> bool:
    return bool((_cfg(cfg) or {}).get("onboarding_enabled", True))


def gc_step_applies(cfg=None) -> bool:
    """The GC step only exists when there is an Official GC to join."""
    from services.gc_gate import official_group_id, join_url
    cfg = _cfg(cfg)
    return official_group_id(cfg) is not None and bool(join_url(cfg))


def active_steps(cfg=None):
    """The steps this deployment runs, in order."""
    return [s for s in STEPS if s.key != "gc" or gc_step_applies(cfg)]


# ── State helpers (pure on a User-like object) ──────────────────────

def done_keys(user) -> list:
    raw = (getattr(user, "onboarding_steps", None) or "").strip()
    return [k for k in raw.split(",") if k]


def is_in_onboarding(user, cfg=None) -> bool:
    """True while the user should see the checklist."""
    if user is None or not is_enabled(cfg):
        return False
    return (getattr(user, "onboarding_started_at", None) is not None
            and getattr(user, "onboarding_done_at", None) is None)


def next_step(user, cfg=None):
    done = set(done_keys(user))
    for s in active_steps(cfg):
        if s.key not in done:
            return s
    return None


def progress(user, cfg=None):
    """(done_count, total) over the active steps."""
    steps = active_steps(cfg)
    done = set(done_keys(user))
    return sum(1 for s in steps if s.key in done), len(steps)


def reward_label(coins, gems) -> str:
    parts = []
    if coins:
        parts.append(f"{coins:,} 🪙")
    if gems:
        parts.append(f"{gems} 💎")
    return " + ".join(parts) or "—"


# ── Mutations ───────────────────────────────────────────────────────

def start(user, now=None):
    """Begin the journey for a fresh /debut account. Idempotent."""
    if getattr(user, "onboarding_started_at", None) is None:
        user.onboarding_started_at = now or datetime.utcnow()
        user.onboarding_steps = ""
        user.onboarding_done_at = None


def complete_step(session, user, key, *, cfg=None, notify=True):
    """Mark ``key`` done for ``user`` and pay its reward, once.

    Returns a dict ``{"step", "coins", "gems", "finished"}`` when something was
    paid, or None when the step was already done, not active, or the user is
    not on the journey. The caller commits.
    """
    if not is_in_onboarding(user, cfg):
        return None
    step = STEP_BY_KEY.get(key)
    if step is None or step not in active_steps(cfg):
        return None
    done = done_keys(user)
    if key in done:
        return None

    done.append(key)
    user.onboarding_steps = ",".join(done)
    coins, gems = step.coins, step.gems
    user.total_coins = (user.total_coins or 0) + coins
    user.total_gems = (user.total_gems or 0) + gems

    finished = all(s.key in done for s in active_steps(cfg))
    if finished:
        user.onboarding_done_at = datetime.utcnow()
        user.total_coins += FINALE_COINS
        user.total_gems += FINALE_GEMS

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "onboarding",
                     f"Journey step: {step.title}"
                     + (" (journey complete)" if finished else ""),
                     coins_change=coins + (FINALE_COINS if finished else 0),
                     gems_change=gems + (FINALE_GEMS if finished else 0))
    except Exception:
        logger.exception("onboarding activity log failed (non-fatal)")

    if notify and getattr(user, "telegram_id", None):
        _PENDING.append((user.telegram_id, key))
    return {"step": step, "coins": coins, "gems": gems, "finished": finished}


def on_event(session, user_id, event_key):
    """Quest-event bridge: called by ``quest_service.track_event``.

    Cheap for everyone off the journey: one dict lookup for an event no step
    listens to, one primary-key read otherwise.
    """
    keys = EVENT_TO_STEPS.get(event_key)
    if not keys:
        return None
    try:
        from models import User
        user = session.get(User, user_id)
        if user is None or not is_in_onboarding(user):
            return None
        result = None
        for key in keys:
            result = complete_step(session, user, key) or result
        if result:
            session.flush()
        return result
    except Exception:
        logger.exception("onboarding on_event failed (non-fatal)")
        return None


def complete_gc_step_for(telegram_id):
    """Mark the GC step done for this Telegram user in its own session."""
    try:
        from database import get_session
        from models import User
        s = get_session()
        try:
            user = s.query(User).filter(User.telegram_id == telegram_id).first()
            if user is None:
                return None
            result = complete_step(s, user, "gc")
            if result:
                s.commit()
            return result
        finally:
            s.close()
    except Exception:
        logger.exception("onboarding gc step failed (non-fatal)")
        return None


# ── Telegram delivery ───────────────────────────────────────────────

def discard_pending(telegram_id):
    """Drop queued cards for one user (the caller is showing the card itself)."""
    keep = [item for item in _PENDING if item[0] != telegram_id]
    _PENDING.clear()
    _PENDING.extend(keep)


def drain_pending(limit=50):
    out = []
    while _PENDING and len(out) < limit:
        out.append(_PENDING.popleft())
    return out


def _load_for_cards(items):
    """Worker-thread read: the users behind queued completions."""
    from database import get_session
    from models import User
    ids = list({tg for tg, _ in items})
    s = get_session()
    try:
        rows = s.query(User).filter(User.telegram_id.in_(ids)).all()
        for r in rows:
            s.expunge(r)
        return {r.telegram_id: r for r in rows}
    finally:
        s.close()


async def flush_pending(bot):
    """Deliver queued "step done" cards. Called by a bot job every ~20 s."""
    import asyncio
    items = drain_pending()
    if not items:
        return 0
    users = await asyncio.to_thread(_load_for_cards, items)
    from services import onboarding_rich
    from services.rich_message import send_rich_message
    sent = 0
    # One card per user per flush, however many steps landed at once: it
    # names the latest completion and shows the whole checklist anyway.
    latest = {}
    for tg, key in items:
        latest[tg] = key
    for tg, key in latest.items():
        user = users.get(tg)
        if user is None or getattr(user, "dm_blocked", False):
            continue
        if key not in done_keys(user):
            continue  # the completing transaction rolled back
        try:
            blocks, html_text, kb = onboarding_rich.step_done_card(user, key)
            await send_rich_message(bot, tg, blocks, html_text, reply_markup=kb)
            sent += 1
        except Exception as exc:
            logger.debug("onboarding card to %s failed: %s", tg, exc)
        await asyncio.sleep(0.05)
    return sent
