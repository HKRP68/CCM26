"""/gstats <player> — a cricketer's career across the whole game.

/stats answers "how has *my* copy of this card played". /gstats answers "how
does this cricketer play, everywhere" — every owner, every edition, every match
type (bot matches, /letsplay, Mini App, Challenge League, tours, super overs),
summed into one record, with the managers who have the best numbers with him.

The card is a small browser rather than a single dump:

  ◀ Prev   All editions (1/4)   Next ▶     ← one page per edition, plus the
  👥 Owners                                   combined record on page 1
  ❌ Close

Paging swaps the card image with the edition's own art, and 👥 Owners flips the
same message over to the /owners answer without posting anything new. Only the
manager who typed the command can drive those buttons.

Managers are named in plain text — ``lost_in_space14``, never ``@lost_in_space14``
— so reading a stat board never pings a dozen strangers (services/display_name).
"""

import asyncio
import io
import logging
import re
from html import escape

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, Update)
from telegram.ext import ContextTypes

from database import get_session
from models import Player, User
from services import global_stats_service as gs
from services.button_timeout import schedule_button_timeout
from services.display_name import manager_name
from services.flags import get_flag

logger = logging.getLogger(__name__)

TG_CAPTION_LIMIT = 1024
_HTML_TAG_RE = re.compile(r"<[^>]+>")
COL = 20  # width of the batting column in the stat block
BUTTON_TTL_SECONDS = 300
MAX_EDITIONS_LISTED = 6  # keeps the combined page inside a photo caption

ALL_EDITIONS = "All editions"


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
    return manager_name(user)


def _version_label(player: Player) -> str:
    return escape((player.version or "Base").strip() or "Base")


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
    """Per-edition split — only meaningful when the card has variants.

    Capped, because this is the one part of the page that grows with the card:
    a cricketer with a dozen editions would push the block past Telegram's
    1024-character caption and force a trim through the stat table itself. The
    editions that don't fit are one Next ▶ away on their own pages.
    """
    if len(versions) <= 1:
        return []
    lines = ["🎴 <b>By edition:</b>"]
    entries = gs.per_version(session, versions)
    for entry in entries[:MAX_EDITIONS_LISTED]:
        version, totals = entry["player"], entry["totals"]
        lines.append(f"• {_version_label(version)} ⭐{version.rating} — "
                     f"{totals['bat_inns']:,} inns · {totals['runs']:,} runs · "
                     f"{totals['wickets_taken']:,} wkts")
    remaining = len(entries) - MAX_EDITIONS_LISTED
    if remaining > 0:
        lines.append(f"<i>…and {remaining} more — page through them above.</i>")
    return lines


# ── Pages ────────────────────────────────────────────────────────────
# Page 1 of a multi-edition card is the combined record (what /gstats has
# always shown); pages 2..N drill into one edition each. A card with a single
# edition has exactly one page and no navigation.

def _build_pages(versions):
    """[{label, ids, card}] — the pages ◀ Prev / Next ▶ move between."""
    if len(versions) <= 1:
        only = versions[0]
        return [{"label": (only.version or "Base").strip() or "Base",
                 "ids": [only.id], "card": only}]
    pages = [{"label": ALL_EDITIONS, "ids": [v.id for v in versions],
              "card": versions[0]}]
    for version in versions:
        pages.append({"label": (version.version or "Base").strip() or "Base",
                      "ids": [version.id], "card": version})
    return pages


def _clamp(page, total_pages):
    return max(0, min(page, total_pages - 1))


