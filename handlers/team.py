"""Handlers for /teamname, /purse, /stats.

``/stats`` is a card image plus a career record. The record has always been a
``<code>`` block padded with ``str.ljust`` into two fake columns — which only
lines up while every number has the width the padding assumed. Where the server
takes Bot API 10.1 rich messages the same record goes out as two native tables
instead (``services/rich_message.py``, ``docs/rich-text-messages.md``), the card
leading with a short caption; where it does not, nothing changes and the record
rides in the caption exactly as before.
"""

import re
import io
import json
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import (
    User, Player, UserRoster, PlayerGameStats,
    ChallengeLeague, ChallengeTeam, ChallengePlayer,
)
from config import get_buy_value, get_sell_value, MAX_ROSTER
from services.activity_service import log_activity
from services.flags import get_flag
from services import rich_message as R

logger = logging.getLogger(__name__)

TEAM_NAME_REGEX = re.compile(r"^[a-zA-Z0-9 '\-]{3,50}$")

# Telegram caps photo captions at 1024 characters, measured on the *visible*
# text (HTML tags are parsed into entities and don't count) in UTF-16 code units.
TG_CAPTION_LIMIT = 1024
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _caption_fits(html_text, limit=TG_CAPTION_LIMIT):
    """True if ``html_text`` fits within a Telegram photo caption.

    Lets us send the card image and its stats as a single message when the
    stats are short enough, falling back to a separate follow-up text otherwise.
    """
    visible = _HTML_TAG_RE.sub("", html_text)
    visible = (visible.replace("&lt;", "<").replace("&gt;", ">")
               .replace("&amp;", "&"))
    return len(visible.encode("utf-16-le")) // 2 <= limit


