"""Two-player challenge mode using the Mini App match flow."""

import json
import logging
import random
import os
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import ChallengeLeague, ChallengePlayer, ChallengeTeam, FantasyLeague, Match, User
from services.match_constants import MATCH_EXPIRE, PITCH_TYPES, random_match_settings
from services.telegram_user_service import resolve_command_target, sync_telegram_user
from handlers.match import (
    _active_cric_match_for_user,
    _active_cric_match_in_chat,
    _active_match_in_chat,
    _active_match_for_user,
    _chat_busy_message,
    _cric_lobby_for_user,
    _mention,
    _user_busy_message,
    _user_label,
)

logger = logging.getLogger(__name__)

CM_LOBBY_EXPIRE = 75

# League team/XI selection can take a while (two team picks, pitch, two XIs),
# so this expiry is generous — it only frees a draft that was clearly abandoned
# (never started), releasing the per-chat lock so the group isn't stuck.
CHALLENGE_DRAFT_EXPIRE = int(os.getenv("CHALLENGE_DRAFT_EXPIRE_SECONDS", "600"))

# Per-turn selection timeout. While a draft waits on a specific player (team
# pick, pitch, or Playing XI), they get a 30s mention reminder; if they still
# haven't acted by the full window, the match is forfeited. Each valid action
# resets the clock (inactivity-based), so an active setup is never killed.
CL_SELECT_WINDOW = int(os.getenv("CL_SELECT_WINDOW_SECONDS", "60"))
CL_SELECT_REMIND = int(os.getenv("CL_SELECT_REMIND_SECONDS", "30"))
# Forfeit penalty: the idle player is fined, the opponent compensated the same.
CL_FORFEIT_COINS = int(os.getenv("CL_FORFEIT_COINS", "3000"))
CL_FORFEIT_GEMS = int(os.getenv("CL_FORFEIT_GEMS", "5"))

CHALLENGE_REPLY_REQUIRED_MESSAGE = "Please reply to a user’s message to challenge them."
BUILT_IN_CHALLENGE_LEAGUES = {
    "ipl": "IPL",
    "bbl": "BBL",
    "int": "INT",
}

IPL_TEAM_NAMES = [
    "Mumbai Indians",
    "Chennai Super Kings",
    "Royal Challengers Bengaluru",
    "Kolkata Knight Riders",
    "Rajasthan Royals",
    "Sunrisers Hyderabad",
    "Delhi Capitals",
    "Gujarat Titans",
    "Punjab Kings",
    "Lucknow Super Giants",
]

BUILT_IN_CHALLENGE_TEAMS = {
    "ipl": IPL_TEAM_NAMES,
}


IPL_TEAM_META = {
    "Mumbai Indians": ("MI", "🔵"),
    "Chennai Super Kings": ("CSK", "🟡"),
    "Royal Challengers Bengaluru": ("RCB", "🔴"),
    "Kolkata Knight Riders": ("KKR", "🟣"),
    "Rajasthan Royals": ("RR", "🩷"),
    "Sunrisers Hyderabad": ("SRH", "🟠"),
    "Delhi Capitals": ("DC", "🔷"),
    "Gujarat Titans": ("GT", "🔵"),
    "Punjab Kings": ("PBKS", "🔴"),
    "Lucknow Super Giants": ("LSG", "🔷"),
}


def _challenge_team_draft_key(draft_id):
    return f"challenge_team_draft_{draft_id}"


def _track_setup_msg(draft, message):
    """Record a setup message id on the draft so the over-by-over match can sweep
    all the pre-match chatter from the chat once play begins (keeping only the
    toss result). Safe no-op for a missing draft/message."""
    if not draft or message is None:
        return
    mid = getattr(message, "message_id", None)
    if mid is None:
        return
    ids = draft.setdefault("setup_msg_ids", [])
    if mid not in ids:
        ids.append(mid)


def _league_battle_title(league_name):
    return f"League Battles · {league_name}" if league_name else "League Battles"