def _render_stats(session, player, versions, pages, index):
    """The stat card for one page. Returns (text, card_player)."""
    page = pages[index]
    is_all = page["label"] == ALL_EDITIONS
    card = page["card"]
    totals = gs.aggregate_stats(session, page["ids"])
    calc = gs.derived(totals)
    flag = get_flag(player.country)

    scope = "all editions" if is_all else escape(page["label"])
    head = [
        f"🌍 <b>GLOBAL STATS — {escape(player.name)}</b> {flag}",
        f"⭐ {card.rating} OVR · {escape(card.category or '')}",
        f"<i>All match types · {scope} · {totals['owners']:,} owner"
        f"{'' if totals['owners'] == 1 else 's'} with a record</i>",
    ]
    if len(pages) > 1:
        head.append(f"<i>Edition {index + 1}/{len(pages)}</i>")
    head += [
        "",
        f"<code>{_stat_block(totals, calc)}</code>",
        f"🏆 <b>POTM awards:</b> {totals['potm']:,}",
    ]

    if not totals["bat_inns"] and not totals["bowl_inns"]:
        head.append("")
        head.append("📭 <i>No match has been played with this "
                    + ("card yet." if not is_all else "cricketer yet.") + "</i>")
        return "\n".join(head), card

    leaders = _leaders_lines(session, page["ids"])
    if leaders:
        head.append("")
        head.extend(leaders)

    # The per-edition breakdown belongs on the combined page — on an edition's
    # own page it would just repeat the numbers directly above it.
    if is_all:
        editions = _editions_lines(session, versions)
        if editions:
            head.append("")
            head.extend(editions)

    head.append("")
    head.append("💡 <i>Your own numbers:</i> <code>/stats "
                + escape(player.name) + "</code>")
    return "\n".join(head), card


def _render_owners(session, user, player, versions, pages, index):
    """The 👥 Owners view — the /owners answer, in the same message."""
    from handlers.owners import render_owners_summary
    page = pages[index]
    scope_versions = (versions if page["label"] == ALL_EDITIONS
                      else [page["card"]])
    text = render_owners_summary(session, user, player, scope_versions)
    if page["label"] != ALL_EDITIONS:
        text += f"\n\n🎴 <i>Scope: {escape(page['label'])} edition only.</i>"
    return text


def _keyboard(owner_tg, base_id, index, total_pages, view):
    rows = []
    if view == "stats":
        if total_pages > 1:
            prev_index = (index - 1) % total_pages
            next_index = (index + 1) % total_pages
            rows.append([
                InlineKeyboardButton(
                    "◀ Prev",
                    callback_data=f"gstpg_{owner_tg}_{base_id}_{prev_index}"),
                InlineKeyboardButton(f"{index + 1}/{total_pages}",
                                     callback_data="noop"),
                InlineKeyboardButton(
                    "Next ▶",
                    callback_data=f"gstpg_{owner_tg}_{base_id}_{next_index}"),
            ])
        rows.append([InlineKeyboardButton(
            "👥 Owners", callback_data=f"gstow_{owner_tg}_{base_id}_{index}")])
    else:
        rows.append([InlineKeyboardButton(
            "◀ Back to stats",
            callback_data=f"gstpg_{owner_tg}_{base_id}_{index}")])
    rows.append([InlineKeyboardButton(
        "❌ Close", callback_data=f"gstcl_{owner_tg}_{base_id}_{index}")])
    return InlineKeyboardMarkup(rows)


def _short_caption(player, card):
    flag = get_flag(player.country)
    return (f"🌍 <b>GLOBAL STATS — {escape(player.name)}</b> {flag}\n"
            f"⭐ {card.rating} OVR · {_version_label(card)}")


async def _photo_source(session, card):
    """What to send as the card image: a file_id, raw bytes, or None.

    A cached Telegram file_id costs nothing to re-send, which is what makes
    flipping between editions feel instant.
    """
    try:
        from services.player_image_service import (
            get_tg_file_id, get_custom_image_bytes, has_custom_card)
        if has_custom_card(card.id, session):
            file_id = get_tg_file_id(session, card.id)
            if file_id:
                return file_id
            custom = await asyncio.to_thread(get_custom_image_bytes, card.id)
            if custom:
                return io.BytesIO(custom)
    except Exception:
        logger.exception("gstats custom card lookup failed; falling back")

    try:
        from services.card_generator import (generate_card,
                                             get_generated_card_file_id)
        file_id = get_generated_card_file_id(card.id)
        if file_id:
            return file_id
        generated = await asyncio.to_thread(generate_card, card)
        if generated:
            return io.BytesIO(generated)
    except Exception:
        logger.exception("gstats card render failed; text only")
    return None


