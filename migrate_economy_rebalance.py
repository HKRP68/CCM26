"""One-time economy rebalance migration for EXISTING databases.

Background: claim / daily / debut / gspin rewards and the GameConfig economy
values are read from DB tables (CommandReward, GSpinReward, GameConfig) that are
seeded only once on a fresh database. The code defaults were lowered in the
"make coins & gems harder to earn" rebalance, but an already-populated DB keeps
its old rows. Run this script once against such a DB to bring those rows in line.

Usage:
    python migrate_economy_rebalance.py

Idempotent and non-destructive: each field is only updated when it still holds
the OLD default value, so any value an admin has since customised is left alone.
Re-running after a successful pass is a no-op.
"""

import logging

from database import SessionLocal
from models import CommandReward, GSpinReward
from services import config_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("economy_rebalance")


# (old_value, new_value) pairs. Only migrate when the row still equals `old`.
COMMAND_REWARD_CHANGES = {
    "claim": {"coin_amount": (500, 0)},
    "daily": {
        "coin_amount": (5000, 1500),
        "milestone_bonus_coins": (10000, 3000),
        "milestone_bonus_gems": (20, 6),
    },
    "debut": {
        "coin_amount": (5000, 1500),
        "gem_amount": (100, 30),
    },
}

# Keyed by label. (old, new) for amount_min / amount_max.
GSPIN_CHANGES = {
    "Coin Stash": {"amount_min": (5000, 1500), "amount_max": (10000, 3000)},
    "Gem Drop": {"amount_min": (10, 3), "amount_max": (500, 150)},
}

# GameConfig single-row economy values. OLD values are the original GameConfig
# column / DEFAULTS values that existing DB rows actually hold; NEW values match
# the authoritative rebalanced constants in config.py (so every config source
# agrees: gspin gems 3-150, daily 1500, debut 1500/30).
GAMECONFIG_CHANGES = {
    "gspin_gem_min": (5, 3),
    "gspin_gem_max": (50, 150),
    "daily_coins": (1000, 1500),
    "daily_streak_bonus_coins": (200, 60),
    "debut_coins": (100000, 1500),
    "debut_gems": (20, 30),
}


def _apply(obj, field, old, new):
    """Set obj.field = new only if it currently equals old. Returns 1 if changed."""
    cur = getattr(obj, field, None)
    if cur == old:
        setattr(obj, field, new)
        return 1
    if cur == new:
        return 0  # already migrated
    log.info("  · skip %s.%s = %r (admin-customised; not %r)",
             getattr(obj, "command_key", getattr(obj, "label", "?")), field, cur, old)
    return 0


def migrate():
    session = SessionLocal()
    changed = 0
    try:
        # ── CommandReward (claim / daily / debut) ──────────────────────
        for key, fields in COMMAND_REWARD_CHANGES.items():
            row = (session.query(CommandReward)
                          .filter(CommandReward.command_key == key).first())
            if not row:
                log.info("CommandReward %r not found — skipping", key)
                continue
            for field, (old, new) in fields.items():
                changed += _apply(row, field, old, new)

        # ── GSpinReward (Coin Stash / Gem Drop) ────────────────────────
        for label, fields in GSPIN_CHANGES.items():
            rows = session.query(GSpinReward).filter(GSpinReward.label == label).all()
            if not rows:
                log.info("GSpinReward %r not found — skipping", label)
                continue
            for row in rows:
                for field, (old, new) in fields.items():
                    changed += _apply(row, field, old, new)

        # ── GameConfig (single row) ────────────────────────────────────
        cfg = config_service.get_config(session)
        gc_updates = {}
        for field, (old, new) in GAMECONFIG_CHANGES.items():
            cur = cfg.get(field)
            if cur == old:
                gc_updates[field] = new
            elif cur != new:
                log.info("  · skip GameConfig.%s = %r (admin-customised; not %r)",
                         field, cur, old)
        if gc_updates:
            config_service.save_config(session, gc_updates,
                                       updated_by="economy_rebalance_migration")
            changed += len(gc_updates)

        session.commit()
        # Drop the config cache so the new values are read immediately.
        try:
            config_service._CACHE["data"] = None
        except Exception:
            pass
        log.info("Done. %d field(s) updated.", changed)
    except Exception:
        session.rollback()
        log.exception("Migration failed — rolled back.")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    migrate()
