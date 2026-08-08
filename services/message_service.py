"""Message template service — admin-editable strings used in bot messages.

Usage in bot code:
    from services.message_service import get_msg
    text = get_msg("welcome", first_name=user.first_name)

If a row with key="welcome" exists in the DB, it's used. Otherwise the
hardcoded default below is used. Templates support {placeholder} substitution
via str.format(); unknown placeholders are silently kept as literal text.

Admin UI at /messages lets you edit the text without redeploying.
"""

import logging
import threading
from datetime import datetime

from models import MessageTemplate

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# REGISTRY — every editable message and its default text + metadata
# ════════════════════════════════════════════════════════════════════
# Each entry: key → {label, category, description, default}
# 'default' is the baked-in message used if no DB override exists.
# 'description' shows in admin UI explaining placeholders.

REGISTRY = {
    # ── Onboarding ──────────────────────────────────────────────
    "welcome_existing": {
        "label": "Welcome (returning user)",
        "category": "Onboarding",
        "description": "Shown by /start when user has already debuted. Placeholders: {first_name}, {team_name}, {coins}, {gems}, {roster}, {max_roster}",
        "default": (
            "👋 Welcome back, <b>{first_name}</b>!\n\n"
            "🏏 <b>{team_name}</b>\n"
            "💰 {coins:,} coins · 💎 {gems} gems\n"
            "📊 Roster: {roster}/{max_roster}\n\n"
            "Type /howto for the full command list."
        ),
    },
    "welcome_new": {
        "label": "Welcome (brand new user)",
        "category": "Onboarding",
        "description": "Shown by /start when user has never debuted. Placeholders: {first_name}",
        "default": (
            "🏏 <b>Welcome to Cricket Bot, {first_name}!</b>\n\n"
            "Build your dream cricket team, collect player cards, and play "
            "ball-by-ball matches against friends or the AI.\n\n"
            "👉 Type <b>/debut</b> to claim your starting team and 100,000 coins!"
        ),
    },
    "debut_success": {
        "label": "Debut completed",
        "category": "Onboarding",
        "description": "Shown after /debut succeeds. Placeholders: {count}, {coins}, {gems}, {player_list}",
        "default": (
            "🎉 <b>Welcome to Cricket Bot!</b>\n"
            "✅ Your debut is complete!\n"
            "✅ You received {count} starting players\n\n"
            "{player_list}\n\n"
            "📊 Your Roster: {count}/{max_roster} players\n"
            "💰 Coins: {coins:,}\n"
            "💎 Gems: {gems}\n\n"
            "<b>Commands:</b>\n"
            "/claim - Get 1 player + 500 coins (hourly)\n"
            "/myroster - View your players\n"
            "/playerinfo [name] - Player details\n"
            "/daily - Daily reward (24h cooldown)\n"
            "/gspin - Lucky Card Pick (8h cooldown)"
        ),
    },
    # ── Rewards ────────────────────────────────────────────────
    "daily_header": {
        "label": "Daily reward header",
        "category": "Rewards",
        "description": "Top of the /daily reward message. Placeholders: {coins}, {gems}, {streak}, {milestone}",
        "default": "📅 <b>Daily Reward Claimed!</b>\n",
    },
    "claim_success": {
        "label": "Claim successful header",
        "category": "Rewards",
        "description": "Shown after /claim grants a player. Placeholders: {player_name}, {rating}, {coins}",
        "default": (
            "🎁 <b>NEW PLAYER CLAIMED!</b>\n\n"
            "{player_name} ({rating} OVR)\n\n"
            "+500 🪙 added to your purse"
        ),
    },
    "match_win_announcement": {
        "label": "Match — winner announcement",
        "category": "Match",
        "description": "Shown when match ends. Placeholders: {winner}, {loser}, {margin}, {coins}, {gems}",
        "default": (
            "🏆 <b>{winner} WIN!</b>\n"
            "⚔️ Beat {loser} {margin}\n\n"
            "🪙 +{coins:,} coins · 💎 +{gems} gems"
        ),
    },
    # ── Errors / generic ──────────────────────────────────────
    "error_generic": {
        "label": "Generic error fallback",
        "category": "Errors",
        "description": "Shown when something unexpected fails. Placeholders: none",
        "default": "⚠️ Something went wrong. Please try again in a moment.",
    },
    "cooldown_active": {
        "label": "Cooldown active",
        "category": "Errors",
        "description": "Shown when a command is on cooldown. Placeholders: {command}, {remaining}",
        "default": "⏳ <b>{command}</b> is on cooldown.\nTry again in <b>{remaining}</b>.",
    },
    "roster_full": {
        "label": "Roster full",
        "category": "Errors",
        "description": "Shown when user can't acquire more players. Placeholders: {max}, {max_roster}",
        "default": (
            "❌ <b>Roster full!</b> ({max}/{max_roster})\n\n"
            "Use /release or /releasemultiple to free up slots."
        ),
    },
    "not_enough_coins": {
        "label": "Insufficient coins",
        "category": "Errors",
        "description": "Shown on purchase failure. Placeholders: {required}, {balance}",
        "default": (
            "❌ <b>Not enough coins!</b>\n\n"
            "Need: <b>{required:,}</b> 🪙\n"
            "Have: <b>{balance:,}</b> 🪙"
        ),
    },
    # ── Promotional / engagement ──────────────────────────────
    "vsbot_intro": {
        "label": "Vsbot intro",
        "category": "Engagement",
        "description": "Shown above the team/over selectors when /vsbot is run. Placeholders: none",
        "default": "🤖 <b>VS BOT</b> — pick a team and overs to play.",
    },
    # ── Cooldowns ──────────────────────────────────────────────
    "daily_cooldown": {
        "label": "/daily cooldown",
        "category": "Cooldowns",
        "description": "Shown when /daily is on cooldown. Placeholders: {remaining}",
        "default": "⏳ Daily on cooldown. Try again in <b>{remaining}</b>",
    },
    "daily_available": {
        "label": "/daily available (button prompt)",
        "category": "Rewards",
        "description": "Intro before tapping the Claim button. Placeholders: {coins}, {players}",
        "default": (
            "📅 <b>Daily Reward Available!</b>\n\n"
            "Tap below to claim your reward:\n"
            "+{coins:,} coins\n+{players} Players\n+1 Streak"
        ),
    },
    "daily_already_today": {
        "label": "/daily already claimed",
        "category": "Cooldowns",
        "description": "Edits the claim message if user double-taps. Placeholders: none",
        "default": "⏳ Already claimed today!",
    },
    "claim_cooldown": {
        "label": "/claim cooldown",
        "category": "Cooldowns",
        "description": "Shown when /claim is on cooldown. Placeholders: {remaining}",
        "default": "⏳ Claim on cooldown. Try again in <b>{remaining}</b>",
    },
    "claim_no_players": {
        "label": "/claim no players available",
        "category": "Errors",
        "description": "Shown when no players exist in DB (rare). Placeholders: none",
        "default": "⚠️ No players available.",
    },
    "gspin_cooldown": {
        "label": "/gspin cooldown",
        "category": "Cooldowns",
        "description": "Shown when /gspin is on cooldown. Placeholders: {remaining}",
        "default": "⏳ GSpin on cooldown. Try again in <b>{remaining}</b>",
    },
    "debut_already": {
        "label": "/debut already done",
        "category": "Onboarding",
        "description": "Shown when user tries /debut again. Placeholders: none",
        "default": "⚠️ You've already debuted! Use /myroster to see your team.",
    },
    "not_debuted": {
        "label": "User hasn't debuted yet",
        "category": "Onboarding",
        "description": "Shown when a non-user tries any command. Placeholders: none",
        "default": "❌ Do /debut first!",
    },
    # ── Match flow ─────────────────────────────────────────────
    "match_busy": {
        "label": "Chat already has a match",
        "category": "Match",
        "description": "Shown when a 2nd match is attempted in same chat. Placeholders: {p1}, {p2}, {mid}, {status}",
        "default": (
            "🚫 <b>A match is already going on here.</b>\n"
            "🏏 <b>{p1}</b> vs <b>{p2}</b>\n"
            "<i>Match #{mid} — status: {status}</i>\n\n"
            "<b>What you can do:</b>\n"
            "  •  Wait for the current match to finish\n"
            "  •  Start your match in a different chat or DM the bot\n"
            "  •  Players in the match can use /endmatch or /resume\n"
            "  •  Spectate with /matchinfo"
        ),
    },
    "match_forfeit": {
        "label": "Match auto-forfeit",
        "category": "Match",
        "description": "Sent on auto-forfeit timeout. Placeholders: {idle_mention}, {winner_mention}, {action}, {timeout}, {fine_coins}, {fine_gems}",
        "default": (
            "⏰ <b>Time over</b> 😔\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "{idle_mention} left the game.\n"
            "<i>(did not {action} within {timeout}s)</i>\n\n"
            "⚠️ Fined: <b>-{fine_coins:,}</b> 🪙   <b>-{fine_gems}</b> 💎\n\n"
            "🏆 {winner_mention} won the match!"
        ),
    },
    "match_warning": {
        "label": "Match 45s warning",
        "category": "Match",
        "description": "Sent at 45s of inactivity. Placeholders: {remaining}, {mention}, {action}",
        "default": (
            "⏱️ <b>{remaining} seconds remaining</b> {mention} to {action}.\n"
            "<i>Match will be forfeited otherwise.</i>"
        ),
    },
    # ── Bowl-out ───────────────────────────────────────────────
    "bowlout_tied_intro": {
        "label": "Tied match → bowl-out intro",
        "category": "Bowl-out",
        "description": "Announces tied match and prompts first pick. Placeholders: {first_picker_mention}",
        "default": (
            "🤝 <b>TIED!</b>\n\n"
            "🎯 <b>BOWL-OUT TIEBREAKER</b>\n"
            "Each team picks 5 bowlers to bowl at unguarded stumps.\n"
            "Most hits wins.\n\n"
            "{first_picker_mention}, pick your <b>1st bowler</b>:"
        ),
    },
    "bowlout_pick_prompt": {
        "label": "Pick next bowler",
        "category": "Bowl-out",
        "description": "Sent for each of the 5 picks. Placeholders: {mention}, {n}",
        "default": "{mention}, pick your <b>bowler #{n}</b>:",
    },
    "bowlout_result": {
        "label": "Bowl-out final result",
        "category": "Bowl-out",
        "description": "Sent at end of bowl-out. Placeholders: {winner_team}, {team1}, {team2}, {hits1}, {hits2}",
        "default": (
            "🏆 <b>BOWL-OUT RESULT</b>\n\n"
            "<b>{winner_team}</b> wins!\n"
            "{team1} {hits1} — {hits2} {team2}"
        ),
    },
}


