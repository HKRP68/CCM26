"""Re-flag every draft pool's home/overseas players from its country column.

A pool player is *home* when the country on their sheet row is the draft's
``home_country``. That is now what the importer decides it on — but it used to
read only an ``indian_status`` label, which meant:

  • a pool uploaded without that column flagged **every** player as home, so a
    squad of eleven "Indians" could hold four nationalities and the overseas cap
    never refused anything; and
  • a draft whose home country isn't India read "Indian"/"Overseas" labels that
    no longer mean anything against it.

Pools already imported still carry those answers, and a **live** draft cannot
simply be re-uploaded — replacing the pool is refused once picking has started.
This walks every draft and recomputes the flags in place.

Usage:
    python migrate_draft_home_country.py [--dry-run] [--draft-id N]

Idempotent: a second run changes nothing, because the flags already agree with
the countries. Players whose country names nothing readable ("Unknown", blank)
are **left exactly as they are**, so a flag an admin fixed by hand survives.
Each draft is committed separately, so one bad row cannot abort the run.

Squads already drafted keep their players: this changes who *counts* as
overseas, not who is on which team. A squad that is now over ``max_overseas``
stays as it is and is reported below — the cap is enforced on the picks still
to come.
"""

import argparse
import logging

from database import SessionLocal
from models import DraftPlayer, PlayerDraft
from services import draft_service as ds

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("draft_home_country")


def _over_cap(session, draft):
    """Teams already holding more overseas players than the draft's cap allows."""
    cap = draft.max_overseas if draft.max_overseas is not None else 11
    out = []
    for team in ds.teams(session, draft.id):
        used = ds.overseas_count(session, team.id)
        if used > cap:
            out.append((team.name, used, cap))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    parser.add_argument("--draft-id", type=int, default=None,
                        help="only this draft (default: every draft)")
    args = parser.parse_args()

    session = SessionLocal()
    totals = {"drafts": 0, "changed": 0, "unknown": 0, "failed": 0}
    try:
        query = session.query(PlayerDraft).order_by(PlayerDraft.id)
        if args.draft_id:
            query = query.filter(PlayerDraft.id == args.draft_id)
        drafts = query.all()
        if not drafts:
            log.info("No drafts found.")
            return

        log.info("Re-flagging %s draft(s)%s", len(drafts),
                 "   [DRY RUN — nothing will be written]" if args.dry_run else "")
        log.info("─" * 60)
        for draft in drafts:
            pool = (session.query(DraftPlayer)
                    .filter(DraftPlayer.draft_id == draft.id).count())
            try:
                changed, unknown = ds.resync_home_status(session, draft)
                over = _over_cap(session, draft)
                if args.dry_run:
                    session.rollback()
                else:
                    session.commit()
            except Exception:
                session.rollback()
                totals["failed"] += 1
                log.exception("#%s %s — failed, skipped", draft.id, draft.name)
                continue

            totals["drafts"] += 1
            totals["changed"] += changed
            totals["unknown"] += unknown
            log.info("#%-4s %-30s %s status · %s in pool · %s re-flagged%s",
                     draft.id, (draft.name or "")[:30], draft.status, pool,
                     changed,
                     f" · {unknown} unreadable country" if unknown else "")
            for name, used, cap in over:
                log.warning("        ⚠️ %s now holds %s overseas (cap %s) — "
                            "already-drafted players are kept", name, used, cap)
    finally:
        session.close()

    log.info("─" * 60)
    log.info("Drafts processed : %s", totals["drafts"])
    log.info("Players re-flagged: %s", totals["changed"])
    if totals["unknown"]:
        log.info("Left alone (no readable country): %s", totals["unknown"])
    if totals["failed"]:
        log.warning("Drafts skipped after an error: %s — re-run to retry",
                    totals["failed"])
    log.info("Done.")


if __name__ == "__main__":
    main()