async def teamname_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /teamname <name>\nExample: /teamname Royal Challengers")
        return

    name = " ".join(context.args).strip()
    if not TEAM_NAME_REGEX.match(name):
        await update.message.reply_text(
            "❌ Team name must be 3-50 characters, letters/numbers/spaces only"
        )
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        old = user.team_name or "None"
        user.team_name = name
        log_activity(session, user.id, "teamname", f"Team name: {old} → {name}")
        session.commit()

        hint = ("" if user.team_logo_asset_key
                else "\n\n🖼 Want a crest on your scorecards? "
                     "Send /setteamlogo in a private chat.")
        if not hint and not user.team_colour:
            hint = ("\n\n🎨 Want your own colour on the scorecards? "
                    "Try /setteamcolour #aa001b")
        await update.message.reply_text(
            f"✅ Team name set to: <b>{name}</b>{hint}", parse_mode="HTML"
        )
    except Exception:
        session.rollback()
        logger.exception(f"Teamname error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# /setteamcolour — the team's own colour on the scorecards
# ══════════════════════════════════════════════════════════════════════

# People type colour names far more readily than hex. This is not a full CSS
# list on purpose: a short set of strong, card-legible colours beats 140 names
# of which half are unreadable pastels.
COLOUR_NAMES = {
    "red": "#c41e3a", "crimson": "#aa001b", "maroon": "#6d0f1a",
    "orange": "#e8590c", "amber": "#d97706", "gold": "#c7922c",
    "yellow": "#eab308", "lime": "#65a30d", "green": "#15803d",
    "emerald": "#047857", "teal": "#0f766e", "cyan": "#0891b2",
    "sky": "#0284c7", "blue": "#0065b3", "navy": "#123a6d",
    "indigo": "#4338ca", "purple": "#7e22ce", "violet": "#6d28d9",
    "magenta": "#be185d", "pink": "#db2777", "brown": "#78350f",
    "slate": "#475569", "black": "#1a1a1a", "white": "#e8e8e8",
}


def _swatch_png(colour_hex, team_name):
    """A small preview of the team's own bar, so the reply shows the colour.

    A hex code tells nobody what their scorecard will look like. This draws the
    real thing — the crest panel gradient, the colour bar, the score chip — with
    the same helpers the card itself uses, so what they see is what they get.
    """
    try:
        from PIL import Image, ImageDraw
        from services import match_summary_card as card

        colour = card._hex_to_rgb(colour_hex, card.TEAM_A)
        dark = card._shade(colour, 0.62)
        on_bar = card._readable_on(colour)
        w, h, pad = 900, 200, 12
        img = Image.new("RGB", (w, h), (252, 252, 254))
        draw = ImageDraw.Draw(img, "RGBA")

        crest_w = 150
        panel = card._diagonal_gradient((crest_w, h - pad * 2), colour, dark)
        mask = Image.new("L", (crest_w, h - pad * 2), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, crest_w - 1, h - pad * 2 - 1], radius=14, fill=255)
        img.paste(panel, (pad, pad), mask)

        draw.rounded_rectangle([pad + crest_w - 14, pad, w - pad, pad + 72],
                               radius=12, fill=(*colour, 255))
        draw.rectangle([pad + crest_w - 14, pad + 58, w - pad, pad + 72],
                       fill=(*colour, 255))
        draw.polygon([(w - 250, pad), (w - pad, pad), (w - pad, pad + 72),
                      (w - 264, pad + 72)], fill=(*dark, 255))

        small = card._font(15, family="display")
        name_x = pad + crest_w + 16
        # Fit the name into the gap before the score chip, the way the card
        # itself does — a long team name must not run under the score.
        name_room = (w - 280) - name_x
        label = str(team_name or "YOUR TEAM").upper()
        big = card._font(30, family="headline")
        while big.size > card.s(14) and draw.textlength(label, font=big) > name_room:
            big = card._font((big.size / card.SCALE) - 1, family="headline")
        while draw.textlength(label, font=big) > name_room and len(label) > 4:
            label = label[:-2] + "…"
        draw.text((name_x, pad + 36), label, font=big, fill=on_bar, anchor="lm")
        draw.text((w - pad - 24, pad + 36), "156-7",
                  font=card._font(30, family="headline"),
                  fill=card._readable_on(dark), anchor="rm")
        # The crest panel shows initials, which is what a team without an
        # uploaded logo actually gets.
        initials = card._initials(team_name or "Your Team")
        mono = card._font(34, family="headline")
        while mono.size > card.s(12) and draw.textlength(initials, font=mono) > crest_w - 26:
            mono = card._font((mono.size / card.SCALE) - 1, family="headline")
        draw.text((pad + crest_w / 2, pad + (h - pad * 2) / 2), initials,
                  font=mono,
                  fill=card._readable_on(card._mix(colour, dark, 0.5)), anchor="mm")
        draw.text((pad + crest_w + 16, pad + 108), "VENKATESH IYER",
                  font=small, fill=card.NAME_INK, anchor="lm")
        draw.text((w - pad - 120, pad + 108), "29*", font=small, fill=colour,
                  anchor="rm")
        draw.text((w - pad - 24, pad + 108), colour_hex.upper(), font=small,
                  fill=card.COL_HEAD, anchor="rm")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # The colour is still set; only the picture is missing.
        logger.warning("team colour swatch failed", exc_info=True)
        return None