def _resolve(session, search_name):
    """(player, versions, error_text)."""
    from services.version_paginator import (
        find_players_for_search, format_multiple_players_message,
        get_versions_ordered)
    matches = find_players_for_search(session, search_name)
    if not matches:
        return None, None, f"❌ Player not found: {escape(search_name)}"
    if len(matches) > 1:
        return None, None, format_multiple_players_message(
            "gstats", search_name, matches)
    player = matches[0]
    base_id = player.parent_player_id or player.id
    versions = get_versions_ordered(session, base_id) or [player]
    return player, versions, None


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

        player, versions, error = _resolve(session, search_name)
        if error:
            await update.message.reply_text(error, parse_mode="HTML")
            return

        base_id = player.parent_player_id or player.id
        pages = _build_pages(versions)
        totals = gs.aggregate_stats(session, [v.id for v in versions])

        if not totals["bat_inns"] and not totals["bowl_inns"]:
            flag = get_flag(player.country)
            await update.message.reply_text(
                f"📭 <b>{escape(player.name)}</b> {flag} hasn't played a single "
                f"match for anyone yet — no global stats to show.\n"
                f"Own him? Put him in your XI and start a match.",
                parse_mode="HTML")
            return

        text, card = _render_stats(session, player, versions, pages, 0)
        keyboard = _keyboard(tg_user.id, base_id, 0, len(pages), "stats")

        # Lead with the card image, carrying the whole block as its caption
        # whenever Telegram's 1024-char cap allows. When it doesn't, the buttons
        # ride the follow-up text message instead — whichever message carries
        # them is the one the callbacks edit.
        full_in_caption = _caption_fits(text)
        caption = text if full_in_caption else _short_caption(player, card)

        photo = await _photo_source(session, card)
        sent = None
        if photo is not None:
            try:
                sent = await update.message.reply_photo(
                    photo=photo, caption=caption, parse_mode="HTML",
                    reply_markup=keyboard if full_in_caption else None)
            except Exception:
                logger.exception("gstats card send failed; text only")
                sent = None

        if sent is not None and full_in_caption:
            _arm_timeout(context, sent, tg_user.id)
            return

        sent_text = await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True)
        _arm_timeout(context, sent_text, tg_user.id)

    except Exception:
        logger.exception("GStats error for %s", tg_user.id)
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def _arm_timeout(context, sent, owner_tg):
    """Strip the buttons after a while so stale cards can't be driven."""
    try:
        schedule_button_timeout(
            context, sent.chat_id, sent.message_id,
            delay_seconds=BUTTON_TTL_SECONDS,
            timeout_key=f"gst_{owner_tg}_{sent.message_id}")
    except Exception:
        logger.debug("gstats button timeout not armed", exc_info=True)


# ── Callbacks ────────────────────────────────────────────────────────

def _parse(data):
    """``gstpg_<owner_tg>_<base_id>_<index>`` → (owner_tg, base_id, index)."""
    parts = (data or "").split("_")
    if len(parts) != 4:
        raise ValueError(data)
    return int(parts[1]), int(parts[2]), int(parts[3])


async def _apply(q, session, user, player, versions, pages, index, view,
                 owner_tg, base_id):
    """Edit the open card into the requested view/page, media included."""
    if view == "stats":
        text, card = _render_stats(session, player, versions, pages, index)
    else:
        text = _render_owners(session, user, player, versions, pages, index)
        card = pages[index]["card"]

    keyboard = _keyboard(owner_tg, base_id, index, len(pages), view)

    if not getattr(q.message, "photo", None):
        await q.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=keyboard,
                                  disable_web_page_preview=True)
        return

    if _caption_fits(text):
        caption = text
    else:
        # An owners list can outrun a caption where the stat block never does.
        # Trim on a line boundary rather than mid-tag, which would break HTML.
        caption = _trim_to_caption(text)

    # Swap the art too when the page moved to another edition; a failed media
    # edit (same file, no network) must not cost the user the new caption.
    media_swapped = False
    if view == "stats" and len(pages) > 1:
        photo = await _photo_source(session, card)
        if photo is not None:
            try:
                await q.edit_message_media(
                    media=InputMediaPhoto(media=photo, caption=caption,
                                          parse_mode="HTML"),
                    reply_markup=keyboard)
                media_swapped = True
            except Exception:
                logger.debug("gstats media swap failed; caption only",
                             exc_info=True)
    if not media_swapped:
        await q.edit_message_caption(caption=caption, parse_mode="HTML",
                                     reply_markup=keyboard)


