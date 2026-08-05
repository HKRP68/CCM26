"""/gstats <player> — a cricketer's career across the whole game.

/stats answers "how has *my* copy of this card played". /gstats answers "how
does this cricketer play, everywhere" — every owner, every edition, every match
type (bot matches, /letsplay, Mini App, Challenge League, tours, super overs),
summed into one record, with the managers who have the best numbers with him.
"""

import io
import logging
import re
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services import global_stats_service as gs
from services.flags import get_flag

logger = logging.getLogger(__name__)

TG_CAPTION_LIMIT = 1024
_HTML_TAG_RE = re.compile(r"<[^>]+>")
COL = 20  # width of the batting column in the stat block


def _caption_fits(html_text, limit=TG_CAPTION_LIMIT):
    """True if ``html_text`` fits in a Telegram photo caption.

    Lets the card image and the full stat block go out as one message, falling
    back to a short caption plus a follow-up only when it would overflow.
    """
    visible = _HTML_TAG_RE.sub("", html_text)
    visible = (visible.replace("&lt;", "<").replace("&gt;", ">")
               .replace("&amp;", "&"))
    return len(visible.encode("utf-16-le")) // 2 <= limit


def _row(left, right):
    return f"{left:<{COL}}{right}"


def _stat_block(totals, calc):
    """The two-column BATTING / BOWLING table, as monospaced text."""
    lines = [
        _row("🏏 BATTING", "🎯 BOWLING"),
        "─" * 38,
        _row(f"Inns: {totals['bat_inns']:,}", f"Inns: {totals['bowl_inns']:,}"),
        _row(f"Runs: {totals['runs']:,}", f"Wkts: {totals['wickets_taken']:,}"),
        _row(f"50s: {totals['fifties']:,}", f"3-Fers: {totals['three_fers']:,}"),
        _row(f"100s: {totals['hundreds']:,}", f"5-Fers: {totals['five_fers']:,}"),
        _row(f"4s/6s: {totals['fours']:,}/{totals['sixes']:,}",
             f"Hattricks: {totals['hattricks']:,}"),
        _row(f"Avg: {calc['bat_avg']}", f"Avg: {calc['bowl_avg']}"),
        _row(f"SR: {calc['bat_sr']}", f"Econ: {calc['economy']}"),
        _row(f"Ducks: {totals['ducks']:,}", f"SR: {calc['bowl_sr']}"),
        _row(f"HS: {gs.hs_str(totals)}", f"BBF: {gs.bbf_str(totals)}"),
        _row("", f"Overs: {calc['overs']}"),
    ]
    return "\n".join(lines)


def _owner_name(user: User) -> str:
    if user.username:
        return f"@{escape(user.username)}"
    return escape((user.first_name or user.team_name or "Manager").strip()
                  or "Manager")


def _leaders_lines(session, player_ids):
    lines = []
    runs = gs.top_owners(session, player_ids, "runs", limit=1)
    wickets = gs.top_owners(session, player_ids, "wickets_taken", limit=1)
    if runs:
        lines.append(f"👑 <b>Most runs:</b> {_owner_name(runs[0]['user'])} "
                     f"— {runs[0]['value']:,}")
    if wickets:
        lines.append(f"🎯 <b>Most wickets:</b> {_owner_name(wickets[0]['user'])} "
                     f"— {wickets[0]['value']:,}")
    return lines


def _editions_lines(session, versions):
    """Per-edition split — only meaningful when the card has variants."""
    if len(versions) <= 1:
        return []
    lines = ["🎴 <b>By edition:</b>"]
    for entry in gs.per_version(session, versions):
        version, totals = entry["player"], entry["totals"]
        label = escape((version.version or "Base").strip() or "Base")
        lines.append(f"• {label} ⭐{version.rating} — {totals['bat_inns']:,} inns · "
                     f"{totals['runs']:,} runs · {totals['wickets_taken']:,} wkts")
    return lines