async def teamcolour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``/setteamcolour #aa001b`` — the team's colour on every scorecard.

    Applies immediately, unlike the crest: a hex code carries nothing to
    moderate, and the card picks readable text for whatever is chosen.
    """
    from services import card_identity

    tg_user = update.effective_user
    message = update.effective_message
    arg = " ".join(context.args or []).strip()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await message.reply_text("❌ Do /debut first!")
            return

        if not arg:
            current = card_identity.normalise_hex(user.team_colour)
            if not current:
                await message.reply_text(
                    "🎨 <b>Team colour</b>\n\n"
                    "Your scorecards use the default colours right now.\n\n"
                    "Set your own with a hex code — <code>/setteamcolour "
                    "#aa001b</code> — or a name like <code>/setteamcolour "
                    "crimson</code>.\n\n"
                    f"<b>Names:</b> {', '.join(sorted(COLOUR_NAMES))}",
                    parse_mode="HTML")
                return
            png = _swatch_png(current, user.team_name)
            caption = (f"🎨 Your team colour is <code>{current}</code>.\n\n"
                       "Change it with /setteamcolour, or clear it with "
                       "/setteamcolour remove.")
            if png:
                await message.reply_photo(photo=io.BytesIO(png), caption=caption,
                                          parse_mode="HTML")
            else:
                await message.reply_text(caption, parse_mode="HTML")
            return

        if arg.lower() in ("remove", "clear", "delete", "off", "reset"):
            had = bool(user.team_colour)
            user.team_colour = None
            log_activity(session, user.id, "teamcolour", "Team colour cleared")
            session.commit()
            card_identity_invalidate(user)
            await message.reply_text(
                "🗑 Team colour cleared — your scorecards go back to the "
                "default colours." if had
                else "You do not have a team colour set.")
            return

        colour = COLOUR_NAMES.get(arg.lower()) or card_identity.normalise_hex(arg)
        if not colour:
            await message.reply_text(
                f"❌ <code>{arg[:30]}</code> is not a colour I understand.\n\n"
                "Send a hex code like <code>#aa001b</code> (the <code>#</code> "
                "is optional), or a name like <code>crimson</code>.\n\n"
                f"<b>Names:</b> {', '.join(sorted(COLOUR_NAMES))}",
                parse_mode="HTML")
            return

        old = user.team_colour or "default"
        user.team_colour = colour
        log_activity(session, user.id, "teamcolour", f"Team colour: {old} → {colour}")
        session.commit()
        card_identity_invalidate(user)

        hint = ("" if user.team_logo_asset_key
                else "\n\n🖼 A crest goes beside it — /setteamlogo in a "
                     "private chat.")
        png = _swatch_png(colour, user.team_name)
        caption = (f"✅ Team colour set to <code>{colour}</code>. "
                   "It is on your scorecards from your next match." + hint)
        if png:
            await message.reply_photo(photo=io.BytesIO(png), caption=caption,
                                      parse_mode="HTML")
        else:
            await message.reply_text(caption, parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("Teamcolour error for %s", tg_user.id)
        await message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def card_identity_invalidate(user):
    """Drop the cached crest/colour for a team so the change shows at once."""
    try:
        from services import team_logo_service
        team_logo_service.invalidate_cache(user.id, user.team_name)
    except Exception:
        logger.warning("team colour cache invalidation failed", exc_info=True)


async def purse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        team = user.team_name or "No team name set"
        body = (
            f"👤 <b>@{user.username or user.first_name}</b>\n"
            f"🏏 Team: {team}\n\n"
            f"💰 <b>Coins:</b> {user.total_coins:,}\n"
            f"💎 <b>Gems:</b> {user.total_gems}\n"
            f"📊 Roster: {user.roster_count}/{MAX_ROSTER}"
        )
        # Lead with the team's approved crest when it has one. Telegram's own
        # file_id costs no upload; a team whose crest predates that id (or
        # whose id Telegram has since rejected) simply gets the text.
        if user.team_logo_file_id:
            try:
                await update.message.reply_photo(
                    photo=user.team_logo_file_id, caption=body, parse_mode="HTML")
                return
            except Exception:
                logger.info("purse could not send the team logo for %s",
                            tg_user.id, exc_info=True)
        await update.message.reply_text(body, parse_mode="HTML")
    except Exception:
        logger.exception(f"Purse error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def _career_blocks(player, flag, owner_name, buy_val, gs):
    """A player's career record as two tables — the twin of the ``<code>`` block.

    Batting and bowling are read separately, so they are separate tables rather
    than one block of two columns glued together by padding.
    """
    try:
        return [
            R.heading(f"📛 {player.name} {flag}", size=3),
            R.paragraph([R.bold(f"⭐ {player.rating} OVR"),
                         f"  ·  {player.category or '—'}"]),
            R.table([
                [R.cell(R.bold("👤 Owner")), R.cell(owner_name)],
                [R.cell(R.bold("💰 Value")),
                 R.cell(f"{buy_val:,} 🪙", align="right")],
                [R.cell(R.bold("🏆 POTM")),
                 R.cell(str(gs.potm), align="right")],
            ], bordered=True, compact=True),
            R.table([
                [R.cell(R.bold("Inns")), R.cell(str(gs.bat_inns), align="right"),
                 R.cell(R.bold("Runs")), R.cell(R.bold(str(gs.runs)),
                                                align="right")],
                [R.cell(R.bold("50s")), R.cell(str(gs.fifties), align="right"),
                 R.cell(R.bold("100s")), R.cell(str(gs.hundreds),
                                                align="right")],
                [R.cell(R.bold("4s / 6s")),
                 R.cell(f"{gs.fours}/{gs.sixes}", align="right"),
                 R.cell(R.bold("Ducks")), R.cell(str(gs.ducks), align="right")],
                [R.cell(R.bold("Avg")), R.cell(str(gs.bat_avg), align="right"),
                 R.cell(R.bold("SR")), R.cell(str(gs.bat_sr), align="right")],
                [R.cell(R.bold("HS")), R.cell(R.bold(gs.hs_str), align="right"),
                 R.cell(""), R.cell("")],
            ], bordered=True, compact=True, caption=R.bold("🏏 Batting")),
            R.table([
                [R.cell(R.bold("Inns")), R.cell(str(gs.bowl_inns), align="right"),
                 R.cell(R.bold("Wickets")),
                 R.cell(R.bold(str(gs.wickets_taken)), align="right")],
                [R.cell(R.bold("3-Fers")), R.cell(str(gs.three_fers),
                                                  align="right"),
                 R.cell(R.bold("5-Fers")), R.cell(str(gs.five_fers),
                                                  align="right")],
                [R.cell(R.bold("Avg")), R.cell(str(gs.bowl_avg), align="right"),
                 R.cell(R.bold("Econ")), R.cell(str(gs.bowl_economy),
                                                align="right")],
                [R.cell(R.bold("SR")), R.cell(str(gs.bowl_sr), align="right"),
                 R.cell(R.bold("Hattricks")), R.cell(str(gs.hattricks),
                                                     align="right")],
                [R.cell(R.bold("BBF")), R.cell(R.bold(gs.bbf_str),
                                               align="right"),
                 R.cell(""), R.cell("")],
            ], bordered=True, compact=True, caption=R.bold("🎯 Bowling")),
            R.footer(["Everyone's numbers with this cricketer: ",
                      R.code(f"/gstats {player.name}")]),
        ]
    except Exception:
        logger.exception("career blocks failed to build for %s", player.name)
        return None


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats <player_name> — show per-owner game stats."""
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /stats <player name>\nExample: /stats Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Check user owns this player
        result = (
            session.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id, Player.name.ilike(f"%{search}%"))
            .first()
        )

        if not result:
            await update.message.reply_text(
                f"❌ Player '{search}' not in your roster. You can only view stats of players you own."
            )
            return

        entry, player = result
        flag = get_flag(player.country)

        # Get or create game stats
        gs = (
            session.query(PlayerGameStats)
            .filter(PlayerGameStats.user_id == user.id, PlayerGameStats.player_id == player.id)
            .first()
        )

        if not gs:
            # No stats yet — never played
            gs = PlayerGameStats(user_id=user.id, player_id=player.id)
            session.add(gs)
            session.commit()

        buy_val = get_buy_value(player.rating)
        owner_name = f"@{user.username}" if user.username else user.first_name

        text = (
            f"📛 <b>{player.name}</b> {flag}\n"
            f"⭐ {player.rating} OVR | {player.category}\n\n"
            f"<code>"
            f"Owner: {owner_name}\n"
            f"Value: {buy_val:,} 🪙\n"
            f"POTM(s): {gs.potm}\n"
            f"\n"
            f"{'🏏 BATTING':<20}{'🎯 BOWLING'}\n"
            f"{'─' * 38}\n"
            f"Inns: {gs.bat_inns:<14}Inns: {gs.bowl_inns}\n"
            f"Runs: {gs.runs:<14}Wickets: {gs.wickets_taken}\n"
            f"50s: {gs.fifties:<15}3-Fers: {gs.three_fers}\n"
            f"100s: {gs.hundreds:<14}5-Fers: {gs.five_fers}\n"
            f"4/6: {gs.fours}/{gs.sixes:<12}Hattricks: {gs.hattricks}\n"
            f"Avg: {gs.bat_avg:<14}Avg: {gs.bowl_avg}\n"
            f"SR: {gs.bat_sr:<15}Economy: {gs.bowl_economy}\n"
            f"Ducks: {gs.ducks:<13}SR: {gs.bowl_sr}\n"
            f"HS: {gs.hs_str:<15}BBF: {gs.bbf_str}\n"
            f"</code>"
        )

        # Lead with the player's card image (custom card if uploaded, else the
        # cached generated/template card). Send the image and stats as ONE
        # message by using the full stats block as the photo caption whenever it
        # fits Telegram's 1024-char cap; only fall back to a short caption + a
        # separate stats message when the block is too long.
        # With rich messages available the record goes out as its own message
        # of native tables, so the card leads with the short caption. The
        # decision is made *before* the photo is sent, which is why it reads the
        # flag rather than the outcome of a send: at worst this costs the same
        # two messages an over-long record has always cost, and once the latch
        # trips (an older Bot API server) it is back to one.
        career_blocks = (_career_blocks(player, flag, owner_name, buy_val, gs)
                         if R.rich_text_enabled() else None)
        full_in_caption = _caption_fits(text) and not career_blocks
        short_caption = (
            f"📛 <b>{player.name}</b> {flag}\n"
            f"⭐ {player.rating} OVR | {player.category}\n"
            f"🏆 POTM(s): {gs.potm}"
        )
        photo_caption = text if full_in_caption else short_caption

        custom_bytes = None
        try:
            from services.player_image_service import has_custom_card, get_custom_image_bytes
            if has_custom_card(player.id, session):
                custom_bytes = await asyncio.to_thread(get_custom_image_bytes, player.id)
        except Exception:
            logger.exception("Stats custom card load failed for %s", player.id)

        photo_sent = False
        if custom_bytes:
            try:
                await update.message.reply_photo(
                    photo=io.BytesIO(custom_bytes), caption=photo_caption, parse_mode="HTML")
                photo_sent = True
            except Exception:
                # Don't let a failed image upload swallow the stats text below.
                logger.exception("Stats custom card send failed; continuing with text")
        else:
            # Reuses a stored Telegram file_id when available (no re-render/upload).
            from services.card_generator import send_generated_card
            sent = await send_generated_card(
                update.message.reply_photo, player,
                caption=photo_caption, parse_mode="HTML")
            photo_sent = sent is not None
        # Send the stats as a follow-up only when they weren't already carried in
        # the photo caption (or when no card image could be sent at all).
        if not (photo_sent and full_in_caption):
            await R.reply_rich(update.message, career_blocks, text)

    except Exception:
        logger.exception(f"Stats error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def _cp_details(player):
    """Parse a ChallengePlayer.details_json blob into a dict."""
    raw = getattr(player, "details_json", None) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_challenge_players(session, search):
    """Return [(ChallengePlayer, ChallengeTeam, ChallengeLeague), ...] for a name.

    Searches every active challenge league. Prefers exact (case-insensitive)
    name matches; if none are exact, returns the partial matches. The same real
    player can appear in several leagues, so this returns every appearance.
    Returns an empty list when the player is not part of any challenge league.
    """
    rows = (
        session.query(ChallengePlayer, ChallengeTeam, ChallengeLeague)
        .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
        .join(ChallengeLeague, ChallengeTeam.league_id == ChallengeLeague.id)
        .filter(
            ChallengeLeague.is_active == True,  # noqa: E712
            ChallengeTeam.is_active == True,  # noqa: E712
            ChallengePlayer.name.ilike(f"%{search}%"),
        )
        .order_by(ChallengeLeague.sort_order, ChallengeLeague.name,
                  ChallengeTeam.sort_order, ChallengePlayer.sort_order)
        .all()
    )
    if not rows:
        return []
    needle = search.strip().lower()
    exact = [r for r in rows if (r[0].name or "").strip().lower() == needle]
    return exact or rows


def _aggregate_player_game_stats(session, player_ids):
    """Sum every owner's PlayerGameStats for one or more master player_ids."""
    agg = {
        "potm": 0, "bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
        "fours": 0, "sixes": 0, "balls_faced": 0, "times_out": 0, "ducks": 0,
        "highest_score": 0, "highest_score_not_out": False,
        "bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
        "balls_bowled": 0, "three_fers": 0, "five_fers": 0, "hattricks": 0,
        "best_bowl_wickets": 0, "best_bowl_runs": 0, "_has_bbf": False,
    }
    ids = [pid for pid in {p for p in (player_ids or []) if p}]
    if not ids:
        return agg
    rows = (session.query(PlayerGameStats)
            .filter(PlayerGameStats.player_id.in_(ids)).all())
    for gs in rows:
        for key in ("potm", "bat_inns", "runs", "fifties", "hundreds", "fours",
                    "sixes", "balls_faced", "times_out", "ducks", "bowl_inns",
                    "wickets_taken", "runs_conceded", "balls_bowled",
                    "three_fers", "five_fers", "hattricks"):
            agg[key] += getattr(gs, key, 0) or 0
        if (gs.highest_score or 0) > agg["highest_score"]:
            agg["highest_score"] = gs.highest_score or 0
            agg["highest_score_not_out"] = bool(gs.highest_score_not_out)
        # Best bowling = most wickets, fewest runs as a tie-break.
        if gs.bowl_inns:
            bw, br = gs.best_bowl_wickets or 0, gs.best_bowl_runs or 0
            if (not agg["_has_bbf"] or bw > agg["best_bowl_wickets"]
                    or (bw == agg["best_bowl_wickets"] and br < agg["best_bowl_runs"])):
                agg["best_bowl_wickets"], agg["best_bowl_runs"] = bw, br
                agg["_has_bbf"] = True
    return agg


def _cp_attr(cp, details, master, key, fallback, cast=None):
    """Read an attribute preferring the per-league details, then the master."""
    val = details.get(key)
    if val in (None, ""):
        val = getattr(master, key, None) if master else None
    if val in (None, ""):
        val = fallback
    if cast is not None:
        try:
            return cast(val)
        except (TypeError, ValueError):
            return fallback
    return val


async def statscl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/statscl <player name> — Challenge League player stats. Anyone can view.

    Lists every league the player appears in (with that league's card values,
    which can differ league to league) plus their overall match career.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /statscl <player name>\nExample: /statscl Virat Kohli")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        rows = _find_challenge_players(session, search)
        if not rows:
            await update.message.reply_text("❌ No player found")
            return

        # A partial search (e.g. "Singh") can match several different players.
        # Resolve to one: prefer an exact name match, else the player with the
        # most league appearances. Group by normalized name so the same person
        # linked to different master ids across leagues stays together — and
        # unrelated players never get merged into one card.
        needle = search.strip().lower()
        groups = {}
        for r in rows:
            groups.setdefault((r[0].name or "").strip().lower(), []).append(r)
        rows = groups.get(needle) or max(groups.values(), key=len)

        # Display name from the first (exact-preferred) appearance.
        name = rows[0][0].name or search

        # Pick a master Player (for the card art) from any appearance that links
        # one, and collect every linked master id for the career aggregate.
        master = None
        source_ids = []
        primary_details = {}
        for cp, _team, _league in rows:
            d = _cp_details(cp)
            spid = getattr(cp, "source_player_id", None) or d.get("source_player_id")
            if spid:
                source_ids.append(spid)
                if master is None:
                    m = session.get(Player, spid)
                    if m is not None:
                        master, primary_details = m, d
        if not primary_details:
            primary_details = _cp_details(rows[0][0])

        country = (getattr(master, "country", None)
                   or primary_details.get("country") or "")
        flag = get_flag(country) if country else ""
        head_rating = _cp_attr(rows[0][0], primary_details, master,
                               "rating", 0, int)
        head_category = _cp_attr(rows[0][0], primary_details, master,
                                 "category", "Player")

        # ── Per-league appearances ──
        league_lines = []
        for cp, team, league in rows[:8]:
            d = _cp_details(cp)
            rating = _cp_attr(cp, d, master, "rating", 0, int)
            role = _cp_attr(cp, d, master, "category", "Player")
            bat_r = _cp_attr(cp, d, master, "bat_rating", 0, int)
            bowl_r = _cp_attr(cp, d, master, "bowl_rating", 0, int)
            bat_hand = _cp_attr(cp, d, master, "bat_hand", "Right")
            bowl_style = _cp_attr(cp, d, master, "bowl_style", "")
            extra = f" · {bowl_style}" if bowl_style else ""
            league_lines.append(
                f"🏆 <b>{league.name}</b> · {team.name}\n"
                f"   ⭐ {rating} OVR · {role}\n"
                f"   🏏 Bat {bat_r} · 🎯 Bowl {bowl_r}\n"
                f"   {bat_hand}-hand{extra}"
            )
        more = ""
        if len(rows) > 8:
            more = f"\n<i>…and {len(rows) - 8} more league(s)</i>"

        # ── Overall match career (shared across leagues/modes) ──
        agg = _aggregate_player_game_stats(session, source_ids)
        bat_avg = round(agg["runs"] / agg["times_out"], 2) if agg["times_out"] else 0.0
        bat_sr = round(agg["runs"] / agg["balls_faced"] * 100, 2) if agg["balls_faced"] else 0.0
        overs = round(agg["balls_bowled"] / 6, 1) if agg["balls_bowled"] else 0.0
        bowl_avg = round(agg["runs_conceded"] / agg["wickets_taken"], 2) if agg["wickets_taken"] else 0.0
        econ = round(agg["runs_conceded"] / overs, 2) if overs else 0.0
        bowl_sr = round(agg["balls_bowled"] / agg["wickets_taken"], 2) if agg["wickets_taken"] else 0.0
        hs = "-"
        if agg["bat_inns"] or agg["highest_score"]:
            hs = f"{agg['highest_score']}{'*' if agg['highest_score_not_out'] else ''}"
        bbf = f"{agg['best_bowl_wickets']}/{agg['best_bowl_runs']}" if agg["_has_bbf"] else "-"
        played = bool(agg["bat_inns"] or agg["bowl_inns"])

        header = (
            f"📛 <b>{name}</b> {flag}\n"
            f"📋 In <b>{len(rows)}</b> Challenge League{'s' if len(rows) != 1 else ''}\n\n"
        )
        # Keep the per-league breakdown tidy in chat by tucking it inside an
        # expandable quote — the header and career stats stay visible, the league
        # cards expand on tap.
        leagues_block = "\n\n".join(league_lines) + more
        text = header + f"<blockquote expandable>{leagues_block}</blockquote>\n\n"
        if played:
            text += (
                f"<b>📊 Match Career</b> (all leagues)\n"
                f"<code>"
                f"POTM(s): {agg['potm']}\n"
                f"\n"
                f"{'🏏 BATTING':<20}{'🎯 BOWLING'}\n"
                f"{'─' * 38}\n"
                f"Inns: {agg['bat_inns']:<14}Inns: {agg['bowl_inns']}\n"
                f"Runs: {agg['runs']:<14}Wickets: {agg['wickets_taken']}\n"
                f"50s: {agg['fifties']:<15}3-Fers: {agg['three_fers']}\n"
                f"100s: {agg['hundreds']:<14}5-Fers: {agg['five_fers']}\n"
                f"4/6: {str(agg['fours']) + '/' + str(agg['sixes']):<13}Hattricks: {agg['hattricks']}\n"
                f"Avg: {bat_avg:<14}Avg: {bowl_avg}\n"
                f"SR: {bat_sr:<15}Economy: {econ}\n"
                f"Ducks: {agg['ducks']:<13}SR: {bowl_sr}\n"
                f"HS: {hs:<15}BBF: {bbf}\n"
                f"</code>"
            )
        else:
            text += "<i>📊 No match stats yet — this player hasn't featured in a completed match.</i>"

        # Lead with the player's card image (master Player art when available).
        card_player = master
        if card_player is None:
            from types import SimpleNamespace
            card_player = SimpleNamespace(
                id=-(rows[0][0].id),
                name=name, rating=head_rating or 50, category=head_category,
                country=country or "",
                bat_hand=primary_details.get("bat_hand") or "Right",
                bowl_hand=primary_details.get("bowl_hand") or "Right",
                bowl_style=primary_details.get("bowl_style") or "",
                bat_rating=int(primary_details.get("bat_rating") or 50),
                bowl_rating=int(primary_details.get("bowl_rating") or 40),
                version=primary_details.get("version") or "Base",
                card_file_id=None,
            )

        # Lead with the player's card image (custom card if the master player
        # has one, else the cached generated/template card). Send the image and
        # stats as ONE message by using the full stats block as the photo caption
        # whenever it fits Telegram's 1024-char cap; only fall back to a short
        # caption + a separate stats message when the block is too long (e.g. a
        # player featuring in many leagues).
        full_in_caption = _caption_fits(text)
        short_caption = (
            f"📛 <b>{name}</b> {flag}\n"
            f"⭐ {head_rating} OVR | {head_category}\n"
            f"📋 In {len(rows)} Challenge League{'s' if len(rows) != 1 else ''}"
        )
        photo_caption = text if full_in_caption else short_caption

        custom_bytes = None
        try:
            from services.player_image_service import has_custom_card, get_custom_image_bytes
            cp_id = getattr(card_player, "id", None)
            if master is not None and cp_id and cp_id > 0 and has_custom_card(cp_id, session):
                custom_bytes = await asyncio.to_thread(get_custom_image_bytes, cp_id)
        except Exception:
            logger.exception("statscl custom card load failed for %s", name)

        photo_sent = False
        if custom_bytes:
            try:
                await update.message.reply_photo(
                    photo=io.BytesIO(custom_bytes), caption=photo_caption, parse_mode="HTML")
                photo_sent = True
            except Exception:
                # Don't let a failed image upload swallow the stats text below.
                logger.exception("statscl custom card send failed; continuing with text")
        elif card_player is not None:
            # Reuses a stored Telegram file_id when available (no re-render/upload).
            # Synthetic (negative-id) card players use the in-memory cache only.
            from services.card_generator import send_generated_card
            sent = await send_generated_card(
                update.message.reply_photo, card_player,
                caption=photo_caption, parse_mode="HTML")
            photo_sent = sent is not None
        # Send the stats as a follow-up only when they weren't already carried in
        # the photo caption (or when no card image could be sent at all).
        if not (photo_sent and full_in_caption):
            await update.message.reply_text(text, parse_mode="HTML")

    except Exception:
        logger.exception("statscl error for search '%s'", search)
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()