def _close_open_tags(html_text):
    """Close whatever elements a trim left open, innermost first.

    Telegram parses a caption's entities before it renders anything: one
    unclosed ``<code>`` and the *whole* edit is rejected, which would cost the
    reader the page they asked for rather than the few lines that didn't fit.
    Every tag this module emits is explicitly closed, so an opener with no
    partner can only be a casualty of the trim.
    """
    open_tags = []
    for match in _HTML_TAG_RE.finditer(html_text):
        tag = match.group(0)
        body = tag.strip("</> ")
        if not body:
            continue
        name = body.split()[0].lower()
        if tag.startswith("</"):
            # Unwind to the matching opener; a stray closer changes nothing.
            if name in open_tags:
                while open_tags and open_tags.pop() != name:
                    pass
        else:
            open_tags.append(name)
    return html_text + "".join(f"</{name}>" for name in reversed(open_tags))


def _trim_to_caption(text, limit=TG_CAPTION_LIMIT):
    """Cut a block down to a caption without breaking its markup.

    Lines go whole, so a cut can never land mid-tag — but the stat block is a
    single ``<code>`` element spanning eleven lines, so a cut *inside* it still
    leaves that element open. Re-balance what survives before sending it.
    """
    lines = text.split("\n")
    while lines and not _caption_fits("\n".join([*lines, "…"]), limit):
        lines.pop()
    if not lines:
        # Not even the first line fits — fall back to plain text, which has no
        # markup left to break.
        return _HTML_TAG_RE.sub("", text)[:limit]
    return _close_open_tags("\n".join(lines)) + "\n…"


async def _load(q, base_id):
    """(session, user, player, versions, pages) or None once it has answered.

    Owns the session until it hands one back: a caller that gets None — or an
    exception — has nothing to close, so a failed lookup can never strand a
    pooled connection.
    """
    session = get_session()
    try:
        player = session.query(Player).get(base_id)
        if not player:
            session.close()
            await q.answer("Player not found", show_alert=True)
            return None
        from services.version_paginator import get_versions_ordered
        versions = get_versions_ordered(session, base_id) or [player]
        user = (session.query(User)
                .filter(User.telegram_id == q.from_user.id).first())
        pages = _build_pages(versions)
    except Exception:
        session.close()
        raise
    return session, user, player, versions, pages


async def gstats_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gstpg_<owner_tg>_<base_id>_<index> — move between editions."""
    await _navigate(update, "stats")


async def gstats_owners_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gstow_<owner_tg>_<base_id>_<index> — flip the card to the owners view."""
    await _navigate(update, "owners")


async def _navigate(update, view):
    q = update.callback_query
    try:
        owner_tg, base_id, index = _parse(q.data)
    except (AttributeError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != owner_tg:
        await q.answer("Not your stats — run /gstats yourself!", show_alert=True)
        return
    await q.answer()

    # The load is inside the guard too: a query that raises there must answer
    # the tap rather than escape the handler and leave the card looking dead.
    session = None
    try:
        loaded = await _load(q, base_id)
        if loaded is None:
            return
        session, user, player, versions, pages = loaded
        index = _clamp(index, len(pages))
        await _apply(q, session, user, player, versions, pages, index, view,
                     owner_tg, base_id)
    except Exception:
        logger.exception("gstats %s callback error", view)
        try:
            await q.answer("⚠️ Error", show_alert=True)
        except Exception:
            logger.debug("gstats error alert failed", exc_info=True)
    finally:
        if session is not None:
            session.close()


async def gstats_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gstcl_<owner_tg>_<base_id>_<index> — drop the buttons, keep the card."""
    q = update.callback_query
    try:
        owner_tg, _base_id, _index = _parse(q.data)
    except (AttributeError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != owner_tg:
        await q.answer("Not your stats — run /gstats yourself!", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("gstats close failed", exc_info=True)