def _render(session, player, versions, totals):
    calc = gs.derived(totals)
    flag = get_flag(player.country)
    scope = ("all editions" if len(versions) > 1
             else escape((player.version or "Base").strip() or "Base"))

    head = [
        f"🌍 <b>GLOBAL STATS — {escape(player.name)}</b> {flag}",
        f"⭐ {player.rating} OVR · {escape(player.category or '')}",
        f"<i>All match types · {scope} · {totals['owners']:,} owner"
        f"{'' if totals['owners'] == 1 else 's'} with a record</i>",
        "",
        f"<code>{_stat_block(totals, calc)}</code>",
        f"🏆 <b>POTM awards:</b> {totals['potm']:,}",
    ]

    leaders = _leaders_lines(session, [v.id for v in versions])
    if leaders:
        head.append("")
        head.extend(leaders)

    editions = _editions_lines(session, versions)
    if editions:
        head.append("")
        head.extend(editions)

    head.append("")
    head.append("💡 <i>Your own numbers:</i> <code>/stats "
                + escape(player.name) + "</code>")
    return "\n".join(head)


def _short_caption(player, totals):
    flag = get_flag(player.country)
    return (f"🌍 <b>GLOBAL STATS — {escape(player.name)}</b> {flag}\n"
            f"⭐ {player.rating} OVR · {totals['runs']:,} runs · "
            f"{totals['wickets_taken']:,} wickets")


async def gstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/gstats &lt;player name&gt;</code>\n"
            "Example: <code>/gstats Virat Kohli</code>\n\n"
            "<i>Career numbers for that cricketer across every owner and "
            "every match type. Use /stats for your own copy.</i>",
            parse_mode="HTML")
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        from services.command_config_service import (
            is_command_enabled, get_disabled_message)
        if not is_command_enabled(session, "gstats"):
            await update.message.reply_text(
                get_disabled_message(session, "gstats"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        from services.version_paginator import (
            find_players_for_search, format_multiple_players_message,
            get_versions_ordered)
        matches = find_players_for_search(session, search_name)
        if not matches:
            await update.message.reply_text(
                f"❌ Player not found: {escape(search_name)}", parse_mode="HTML")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                format_multiple_players_message("gstats", search_name, matches),
                parse_mode="HTML")
            return

        player = matches[0]
        base_id = player.parent_player_id or player.id
        versions = get_versions_ordered(session, base_id) or [player]
        totals = gs.aggregate_stats(session, [v.id for v in versions])

        if not totals["bat_inns"] and not totals["bowl_inns"]:
            flag = get_flag(player.country)
            await update.message.reply_text(
                f"📭 <b>{escape(player.name)}</b> {flag} hasn't played a single "
                f"match for anyone yet — no global stats to show.\n"
                f"Own him? Put him in your XI and start a match.",
                parse_mode="HTML")
            return

        text = _render(session, player, versions, totals)

        # Lead with the card image, carrying the whole block as its caption
        # whenever Telegram's 1024-char cap allows.
        full_in_caption = _caption_fits(text)
        caption = text if full_in_caption else _short_caption(player, totals)

        photo_sent = False
        try:
            from services.player_image_service import (
                has_custom_card, get_custom_image_bytes)
            if has_custom_card(player.id, session):
                import asyncio
                custom = await asyncio.to_thread(get_custom_image_bytes, player.id)
                if custom:
                    await update.message.reply_photo(
                        photo=io.BytesIO(custom), caption=caption,
                        parse_mode="HTML")
                    photo_sent = True
        except Exception:
            logger.exception("gstats custom card send failed; continuing")

        if not photo_sent:
            try:
                from services.card_generator import send_generated_card
                sent = await send_generated_card(
                    update.message.reply_photo, player,
                    caption=caption, parse_mode="HTML")
                photo_sent = sent is not None
            except Exception:
                logger.exception("gstats card render failed; text only")

        if not (photo_sent and full_in_caption):
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception("GStats error for %s", tg_user.id)
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()
