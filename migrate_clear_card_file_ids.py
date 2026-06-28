"""One-time migration: drop stale generated-card Telegram file_ids.

Background: ``Player.card_file_id`` caches the Telegram file_id of the last
auto-generated card so repeat sends skip the re-upload (the "send quick" fast
path). Those ids were produced by the old procedural card generator. After
switching the default to the website **template** style, any cached id still
points at the old procedural render, so a player with a cached id would keep
showing the stale procedural card instead of the new template card.

Run this script once after deploying the template-card rollout to clear those
cached ids. The next send for each player then regenerates the template card and
re-caches its file_id, so sends stay fast and correct.

Usage:
    python migrate_clear_card_file_ids.py

Idempotent and non-destructive: it only NULLs ``card_file_id`` (a regenerable
cache key), never touches admin-uploaded custom images or any player stats.
Re-running after a successful pass clears nothing (no-op).
"""

import logging

from services.card_generator import clear_persisted_card_file_ids

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("clear_card_file_ids")


def main():
    cleared = clear_persisted_card_file_ids()
    log.info("Cleared cached card_file_id on %s player row(s).", cleared)


if __name__ == "__main__":
    main()
