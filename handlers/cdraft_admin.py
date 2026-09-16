"""`/cdraftset` — the Challenge Draft player pool, from Telegram.

Which cards `/cdraft` deals is three settings: the **rating band** every slot
draws its own target from, how far apart the **two cards in one slot** may be,
and the **editions** (``Player.version``) that may be dealt at all. All three
live in ``GameConfig`` and are editable on the admin website's Match Gameplay
page; this is the same settings from a DM, so an owner can retune a draft
without opening the panel.

```text
/cdraftset                        the current pool, and whether it can be dealt
/cdraftset min 80                 the band's floor
/cdraftset max 92                 the band's ceiling
/cdraftset range 80 92            both at once
/cdraftset spread 1               how far apart the two cards in a slot may be
/cdraftset versions Base, Legend  only these editions may be dealt
/cdraftset versions all           clear the list — every edition allowed
/cdraftset reset                  back to the defaults
```

Admin-only, gated by ``services.admin_ids.is_admin`` exactly as
``handlers.tournament_access`` is, and reading/writing through the same
``GameConfig`` row so the website and the bot can never disagree.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import GameConfig
from services import cdraft_service
from services.admin_ids import is_admin
from services.config_service import _refresh as _refresh_cfg

logger = logging.getLogger(__name__)

NOT_ADMIN = "⛔ Only bot admins can use this command."

USAGE = (
    "🎯 <b>/cdraftset</b> — the Challenge Draft pool\n\n"
    "<code>/cdraftset</code> — show the current pool\n"
    "<code>/cdraftset min 80</code> — the band's floor\n"
    "<code>/cdraftset max 92</code> — the band's ceiling\n"
    "<code>/cdraftset range 80 92</code> — both at once\n"
    "<code>/cdraftset spread 1</code> — ± OVR between the two cards in a slot\n"
    "<code>/cdraftset versions Base, Legend</code> — only these editions\n"
    "<code>/cdraftset versions all</code> — every edition allowed\n"
    "<code>/cdraftset reset</code> — back to the defaults"
)


def _load_config_row(session):
    """Return the single GameConfig row, creating it if missing."""
    row = session.query(GameConfig).first()
    if not row:
        row = GameConfig()
        session.add(row)
        session.flush()
    return row


def _commit(session):
    """Commit the row and refresh the config cache so the change takes now.

    The write is already committed by the time the cache is touched, so a
    refresh hiccup must not make the caller report failure and roll back a
    change that actually landed — the same call the tournament allowlist makes.
    """
    session.commit()
    try:
        _refresh_cfg(session)
    except Exception:
        logger.exception("cdraft settings saved, but the config cache refresh failed")


def known_versions(session):
    """Every edition label in the catalogue, with ``Base`` first.

    ``Base`` is always offered even when no row literally carries the string,
    because it is a state (a card with no parent), not a label — the same
    reading the admin panel's own version filter uses.
    """
    from models import Player
    from services.player_service import not_career

    rows = (not_career(session.query(Player.version))
            .filter(Player.version.isnot(None))
            .distinct().all())
    labels = []
    seen = {cdraft_service.BASE_LABEL.casefold()}
    for (value,) in rows:
        name = str(value or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        labels.append(name)
    return [cdraft_service.BASE_LABEL] + sorted(labels, key=str.casefold)


def resolve_versions(requested, available):
    """Match typed names against the catalogue's labels, case-insensitively.

    Returns ``(labels, unknown)``. Names are echoed back in the catalogue's own
    spelling so a stored list always matches what the website shows, and an
    unknown name is reported rather than saved — a typo that silently emptied
    the pool would only surface as a draft nobody can start.
    """
    by_fold = {label.casefold(): label for label in available}
    labels, unknown = [], []
    seen = set()
    for name in requested:
        key = str(name or "").strip().casefold()
        if not key:
            continue
        match = by_fold.get(key)
        if match is None:
            unknown.append(str(name).strip())
        elif key not in seen:
            seen.add(key)
            labels.append(match)
    return labels, unknown


def _pool_report(session):
    """The current settings plus whether they can actually deal a draft."""
    from services import player_cache

    settings = cdraft_service.load_settings()
    versions = settings["versions"]
    lines = [
        "🎯 <b>Challenge Draft pool</b>",
        "═════════════════════════════",
        f"📊 <b>Rating:</b> {settings['rating_min']}–{settings['rating_max']} OVR "
        f"<i>(each slot draws at random inside this band)</i>",
        f"🎚️ <b>Pair spread:</b> ±{settings['pair_spread']} OVR within a slot",
        f"🃏 <b>Editions:</b> "
        + (", ".join(versions) if versions else "all (nothing restricted)"),
        "",
    ]
    try:
        feasibility = cdraft_service.pool_feasibility(
            player_cache.get_all_active(), versions)
    except Exception:
        logger.exception("/cdraftset: could not read the player pool")
        lines.append("⚠️ Could not read the player pool just now.")
        return "\n".join(lines)

    short = cdraft_service.feasibility_shortfalls(feasibility)
    lines.append("<b>Available, by role</b> (different players needed):")
    for role, (have, need) in feasibility.items():
        mark = "✅" if have >= need else "❌"
        lines.append(f"{mark} {role}: {have} / {need}")
    if short:
        names = ", ".join(role for role, _h, _n in short)
        lines.append("")
        lines.append(
            f"⚠️ <b>/cdraft cannot start:</b> not enough {names}. "
            "Tick more editions or widen the rating.")
    lines.append("")
    lines.append(f"<i>Known editions:</i> {', '.join(known_versions(session))}")
    return "\n".join(lines)


def _rating_arg(token):
    """A rating from the command line, clamped. Raises ValueError on junk."""
    value = int(str(token).strip())
    return max(cdraft_service.RATING_FLOOR_LIMIT,
               min(cdraft_service.RATING_CEILING_LIMIT, value))


async def cdraftset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cdraftset — read or change the Challenge Draft pool."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text(NOT_ADMIN)
        return

    args = list(context.args or [])
    session = get_session()
    try:
        if not args:
            await message.reply_text(_pool_report(session), parse_mode="HTML")
            return

        action = args[0].strip().lower()
        row = _load_config_row(session)
        settings = cdraft_service.load_settings()

        if action == "reset":
            row.cdraft_rating_min = cdraft_service.RATING_BOTTOM
            row.cdraft_rating_max = cdraft_service.RATING_TOP
            row.cdraft_pair_spread = cdraft_service.PAIR_SPREAD
            row.cdraft_versions_json = None
            _commit(session)
            await message.reply_text(
                f"✅ Pool reset — {cdraft_service.RATING_BOTTOM}–"
                f"{cdraft_service.RATING_TOP} OVR, every edition allowed.\n\n"
                + _pool_report(session), parse_mode="HTML")
            return

        if action in ("min", "max", "range"):
            try:
                if action == "range":
                    if len(args) < 3:
                        raise ValueError("range needs two numbers")
                    low, high = _rating_arg(args[1]), _rating_arg(args[2])
                elif action == "min":
                    low, high = _rating_arg(args[1]), settings["rating_max"]
                else:
                    low, high = settings["rating_min"], _rating_arg(args[1])
            except (IndexError, ValueError):
                await message.reply_text(
                    f"❌ Give a whole number from "
                    f"{cdraft_service.RATING_FLOOR_LIMIT} to "
                    f"{cdraft_service.RATING_CEILING_LIMIT}.\n\n" + USAGE,
                    parse_mode="HTML")
                return
            # Typed backwards is a typo, not a request for an empty band.
            if low > high:
                low, high = high, low
            row.cdraft_rating_min = low
            row.cdraft_rating_max = high
            _commit(session)
            await message.reply_text(
                f"✅ Draft rating set to <b>{low}–{high} OVR</b>.\n\n"
                + _pool_report(session), parse_mode="HTML")
            return

        if action == "spread":
            try:
                spread = int(str(args[1]).strip())
            except (IndexError, ValueError):
                await message.reply_text(
                    f"❌ Give a whole number from "
                    f"{cdraft_service.SPREAD_LIMIT_LOW} to "
                    f"{cdraft_service.SPREAD_LIMIT_HIGH}.\n\n" + USAGE,
                    parse_mode="HTML")
                return
            spread = max(cdraft_service.SPREAD_LIMIT_LOW,
                         min(cdraft_service.SPREAD_LIMIT_HIGH, spread))
            row.cdraft_pair_spread = spread
            _commit(session)
            await message.reply_text(
                f"✅ The two cards in a slot must now be within "
                f"<b>{spread} OVR</b> of each other.\n\n"
                + _pool_report(session), parse_mode="HTML")
            return

        if action == "versions":
            raw = " ".join(args[1:]).strip()
            if not raw:
                await message.reply_text(
                    "❌ Name the editions, or <code>all</code>.\n\n" + USAGE,
                    parse_mode="HTML")
                return
            if raw.lower() in ("all", "any", "none", "clear", "*"):
                row.cdraft_versions_json = None
                _commit(session)
                await message.reply_text(
                    "✅ Every edition is allowed again.\n\n" + _pool_report(session),
                    parse_mode="HTML")
                return
            available = known_versions(session)
            labels, unknown = resolve_versions(
                [part for part in raw.split(",")], available)
            if unknown:
                await message.reply_text(
                    f"❌ Not an edition in the catalogue: "
                    f"<b>{', '.join(unknown)}</b>\n\n"
                    f"<i>Known editions:</i> {', '.join(available)}",
                    parse_mode="HTML")
                return
            if not labels:
                await message.reply_text(
                    "❌ Name at least one edition, or <code>all</code>.\n\n" + USAGE,
                    parse_mode="HTML")
                return
            row.cdraft_versions_json = cdraft_service.dump_allowed_versions(labels)
            _commit(session)
            await message.reply_text(
                f"✅ Drafts now deal only: <b>{', '.join(labels)}</b>.\n\n"
                + _pool_report(session), parse_mode="HTML")
            return

        await message.reply_text(USAGE, parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/cdraftset failed")
        await message.reply_text("⚠️ Could not update the Challenge Draft pool.")
    finally:
        session.close()
