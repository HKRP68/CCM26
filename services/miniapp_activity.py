"""Activity-message copy for Mini App actions echoed back to a Telegram group.

When a user performs an important action inside the Mini App (spin, daily claim,
quest reward claims, buying/opening packs, buying/selling/upgrading players,
applying traits) and the Mini App was opened *from a group*, the bot posts a
short activity message into that same group so other members can see the action
and stay engaged.

These functions are pure (no DB/network) so they're easy to unit test. They take
the actor's display name plus action-specific context and return an HTML string
suitable for Telegram ``sendMessage`` with ``parse_mode="HTML"``. They return
``None`` when required context is missing, so the caller can silently skip.
"""

import html
import logging

logger = logging.getLogger(__name__)


def _esc(value, default=""):
    """HTML-escape a user/content-controlled value for safe HTML messages."""
    if value is None:
        return default
    return html.escape(str(value))


def _fmt_int(value):
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def _gspin_reward_phrase(reward):
    """Human phrase for a GSpin reward dict (see gspin_reward_service.apply_reward)."""
    if not isinstance(reward, dict):
        return None
    rtype = reward.get("type")
    if rtype in ("coins", "gems", "quest_points"):
        unit = {"coins": "🪙", "gems": "💎", "quest_points": "QP"}.get(rtype, "")
        return f"{_fmt_int(reward.get('amount') or 0)} {unit}".strip()
    if rtype == "player":
        pname = _esc(reward.get("player_name"))
        rating = reward.get("player_rating")
        if not pname:
            return None
        return f"<b>{pname}</b> ({rating} OVR)" if rating else f"<b>{pname}</b>"
    if rtype == "pack":
        return _esc(reward.get("label")) or "a pack"
    # Fallback to the reward label (e.g. coins-instead-of-player edge case)
    return _esc(reward.get("label")) or "a reward"


def _top_player(players):
    """Pick the highest-rated player from a list of {name, rating} dicts."""
    best = None
    for p in players or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        if best is None or (p.get("rating") or 0) > (best.get("rating") or 0):
            best = p
    return best


def _reward_parts(reward):
    """Return human-readable reward fragments from a quest reward dict."""
    if not isinstance(reward, dict):
        return []
    parts = []
    coins = reward.get("coins") or 0
    gems = reward.get("gems") or 0
    points = reward.get("points") or reward.get("quest_points") or 0
    if coins:
        parts.append(f"+{_fmt_int(coins)} 🪙")
    if gems:
        parts.append(f"+{_fmt_int(gems)} 💎")
    if points:
        parts.append(f"+{_fmt_int(points)} QP")
    return parts


def build_message(action, name, **ctx):
    """Return the HTML activity message for ``action`` or ``None`` to skip."""
    try:
        who = f"<b>{_esc(name, 'A manager')}</b>"

        if action == "gspin":
            phrase = _gspin_reward_phrase(ctx.get("reward"))
            if not phrase:
                return None
            return f"🎡 {who} spun the GSpin and won {phrase}!"

        if action == "daily":
            coins = _fmt_int(ctx.get("coins") or 0)
            gems = ctx.get("gems") or 0
            streak = ctx.get("streak")
            parts = [f"+{coins} 🪙"]
            if gems:
                parts.append(f"+{_fmt_int(gems)} 💎")
            reward_str = ", ".join(parts)
            streak_str = f" — Day {streak} streak" if streak else ""
            msg = f"🎁 {who} claimed the daily reward{streak_str} ({reward_str})!"
            mplayer = ctx.get("milestone_player")
            if ctx.get("milestone") and mplayer:
                mname = mplayer.get("name") if isinstance(mplayer, dict) else mplayer
                if mname:
                    msg += f"\n🏆 Milestone bonus: <b>{_esc(mname)}</b>!"
            return msg

        if action == "buy_pack":
            pack = _esc(ctx.get("pack_name")) or "a pack"
            return f"🛒 {who} bought a <b>{pack}</b>!"

        if action == "open_pack":
            pack = _esc(ctx.get("pack_name")) or "a pack"
            best = _top_player(ctx.get("players"))
            if best:
                pname = _esc(best.get("name"))
                rating = best.get("rating")
                pulled = f"<b>{pname}</b> ({rating} OVR)" if rating else f"<b>{pname}</b>"
                return f"📦 {who} opened a <b>{pack}</b> and pulled {pulled}!"
            return f"📦 {who} opened a <b>{pack}</b>!"

        if action == "buy_player":
            pname = _esc(ctx.get("player_name"))
            if not pname:
                return None
            rating = ctx.get("rating")
            price = ctx.get("price")
            ovr = f" ({rating} OVR)" if rating else ""
            cost = f" for {_fmt_int(price)} 🪙" if price else ""
            return f"💰 {who} signed <b>{pname}</b>{ovr}{cost}!"

        if action == "sell_player":
            pname = _esc(ctx.get("player_name"))
            if not pname:
                return None
            rating = ctx.get("rating")
            sell = ctx.get("sell")
            ovr = f" ({rating} OVR)" if rating else ""
            got = f" for {_fmt_int(sell)} 🪙" if sell else ""
            return f"📤 {who} released <b>{pname}</b>{ovr}{got}."

        if action == "sell_multi":
            count = ctx.get("count") or 0
            if count <= 0:
                return None
            total = ctx.get("total")
            got = f" for {_fmt_int(total)} 🪙" if total else ""
            noun = "player" if count == 1 else "players"
            return f"📤 {who} released {count} {noun}{got}."

        if action == "upgrade_player":
            pname = _esc(ctx.get("player_name"))
            if not pname:
                return None
            version = _esc(ctx.get("version"))
            rating = ctx.get("rating")
            ovr = f" ({rating} OVR)" if rating else ""
            ver = f" to {version}" if version else ""
            return f"⬆️ {who} upgraded <b>{pname}</b>{ver}{ovr}!"

        if action == "apply_trait":
            pname = _esc(ctx.get("player_name"))
            tname = _esc(ctx.get("trait_name"))
            if not pname or not tname:
                return None
            emoji = ctx.get("trait_emoji") or ""
            emoji = f"{emoji} " if emoji else ""
            return f"✨ {who} applied {emoji}<b>{tname}</b> to <b>{pname}</b>!"

        if action == "quest_claim":
            title = _esc(ctx.get("quest_title")) or "a quest"
            parts = _reward_parts(ctx.get("reward"))
            reward_text = f" ({', '.join(parts)})" if parts else ""
            return f"🎯 {who} completed <b>{title}</b> and claimed the reward{reward_text}!"

        if action == "quest_claim_all":
            count = int(ctx.get("count") or 0)
            if count <= 0:
                return None
            qtype = _esc(ctx.get("quest_type")) or "quest"
            noun = "quest" if count == 1 else "quests"
            parts = _reward_parts(ctx.get("reward"))
            reward_text = f" ({', '.join(parts)})" if parts else ""
            return f"🎯 {who} claimed {count} {qtype} {noun}{reward_text}!"

        return None
    except Exception:
        logger.exception("build_message failed for action=%s", action)
        return None
