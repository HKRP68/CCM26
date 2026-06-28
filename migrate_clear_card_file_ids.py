"""One-time migration: clear cached generated-card Telegram file_ids.

Background: before the template-card change, ``services/card_sender`` wrote the
file_id of each auto-generated card into ``Player.card_file_id`` as a speed
cache. That cache now lives in process memory instead, and ``card_file_id`` is
reserved for the admin ``/setcardid`` manual pin. Any value left in the column
from the old behaviour is a stale procedural/template id that would be served as
if it were a manual pin, showing an out-of-date card.

Run this once after deploying the template-card rollout to clear those legacy
ids so every player re-renders the current card.

WARNING: this clears EVERY ``Player.card_file_id``, including admin ``/setcardid``
pins. Only run it if you do not rely on ``/setcardid`` (re-pin afterwards if you
do).

Usage:
    python migrate_clear_card_file_ids.py

Idempotent: re-running after a successful pass clears nothing (no-op).
"""

import logging

from database import SessionLocal
from models import Player

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("clear_card_file_ids")


def main():
    session = SessionLocal()
    try:
        cleared = (session.query(Player)
                   .filter(Player.card_file_id.isnot(None))
                   .update({Player.card_file_id: None}, synchronize_session=False))
        session.commit()
        log.info("Cleared cached card_file_id on %s player row(s).", cleared)
    finally:
        session.close()


if __name__ == "__main__":
    main()
