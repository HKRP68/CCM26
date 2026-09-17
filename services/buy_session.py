"""One open buy card per user — the guard behind /buypl (and /buy, /b).

A buy card is a live offer: it names a price, carries a Buy button, and lives
for as long as its buttons do. Nothing used to stop a user opening five of them
at once, and five live offers against one purse is a mess for everybody — the
buyer loses track of which card they are actually looking at, a busy group fills
with half-dead cards, and a card opened before a purchase can still be tapped
after the coins have gone somewhere else.

So a user gets one at a time. While a card of theirs is open, the next /buypl is
refused and points back at the card already waiting; confirming it, closing it,
or letting it time out frees the slot.

The record is in-process, exactly like :mod:`utils.idempotency`. It guards a UI
flow, not money — the money is guarded by the idempotency claim on the Buy
button and the balance check at the moment the coins move — so losing the whole
map on a restart costs nothing worse than one extra open card. It expires with
the card's own button timeout (:data:`BUY_CARD_TTL`), so an abandoned card never
holds the slot for longer than it stays tappable.

Only cards opened by the /buypl command are tracked. /playerinfo renders the
same carousel for a multi-version player, and browsing player details is not
"being in a buy" — a card the user never asked to buy from must not lock the
command they did ask for.
"""

import threading
import time

# Seconds a tracked card holds the slot. Matches the button timeout the buy card
# is sent with (``handlers.buy._send_version_page``), so the slot frees at the
# same moment the buttons stop working.
BUY_CARD_TTL = 120.0

_LOCK = threading.Lock()
_OPEN: dict[int, dict] = {}     # telegram id -> open-card record
_MAX_ENTRIES = 5000


def _expired(record, now):
    return (now - record["opened_at"]) >= BUY_CARD_TTL


def _evict(now):
    """Drop timed-out records. Called under ``_LOCK``."""
    for tg_id in [k for k, rec in _OPEN.items() if _expired(rec, now)]:
        _OPEN.pop(tg_id, None)
    if len(_OPEN) > _MAX_ENTRIES:
        overflow = len(_OPEN) - _MAX_ENTRIES
        oldest = sorted(_OPEN.items(), key=lambda kv: kv[1]["opened_at"])[:overflow]
        for tg_id, _rec in oldest:
            _OPEN.pop(tg_id, None)


def open_card(tg_id, *, chat_id, message_id, player_name=None, chat_title=None):
    """Record that ``tg_id`` now has a buy card waiting on their answer."""
    if tg_id is None:
        return
    now = time.monotonic()
    with _LOCK:
        _evict(now)
        _OPEN[int(tg_id)] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "player_name": player_name,
            "chat_title": chat_title,
            "opened_at": now,
        }


def active_card(tg_id):
    """This user's open card, or ``None`` when the slot is free.

    Returns a copy, so a caller reading it can never mutate the live record.
    """
    if tg_id is None:
        return None
    now = time.monotonic()
    with _LOCK:
        record = _OPEN.get(int(tg_id))
        if record is None:
            return None
        if _expired(record, now):
            _OPEN.pop(int(tg_id), None)
            return None
        return dict(record)


def move_card(tg_id, *, from_chat_id, from_message_id, to_chat_id, to_message_id):
    """Follow a tracked card to its new message.

    Paging between a text version and a card with a picture can't be done with
    an edit, so the card is deleted and re-sent under a new message id. Without
    this the record would keep pointing at a message that no longer exists, and
    "close the card you have open" would name something the user cannot see.

    The move only applies when the record still points at the old message, so
    paging an untracked /playerinfo carousel never steals the slot.
    """
    if tg_id is None:
        return
    with _LOCK:
        record = _OPEN.get(int(tg_id))
        if record is None:
            return
        if (record["chat_id"] != int(from_chat_id)
                or record["message_id"] != int(from_message_id)):
            return
        record["chat_id"] = int(to_chat_id)
        record["message_id"] = int(to_message_id)


def close_card(tg_id, *, chat_id=None, message_id=None):
    """Free the slot. Returns the record that was closed, or ``None``.

    Pass ``chat_id``/``message_id`` to close only when the record points at that
    exact message: the Close and Buy buttons are shared with the /playerinfo
    carousel, and cancelling one of those must not silently close the buy card
    the user still has waiting somewhere else.
    """
    if tg_id is None:
        return None
    with _LOCK:
        record = _OPEN.get(int(tg_id))
        if record is None:
            return None
        if message_id is not None and (record["message_id"] != int(message_id)
                                       or record["chat_id"] != int(chat_id)):
            return None
        return _OPEN.pop(int(tg_id), None)


def reset():
    """Forget every open card. For tests."""
    with _LOCK:
        _OPEN.clear()