# ════════════════════════════════════════════════════════════════════
# Cache + accessors
# ════════════════════════════════════════════════════════════════════

_lock = threading.Lock()
_cache = {"data": None, "loaded_at": None}


def _refresh(session=None):
    own = False
    if session is None:
        from database import get_session
        session = get_session(); own = True
    try:
        rows = session.query(MessageTemplate).filter(MessageTemplate.is_active == True).all()
        data = {r.key: r.body for r in rows}
        _cache["data"] = data
        _cache["loaded_at"] = datetime.utcnow()
    except Exception:
        logger.exception("message template cache refresh failed")
        _cache["data"] = {}
    finally:
        if own: session.close()


def invalidate():
    """Force refresh on next access. Call after admin save."""
    with _lock:
        _cache["data"] = None


def get_msg(key, **placeholders):
    """Return the formatted message text for `key`.
    Falls back to the registry default if no DB override.
    Unknown placeholders silently keep their literal {curly} form.
    """
    if _cache["data"] is None:
        _refresh()
    # {max_roster} is always available, to every template and to admin-edited
    # overrides, so a squad-cap rebalance can't leave a stale number baked into
    # a message. An explicit caller value still wins.
    from config import MAX_ROSTER
    placeholders.setdefault("max_roster", MAX_ROSTER)
    overrides = _cache["data"] or {}
    template = overrides.get(key)
    if template is None:
        meta = REGISTRY.get(key)
        if not meta:
            logger.warning(f"get_msg: unknown template key '{key}'")
            return ""
        template = meta["default"]
    # Safe-format: missing keys keep literal text instead of crashing
    try:
        return template.format(**placeholders)
    except (KeyError, IndexError):
        # Fall back to a safe-substitute that leaves unknown placeholders alone
        import string
        class _SafeDict(dict):
            def __missing__(self, k): return "{" + k + "}"
        return string.Formatter().vformat(template, (), _SafeDict(placeholders))
    except Exception:
        logger.exception(f"get_msg: format failed for key={key}")
        return template  # raw template if everything fails


