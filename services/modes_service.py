"""Helpers for the admin-managed /modes league selector."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models import GameConfig, ModeLeague

DEFAULT_MODE_LEAGUES = (
    "IPL",
    "BBL",
    "International T20 League",
)

DEFAULT_MODES_INTRO = (
    "🏏 <b>Select Match Mode</b>\n\n"
    "Choose the league format for this matchup."
)


def seed_default_mode_leagues(db):
    """Create the Phase 1 default league rows if they are missing."""
    existing = {name for (name,) in db.query(ModeLeague.name).all()}
    added = 0
    for idx, name in enumerate(DEFAULT_MODE_LEAGUES, start=1):
        if name not in existing:
            db.add(ModeLeague(name=name, sort_order=idx * 10, is_active=True))
            added += 1
    return added


def get_modes_config(db):
    """Return the singleton game config row, creating it when needed."""
    cfg = db.query(GameConfig).order_by(GameConfig.id.asc()).first()
    if not cfg:
        cfg = GameConfig()
        db.add(cfg)
        db.flush()
    return cfg


def get_active_mode_leagues(db):
    """Return active /modes leagues in admin-defined order."""
    return (db.query(ModeLeague)
            .filter(ModeLeague.is_active == True)
            .order_by(ModeLeague.sort_order.asc(), ModeLeague.name.asc())
            .all())


def build_modes_keyboard(leagues, host_tg_id, guest_tg_id):
    rows = []
    for league in leagues:
        rows.append([
            InlineKeyboardButton(
                league.name,
                callback_data=f"modeleague:{league.id}:{host_tg_id}:{guest_tg_id}",
            )
        ])
    return InlineKeyboardMarkup(rows)