def normalize_challenge_league(value):
    """Return a command-safe league key using only letters and digits."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _league_display_from_key(league_key, known_leagues=None):
    if known_leagues and league_key in known_leagues:
        return known_leagues[league_key]
    return BUILT_IN_CHALLENGE_LEAGUES.get(league_key, league_key.upper())


def _challenge_command_name(update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not text.startswith("/"):
        return ""
    token = text.split(maxsplit=1)[0][1:]
    return token.split("@", 1)[0].lower()


def _league_key_from_command(command_name):
    command = (command_name or "").lower()
    if command.startswith("challenge") and len(command) > len("challenge"):
        return normalize_challenge_league(command[len("challenge"):])
    if command.startswith("c") and len(command) > 1:
        return normalize_challenge_league(command[1:])
    return None


def _challenge_leagues(session):
    leagues = dict(BUILT_IN_CHALLENGE_LEAGUES)
    try:
        for league in (session.query(ChallengeLeague)
                       .filter(ChallengeLeague.is_active == True)
                       .all()):
            key = normalize_challenge_league(league.short_code or league.name)
            if key:
                leagues[key] = league.name.strip()
    except Exception:
        logger.exception("Failed to load admin challenge leagues")
    try:
        for name, in session.query(FantasyLeague.name).all():
            key = normalize_challenge_league(name)
            if key:
                leagues.setdefault(key, name.strip())
    except Exception:
        logger.exception("Failed to load fantasy challenge leagues")
    return leagues


def _challenge_league_command_aliases(session):
    """Return exact command aliases for admin-managed challenge leagues."""
    aliases = {}
    try:
        for league in (session.query(ChallengeLeague)
                       .filter(ChallengeLeague.is_active == True)
                       .all()):
            league_key = normalize_challenge_league(league.short_code or league.name)
            if not league_key:
                continue
            display_name = (league.name or league_key.upper()).strip()
            command = (league.command or "").strip().lower().lstrip("/").split("@", 1)[0]
            if command:
                aliases[command] = (league_key, display_name)
            short = normalize_challenge_league(league.short_code)
            if short:
                aliases.setdefault(f"c{short}", (league_key, display_name))
                aliases.setdefault(f"challenge{short}", (league_key, display_name))
    except Exception:
        logger.exception("Failed to load admin challenge command aliases")
    return aliases


def is_challenge_league_command(command_name, session):
    """Return ``(league_key, display_name)`` for supported challenge league commands."""
    command = (command_name or "").lower().lstrip("/")
    aliases = _challenge_league_command_aliases(session)
    if command in aliases:
        return aliases[command]
    league_key = _league_key_from_command(command)
    if not league_key:
        return None, None
    leagues = _challenge_leagues(session)
    if league_key not in leagues:
        return None, None
    return league_key, _league_display_from_key(league_key, leagues)


def _reply_target_telegram_user(update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    return getattr(reply, "from_user", None) if reply is not None else None



def _max_overs(session=None):
    """Return the website-configured challenge limit, clamped defensively."""
    from services.config_service import get_challenge_max_overs
    return get_challenge_max_overs(session)


def _cm_lobby_key(lobby_id):
    return f"cm_lobby_{lobby_id}"


def _cm_chat_key(chat_id):
    return f"cm_lobby_chat_{chat_id}"


def _challenge_draft_chat_key(chat_id):
    """Per-chat lock for a Challenge League team/XI selection draft.

    Only one league challenge setup may be in progress in a chat at a time, so
    a second user cannot start a fresh league challenge while another player's
    team/player selection is still under way in the same group.
    """
    return f"cl_draft_chat_{chat_id}"


def _active_draft_in_chat(bot_data, chat_id):
    """Return the live league draft for this chat, or None.

    Self-heals a stale chat→draft pointer (e.g. if the draft dict was already
    removed) so a chat is never permanently locked out of new challenges.
    """
    draft_id = bot_data.get(_challenge_draft_chat_key(chat_id))
    if draft_id is None:
        return None
    draft = bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft:
        bot_data.pop(_challenge_draft_chat_key(chat_id), None)
        return None
    return draft


def _release_draft_chat_lock(bot_data, draft):
    """Drop the per-chat draft lock once a draft ends or becomes a live match."""
    if not draft:
        return
    chat_id = draft.get("chat_id")
    if chat_id is not None:
        bot_data.pop(_challenge_draft_chat_key(chat_id), None)


def _waiting_cm_lobby_in_chat(bot_data, chat_id):
    """True only if an *unanswered* /cm invite is waiting in this chat.

    An accepted /cm lobby that stalled (e.g. the toss winner never pressed a
    button) keeps its ``_cm_chat_key`` entry with no expiry and can't be
    cancelled, so treating it as a blocker would lock every league challenge in
    the chat forever. Live-match concurrency is already covered by the DB
    active-match checks, so we only block on a lobby still awaiting a response.
    """
    lobby_id = bot_data.get(_cm_chat_key(chat_id))
    if lobby_id is None:
        return False
    lobby = bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        bot_data.pop(_cm_chat_key(chat_id), None)
        return False
    return not lobby.get("accepted")


def _cm_user_lobby(bot_data, user_id):
    return next((lobby for key, lobby in bot_data.items()
                 if key.startswith("cm_lobby_")
                 and isinstance(lobby, dict)
                 and user_id in (lobby.get("challenger_user_id"),
                                 lobby.get("target_user_id"))), None)


def _pop_lobby(context, lobby_id):
    key = _cm_lobby_key(lobby_id)
    lobby = context.bot_data.pop(key, None)
    if lobby:
        context.bot_data.pop(_cm_chat_key(lobby.get("chat_id")), None)
    return lobby


def _cancel_cm_timer(context, lobby_id):
    try:
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(f"cm_lobby_{lobby_id}"):
                job.schedule_removal()
    except Exception:
        logger.exception("Failed to cancel /cm lobby timer")


def _xi_error(errors_or_count):
    if isinstance(errors_or_count, (list, tuple)):
        return "❌ Your playing XI is invalid. Use /xi to fix it:\n" + "\n".join(
            f"• {error}" for error in errors_or_count)
    if errors_or_count == 0:
        return "❌ You do not have a squad yet. Use /debut first, then accept the challenge again."
    return ("❌ You need a valid playing XI before accepting challenge mode. "
            f"You currently have {errors_or_count}/11 players. Use /autobuild or /xi after completing your squad.")


def _validate_user_xi(session, user_id):
    from handlers.lineup import validate_xi, _get_ordered_roster
    roster = _get_ordered_roster(session, user_id)
    valid, errors = validate_xi(roster)
    return valid, errors, len(roster)


def _get_challenge_league_record(session, league_key):
    if not league_key:
        return None
    try:
        for league in (session.query(ChallengeLeague)
                       .filter(ChallengeLeague.is_active == True)
                       .all()):
            if normalize_challenge_league(league.short_code or league.name) == league_key:
                return league
            command = (league.command or "").strip().lower().lstrip("/").split("@", 1)[0]
            if command and is_challenge_league_command(command, session)[0] == league_key:
                return league
    except Exception:
        logger.exception("Failed to load challenge league record")
    return None


def _league_image_url(league_record):
    return (getattr(league_record, "image_url", None) or "").strip() or None


def _league_teams(session, league_key, league_record=None):
    teams = []
    if league_record is not None:
        try:
            query = session.query(ChallengeTeam).filter(ChallengeTeam.league_id == league_record.id)
            if hasattr(ChallengeTeam, "is_active"):
                query = query.filter(ChallengeTeam.is_active == True)
            teams = [team.name for team in (query.order_by(ChallengeTeam.sort_order, ChallengeTeam.name).all())
                     if (team.name or "").strip()]
        except Exception:
            logger.exception("Failed to load challenge teams for league %s", league_key)
    if not teams:
        teams = BUILT_IN_CHALLENGE_TEAMS.get(league_key, [])
    return teams


def _team_keyboard(draft_id, teams, unavailable_teams=None, team_codes=None):
    """Build the team-selection keyboard.

    Buttons show the team's short code (e.g. ``MI``, ``CSK``) rather than the
    long full name; ``team_codes`` maps full name → short code (precomputed at
    draft time so it stays consistent across re-renders). A Cancel button lets
    either participant abort the selection.
    """
    unavailable = {team for team in (unavailable_teams or []) if team}
    codes = team_codes or {}
    rows = []
    row = []
    for idx, team in enumerate(teams):
        if team in unavailable:
            continue
        label = codes.get(team) or team
        row.append(InlineKeyboardButton(label, callback_data=f"cl_team_{draft_id}_{idx}"))
        # Two teams per row for a more compact picker.
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Cancel aborts the whole setup (either player); Deny Match lets the guest
    # refuse the challenge right here in Team Selection (validated in the handler).
    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cl_cancel_{draft_id}"),
        InlineKeyboardButton("🚫 Deny Match", callback_data=f"cl_denymatch_{draft_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def _team_selection_status(draft):
    lines = []
    host_team = draft.get("host_team")
    target_team = draft.get("target_team")
    if host_team:
        host = draft.get("host") or {}
        lines.append(
            f"✅ {_mention(host.get('tg_id'), host.get('name') or 'User 1')} "
            f"selected <b>{host_team}</b>."
        )
    if target_team:
        target = draft.get("target") or {}
        lines.append(
            f"✅ {_mention(target.get('tg_id'), target.get('name') or 'User 2')} "
            f"selected <b>{target_team}</b>."
        )
    return lines


# ── Pitch selection (host chooses the surface after teams are picked) ──
# Short, friendly one-liners shown beside each surface in the picker prompt.
_PITCH_DESC = {
    "Dry": "spin-friendly, tough to score",
    "Dusty": "big turn, spinners thrive",
    "Hard": "true bounce, balanced contest",
    "Flat": "batting paradise, run-fest",
    "Green": "seamers dominate, low scoring",
    "Bouncy": "extra carry, pace & bounce",
}


def _pitch_keyboard(draft_id):
    """Pitch-selection keyboard: host picks a surface, guest may Deny the match.

    Two surfaces per row, with a guest-only Deny Match button on its own row at
    the bottom. challenge.py validates the clicker for each button.
    """
    rows, row = [], []
    for idx, pitch in enumerate(PITCH_TYPES):
        row.append(InlineKeyboardButton(
            pitch, callback_data=f"cl_pitch_{draft_id}_{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        "❌ Deny Match", callback_data=f"cl_denymatch_{draft_id}")])
    return InlineKeyboardMarkup(rows)


def _pitch_prompt(draft):
    host = draft.get("host") or {}
    mention = _mention(host.get("tg_id"), host.get("name") or "Host")
    lines = [
        f"🏆 <b>{_league_battle_title(draft.get('league_name'))}</b>",
        "═════════════════════════════",
        f"🟢 {draft.get('host_team')}  🆚  {draft.get('target_team')}",
        "",
        "🌱 <b>Choose the pitch</b>",
    ]
    for pitch in PITCH_TYPES:
        desc = _PITCH_DESC.get(pitch)
        lines.append(f"• <b>{pitch}</b>" + (f" — {desc}" if desc else ""))
    lines.append("")
    lines.append(f"{mention}, pick the surface you want to play on.")
    return "\n".join(lines)


async def _send_challenge_xi_prompt(context, draft, message_obj):
    """Send the 'challenge created' recap + Playing XI selection keyboard."""
    draft_id = draft.get("draft_id")
    session = get_session()
    try:
        created_message = _challenge_created_text(draft, session)
    finally:
        session.close()
    if message_obj is None:
        return
    try:
        sent = await message_obj.reply_text(
            created_message,
            parse_mode="HTML",
            reply_markup=_challenge_xi_keyboard(draft_id, draft),
        )
        _track_setup_msg(draft, sent)
    except Exception:
        logger.exception("Failed to send challenge created Playing XI message")


async def challenge_pitch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cl_pitch_{draft_id}_{idx} — host selects the pitch, then XI selection opens."""
    query = update.callback_query
    try:
        _, _, draft_id, pitch_idx = query.data.split("_")
        draft_id = int(draft_id)
        pitch_idx = int(pitch_idx)
    except Exception:
        await query.answer("Invalid pitch selection.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    if pitch_idx < 0 or pitch_idx >= len(PITCH_TYPES):
        await query.answer("Invalid pitch selection.", show_alert=True)
        return

    # Only the host chooses the pitch. The guest (and anyone else) is told this
    # is the host's call — the guest's button on this prompt is Deny Match.
    host_tg_id = (draft.get("host") or {}).get("tg_id") or draft.get("host_tg_id")
    if query.from_user.id != host_tg_id:
        await query.answer("Only the host can select the pitch.", show_alert=True)
        return
    if draft.get("pitch_type"):
        await query.answer("Pitch already chosen.", show_alert=True)
        return

    pitch = PITCH_TYPES[pitch_idx]
    draft["pitch_type"] = pitch
    await query.answer(f"Pitch: {pitch}")

    desc = _PITCH_DESC.get(pitch)
    confirm = (
        f"🏆 <b>{_league_battle_title(draft.get('league_name'))}</b>\n"
        "═════════════════════════════\n"
        f"🟢 {draft.get('host_team')}  🆚  {draft.get('target_team')}\n"
        f"🌱 <b>Pitch:</b> {pitch}" + (f" — {desc}" if desc else "")
    )
    try:
        await query.edit_message_text(confirm, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_caption(caption=confirm, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to update pitch confirmation message")

    await _send_challenge_xi_prompt(context, draft, getattr(query, "message", None))
    # Both players now pick their Playing XI — arm the clock on both sides.
    await _arm_selection_timer(
        context, draft,
        [draft.get("host_tg_id"), draft.get("target_tg_id")], "xi")


async def challenge_deny_match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cl_denymatch_{draft_id} — only the guest may deny the match.

    This button sits on the pitch-selection prompt next to the host's pitch
    buttons. The guest pressing it tears down the challenge; the host pressing it
    is told to use their own command; anyone else is ignored.
    """
    query = update.callback_query
    try:
        _, _, draft_id = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return

    host_tg_id = (draft.get("host") or {}).get("tg_id") or draft.get("host_tg_id")
    target_tg_id = (draft.get("target") or {}).get("tg_id") or draft.get("target_tg_id")

    # Only the guest denies; the host has their own command, others aren't part
    # of this challenge.
    if query.from_user.id == host_tg_id:
        await query.answer(
            "This button is not for you. Please use your own command.",
            show_alert=True)
        return
    if query.from_user.id != target_tg_id:
        await query.answer("Only the guest can deny this match.", show_alert=True)
        return

    await _disarm_selection_timer(context, draft)
    _release_draft_chat_lock(context.bot_data, draft)
    context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    await query.answer("Match denied.")
    target = draft.get("target") or {}
    message = (
        "❌ <b>Match denied</b> by "
        f"{_mention(target.get('tg_id'), target.get('name') or 'Guest')}."
    )
    try:
        await query.edit_message_text(message, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_caption(caption=message, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to update denied pitch selection message")


def _challenge_xi_keyboard(draft_id, draft):
    host_label = _team_button_label("Select", draft.get("host_team"))
    target_label = _team_button_label("Select", draft.get("target_team"))
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(host_label, callback_data=f"cl_xi_{draft_id}_host"),
        InlineKeyboardButton(target_label, callback_data=f"cl_xi_{draft_id}_target"),
    ]])


def _team_button_label(prefix, team_name):
    code = _team_short_code(team_name)
    return f"{prefix} {code} XI" if code else f"{prefix} XI"


def _team_short_code(team_name, league_key=None, session=None):
    team_name = (team_name or "").strip()
    if not team_name:
        return ""
    if team_name in IPL_TEAM_META:
        return IPL_TEAM_META[team_name][0]
    if session is not None:
        try:
            query = session.query(ChallengeTeam).filter(ChallengeTeam.name == team_name)
            if league_key:
                league = _get_challenge_league_record(session, league_key)
                if league is not None:
                    query = query.filter(ChallengeTeam.league_id == league.id)
            team = query.first()
            short = (getattr(team, "short_name", None) or "").strip() if team else ""
            if short:
                return short.upper()
        except Exception:
            logger.exception("Failed to load challenge team short code for %s", team_name)
    words = re.findall(r"[A-Za-z0-9]+", team_name)
    if len(words) > 1:
        return "".join(word[0] for word in words[:4]).upper()
    return team_name[:4].upper()


def _team_emoji(team_name):
    return IPL_TEAM_META.get((team_name or "").strip(), (None, "🏏"))[1]


def _challenge_created_text(draft, session=None):
    host = draft.get("host") or {}
    target = draft.get("target") or {}
    host_team = draft.get("host_team") or "Host XI"
    target_team = draft.get("target_team") or "Guest XI"
    host_code = _team_short_code(host_team, draft.get("league_key"), session)
    target_code = _team_short_code(target_team, draft.get("league_key"), session)
    host_team_line = f"{_team_emoji(host_team)} <b>{host_team}</b> ({host_code})" if host_code else f"{_team_emoji(host_team)} <b>{host_team}</b>"
    target_team_line = f"{_team_emoji(target_team)} <b>{target_team}</b> ({target_code})" if target_code else f"{_team_emoji(target_team)} <b>{target_team}</b>"
    return (
        f"🏏 <b>{draft.get('league_name') or 'IPL'} Challenge Created!</b>\n"
        f"👑 <b>Host:</b> {_mention(host.get('tg_id'), host.get('name') or 'User 1')}\n"
        f"⚔️ <b>Guest:</b> {_mention(target.get('tg_id'), target.get('name') or 'User 2')}\n"
        f"{host_team_line}\n"
        "vs\n"
        f"{target_team_line}\n"
        "🔥 The battle is ready!\n\n"
        "Now both players must select their Playing XI."
    )


def _challenge_team_players(session, draft, side):
    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    if not team_name:
        return []
    try:
        query = session.query(ChallengeTeam).filter(ChallengeTeam.name == team_name)
        league = _get_challenge_league_record(session, draft.get("league_key"))
        if league is not None:
            query = query.filter(ChallengeTeam.league_id == league.id)
        team = query.first()
        if not team:
            return []
        return (session.query(ChallengePlayer)
                .filter(ChallengePlayer.team_id == team.id)
                .order_by(ChallengePlayer.sort_order, ChallengePlayer.name)
                .all())
    except Exception:
        logger.exception("Failed to load challenge team players for XI selection")
        return []




def _challenge_xi_selection(draft, side):
    selections = draft.setdefault("xi_selections", {})
    return selections.setdefault(side, {"player_ids": [], "confirmed": False})


def _challenge_xi_ready(draft):
    selections = draft.get("xi_selections") or {}
    return bool(
        selections.get("host", {}).get("confirmed")
        and selections.get("target", {}).get("confirmed")
    )


def _challenge_match_ready_text(draft):
    host = draft.get("host") or {}
    target = draft.get("target") or {}
    league_name = draft.get("league_name") or "IPL"
    host_team = draft.get("host_team") or "Host XI"
    target_team = draft.get("target_team") or "Guest XI"
    host_code = _team_short_code(host_team, draft.get("league_key")) or host_team
    target_code = _team_short_code(target_team, draft.get("league_key")) or target_team
    game_mode = draft.get("game_mode") or "Classic Challenge"
    pitch_profile = draft.get("pitch_profile") or draft.get("pitch_type") or "Balanced Pitch"
    if pitch_profile and not str(pitch_profile).lower().endswith("pitch"):
        pitch_profile = f"{pitch_profile} Pitch"
    return (
        "🏏 <b>MATCH READY!</b>\n\n"
        f"⚔️ <b>Challenge Mode:</b> {league_name}\n\n"
        f"{_team_emoji(host_team)} <b>Host Team:</b> {host_team}\n"
        f"👤 <b>Host:</b> {_mention(host.get('tg_id'), host.get('name') or 'Host')}\n\n"
        f"{_team_emoji(target_team)} <b>Guest Team:</b> {target_team}\n"
        f"👤 <b>Guest:</b> {_mention(target.get('tg_id'), target.get('name') or 'Guest')}\n\n"
        f"🎮 <b>Game Mode:</b> {game_mode}\n"
        f"🌱 <b>Pitch Profile:</b> {pitch_profile}\n\n"
        f"🔥 {host_code} vs {target_code} is ready to begin!\n"
        f"🟢 {_mention(host.get('tg_id'), host.get('name') or 'Host')} Click on Start Match"
    )


def _challenge_start_match_keyboard(draft_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Start Match", callback_data=f"cl_start_{draft_id}"),
    ]])


def _challenge_player_details(player):
    raw = getattr(player, "details_json", None) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _challenge_player_category(player):
    data = _challenge_player_details(player)
    value = (data.get("category") or data.get("Category") or data.get("role") or data.get("Role") or "")
    value = str(value).strip()
    low = value.lower().replace("-", " ")
    if low in ("wk", "keeper", "wicketkeeper", "wicket keeper", "wicket keeper batter", "wicket keeper batsman"):
        return "Wicket Keeper"
    if low in ("all rounder", "allrounder", "all round", "alr", "all-rounder"):
        return "All-rounder"
    if low in ("bowler", "bowl"):
        return "Bowler"
    if low in ("batsman", "batter", "bat"):
        return "Batsman"
    return value or "Player"


def _challenge_is_wicket_keeper(player):
    category = _challenge_player_category(player).lower()
    return "wicket" in category or category == "wk"


def _challenge_is_bowling_option(player):
    category = _challenge_player_category(player).lower().replace("-", " ")
    return "bowler" in category or "all rounder" in category or "allrounder" in category


def _challenge_xi_validation(players):
    if len(players) != 11:
        return False, "Select exactly 11 players."
    if not any(_challenge_is_wicket_keeper(player) for player in players):
        return False, "Wicket Keeper is Must"
    bowling_options = sum(1 for player in players if _challenge_is_bowling_option(player))
    if bowling_options < 5:
        return False, "At least 5 Bowling Option Must (Bowlers + Allrounders)"
    return True, ""


def _challenge_rule_checkbox(passed):
    return "☑️" if passed else "☐"


def _challenge_xi_text(draft, side, team_name, players, selected_ids):
    owner = draft.get(side) or {}
    selected_set = {int(pid) for pid in selected_ids}
    selected_players = [player for player in players if int(getattr(player, "id")) in selected_set]
    selected_players.sort(key=lambda player: selected_ids.index(int(getattr(player, "id"))))
    keeper_count = sum(1 for player in selected_players if _challenge_is_wicket_keeper(player))
    bowling_options = sum(1 for player in selected_players if _challenge_is_bowling_option(player))
    lines = [
        f"🏏 <b>{team_name} Playing XI Selection</b>",
        f"{_mention(owner.get('tg_id'), owner.get('name') or 'Player')}, select exactly 11 players.",
        "Tap a checked player again to remove them from your XI.",
        "",
        f"<b>Selected:</b> {len(selected_ids)}/11",
        "",
        "<b>Rules:</b>",
        f"{_challenge_rule_checkbox(keeper_count >= 1)} 1 Wicket Keeper ({keeper_count}/1)",
        f"{_challenge_rule_checkbox(bowling_options >= 5)} At least 5 Bowling Options ({bowling_options}/5)",
        "• Selection order becomes batting order",
    ]
    if selected_players:
        lines.extend(["", "<b>Batting order:</b>"])
        lines.extend(f"{idx}. {player.name} ({_challenge_player_category(player)})" for idx, player in enumerate(selected_players, start=1))
    return "\n".join(lines)


def _challenge_xi_player_keyboard(draft_id, side, players, selected_ids):
    selected_set = {int(pid) for pid in selected_ids}
    rows = []
    row = []
    for player in players:
        player_id = int(getattr(player, "id"))
        prefix = "✅ " if player_id in selected_set else ""
        row.append(InlineKeyboardButton(
            f"{prefix}{player.name}",
            callback_data=f"cl_pick_{draft_id}_{side}_{player_id}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if len(selected_ids) == 11:
        rows.append([InlineKeyboardButton("Confirm XI", callback_data=f"cl_confirm_{draft_id}_{side}")])
    return InlineKeyboardMarkup(rows)


def _same_team_challenge_enabled(session=None, league_key=None):
    try:
        if session is not None and league_key:
            league = _get_challenge_league_record(session, league_key)
            if league is not None and hasattr(league, "same_team_allowed"):
                return bool(league.same_team_allowed)
        from services.config_service import get_allow_same_team_challenge
        return bool(get_allow_same_team_challenge(session))
    except Exception:
        logger.exception("Failed to read same-team challenge config")
        return False


def _same_team_allowed_for_draft(draft):
    session = get_session()
    try:
        return _same_team_challenge_enabled(session, draft.get("league_key"))
    finally:
        session.close()


def _team_picker_prompt(draft, player_key):
    league_name = draft.get("league_name")
    player = draft.get(player_key) or {}
    mention = _mention(player.get("tg_id"), player.get("name") or "Player")
    lines = [
        f"🏆 <b>{_league_battle_title(league_name)}</b>",
        "═════════════════════════════",
    ]
    lines.extend(_team_selection_status(draft))
    if len(lines) > 2:
        lines.append("")
    lines.append(f"{mention}, please select your {league_name} team.")
    return "\n".join(lines)


def _local_static_path(image_url):
    if not image_url or not image_url.startswith("/static/"):
        return None
    candidate = os.path.abspath(os.path.join(os.getcwd(), image_url.lstrip("/")))
    static_root = os.path.abspath(os.path.join(os.getcwd(), "static"))
    if candidate.startswith(static_root + os.sep) and os.path.exists(candidate):
        return candidate
    return None


async def _send_league_team_picker(update, context, *, challenger, target, league_key, league_name, league_record, teams, session=None):
    # One game per chat / one match per player (any game mode). Block early so a
    # Challenge League draft can't start on top of a live match in this chat or
    # while either player is already busy elsewhere.
    cid = update.effective_chat.id
    # One league setup per chat: block a second challenge while another player's
    # team/player selection is still under way in this group.
    if _active_draft_in_chat(context.bot_data, cid) or _waiting_cm_lobby_in_chat(context.bot_data, cid):
        await update.message.reply_text(
            "⚠️ A Challenge League team selection is already in progress in this chat. "
            "Finish, cancel, or deny it before starting another.",
            parse_mode="HTML")
        return
    if session is not None:
        chat_busy = _active_match_in_chat(session, cid) or _active_cric_match_in_chat(session, cid)
        if chat_busy:
            await update.message.reply_text(_chat_busy_message(chat_busy), parse_mode="HTML")
            return
        host_busy = _active_match_for_user(session, challenger.id)
        if host_busy:
            await update.message.reply_text(_user_busy_message(host_busy), parse_mode="HTML",
                                            disable_web_page_preview=True)
            return
        guest_busy = _active_match_for_user(session, target.id)
        if guest_busy:
            await update.message.reply_text(
                f"⚠️ {_user_label(target)} is already in an active match "
                f"(#{guest_busy.id}). They must finish it first.",
                parse_mode="HTML", disable_web_page_preview=True)
            return

    draft_id = random.randint(100000, 999999)
    while context.bot_data.get(_challenge_team_draft_key(draft_id)):
        draft_id = random.randint(100000, 999999)
    # Resolve short codes once (a session is in scope here) so button labels and
    # later re-renders in the callback stay consistent without re-querying.
    team_codes = {t: (_team_short_code(t, league_key, session) or t) for t in teams}
    context.bot_data[_challenge_team_draft_key(draft_id)] = {
        "draft_id": draft_id,
        "chat_id": update.effective_chat.id,
        "host_user_id": challenger.id,
        "host_tg_id": challenger.telegram_id,
        "target_user_id": target.id,
        "target_tg_id": target.telegram_id,
        "league_key": league_key,
        "league_name": league_name,
        "teams": teams,
        "team_codes": team_codes,
        "turn": "host",
        "host": {
            "user_id": challenger.id,
            "tg_id": challenger.telegram_id,
            "name": _user_label(challenger),
        },
        "target": {
            "user_id": target.id,
            "tg_id": target.telegram_id,
            "name": _user_label(target),
        },
        "created_at": datetime.utcnow().isoformat(),
    }
    # Lock this chat to the new draft so a concurrent league challenge is refused.
    context.bot_data[_challenge_draft_chat_key(update.effective_chat.id)] = draft_id
    draft = context.bot_data[_challenge_team_draft_key(draft_id)]
    caption = _team_picker_prompt(draft, "host")
    markup = _team_keyboard(draft_id, teams, team_codes=team_codes)
    image_url = _league_image_url(league_record)
    local_path = _local_static_path(image_url)
    sent = None
    try:
        if image_url:
            try:
                if local_path:
                    with open(local_path, "rb") as photo:
                        sent = await update.message.reply_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=markup)
                else:
                    sent = await update.message.reply_photo(photo=image_url, caption=caption, parse_mode="HTML", reply_markup=markup)
            except Exception:
                logger.exception("Failed to send league image for %s; falling back to text", league_key)
                sent = None
        if sent is None:
            sent = await update.message.reply_text(caption, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # The draft + chat lock were installed before this send; if we couldn't
        # post the picker at all, release them so the chat isn't locked with no
        # buttons and no expiry timer (there's no participant to cancel it).
        logger.exception("Failed to send league team picker; releasing draft lock")
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
        return
    _track_setup_msg(draft, sent)

    # Free the chat lock if this draft is abandoned mid-selection (app crash,
    # network loss) and never started — mirrors the /cm lobby expiry.
    try:
        if context.job_queue:
            context.job_queue.run_once(
                _expire_challenge_draft, CHALLENGE_DRAFT_EXPIRE,
                name=f"cl_draft_{draft_id}",
                data={"draft_id": draft_id, "chat_id": cid,
                      "message_id": sent.message_id},
            )
    except Exception:
        logger.exception("Failed to schedule challenge draft expiry")

    # Start the per-turn selection clock on the host (they pick their team first).
    await _arm_selection_timer(context, draft, [draft.get("host_tg_id")], "team")


async def _start_challenge_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 target: User, league_key=None, league_name=None):
    """Create a targeted challenge lobby once host/guest validation has passed."""
    cid = update.effective_chat.id
    session = get_session()
    try:
        challenger = sync_telegram_user(session, update.effective_user)
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return
        target = session.merge(target)
        if target.id == challenger.id:
            await update.message.reply_text("❌ You cannot challenge yourself.")
            return

        valid, errors, count = _validate_user_xi(session, challenger.id)
        if not valid:
            await update.message.reply_text(_xi_error(errors if errors else count), parse_mode="HTML")
            return

        existing = _active_match_in_chat(session, cid) or _active_cric_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(_chat_busy_message(existing), parse_mode="HTML")
            return
        if (_active_cric_match_for_user(session, challenger.id)
                or _cric_lobby_for_user(context.bot_data, challenger.id)
                or _cm_user_lobby(context.bot_data, challenger.id)):
            await update.message.reply_text("⚠️ You already have an active match or lobby!")
            return
        if context.bot_data.get(_cm_chat_key(cid)) or _active_draft_in_chat(context.bot_data, cid):
            await update.message.reply_text("⚠️ There is already a challenge waiting in this chat!")
            return

        league_key = normalize_challenge_league(league_key) if league_key else None
        league_name = (league_name or _league_display_from_key(league_key) if league_key else "Challenge Mode")
        lobby_title = f"{league_name} CHALLENGE MODE" if league_key else "CHALLENGE MODE LOBBY"
        lobby_id = random.randint(100000, 999999)
        while context.bot_data.get(_cm_lobby_key(lobby_id)):
            lobby_id = random.randint(100000, 999999)
        context.bot_data[_cm_lobby_key(lobby_id)] = {
            "lobby_id": lobby_id,
            "chat_id": cid,
            "original_lobby_chat_id": cid,
            "challenger_user_id": challenger.id,
            "challenger_tg_id": challenger.telegram_id,
            "target_user_id": target.id,
            "target_tg_id": target.telegram_id,
            "league_key": league_key,
            "league_name": league_name,
            "overs": min(_max_overs(session), 2),
            "created_at": datetime.utcnow().isoformat(),
        }
        context.bot_data[_cm_chat_key(cid)] = lobby_id
        msg = await update.message.reply_text(
            f"⚔️ <b>{lobby_title}</b>\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> {_user_label(challenger)}\n"
            f"• <b>Guest:</b> {_user_label(target)}\n"
            f"• <b>Rules:</b> 2 wickets per innings · up to {min(_max_overs(session), 2)} over(s)\n"
            "• <b>Flow:</b> fast /wpm-style Mini App gameplay with live spectating\n\n"
            "The guest accepts, toss winner chooses, then everyone opens the same live board.\n"
            f"⏳ <i>Expires in {CM_LOBBY_EXPIRE} seconds if unanswered.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"cm_accept_{lobby_id}_{target.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"cm_deny_{lobby_id}_{target.id}"),
            ], [
                InlineKeyboardButton("❌ Cancel Lobby", callback_data=f"cm_cancel_{lobby_id}_{challenger.id}"),
            ]]),
        )
        context.bot_data[_cm_lobby_key(lobby_id)]["lobby_msg_id"] = msg.message_id
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_cm_lobby, CM_LOBBY_EXPIRE,
                    name=f"cm_lobby_{lobby_id}",
                    data={"lobby_id": lobby_id, "chat_id": cid, "message_id": msg.message_id},
                )
        except Exception:
            logger.exception("Failed to schedule challenge lobby expiry")
    finally:
        session.close()


async def challenge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a targeted /cm lobby, preserving legacy mention/reply targeting."""
    session = get_session()
    try:
        challenger = sync_telegram_user(session, update.effective_user)
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return
        target, target_source = resolve_command_target(session, update, context, "cm")
        if not target:
            if target_source == "missing":
                await update.message.reply_text(
                    "Usage: <code>/cm @username</code>\n"
                    "Tip: for users without @username, reply to their message and run /cm.",
                    parse_mode="HTML")
            elif target_source == "not_mention":
                await update.message.reply_text(
                    "❌ Please reply to the user's message or use a real @username mention.",
                    parse_mode="HTML")
            else:
                await update.message.reply_text(
                    "❌ User not found. They need to use /debut first; if they changed or "
                    "don't have a username, reply to their message and run /cm.")
            return
        await _start_challenge_lobby(update, context, target)
    finally:
        session.close()


async def challenge_league_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start built-in or admin-created league challenge commands from replies."""
    command_name = _challenge_command_name(update)
    session = get_session()
    try:
        league_key, league_name = is_challenge_league_command(command_name, session)
        if not league_key:
            return

        target_tg = _reply_target_telegram_user(update)
        if not target_tg:
            await update.message.reply_text(CHALLENGE_REPLY_REQUIRED_MESSAGE)
            return
        if getattr(target_tg, "is_bot", False):
            await update.message.reply_text("❌ Bot accounts cannot be challenged.")
            return
        if update.effective_user and target_tg.id == update.effective_user.id:
            await update.message.reply_text("❌ You cannot challenge yourself.")
            return

        target = sync_telegram_user(session, target_tg)
        if not target:
            await update.message.reply_text("❌ User not found. They need to use /debut first.")
            return

        challenger = sync_telegram_user(session, update.effective_user)
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return

        league_record = _get_challenge_league_record(session, league_key)
        teams = _league_teams(session, league_key, league_record)
        if not teams:
            await update.message.reply_text(f"❌ No teams configured for {league_name} yet.")
            return
        await _send_league_team_picker(
            update, context, challenger=challenger, target=target,
            league_key=league_key, league_name=league_name,
            league_record=league_record, teams=teams, session=session,
        )
    finally:
        session.close()


async def challenge_team_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, _, draft_id, team_idx = query.data.split("_")
        draft_id = int(draft_id)
        team_idx = int(team_idx)
    except Exception:
        await query.answer("Invalid team selection.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft:
        await query.answer("This team selection is no longer active.", show_alert=True)
        return
    teams = draft.get("teams") or []
    if team_idx < 0 or team_idx >= len(teams):
        await query.answer("Invalid team selection.", show_alert=True)
        return

    turn = draft.get("turn") or "host"
    if turn == "complete":
        await query.answer("Team selection is already complete.", show_alert=True)
        return

    player_key = "host" if turn == "host" else "target"
    expected_tg_id = (draft.get(player_key) or {}).get("tg_id")
    if expected_tg_id is None:
        expected_tg_id = draft.get("host_tg_id") if turn == "host" else draft.get("target_tg_id")
    if query.from_user.id != expected_tg_id:
        await query.answer("This button is not for you. Please use your own command.", show_alert=True)
        return

    selected_team = teams[team_idx]
    same_team_allowed = _same_team_allowed_for_draft(draft)
    if turn == "target" and selected_team == draft.get("host_team") and not same_team_allowed:
        await query.answer("This team is already selected. Please choose another team.", show_alert=True)
        return

    if turn == "host":
        draft["host_team"] = selected_team
        draft["turn"] = "target"
        await query.answer(f"Selected {selected_team}")
        message = _team_picker_prompt(draft, "target")
    else:
        draft["target_team"] = selected_team
        draft["turn"] = "complete"
        await query.answer(f"Selected {selected_team}")
        lines = [
            f"🏆 <b>{_league_battle_title(draft.get('league_name'))}</b>",
            "═════════════════════════════",
        ]
        lines.extend(_team_selection_status(draft))
        message = "\n".join(lines)

    try:
        await query.edit_message_caption(
            caption=message,
            parse_mode="HTML",
            reply_markup=_team_keyboard(
                draft_id, teams, [] if same_team_allowed else [draft.get("host_team")],
                team_codes=draft.get("team_codes"),
            ),
        )
    except Exception:
        try:
            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=_team_keyboard(
                    draft_id, teams, [] if same_team_allowed else [draft.get("host_team")],
                    team_codes=draft.get("team_codes"),
                ),
            )
        except Exception:
            logger.exception("Failed to update league team picker message")

    if draft.get("turn") == "complete" and not draft.get("challenge_created_sent"):
        draft["challenge_created_sent"] = True
        message_obj = getattr(query, "message", None)
        # The team-picker message lives on as `query.message`; track it so the
        # match-start sweep removes it from the chat.
        if message_obj is not None:
            _track_setup_msg(draft, message_obj)
            # New step: the host now picks the pitch before Playing XI selection.
            try:
                sent = await message_obj.reply_text(
                    _pitch_prompt(draft),
                    parse_mode="HTML",
                    reply_markup=_pitch_keyboard(draft_id),
                )
                _track_setup_msg(draft, sent)
            except Exception:
                logger.exception("Failed to send pitch selection message")

    # Reset the selection clock for whoever the draft now waits on.
    if draft.get("turn") == "target":
        await _arm_selection_timer(context, draft, [draft.get("target_tg_id")], "team")
    elif draft.get("turn") == "complete":
        # The guest's team is set; the host now picks the pitch.
        await _arm_selection_timer(context, draft, [draft.get("host_tg_id")], "pitch")


async def challenge_team_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cl_cancel_{draft_id} — either participant aborts team selection."""
    query = update.callback_query
    try:
        _, _, draft_id = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft:
        await query.answer("This team selection is no longer active.", show_alert=True)
        return
    if draft.get("turn") == "complete":
        await query.answer("Team selection is already complete.", show_alert=True)
        return

    participants = {draft.get("host_tg_id"), draft.get("target_tg_id")}
    if query.from_user.id not in participants:
        await query.answer("Only the players in this challenge can cancel.", show_alert=True)
        return

    await _disarm_selection_timer(context, draft)
    _release_draft_chat_lock(context.bot_data, draft)
    context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    await query.answer("Cancelled.")
    message = "❌ <b>Team selection cancelled.</b>"
    try:
        await query.edit_message_caption(caption=message, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_text(message, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to update cancelled team picker message")


async def _expire_cm_lobby(ctx):
    lobby_id = ctx.job.data["lobby_id"]
    lobby = _pop_lobby(ctx, lobby_id)
    if not lobby or lobby.get("accepted"):
        return
    try:
        await ctx.bot.edit_message_text(
            "⏰ <b>Challenge expired</b> — no response.\nStart again with /cm @username.",
            chat_id=ctx.job.data["chat_id"], message_id=ctx.job.data["message_id"],
            parse_mode="HTML")
    except Exception:
        logger.exception("/cm lobby expiry message failed")


async def _expire_challenge_draft(ctx):
    """Free an abandoned league draft so its per-chat lock doesn't stick.

    Keys off ``match_launched`` (set only once the live Match row exists), so a
    draft abandoned at any pre-match step — team pick, pitch, XI, or even the
    toss — is cleaned up, while a launched match (which owns the chat through
    the active-match checks) is left untouched.
    """
    data = ctx.job.data
    draft_id = data["draft_id"]
    draft = ctx.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("match_launched"):
        return
    _cancel_selection_jobs(ctx, draft_id)
    _release_draft_chat_lock(ctx.bot_data, draft)
    ctx.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    text = "⏰ <b>Team selection expired</b> — start again when you're ready."
    try:
        await ctx.bot.edit_message_caption(
            chat_id=data["chat_id"], message_id=data["message_id"],
            caption=text, parse_mode="HTML")
    except Exception:
        try:
            await ctx.bot.edit_message_text(
                text, chat_id=data["chat_id"], message_id=data["message_id"],
                parse_mode="HTML")
        except Exception:
            logger.debug("challenge draft expiry message edit failed", exc_info=True)


# ── Per-turn selection timeout (team / pitch / Playing XI) ──────────────────
# While a draft waits on specific players, arm a 30s reminder + forfeit timer.
# Each valid action resets the clock; if a player never acts they forfeit, and
# the opponent is compensated.

def _selection_phase_label(phase):
    return {"team": "team", "pitch": "pitch", "xi": "Playing XI"}.get(phase, "selection")


def _mention_for_tg(draft, tg_id):
    for side in ("host", "target"):
        info = draft.get(side) or {}
        if info.get("tg_id") == tg_id:
            return _mention(tg_id, info.get("name") or "Player")
    return _mention(tg_id, "Player")


def _cancel_selection_jobs(context, draft_id):
    try:
        jq = getattr(context, "job_queue", None)
        if jq:
            for name in (f"cl_remind_{draft_id}", f"cl_forfeit_{draft_id}"):
                for job in jq.get_jobs_by_name(name):
                    job.schedule_removal()
    except Exception:
        logger.debug("cancel selection jobs failed", exc_info=True)


async def _clear_selection_reminder(context, draft):
    """Delete the last reminder ping for this draft, if any."""
    msg_id = draft.pop("sel_remind_msg_id", None)
    chat_id = draft.get("chat_id")
    if msg_id and chat_id is not None:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


async def _arm_selection_timer(context, draft, awaiting, phase):
    """(Re)start the reminder + forfeit timers for the players in ``awaiting``."""
    draft_id = draft.get("draft_id")
    _cancel_selection_jobs(context, draft_id)
    await _clear_selection_reminder(context, draft)
    awaiting = [tg for tg in (awaiting or []) if tg]
    draft["sel_awaiting"] = awaiting
    draft["sel_phase"] = phase
    jq = getattr(context, "job_queue", None)
    if not awaiting or not jq:
        return
    data = {"draft_id": draft_id}
    try:
        jq.run_once(_selection_reminder, CL_SELECT_REMIND, name=f"cl_remind_{draft_id}", data=data)
        jq.run_once(_selection_forfeit, CL_SELECT_WINDOW, name=f"cl_forfeit_{draft_id}", data=data)
    except Exception:
        logger.exception("Failed to schedule selection timers")


async def _touch_selection_timer(context, draft):
    """Reset the inactivity clock after a valid action (same awaiting players)."""
    awaiting = draft.get("sel_awaiting")
    phase = draft.get("sel_phase")
    if awaiting and phase:
        await _arm_selection_timer(context, draft, awaiting, phase)


async def _disarm_selection_timer(context, draft):
    _cancel_selection_jobs(context, draft.get("draft_id"))
    await _clear_selection_reminder(context, draft)
    draft["sel_awaiting"] = []


async def _selection_reminder(ctx):
    """30s mark: re-mention the awaited player(s) (delete the previous ping)."""
    draft_id = ctx.job.data["draft_id"]
    draft = ctx.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("match_launched") or draft.get("match_started"):
        return
    awaiting = draft.get("sel_awaiting") or []
    chat_id = draft.get("chat_id")
    if not awaiting or chat_id is None:
        return
    prev = draft.pop("sel_remind_msg_id", None)
    if prev:
        try:
            await ctx.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    mentions = ", ".join(_mention_for_tg(draft, tg) for tg in awaiting)
    secs = max(0, CL_SELECT_WINDOW - CL_SELECT_REMIND)
    text = (f"⏳ {mentions}, you have <b>{secs} seconds</b> to pick your "
            f"{_selection_phase_label(draft.get('sel_phase'))} — or the match is "
            f"forfeited (−{CL_FORFEIT_COINS:,} 🪙 −{CL_FORFEIT_GEMS} 💎).")
    try:
        sent = await ctx.bot.send_message(chat_id, text, parse_mode="HTML")
        draft["sel_remind_msg_id"] = sent.message_id
    except Exception:
        logger.exception("Failed to send selection reminder")


async def _selection_forfeit(ctx):
    """Window elapsed: forfeit the match, fining the idle player(s)."""
    draft_id = ctx.job.data["draft_id"]
    draft = ctx.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("match_launched") or draft.get("match_started"):
        return
    idle = list(draft.get("sel_awaiting") or [])
    if not idle:
        return
    _cancel_selection_jobs(ctx, draft_id)
    chat_id = draft.get("chat_id")
    prev = draft.pop("sel_remind_msg_id", None)
    if prev and chat_id is not None:
        try:
            await ctx.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    # Tear the draft down first so nothing else can act on it.
    _release_draft_chat_lock(ctx.bot_data, draft)
    ctx.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    label = _selection_phase_label(draft.get("sel_phase"))
    summary = _apply_selection_forfeit(draft, idle)
    if chat_id is None:
        return
    try:
        await ctx.bot.send_message(
            chat_id,
            f"⌛ <b>Match forfeited</b> — no {label} selection in time.\n{summary}",
            parse_mode="HTML")
    except Exception:
        logger.exception("Failed to announce selection forfeit")


def _apply_selection_forfeit(draft, idle_tgs):
    """Fine the idle player(s) and compensate the active opponent. Returns text."""
    from services.activity_service import log_activity
    idle_set = set(idle_tgs)
    participants = []
    for side in ("host", "target"):
        info = draft.get(side) or {}
        tg = info.get("tg_id")
        if tg:
            participants.append((tg, info.get("user_id"), info.get("name") or "Player"))
    fined, compensated = [], []
    session = get_session()
    try:
        for tg, uid, name in participants:
            user = session.query(User).get(uid) if uid else None
            if user is None:
                user = session.query(User).filter(User.telegram_id == tg).first()
            if user is None:
                continue
            if tg in idle_set:
                user.total_coins = max(0, (user.total_coins or 0) - CL_FORFEIT_COINS)
                user.total_gems = max(0, (user.total_gems or 0) - CL_FORFEIT_GEMS)
                log_activity(session, user.id, "challenge_forfeit",
                             f"No selection in time: -{CL_FORFEIT_COINS} coins, -{CL_FORFEIT_GEMS} gems",
                             coins_change=-CL_FORFEIT_COINS, gems_change=-CL_FORFEIT_GEMS)
                fined.append((tg, name))
            else:
                user.total_coins = (user.total_coins or 0) + CL_FORFEIT_COINS
                user.total_gems = (user.total_gems or 0) + CL_FORFEIT_GEMS
                log_activity(session, user.id, "challenge_forfeit_compensation",
                             f"Opponent failed to select: +{CL_FORFEIT_COINS} coins, +{CL_FORFEIT_GEMS} gems",
                             coins_change=CL_FORFEIT_COINS, gems_change=CL_FORFEIT_GEMS)
                compensated.append((tg, name))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Challenge forfeit economy failed")
    finally:
        session.close()
    lines = []
    for tg, name in fined:
        lines.append(f"⚠️ {_mention(tg, name)} fined −{CL_FORFEIT_COINS:,} 🪙 −{CL_FORFEIT_GEMS} 💎")
    for tg, name in compensated:
        lines.append(f"🎁 {_mention(tg, name)} compensated +{CL_FORFEIT_COINS:,} 🪙 +{CL_FORFEIT_GEMS} 💎")
    return "\n".join(lines)


async def challenge_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, lobby_id, invited_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id) or user.id != lobby.get("target_user_id"):
            await query.answer("Only the invited player can accept.", show_alert=True)
            return
        if lobby.get("accepted"):
            await query.answer("Challenge already accepted — toss winner must choose.", show_alert=True)
            return
        valid, errors, count = _validate_user_xi(session, user.id)
        if not valid:
            await query.answer(_xi_error(errors if errors else count), show_alert=True)
            return
        challenger = session.query(User).get(lobby["challenger_user_id"])
        if not challenger:
            _pop_lobby(context, lobby_id)
            await query.answer("The challenger no longer exists.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, user.id)
                or _active_cric_match_for_user(session, challenger.id)
                or _cric_lobby_for_user(context.bot_data, user.id)
                or _cric_lobby_for_user(context.bot_data, challenger.id)
                or (_cm_user_lobby(context.bot_data, user.id) not in (None, lobby))):
            await query.answer("A challenge player already has an active match or lobby!", show_alert=True)
            return
        lobby["accepted"] = True
        # The invited player (acceptor) calls the toss.
        lobby["caller_user_id"] = user.id
        lobby["caller_tg_id"] = user.telegram_id
        _cancel_cm_timer(context, lobby_id)
        await query.answer("Challenge accepted!")
        from services.match_broadcast import coin_call_keyboard
        await query.edit_message_text(
            "🪙 <b>CHALLENGE TOSS</b>\n"
            "═════════════════════════════\n"
            f"{_mention(user)}, call it in the air!\n"
            "<b>Heads</b> or <b>Tails?</b>",
            parse_mode="HTML", reply_markup=coin_call_keyboard(
                f"cm_coin_heads_{lobby_id}_{user.id}",
                f"cm_coin_tails_{lobby_id}_{user.id}"))
    finally:
        session.close()


async def challenge_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow the /cm challenger or a chat admin to cancel a waiting lobby."""
    query = update.callback_query
    _, _, lobby_id, _challenger_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    if lobby.get("accepted"):
        await query.answer("This challenge has already reached the toss.", show_alert=True)
        return
    is_admin = False
    try:
        member = await context.bot.get_chat_member(lobby["chat_id"], query.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        pass
    if query.from_user.id != lobby.get("challenger_tg_id") and not is_admin:
        await query.answer("Only the challenger or a chat admin can cancel this lobby.", show_alert=True)
        return
    _pop_lobby(context, lobby_id)
    _cancel_cm_timer(context, lobby_id)
    await query.answer("Challenge cancelled.")
    await query.edit_message_text("❌ /cm challenge lobby has been cancelled.")


async def challenge_deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, lobby_id, invited_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id) or user.id != lobby.get("target_user_id"):
            await query.answer("Only the invited player can deny.", show_alert=True)
            return
        _pop_lobby(context, lobby_id)
        _cancel_cm_timer(context, lobby_id)
        await query.answer()
        await query.edit_message_text("❌ Challenge denied.")
    finally:
        session.close()


async def challenge_coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cm_coin_(heads|tails)_<lobby_id>_<caller_uid> — invited player calls the
    toss; the coin is flipped and the winner then chooses bat or bowl."""
    query = update.callback_query
    _, _, call, lobby_id, caller_id = query.data.split("_")
    lobby_id = int(lobby_id)
    caller_id = int(caller_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby or not lobby.get("accepted"):
        await query.answer("This toss is no longer active.", show_alert=True)
        return
    if call not in ("heads", "tails"):
        await query.answer("Invalid call.", show_alert=True)
        return
    if lobby.get("toss_winner_id"):
        await query.answer("Toss already done — pick bat or bowl.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != caller_id or user.id != lobby.get("caller_user_id"):
            await query.answer("Only the calling player can toss!", show_alert=True)
            return
        await query.answer()
        from services.match_broadcast import run_coin_toss
        coin, won = await run_coin_toss(
            lambda t: query.edit_message_text(t, parse_mode="HTML"), call)

        challenger = session.query(User).get(lobby["challenger_user_id"])
        target = session.query(User).get(lobby["target_user_id"])
        if not challenger or not target:
            _pop_lobby(context, lobby_id)
            await query.edit_message_text("Challenge players no longer exist.")
            return
        # The target called; they win if the coin matches their call.
        winner = target if won else challenger
        lobby["toss_winner_id"] = winner.id
        lobby["toss_winner_tg_id"] = winner.telegram_id
        await query.edit_message_text(
            "🪙 <b>CHALLENGE TOSS</b>\n"
            "═════════════════════════════\n"
            f"The coin lands on <b>{coin.upper()}</b> — "
            f"{_mention(target)} called <b>{call.upper()}</b>.\n\n"
            f"🏆 {_mention(winner)} won the toss. Choose your decision:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"cm_toss_bat_{lobby_id}_{winner.id}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"cm_toss_bowl_{lobby_id}_{winner.id}"),
            ]]))
    except Exception:
        logger.exception("/cm coin toss failed")
        await query.answer("Toss failed — start again with /cm.", show_alert=True)
    finally:
        session.close()


async def challenge_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, decision, lobby_id, winner_id = query.data.split("_")
    lobby_id = int(lobby_id)
    winner_id = int(winner_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby or not lobby.get("accepted"):
        await query.answer("This toss is no longer active.", show_alert=True)
        return
    if decision not in ("bat", "bowl"):
        await query.answer("Invalid toss decision.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != winner_id or winner_id != lobby.get("toss_winner_id"):
            await query.answer("Toss winner only.", show_alert=True)
            return
        if _active_cric_match_in_chat(session, lobby["chat_id"]):
            _pop_lobby(context, lobby_id)
            await query.answer("A match is already active in this chat.", show_alert=True)
            return

        challenger = session.query(User).get(lobby["challenger_user_id"])
        target = session.query(User).get(lobby["target_user_id"])
        if not challenger or not target:
            _pop_lobby(context, lobby_id)
            await query.answer("Challenge players no longer exist.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, challenger.id)
                or _active_cric_match_for_user(session, target.id)):
            _pop_lobby(context, lobby_id)
            await query.answer("A challenge player is already in another active match.", show_alert=True)
            return
        opponent_id = target.id if winner_id == challenger.id else challenger.id
        settings = random_match_settings()
        match = Match(
            user1_id=challenger.id, user2_id=target.id, status="toss",
            overs=lobby["overs"], toss_winner_id=winner_id,
            toss_decision=decision,
            batting_first_id=winner_id if decision == "bat" else opponent_id,
            bowling_first_id=opponent_id if decision == "bat" else winner_id,
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=lobby["chat_id"], created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(match)
        session.commit()

        from services.match_webapp_service import init_match_for_webapp
        ok, message = init_match_for_webapp(session, match.id, challenge_rules=True)
        if not ok:
            session.delete(match)
            session.commit()
            await query.answer(f"Failed to launch challenge: {message}", show_alert=True)
            return

        _pop_lobby(context, lobby_id)
        await query.answer()
        await query.edit_message_text(
            f"✅ {_user_label(user)} elected to {'BAT' if decision == 'bat' else 'BOWL'} FIRST.\n"
            "Opening the Challenge Mode Mini App…")
        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bat_team = bat_user.team_name or f"{('@' + bat_user.username) if bat_user.username else (bat_user.first_name or 'Player')}'s XI"
        bowl_team = bowl_user.team_name or f"{('@' + bowl_user.username) if bowl_user.username else (bowl_user.first_name or 'Player')}'s XI"
        toss_note = (f"{_user_label(user)} won & chose to "
                     f"{'bat' if decision == 'bat' else 'bowl'}")
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, lobby["chat_id"], match, bat_team, bowl_team,
            _mention(bat_user), _mention(bowl_user),
            rules_note="Challenge Mode · 2 wickets per innings",
            toss_note=toss_note)
    except Exception:
        session.rollback()
        logger.exception("/cm toss decision failed")
        await query.answer("Failed to launch challenge match.", show_alert=True)
    finally:
        session.close()


async def challenge_xi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the selected team's full player list as XI selection buttons."""
    query = update.callback_query
    try:
        _, _, draft_id, side = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid Playing XI button.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid Playing XI button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return

    expected_tg_id = (draft.get(side) or {}).get("tg_id")
    if query.from_user.id != expected_tg_id:
        await query.answer("This XI selection is not for you.", show_alert=True)
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    if not players:
        await query.answer(f"No players are configured for {team_name} yet.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    selected_ids = selection.setdefault("player_ids", [])
    draft.setdefault("xi_started", {})[side] = True
    await _touch_selection_timer(context, draft)
    await query.answer(f"Select your {team_name} Playing XI.")
    try:
        await query.message.reply_text(
            _challenge_xi_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, selected_ids),
        )
    except Exception:
        logger.exception("Failed to send challenge XI player selection buttons")


async def challenge_xi_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle one player in the owner's XI, preserving click order as batting order."""
    query = update.callback_query
    try:
        _, _, draft_id, side, player_id = query.data.split("_")
        draft_id = int(draft_id)
        player_id = int(player_id)
    except Exception:
        await query.answer("Invalid player selection.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid player selection.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return
    expected_tg_id = (draft.get(side) or {}).get("tg_id")
    if query.from_user.id != expected_tg_id:
        await query.answer("This XI selection is not for you.", show_alert=True)
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    player_map = {int(getattr(player, "id")): player for player in players}
    player = player_map.get(player_id)
    if not player:
        await query.answer("This player is not available for your team.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    selected_ids = selection.setdefault("player_ids", [])
    if selection.get("confirmed"):
        await query.answer("Your Playing XI is already confirmed.", show_alert=True)
        return
    if player_id in selected_ids:
        selected_ids.remove(player_id)
        await query.answer(f"Removed: {len(selected_ids)}/11")
    else:
        if len(selected_ids) >= 11:
            await query.answer("You have already selected 11 players. Tap a checked player to remove one.", show_alert=True)
            return

        proposed_ids = selected_ids + [player_id]
        proposed_players = [player_map[pid] for pid in proposed_ids if pid in player_map]
        if len(proposed_ids) == 11:
            valid, error = _challenge_xi_validation(proposed_players)
            if not valid:
                await query.answer(error, show_alert=True)
                return

        selected_ids.append(player_id)
        await query.answer(f"Selected: {len(selected_ids)}/11")
    # Active picking resets the inactivity clock.
    await _touch_selection_timer(context, draft)
    try:
        await query.edit_message_text(
            _challenge_xi_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, selected_ids),
        )
    except Exception:
        logger.exception("Failed to update challenge XI selection message")


async def challenge_xi_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm a valid 11-player XI for the selected challenge team owner."""
    query = update.callback_query
    try:
        _, _, draft_id, side = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid XI confirmation.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid XI confirmation.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return
    expected_tg_id = (draft.get(side) or {}).get("tg_id")
    if query.from_user.id != expected_tg_id:
        await query.answer("This XI selection is not for you.", show_alert=True)
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    player_map = {int(getattr(player, "id")): player for player in players}
    selection = _challenge_xi_selection(draft, side)
    selected_ids = selection.setdefault("player_ids", [])
    if selection.get("confirmed"):
        await query.answer("Your Playing XI is already confirmed.", show_alert=True)
        return

    selected_players = [player_map[pid] for pid in selected_ids if pid in player_map]
    valid, error = _challenge_xi_validation(selected_players)
    if not valid:
        await query.answer(error, show_alert=True)
        return

    selection["confirmed"] = True
    # This side is done; keep waiting on the other side (or stop if both are in).
    if _challenge_xi_ready(draft):
        await _disarm_selection_timer(context, draft)
    else:
        other_tg = (draft.get("target_tg_id") if side == "host"
                    else draft.get("host_tg_id"))
        await _arm_selection_timer(context, draft, [other_tg], "xi")
    await query.answer("Playing XI confirmed!")
    batting_order = "\n".join(f"{idx}. {player.name}" for idx, player in enumerate(selected_players, start=1))
    try:
        await query.edit_message_text(
            f"✅ <b>{team_name} Playing XI Confirmed</b>\n"
            f"<b>Selected:</b> 11/11\n\n"
            f"<b>Batting order:</b>\n{batting_order}",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to confirm challenge XI selection message")

    if _challenge_xi_ready(draft) and not draft.get("match_ready_sent"):
        draft["match_ready_sent"] = True
        message_obj = getattr(query, "message", None)
        if message_obj is not None:
            try:
                await message_obj.reply_text(
                    _challenge_match_ready_text(draft),
                    parse_mode="HTML",
                    reply_markup=_challenge_start_match_keyboard(draft_id),
                )
            except Exception:
                logger.exception("Failed to send challenge match-ready message")


async def challenge_start_match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow only the host to start a challenge league match after both XIs are confirmed."""
    query = update.callback_query
    try:
        _, _, draft_id = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid match start button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This match is no longer active.", show_alert=True)
        return
    host_tg_id = (draft.get("host") or {}).get("tg_id") or draft.get("host_tg_id")
    if query.from_user.id != host_tg_id:
        await query.answer("Only the Host can start this match.", show_alert=True)
        return
    if not _challenge_xi_ready(draft):
        await query.answer("Both players must confirm their Playing XI first.", show_alert=True)
        return
    if draft.get("match_started"):
        await query.answer("Match already started.", show_alert=True)
        return

    draft["match_started"] = True
    await _disarm_selection_timer(context, draft)
    # Keep the per-chat lock held through the toss: the Match row (which the
    # active-match checks key on) is only created later in cipl_toss_callback,
    # so releasing here would briefly let a second /cipl open in this chat. The
    # lock is released in cipl_toss_callback once that Match row exists.
    await query.answer("Match started!")
    # Toss happens exactly like the current system: the guest calls heads/tails,
    # the winner elects bat/bowl, then the over-by-over match begins in chat.
    target = draft.get("target") or {}
    try:
        await query.edit_message_text(
            f"🪙 <b>TOSS</b>\n"
            f"{_mention(target.get('tg_id'), target.get('name') or 'Guest')}, "
            f"call the coin:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Heads", callback_data=f"cipl_coin_heads_{draft_id}"),
                InlineKeyboardButton("Tails", callback_data=f"cipl_coin_tails_{draft_id}"),
            ]]),
        )
    except Exception:
        logger.exception("Failed to start challenge toss")


# Legacy callback kept for safety if old inline buttons are still delivered.
async def challenge_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "This /cm challenge now opens in the Mini App after the toss. Start a fresh /cm if needed.",
        show_alert=True)