def all_with_metadata(session):
    """For admin UI: returns list of dicts merging registry + any DB row."""
    rows = {r.key: r for r in session.query(MessageTemplate).all()}
    out = []
    for key, meta in REGISTRY.items():
        row = rows.get(key)
        out.append({
            "key": key,
            "label": meta["label"],
            "category": meta["category"],
            "description": meta["description"],
            "default": meta["default"],
            "current": row.body if row else meta["default"],
            "is_overridden": row is not None,
            "is_active": row.is_active if row else True,
            "updated_at": row.updated_at if row else None,
            "updated_by": row.updated_by if row else None,
        })
    # Sort by category then label
    out.sort(key=lambda x: (x["category"], x["label"]))
    return out


def save_template(session, key, body, updated_by=None):
    """Insert or update a template row. Returns (success, message)."""
    if key not in REGISTRY:
        return False, f"Unknown key: {key}"
    body = (body or "").strip()
    if not body:
        return False, "Body cannot be empty."
    row = session.query(MessageTemplate).filter(MessageTemplate.key == key).first()
    meta = REGISTRY[key]
    if row:
        row.body = body
        row.label = meta["label"]
        row.description = meta["description"]
        row.category = meta["category"]
        row.is_active = True
        row.updated_at = datetime.utcnow()
        row.updated_by = updated_by
    else:
        row = MessageTemplate(
            key=key, label=meta["label"], description=meta["description"],
            category=meta["category"], body=body, is_active=True,
            updated_at=datetime.utcnow(), updated_by=updated_by,
        )
        session.add(row)
    session.flush()
    invalidate()
    return True, "Saved."


def reset_to_default(session, key):
    """Remove the DB override for a key — falls back to registry default."""
    row = session.query(MessageTemplate).filter(MessageTemplate.key == key).first()
    if row:
        session.delete(row)
        session.flush()
        invalidate()
    return True
