"""Anti-spam for the auction room — per PERSON, never per room.

The old defence against a flooded room was a quiet gap after every bid: for
three seconds after anybody bid, *nobody* could. That stops spam, but it also
stops the auction — two franchises going for the same player at the same
moment is the whole point, and the gap turned the second one away. So the
room-wide gap is off by default now (``auction_service.bid_gap_seconds``) and
spam is stopped where it actually comes from: one pair of thumbs.

Three rules, all in memory and all keyed by ``(chat_id, telegram_id)``:

* **A cooldown.** One bid attempt per ``BID_COOLDOWN`` seconds per person.
  A second ``/bid``, ``.bid`` or button press inside it is dropped without a
  reply — the first one is already on its way, and a reply per extra tap is
  the flood this exists to stop.
* **A burst mute.** More than ``BURST_LIMIT`` taps *dropped by the cooldown*
  inside ``BURST_WINDOW`` seconds (a stuck key, a macro, a bot) mutes that
  person's bidding for ``MUTE_SECONDS``, with ONE warning. Only dropped taps
  count: an owner re-bidding every time they are outbid in a hot war never
  trips it, however many bids they land. Other franchises — and the person's
  own co-owners — bid on untouched.
* **Refusals said once.** The same refusal to the same person on the same lot
  inside ``REFUSAL_REPEAT`` seconds is not said again.

Nothing here touches the database, and none of it is authoritative: the price,
the clock and the "who holds it" all still live in ``place_bid``'s conditional
UPDATE. A process restart simply forgets who was typing fast, which is fine.
Admins are exempt — the caller decides that, since only it knows who is one.
"""

from __future__ import annotations

import threading
import time
from collections import deque

BID_COOLDOWN = 1.0      # seconds between two bid attempts by one person
BURST_WINDOW = 10.0     # seconds the burst counter looks back over
BURST_LIMIT = 5         # cooldown-dropped taps in the window before a mute
MUTE_SECONDS = 15.0     # how long a burst mutes that person's bidding
REFUSAL_REPEAT = 6.0    # seconds before the same refusal is said again

OK = "ok"
COOLDOWN = "cooldown"
MUTED = "muted"

_lock = threading.Lock()
_attempts = {}   # key -> deque of cooldown-dropped tap times (monotonic)
_last_ok = {}    # key -> time of the last attempt that was let through
_muted = {}      # key -> (muted_until, warned)
_refusals = {}   # (key, lot_id, text) -> time last said


class Verdict:
    """What to do with one bid attempt."""

    __slots__ = ("state", "wait", "warn")

    def __init__(self, state, wait=0.0, warn=False):
        self.state = state
        self.wait = max(0.0, float(wait))
        # True exactly once per mute: the one moment the person is told.
        self.warn = warn

    @property
    def allowed(self):
        return self.state == OK

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Verdict({self.state!r}, wait={self.wait:.1f}, warn={self.warn})"


def _prune(now):
    """Drop state nobody needs any more, so the maps cannot grow forever."""
    horizon = now - max(BURST_WINDOW, MUTE_SECONDS, REFUSAL_REPEAT) - 1
    for key in [k for k, v in _attempts.items() if not v or v[-1] < horizon]:
        _attempts.pop(key, None)
    for key in [k for k, at in _last_ok.items() if at < horizon]:
        _last_ok.pop(key, None)
    for key in [k for k, (until, _) in _muted.items() if until < now]:
        _muted.pop(key, None)
    for key in [k for k, at in _refusals.items() if at < horizon]:
        _refusals.pop(key, None)


def check_bid(chat_id, telegram_id, *, now=None):
    """Record one bid attempt and say whether it may go through."""
    now = time.monotonic() if now is None else now
    key = (int(chat_id or 0), int(telegram_id or 0))
    with _lock:
        if len(_last_ok) > 512:
            _prune(now)
        muted = _muted.get(key)
        if muted is not None:
            until, warned = muted
            if now < until:
                _muted[key] = (until, True)
                return Verdict(MUTED, until - now, warn=not warned)
            _muted.pop(key, None)
            _attempts.pop(key, None)

        last = _last_ok.get(key)
        if last is None or now - last >= BID_COOLDOWN:
            _last_ok[key] = now
            return Verdict(OK)

        seen = _attempts.setdefault(key, deque())
        seen.append(now)
        while seen and seen[0] <= now - BURST_WINDOW:
            seen.popleft()
        if len(seen) > BURST_LIMIT:
            _muted[key] = (now + MUTE_SECONDS, True)
            seen.clear()
            return Verdict(MUTED, MUTE_SECONDS, warn=True)
        return Verdict(COOLDOWN, BID_COOLDOWN - (now - last))


def should_say(chat_id, telegram_id, lot_id, text, *, now=None):
    """True unless this exact refusal was just said to this person."""
    now = time.monotonic() if now is None else now
    key = ((int(chat_id or 0), int(telegram_id or 0)), int(lot_id or 0),
           str(text))
    with _lock:
        said = _refusals.get(key)
        if said is not None and now - said < REFUSAL_REPEAT:
            return False
        _refusals[key] = now
        return True


def muted_message(verdict):
    return (f"🚫 Too many bids too fast — your bidding is paused for "
            f"{int(round(verdict.wait)) or 1}s. Everyone else carries on.")


def reset():
    """Forget everything (tests, and ``/arestart``)."""
    with _lock:
        _attempts.clear()
        _last_ok.clear()
        _muted.clear()
        _refusals.clear()
