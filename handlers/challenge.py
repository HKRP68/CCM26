"""Two-player challenge mode using the Mini App match flow."""

import logging
import random
import os
import re
from datetime import datetime, timedelta
from html import escape as _esc

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import ChallengeLeague, ChallengePlayer, ChallengeTeam, FantasyLeague, Match, User
from services import xi_rules
from services.match_constants import MATCH_EXPIRE, PITCH_TYPES, random_match_settings
from services.telegram_user_service import resolve_command_target, sync_telegram_user
from services.xi_memory_service import load_last_xi, save_last_xi
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
# pick, pitch, or Playing XI), they get a 5-minute window to choose; 30s before
# it elapses they get a mention reminder, and if they still haven't acted by the
# full window the challenge is simply cancelled (no penalty — fines only apply
# once a live match is underway). Each valid action resets the clock
# (inactivity-based), so an active setup is never killed.
CL_SELECT_WINDOW = int(os.getenv("CL_SELECT_WINDOW_SECONDS", "300"))
CL_SELECT_REMIND = int(os.getenv("CL_SELECT_REMIND_SECONDS", "270"))

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


def _tournament_command_map(session):
    """Return ``{command: ChallengeLeague}`` for every league's tournament command."""
    out = {}
    try:
        for league in (session.query(ChallengeLeague)
                       .filter(ChallengeLeague.is_active == True)  # noqa: E712
                       .all()):
            cmd = (league.tournament_command or "").strip().lower().lstrip("/").split("@", 1)[0]
            if cmd:
                out[cmd] = league
    except Exception:
        logger.exception("Failed to load tournament command map")
    return out


def is_tournament_command(command_name, session):
    """Return the ``ChallengeLeague`` whose tournament command matches, or None."""
    command = (command_name or "").lower().lstrip("/").split("@", 1)[0]
    if not command:
        return None
    return _tournament_command_map(session).get(command)


def _active_tournament_command(session, tour):
    """Best-effort tournament command string for an active tournament."""
    if tour.league_id:
        lg = session.query(ChallengeLeague).get(tour.league_id)
        if lg and lg.tournament_command:
            return lg.tournament_command
    return tour.command_snapshot or ""


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
    """Drop the per-chat draft lock once a draft ends or becomes a live match.

    Only releases the lock if it still points at *this* draft — a stale Deny
    Match button on an old draft must not pop a newer draft's chat lock.
    """
    if not draft:
        return
    chat_id = draft.get("chat_id")
    if chat_id is None:
        return
    if bot_data.get(_challenge_draft_chat_key(chat_id)) == draft.get("draft_id"):
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


def _league_has_players(session, league_id):
    """True if ``league_id`` has at least one player on an active team.

    Checks for ``ChallengePlayer`` rows (not just empty ``ChallengeTeam`` shells) so a
    duplicate league that has team names but no roster is not preferred over the league
    that actually holds the players. Lets DB errors propagate so the caller can tell a
    query failure apart from a genuinely empty league rather than guessing.
    """
    query = (session.query(ChallengePlayer.id)
             .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
             .filter(ChallengeTeam.league_id == league_id))
    if hasattr(ChallengeTeam, "is_active"):
        query = query.filter(ChallengeTeam.is_active)
    return query.first() is not None


def _get_challenge_league_record(session, league_key):
    if not league_key:
        return None
    try:
        # Order by id so the scan is deterministic. When two active leagues share a
        # key (a duplicate/leftover sharing a short_code or command), prefer the one
        # that actually has a populated roster — otherwise resolution could land on an
        # empty duplicate, the team picker would silently fall back to built-in names
        # that don't exist in that league, and the XI step would surface a spurious
        # "No players are configured for <team> yet." alert.
        candidates = []
        for league in (session.query(ChallengeLeague)
                       .filter(ChallengeLeague.is_active)
                       .order_by(ChallengeLeague.id)
                       .all()):
            if normalize_challenge_league(league.short_code or league.name) == league_key:
                candidates.append(league)
                continue
            command = (league.command or "").strip().lower().lstrip("/").split("@", 1)[0]
            if command and is_challenge_league_command(command, session)[0] == league_key:
                candidates.append(league)
        if not candidates:
            return None
        if len(candidates) == 1:
            # The overwhelmingly common case: a single matching league. Return it
            # directly so a transient roster-check error can never second-guess it.
            return candidates[0]
        # Multiple active leagues share this key. Prefer one with an actual roster so
        # the picker + XI step don't land on an empty shell. A roster check that errors
        # is treated as "unknown" (preferred over a *known*-empty league) so a transient
        # DB blip doesn't cause us to pin the empty duplicate.
        known_empty = None
        indeterminate = None
        for league in candidates:
            try:
                has_players = _league_has_players(session, league.id)
            except Exception:
                logger.exception("Roster check failed for league %s during resolution", league.id)
                if indeterminate is None:
                    indeterminate = league
                continue
            if has_players:
                return league
            if known_empty is None:
                known_empty = league
        return indeterminate or known_empty or candidates[0]
    except Exception:
        logger.exception("Failed to load challenge league record")
    return None


def _resolve_draft_league(session, draft):
    """Resolve the ChallengeLeague for a draft.

    Prefer the draft's pinned ``league_id`` (set for CL Tour matches) so the
    league is found by id even if it has been deactivated mid-tour; fall back to
    the active-only key lookup for normal /cipl drafts that carry no league_id.
    """
    league_id = (draft or {}).get("league_id")
    if league_id:
        try:
            league = session.get(ChallengeLeague, int(league_id))
            if league is not None:
                return league
        except Exception:
            logger.exception("Failed to load draft league by id %s", league_id)
    return _get_challenge_league_record(session, (draft or {}).get("league_key"))


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
    "Even": "neutral, balanced T20 track",
}


def _pitch_keyboard(draft_id, allow_deny=True):
    """Pitch-selection keyboard: host picks a surface, guest may Deny the match.

    Two surfaces per row, with a guest-only Deny Match button on its own row at
    the bottom. challenge.py validates the clicker for each button. Pass
    ``allow_deny=False`` for CL-tour matches: the series is already agreed, so
    there is nothing to deny and the deny handler wouldn't free the tour slot.
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
    if allow_deny:
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
    if draft.get("vs_bot"):
        # Show the bot's locked-in XI here — it has no "Select XI" button of its
        # own, so this is the only place the user gets to see what they're up
        # against before the toss.
        created_message += "\n" + _bot_xi_recap(draft)
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

    # Generate the dynamic conditions + Pitch Report for this surface. The same
    # conditions dict is threaded into the live match (handlers/cipl_play.py) so
    # the weather actually nudges ball outcomes — not just flavour text.
    try:
        from services.pitch_report import build_pitch_report
        report_text, conditions = build_pitch_report(pitch)
        draft["conditions"] = conditions
    except Exception:
        logger.exception("Failed to build pitch report")
        report_text = None

    if report_text:
        confirm = (
            f"🏆 <b>{_league_battle_title(draft.get('league_name'))}</b>\n"
            "═════════════════════════════\n"
            f"🟢 {draft.get('host_team')}  🆚  {draft.get('target_team')}\n\n"
            f"{report_text}"
        )
    else:
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

    # Vs the bot: lock in its Playing XI now so the only thing left is the
    # human's own XI selection, which runs exactly as it does in /cipl.
    if draft.get("vs_bot") and not _autoconfirm_bot_xi(draft):
        await _disarm_selection_timer(context, draft)
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
        try:
            await query.message.reply_text(
                f"❌ The bot's team ({draft.get('target_team')}) doesn't have a "
                f"squad it can field an XI from. Try /ciplbot again.")
        except Exception:
            logger.exception("ciplbot: failed to report a missing bot squad")
        return

    await _send_challenge_xi_prompt(context, draft, getattr(query, "message", None))
    # Both players now pick their Playing XI — arm the clock on both sides (only
    # the human side exists in a bot match).
    waiting = [draft.get("host_tg_id")]
    if not draft.get("vs_bot"):
        waiting.append(draft.get("target_tg_id"))
    await _arm_selection_timer(context, draft, waiting, "xi")


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
    if draft.get("vs_bot"):
        # The bot's XI is already locked in — only the human picks one.
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(host_label, callback_data=f"cl_xi_{draft_id}_host"),
        ]])
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
    lines = [
        f"🏏 <b>{(draft.get('league_name') or 'IPL').upper()} — CHALLENGE</b>",
    ]
    # CL Tour matches show their series position right under the title.
    if draft.get("cl_tour_id"):
        lines.append(
            f"🏆 <i>CL Tour · Match {draft.get('cl_tour_match_no')}"
            f"/{draft.get('cl_tour_match_count')}</i>")
    lines.extend([
        "═════════════════════════════",
        f"👑 <b>Host:</b>  {_mention(host.get('tg_id'), host.get('name') or 'User 1')}",
        f"    {host_team_line}",
        "         ⚔️  <b>VS</b>  ⚔️",
        f"⚔️ <b>Guest:</b> {_mention(target.get('tg_id'), target.get('name') or 'User 2')}",
        f"    {target_team_line}",
        "═════════════════════════════",
        "🔥 <b>The battle is set!</b>",
        "",
        "🎽 Both captains — tap your team below to pick your <b>Playing XI</b>.",
    ])
    return "\n".join(lines)


def _resolve_team_id(session, draft, side):
    """Return the ChallengeTeam.id for ``side``'s team in this draft, or None.

    League-scoped (filters by the draft's league), so the same team name in two
    leagues maps to two distinct ids — the key used to remember a user's last XI
    per team.
    """
    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    if not team_name:
        return None
    try:
        league = _resolve_draft_league(session, draft)
        if league is None:
            # No resolvable league means we can't pin the team to one league;
            # disable XI memory rather than risk a same-named team in another
            # league and break per-team isolation.
            return None
        team = (session.query(ChallengeTeam)
                .filter(ChallengeTeam.name == team_name,
                        ChallengeTeam.league_id == league.id)
                .first())
        return team.id if team else None
    except Exception:
        logger.exception("Failed to resolve challenge team id for XI memory")
        return None


def _persist_last_xi(draft, side, tg_id, player_ids):
    """Remember ``player_ids`` as ``tg_id``'s last confirmed XI for this team.

    Best-effort — resolving the team or saving must never interrupt match flow.
    Shared by the confirm path and post-confirm /change so a swap or reorder made
    after confirming still updates the saved XI.
    """
    try:
        session = get_session()
        try:
            team_id = _resolve_team_id(session, draft, side)
        finally:
            session.close()
        if team_id:
            save_last_xi(tg_id, team_id, player_ids)
    except Exception:
        logger.exception("Failed to persist last XI")


def _valid_saved_subset(saved_ids, players):
    """Saved player ids that still exist in the team's current roster, in saved
    (batting) order. Drops any player removed since the XI was saved, so a stale
    saved XI degrades gracefully instead of being discarded."""
    if not saved_ids:
        return []
    current = {int(getattr(p, "id")) for p in players}
    return [int(pid) for pid in saved_ids if int(pid) in current]


def _query_team_players(session, draft, side):
    """Load a team's players for ``side``. Returns a (possibly empty) list when the
    team genuinely has no roster, but lets DB errors propagate so the caller can tell
    a real "no players" apart from a transient load failure (see below)."""
    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    if not team_name:
        return []
    query = session.query(ChallengeTeam).filter(ChallengeTeam.name == team_name)
    league = _resolve_draft_league(session, draft)
    if league is not None:
        query = query.filter(ChallengeTeam.league_id == league.id)
    team = query.first()
    if not team:
        return []
    return (session.query(ChallengePlayer)
            .filter(ChallengePlayer.team_id == team.id)
            .order_by(ChallengePlayer.sort_order, ChallengePlayer.name)
            .all())


def _challenge_team_players(session, draft, side):
    """Backward-compatible loader that swallows errors into an empty list.

    Kept for the post-open callbacks (toggle/confirm/quick-select) that run only
    after the squad has already loaded once. The squad-open path uses
    ``_load_team_players_with_retry`` instead so a transient DB blip there is not
    mistaken for a genuinely empty team and shown as "No players are configured".
    """
    try:
        return _query_team_players(session, draft, side)
    except Exception:
        logger.exception("Failed to load challenge team players for XI selection")
        return []


class _TeamPlayersLoadError(Exception):
    """Raised when a team's players could not be loaded due to a transient DB error
    (connection recycled/killed, pool timeout, etc.) rather than the team being empty."""


def _load_team_players_with_retry(draft, side, attempts=2):
    """Load a team's players using a fresh session per attempt, retrying once on a
    transient DB error before giving up.

    Returns ``(players, team_id, league_cfg)`` where ``league_cfg`` is ``None`` or a
    dict of the league's overseas/format settings read while the session is open (so
    no detached-instance access happens after it closes). Raises
    ``_TeamPlayersLoadError`` only when every attempt failed with an error — never for
    a genuinely empty roster (which comes back as an empty ``players`` list). This lets
    the squad-open path show a "tap again" hint on a transient blip instead of a
    misleading "no players" alert.
    """
    last_exc = None
    for _ in range(max(1, attempts)):
        session = get_session()
        try:
            players = _query_team_players(session, draft, side)
            team_id = _resolve_team_id(session, draft, side)
            league = _resolve_draft_league(session, draft)
            league_cfg = None
            if league is not None:
                # Read scalars now, before the session closes — defaults apply only to
                # missing/NULL values so an explicit 0 ("no overseas") is preserved.
                min_raw = getattr(league, "min_overseas", None)
                max_raw = getattr(league, "max_overseas", None)
                league_cfg = {
                    "overseas_min": int(min_raw) if min_raw is not None else 0,
                    "overseas_max": int(max_raw) if max_raw is not None else 11,
                    "ball_format": getattr(league, "match_format", "T20") or "T20",
                }
            return players, team_id, league_cfg
        except Exception as exc:  # transient DB failure — retry with a fresh session
            last_exc = exc
            logger.exception("Failed to load challenge XI squad; will retry if attempts remain")
        finally:
            session.close()
    raise _TeamPlayersLoadError() from last_exc




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


# The Challenge League XI rulebook lives in services/xi_rules.py so the bot's XI
# builder (and its tests) can apply the same rules without importing this whole
# Telegram handler module. These aliases keep every existing call site unchanged.
_challenge_player_details = xi_rules.challenge_player_details
_challenge_player_category = xi_rules.challenge_player_category
_challenge_player_rating = xi_rules.challenge_player_rating


def _challenge_side_for_user(draft, tg_id):
    """Return 'host'/'target' for this Telegram id, or None if not a participant."""
    for side in ("host", "target"):
        if (draft.get(side) or {}).get("tg_id") == tg_id:
            return side
    return None


def _store_xi_message_ref(selection, message):
    """Remember the XI message so /change can edit it in place. No-op if unknown."""
    if message is None:
        return
    try:
        selection["msg_id"] = message.message_id
        selection["msg_chat_id"] = getattr(message, "chat_id", None) or message.chat.id
    except Exception:
        pass


_challenge_is_wicket_keeper = xi_rules.challenge_is_wicket_keeper
_challenge_is_bowling_option = xi_rules.challenge_is_bowling_option
_challenge_is_overseas = xi_rules.challenge_is_overseas
_challenge_xi_validation = xi_rules.validate_challenge_xi


def _challenge_overseas_limits(draft):
    """Return ``(min_overseas, max_overseas)`` for the draft, defaulting to 0/11."""
    try:
        lo = int(draft.get("overseas_min") or 0)
    except (TypeError, ValueError):
        lo = 0
    try:
        hi = int(draft.get("overseas_max", 11))
    except (TypeError, ValueError):
        hi = 11
    return lo, hi


def _challenge_rule_checkbox(passed):
    return "☑️" if passed else "☐"


def _challenge_player_rating_suffix(player):
    """Return ` · {rating}` for display, or "" when the player has no rating."""
    rating = _challenge_player_rating(player)
    return f" · {rating}" if rating is not None else ""


TELEGRAM_MSG_LIMIT = 4096


def _join_within_limit(lines, *, limit=TELEGRAM_MSG_LIMIT, notice="… (list trimmed — use the numbers above)"):
    """Join lines into one message, trimming trailing lines to stay under Telegram's
    4096-char limit. Admin teams can be bulk-loaded with very large rosters, so an
    unbounded roster could otherwise exceed the limit and the picker would fail to
    open/update. Trailing lines (the redundant batting-order block, then the highest
    squad numbers) drop first, keeping the lower numbers and their mapping intact."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    budget = limit - len(notice) - 1
    kept = []
    total = 0
    for line in lines:
        add = len(line) + (1 if kept else 0)
        if total + add > budget:
            break
        kept.append(line)
        total += add
    kept.append(notice)
    return "\n".join(kept)


def _challenge_xi_text(draft, side, team_name, players, selected_ids):
    """Build the picker view: rule status that updates as players are picked.

    The squad itself lives on the numbered name buttons, so the text stays
    concise — header, live rule checkboxes, and the current batting order.
    """
    owner = draft.get(side) or {}
    selected_ids = [int(pid) for pid in selected_ids]
    selected_set = set(selected_ids)
    selected_players = [player for player in players if int(getattr(player, "id")) in selected_set]
    selected_players.sort(key=lambda player: selected_ids.index(int(getattr(player, "id"))))
    keeper_count = sum(1 for player in selected_players if _challenge_is_wicket_keeper(player))
    bowling_options = sum(1 for player in selected_players if _challenge_is_bowling_option(player))
    min_overseas, max_overseas = _challenge_overseas_limits(draft)
    overseas_count = sum(1 for player in selected_players if _challenge_is_overseas(player))
    lines = [
        f"🏏 <b>{team_name} Playing XI Selection</b>",
        f"{_mention(owner.get('tg_id'), owner.get('name') or 'Player')}, select exactly 11 players.",
        "Tap a player to add/remove, or reply with their numbers "
        "(e.g. <code>1 2 3 4 5 6 7 8 9 10 11</code>).",
        "",
        f"<b>Selected:</b> {len(selected_ids)}/11",
        "",
        "<b>Rules:</b>",
        f"{_challenge_rule_checkbox(keeper_count >= 1)} 1 Wicket Keeper ({keeper_count}/1)",
        f"{_challenge_rule_checkbox(bowling_options >= 5)} At least 5 Bowling Options ({bowling_options}/5)",
    ]
    # Only surface the overseas rule when the league actually configured a limit.
    if min_overseas > 0 or max_overseas < 11:
        overseas_ok = min_overseas <= overseas_count <= max_overseas
        lines.append(
            f"{_challenge_rule_checkbox(overseas_ok)} Overseas ✈️ {overseas_count} "
            f"(min {min_overseas} / max {max_overseas})"
        )
    lines.append("• Selection order becomes batting order")
    if selected_players:
        lines.extend(["", "<b>Batting order:</b>"])
        lines.extend(f"{idx}. {player.name} ({_challenge_player_category(player)})" for idx, player in enumerate(selected_players, start=1))
    return _join_within_limit(lines)


def _challenge_xi_confirmed_text(draft, side, team_name, players, selected_ids):
    """XI/bench view: 1-11 Playing XI (batting order), bench continuing from 12."""
    selected_ids = [int(pid) for pid in selected_ids]
    pid_map = {int(getattr(player, "id")): player for player in players}
    xi_players = [pid_map[pid] for pid in selected_ids if pid in pid_map]
    bench_players = [player for player in players if int(getattr(player, "id")) not in set(selected_ids)]
    lines = [f"✅ <b>{team_name} Playing XI</b>", "", "<b>🏏 Playing XI</b>"]
    for idx, player in enumerate(xi_players, start=1):
        lines.append(f"{idx}. {player.name} ({_challenge_player_category(player)}){_challenge_player_rating_suffix(player)}")
    if bench_players:
        lines.extend(["", "<b>🪑 Bench</b>"])
        for idx, player in enumerate(bench_players, start=12):
            lines.append(f"{idx}. {player.name} ({_challenge_player_category(player)}){_challenge_player_rating_suffix(player)}")
    lines.extend([
        "",
        "✏️ Swap a player with <code>/change &lt;out&gt; &lt;in&gt;</code> — e.g. <code>/change 2 13</code>",
    ])
    return _join_within_limit(lines)


def _challenge_xi_player_keyboard(draft_id, side, players, selected_ids,
                                  saved_available=False, team_code=""):
    """Numbered name buttons (2/row), e.g. "1. Dhoni". A ✅ marks picked players;
    the button text carries the roster number + name, the callback carries
    player_id so the toggle handler is unchanged. Confirm XI shows only at 11.

    When ``saved_available`` is set and nothing is picked yet, a one-tap
    "⚡ Use my last {CODE} XI" button is shown on top — it loads (and, if fully
    valid, instantly confirms) the user's last saved XI for this team."""
    selected_set = {int(pid) for pid in selected_ids}
    rows = []
    if saved_available and not selected_ids:
        label = f"⚡ Use my last {team_code} XI" if team_code else "⚡ Use my last XI"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"cl_useprev_{draft_id}_{side}")])
    row = []
    for idx, player in enumerate(players, start=1):
        player_id = int(getattr(player, "id"))
        mark = "✅ " if player_id in selected_set else ""
        row.append(InlineKeyboardButton(
            f"{mark}{idx}. {player.name}",
            callback_data=f"cl_pick_{draft_id}_{side}_{player_id}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    control = []
    if len(selected_ids) == 11:
        control.append(InlineKeyboardButton("✅ Confirm XI", callback_data=f"cl_confirm_{draft_id}_{side}"))
    if selected_ids:
        control.append(InlineKeyboardButton("🧹 Clear", callback_data=f"cl_clear_{draft_id}_{side}"))
    if control:
        rows.append(control)
    return InlineKeyboardMarkup(rows)


def _challenge_xi_postselect_keyboard(draft_id, side, confirmed):
    """Keyboard for the XI/bench view (after a full 11 is picked or confirmed)."""
    if confirmed:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Edit XI", callback_data=f"cl_edit_{draft_id}_{side}"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm XI", callback_data=f"cl_confirm_{draft_id}_{side}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"cl_edit_{draft_id}_{side}"),
    ]])


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


async def _send_league_team_picker(update, context, *, challenger, target, league_key, league_name, league_record, teams, session=None, tournament_id=None, is_tournament=False, vs_bot=False):
    # ``effective_message`` rather than ``update.message`` so this also works when
    # the picker is opened from a button (the /ciplbot Rematch), where
    # ``update.message`` is None.
    reply = update.effective_message
    if reply is None:
        logger.warning("league team picker: no message to reply to")
        return
    # One game per chat / one match per player (any game mode). Block early so a
    # Challenge League draft can't start on top of a live match in this chat or
    # while either player is already busy elsewhere.
    cid = update.effective_chat.id
    # One league setup per chat: block a second challenge while another player's
    # team/player selection is still under way in this group.
    if _active_draft_in_chat(context.bot_data, cid) or _waiting_cm_lobby_in_chat(context.bot_data, cid):
        await reply.reply_text(
            "⚠️ A Challenge League team selection is already in progress in this chat. "
            "Finish, cancel, or deny it before starting another.",
            parse_mode="HTML")
        return
    if session is not None:
        chat_busy = _active_match_in_chat(session, cid) or _active_cric_match_in_chat(session, cid)
        if chat_busy:
            await reply.reply_text(_chat_busy_message(chat_busy), parse_mode="HTML")
            return
        host_busy = _active_match_for_user(session, challenger.id)
        if host_busy:
            await reply.reply_text(_user_busy_message(host_busy), parse_mode="HTML",
                                            disable_web_page_preview=True)
            return
        # The bot opponent is one shared User row that sits in every concurrent
        # practice match, so checking it would block the second /ciplbot.
        guest_busy = None if vs_bot else _active_match_for_user(session, target.id)
        if guest_busy:
            await reply.reply_text(
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
        # Official tournament match? The tournament id is carried through the
        # whole draft → toss → play flow so the result is recorded against it.
        "is_tournament": bool(is_tournament),
        "tournament_id": tournament_id,
        # /ciplbot: the "target" is the AI opponent. It picks its own team and
        # Playing XI, calls nothing, and the match is unranked practice.
        "vs_bot": bool(vs_bot),
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
            # The AI opponent shows as "🤖 Bot" everywhere, not as the internal
            # username of the shared bot User row.
            "name": "🤖 Bot" if vs_bot else _user_label(target),
        },
        "created_at": datetime.utcnow().isoformat(),
    }
    # Lock this chat to the new draft so a concurrent league challenge is refused.
    context.bot_data[_challenge_draft_chat_key(update.effective_chat.id)] = draft_id
    draft = context.bot_data[_challenge_team_draft_key(draft_id)]
    # Pin the league by id (mirrors the CL-tour path) so roster/team resolution at
    # XI-selection time uses the exact league whose teams populated this picker.
    # Without this the XI step re-resolves the league from league_key alone, which
    # can land on a different active league sharing the same key/command — whose id
    # has no matching team — yielding a spurious "No players are configured" alert.
    if league_record is not None:
        draft["league_id"] = league_record.id
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
                        sent = await reply.reply_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=markup)
                else:
                    sent = await reply.reply_photo(photo=image_url, caption=caption, parse_mode="HTML", reply_markup=markup)
            except Exception:
                logger.exception("Failed to send league image for %s; falling back to text", league_key)
                sent = None
        if sent is None:
            sent = await reply.reply_text(caption, parse_mode="HTML", reply_markup=markup)
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


async def launch_cl_tour_match(context, *, message_obj, chat_id, host, target,
                              league_key, league_name, league_record,
                              host_team, target_team, session,
                              cl_tour_id, cl_tour_match_id, cl_tour_match_no,
                              cl_tour_match_count):
    """Open a Challenge League Tour match.

    The league and both teams are fixed at tour-creation time, so this builds a
    Challenge League draft that is already at the ``turn == "complete"`` state and
    jumps straight to the host's pitch-selection step. From there the regular
    /cipl flow (pitch → Playing XI → toss → over-by-over) runs unchanged. The
    ``cl_tour_*`` tags ride the draft → match state so the result is recorded
    back into the series (see handlers/cipl_play.py).

    Returns (ok, error_message). On a guard failure ok is False and the caller
    surfaces error_message.
    """
    # Same concurrency guards as a normal league challenge: one setup per chat,
    # one live match per chat, one live match per player.
    if _active_draft_in_chat(context.bot_data, chat_id) or _waiting_cm_lobby_in_chat(context.bot_data, chat_id):
        return False, ("⚠️ A Challenge League team selection is already in progress "
                       "in this chat. Finish, cancel, or deny it first.")
    if session is not None:
        chat_busy = _active_match_in_chat(session, chat_id) or _active_cric_match_in_chat(session, chat_id)
        if chat_busy:
            return False, _chat_busy_message(chat_busy)
        if _active_match_for_user(session, host.id):
            return False, _user_busy_message(_active_match_for_user(session, host.id))
        if _active_match_for_user(session, target.id):
            return False, (f"⚠️ {_user_label(target)} is already in an active match. "
                           "They must finish it first.")
        # Also block a live Mini-App cric match for either player (DB-backed).
        if _active_cric_match_for_user(session, host.id):
            return False, "⚠️ You already have an active match — finish it first."
        if _active_cric_match_for_user(session, target.id):
            return False, (f"⚠️ {_user_label(target)} already has an active match — "
                           "they must finish it first.")
    # Block if either player is tied up in an in-memory lobby (a /wpm cric lobby
    # or a /cm lobby) that hasn't produced a Match row yet — otherwise the second
    # flow to launch is only rejected later at toss, stranding this setup.
    if _cric_lobby_for_user(context.bot_data, host.id) or _cm_user_lobby(context.bot_data, host.id):
        return False, "⚠️ You already have a waiting match lobby — finish it first."
    if _cric_lobby_for_user(context.bot_data, target.id) or _cm_user_lobby(context.bot_data, target.id):
        return False, (f"⚠️ {_user_label(target)} already has a waiting match lobby — "
                       "they must finish it first.")

    draft_id = random.randint(100000, 999999)
    while context.bot_data.get(_challenge_team_draft_key(draft_id)):
        draft_id = random.randint(100000, 999999)

    teams = [host_team, target_team]
    team_codes = {t: (_team_short_code(t, league_key, session) or t) for t in teams}
    context.bot_data[_challenge_team_draft_key(draft_id)] = {
        "draft_id": draft_id,
        "chat_id": chat_id,
        "host_user_id": host.id,
        "host_tg_id": host.telegram_id,
        "target_user_id": target.id,
        "target_tg_id": target.telegram_id,
        "league_key": league_key,
        "league_name": league_name,
        "is_tournament": False,
        "tournament_id": None,
        "teams": teams,
        "team_codes": team_codes,
        "turn": "complete",
        "host_team": host_team,
        "target_team": target_team,
        "host": {"user_id": host.id, "tg_id": host.telegram_id, "name": _user_label(host)},
        "target": {"user_id": target.id, "tg_id": target.telegram_id, "name": _user_label(target)},
        # CL Tour wiring — threaded through to the live match state for result
        # recording, and shown in the challenge-created recap.
        "cl_tour_id": cl_tour_id,
        "cl_tour_match_id": cl_tour_match_id,
        "cl_tour_match_no": cl_tour_match_no,
        "cl_tour_match_count": cl_tour_match_count,
        "created_at": datetime.utcnow().isoformat(),
    }
    context.bot_data[_challenge_draft_chat_key(chat_id)] = draft_id
    draft = context.bot_data[_challenge_team_draft_key(draft_id)]

    # Carry the league's format + overseas limits onto the draft up front so the
    # match uses the league's real format (T20/The100) and XI rules — the normal
    # /cipl flow sets these lazily when the XI picker opens, but pinning them here
    # removes any reliance on that and the toss-time `ball_format` fallback.
    if league_record is not None:
        # Pin the league by id so roster/format/overseas resolution doesn't rely on
        # active-only league_key matching — a league deactivated mid-tour still
        # resolves correctly (see _resolve_draft_league).
        draft["league_id"] = league_record.id
        draft["ball_format"] = getattr(league_record, "match_format", "T20") or "T20"
        # Default only when the value is actually absent — an explicit 0 cap
        # ("no overseas allowed") must be preserved (matches the XI callback).
        min_raw = getattr(league_record, "min_overseas", None)
        max_raw = getattr(league_record, "max_overseas", None)
        draft["overseas_min"] = int(min_raw) if min_raw is not None else 0
        draft["overseas_max"] = int(max_raw) if max_raw is not None else 11

    # The teams are already set, so the next step is the host's pitch pick —
    # exactly the message the normal flow posts once both teams are chosen. Send
    # it to the tour chat (not wherever the Play button was tapped) so the whole
    # setup + match runs in the group the tour belongs to.
    sent = None
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=_pitch_prompt(draft), parse_mode="HTML",
            reply_markup=_pitch_keyboard(draft_id, allow_deny=False))
    except Exception:
        logger.exception("Failed to send CL tour pitch prompt; releasing draft lock")
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
        return False, "⚠️ Could not start the match. Try again."
    _track_setup_msg(draft, sent)

    try:
        if context.job_queue:
            context.job_queue.run_once(
                _expire_challenge_draft, CHALLENGE_DRAFT_EXPIRE,
                name=f"cl_draft_{draft_id}",
                data={"draft_id": draft_id, "chat_id": chat_id,
                      "message_id": sent.message_id})
    except Exception:
        logger.exception("Failed to schedule CL tour draft expiry")

    await _arm_selection_timer(context, draft, [host.telegram_id], "pitch")
    return True, None


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


def _fixture_open_for_pair(tournament_id, name1, name2):
    """True if the two team names still have an open scheduled fixture.

    Returns True (unrestricted) when the tournament has no generated schedule, so
    free-play tournaments behave exactly as before.
    """
    if not tournament_id or not name1 or not name2:
        return True
    from services import league_schedule_service
    from models import Tournament
    session = get_session()
    try:
        tour = session.query(Tournament).get(int(tournament_id))
        if not tour or not tour.schedule_generated:
            return True
        opts = league_schedule_service.remaining_opponent_names(session, tournament_id, name1)
        return name2 in opts
    finally:
        session.close()


async def _handle_tournament_command(update, context, session, league):
    """Gate + start a Challenge League Tournament match from a tournament command.

    Enforces the tournament command availability rules (spec §4): an active
    tournament must exist, the command must belong to its league, the tournament
    must not be completed/cancelled/paused, and both teams must be participants.
    """
    from services import tournament_service

    user = update.effective_user
    if not tournament_service.is_tournament_command_allowed(user.id if user else None):
        await update.message.reply_text(
            "⛔ The Challenge League Tournament Command is restricted.\n"
            "Only approved players can start tournament matches. "
            "Contact an admin to be added.")
        return

    active = tournament_service.get_active_tournament(session)
    if not active:
        await update.message.reply_text(
            "❌ No Challenge League Tournament is currently active.")
        return

    if active.league_id != league.id:
        await update.message.reply_text(
            "❌ This Tournament Command is currently unavailable.\n\n"
            f"Active Tournament: {active.name}\n"
            f"League: {active.league_name or ''}\n"
            f"Tournament Command: {_active_tournament_command(session, active)}")
        return

    if active.status == "completed":
        await update.message.reply_text("❌ This tournament has already been completed.")
        return
    if active.status == "cancelled":
        await update.message.reply_text("❌ This tournament has been cancelled.")
        return
    if active.status == "paused":
        await update.message.reply_text(
            "⏸️ This Challenge League Tournament is currently paused.")
        return
    if active.status != "active":
        await update.message.reply_text(
            "❌ No Challenge League Tournament is currently active.")
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

    league_key = normalize_challenge_league(league.short_code or league.name)
    league_name = (league.name or (league_key or "").upper()).strip()

    # Restrict the team picker to teams that are participating in the tournament.
    part_names = tournament_service.participating_team_names(session, active.id)
    teams = [t for t in _league_teams(session, league_key, league) if t in part_names]
    if len(teams) < 2:
        await update.message.reply_text(
            "❌ One or both teams are not participating in the active tournament.")
        return

    await _send_league_team_picker(
        update, context, challenger=challenger, target=target,
        league_key=league_key, league_name=league_name,
        league_record=league, teams=teams, session=session,
        tournament_id=active.id, is_tournament=True)


async def challenge_league_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start built-in or admin-created league challenge commands from replies."""
    command_name = _challenge_command_name(update)
    session = get_session()
    try:
        # A "<league command>bot" alias (e.g. /cbblbot) starts a bot match in
        # that league. This is the only place dynamic league commands can be
        # routed — the regex handler that reaches here owns its handler group,
        # so a second one would never fire.
        from handlers.ciplbot import ciplbot_handler, league_key_from_bot_command
        bot_league_key, _bot_league_name = league_key_from_bot_command(
            command_name, session)
        if bot_league_key:
            session.close()
            session = None
            await ciplbot_handler(update, context)
            return

        # Official tournament command takes precedence over the casual league command.
        tournament_league = is_tournament_command(command_name, session)
        if tournament_league is not None:
            await _handle_tournament_command(update, context, session, tournament_league)
            return

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
        # The bot-alias branch above closes and clears the session before
        # delegating, so it opens its own rather than sharing a closed one.
        if session is not None:
            session.close()


# ════════════════════════════════════════════════════════════════════
# /ciplbot — the AI opponent's automatic team and Playing XI
# ════════════════════════════════════════════════════════════════════

def _league_squad_sizes(session, draft):
    """``{team name: squad size}`` for every team in this draft's league.

    One grouped COUNT rather than a query per team: picking the bot's opponent
    needs the size of all ten-or-so squads, and doing that one at a time inside
    an awaited callback stalls the event loop for the whole round trip.
    """
    from sqlalchemy import func

    try:
        league = _resolve_draft_league(session, draft)
        query = (session.query(ChallengeTeam.name, func.count(ChallengePlayer.id))
                 .outerjoin(ChallengePlayer, ChallengePlayer.team_id == ChallengeTeam.id))
        if league is not None:
            query = query.filter(ChallengeTeam.league_id == league.id)
        return {name: count for name, count in query.group_by(ChallengeTeam.name).all()}
    except Exception:
        logger.exception("ciplbot: reading league squad sizes failed")
        return {}


def _pick_bot_league_team(draft, host_team):
    """Pick the bot's league team: any other team with a squad it can field.

    Returns None when no other team in the league has eleven players, which is
    a real configuration problem worth surfacing rather than papering over.
    """
    from services.bot_xi_builder import pick_bot_team

    session = get_session()
    try:
        sizes = _league_squad_sizes(session, draft)
        chosen = pick_bot_team(draft.get("teams") or [], (host_team,),
                               lambda name: sizes.get(name, 0))
        if chosen and sizes.get(chosen, 0) < 11:
            return None
        return chosen
    finally:
        session.close()


def _autoconfirm_bot_xi(draft):
    """Build and lock in the bot's Playing XI for this draft.

    Highest OVR first under the league's own rules (11 players, a keeper, five
    bowling options, the overseas min/max), via
    ``services.bot_xi_builder.build_challenge_bot_xi``. Returns True on success;
    on failure the caller should abandon the draft rather than start a match
    with a bot side that has no XI.
    """
    from services.bot_xi_builder import build_challenge_bot_xi

    session = get_session()
    try:
        players = _query_team_players(session, draft, "target")
        xi = build_challenge_bot_xi(players, *_challenge_overseas_limits(draft))
        if len(xi) != 11:
            logger.error("ciplbot: could not build an XI for %s (%s players)",
                         draft.get("target_team"), len(players))
            return False
        selection = _challenge_xi_selection(draft, "target")
        selection["player_ids"] = [int(p.id) for p in xi]
        selection["confirmed"] = True
        # Cache the names so the recap can show the XI without another query.
        draft["bot_xi_names"] = [str(p.name or "Player") for p in xi]
        return True
    except Exception:
        logger.exception("ciplbot: bot XI generation failed")
        return False
    finally:
        session.close()


def _bot_xi_recap(draft):
    """The bot's locked-in XI, numbered, for the Playing-XI prompt."""
    names = draft.get("bot_xi_names") or []
    if not names:
        return ""
    lines = [f"\n🤖 <b>{_esc(draft.get('target_team') or 'Bot')}</b> "
             f"(bot XI — auto-picked, best available):"]
    lines.extend(f"{i:>2}. {_esc(n)}" for i, n in enumerate(names, 1))
    return "\n".join(lines)


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

    # Schedule gating: in a tournament with a generated schedule, the two chosen
    # teams must still have an uncompleted scheduled fixture between them. This is
    # what enforces "a team can't play again once its match is done".
    if turn == "target" and draft.get("is_tournament") and draft.get("tournament_id"):
        host_team = draft.get("host_team")
        if not _fixture_open_for_pair(draft.get("tournament_id"), host_team, selected_team):
            await query.answer(
                f"No scheduled fixture left between {host_team} and {selected_team}. "
                "Pick a team they're still scheduled to play.",
                show_alert=True)
            return

    if turn == "host" and draft.get("vs_bot"):
        # Vs the bot there is no second human to wait on: the bot takes one of
        # the remaining teams straight away and the draft moves to the pitch.
        draft["host_team"] = selected_team
        bot_team = _pick_bot_league_team(draft, selected_team)
        if not bot_team:
            await query.answer(
                "No other team in this league has a full squad to play with.",
                show_alert=True)
            draft["host_team"] = None
            return
        draft["target_team"] = bot_team
        draft["turn"] = "complete"
        await query.answer(f"Selected {selected_team} — bot takes {bot_team}")
        lines = [
            f"🤖 <b>{_league_battle_title(draft.get('league_name'))} vs BOT</b>",
            "═════════════════════════════",
        ]
        lines.extend(_team_selection_status(draft))
        lines.append("")
        lines.append("🎯 <i>Unranked practice — no stats, coins, gems or Win/Loss.</i>")
        message = "\n".join(lines)
    elif turn == "host":
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
            # Vs the bot there is no guest who could deny the match.
            try:
                sent = await message_obj.reply_text(
                    _pitch_prompt(draft),
                    parse_mode="HTML",
                    reply_markup=_pitch_keyboard(
                        draft_id, allow_deny=not draft.get("vs_bot")),
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
# While a draft waits on specific players, give them a 5-minute window with a
# reminder shortly before it elapses. Each valid action resets the clock; if a
# player never acts the challenge is cancelled (no fine during setup).

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
    """Near the deadline: re-mention the awaited player(s) (delete the previous ping)."""
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
            f"{_selection_phase_label(draft.get('sel_phase'))} — or the match "
            f"will be cancelled.")
    try:
        sent = await ctx.bot.send_message(chat_id, text, parse_mode="HTML")
        draft["sel_remind_msg_id"] = sent.message_id
    except Exception:
        logger.exception("Failed to send selection reminder")


async def _delete_setup_messages(context, draft):
    """Delete the draft's tracked setup messages so their buttons disappear."""
    chat_id = draft.get("chat_id")
    if chat_id is None:
        return
    for mid in list(draft.get("setup_msg_ids") or []):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def _selection_forfeit(ctx):
    """Window elapsed during setup: just cancel the match (no fine).

    Pre-match selection (team / pitch / Playing XI) carries no penalty — if a
    player doesn't pick in time the challenge is simply cancelled and its
    buttons removed. (Fines only apply once a live match is underway.)
    """
    draft_id = ctx.job.data["draft_id"]
    draft = ctx.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("match_launched") or draft.get("match_started"):
        return
    if not (draft.get("sel_awaiting") or []):
        return
    _cancel_selection_jobs(ctx, draft_id)
    chat_id = draft.get("chat_id")
    prev = draft.pop("sel_remind_msg_id", None)
    if prev and chat_id is not None:
        try:
            await ctx.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    # Remove the picker/prompt messages so their buttons disappear, then drop
    # the draft + per-chat lock so a fresh challenge can start.
    await _delete_setup_messages(ctx, draft)
    label = _selection_phase_label(draft.get("sel_phase"))
    _release_draft_chat_lock(ctx.bot_data, draft)
    ctx.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    if chat_id is None:
        return
    try:
        await ctx.bot.send_message(
            chat_id,
            f"⌛ <b>Challenge cancelled</b> — no {label} selection in time.",
            parse_mode="HTML")
    except Exception:
        logger.exception("Failed to announce selection cancel")


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
    # Load the squad with a one-shot retry on a transient DB error. A blip here used
    # to be swallowed into an empty list and shown as "No players are configured",
    # which looked random (any team, any league) and falsely blamed the roster.
    try:
        players, team_id, league_cfg = _load_team_players_with_retry(draft, side)
    except _TeamPlayersLoadError:
        await query.answer(
            "⚠️ Couldn't load the squad just now. Please tap Select XI again.",
            show_alert=True)
        return
    if league_cfg is not None:
        # Cache the league's overseas limits + match format on the draft so the
        # picker render and confirm callbacks enforce them without re-hitting the DB.
        draft["overseas_min"] = league_cfg["overseas_min"]
        draft["overseas_max"] = league_cfg["overseas_max"]
        draft["ball_format"] = league_cfg["ball_format"]
    if not players:
        await query.answer(f"No players are configured for {team_name} yet.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    selected_ids = selection.setdefault("player_ids", [])
    # Surface the user's last saved XI for this team as a one-tap option. Only the
    # players still on the roster are offered (stale picks are dropped).
    saved_subset = _valid_saved_subset(load_last_xi(query.from_user.id, team_id), players) if team_id else []
    draft.setdefault("saved_xi", {})[side] = saved_subset
    team_code = _team_short_code(team_name, draft.get("league_key"))
    draft.setdefault("xi_started", {})[side] = True
    await _touch_selection_timer(context, draft)
    await query.answer(f"Select your {team_name} Playing XI.")
    try:
        sent = await query.message.reply_text(
            _challenge_xi_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(
                draft_id, side, players, selected_ids,
                saved_available=bool(saved_subset), team_code=team_code),
        )
        _store_xi_message_ref(selection, sent)
    except Exception:
        logger.exception("Failed to send challenge XI player selection buttons")


async def challenge_xi_useprev_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cl_useprev_{draft_id}_{side} — load this user's last saved XI for the team.

    One tap: if the saved XI is still fully valid (all 11 on the roster, rules
    pass) it is selected AND confirmed immediately. If it is partially stale, the
    still-valid players are pre-checked and the user fills the remaining gaps.
    """
    query = update.callback_query
    try:
        _, _, draft_id, side = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid button.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return
    if query.from_user.id != (draft.get(side) or {}).get("tg_id"):
        await query.answer("This XI selection is not for you.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    if selection.get("confirmed"):
        await query.answer("Your Playing XI is already confirmed.", show_alert=True)
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
        team_id = _resolve_team_id(session, draft, side)
    finally:
        session.close()

    # Prefer the subset cached when the picker opened; re-derive from the DB if
    # the draft was rebuilt in between.
    saved_subset = (draft.get("saved_xi") or {}).get(side)
    if saved_subset is None:
        saved_subset = _valid_saved_subset(load_last_xi(query.from_user.id, team_id), players) if team_id else []
    if not saved_subset:
        await query.answer("No saved XI to load for this team yet.", show_alert=True)
        return

    player_map = {int(getattr(p, "id")): p for p in players}
    selection["player_ids"] = list(saved_subset)
    await _touch_selection_timer(context, draft)

    # Fully valid saved XI → one-tap select + confirm.
    if len(saved_subset) == 11:
        selected_players = [player_map[pid] for pid in saved_subset if pid in player_map]
        valid, _error = _challenge_xi_validation(selected_players, *_challenge_overseas_limits(draft))
        if valid:
            await query.answer("Loaded & confirmed your last XI!")
            await _finalize_xi_confirm(context, query, draft, draft_id, side,
                                       team_name, players, saved_subset)
            return

    # Partially valid (or rules changed) → pre-check what's valid, let them finish.
    if len(saved_subset) == 11:
        await query.answer("Loaded your last XI — review and Confirm.")
    else:
        await query.answer(f"Loaded {len(saved_subset)}/11 from your last XI — fill the rest.")
    _store_xi_message_ref(selection, getattr(query, "message", None))
    try:
        await query.edit_message_text(
            _challenge_xi_text(draft, side, team_name, players, saved_subset),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, saved_subset),
        )
    except Exception:
        logger.exception("Failed to render loaded last-XI selection message")


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
            valid, error = _challenge_xi_validation(proposed_players, *_challenge_overseas_limits(draft))
            if not valid:
                await query.answer(error, show_alert=True)
                return

        selected_ids.append(player_id)
        await query.answer(f"Selected: {len(selected_ids)}/11")
    # Active picking resets the inactivity clock.
    await _touch_selection_timer(context, draft)
    _store_xi_message_ref(selection, getattr(query, "message", None))
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
    valid, error = _challenge_xi_validation(selected_players, *_challenge_overseas_limits(draft))
    if not valid:
        await query.answer(error, show_alert=True)
        return

    await query.answer("Playing XI confirmed!")
    await _finalize_xi_confirm(context, query, draft, draft_id, side, team_name,
                               players, selected_ids)


async def _finalize_xi_confirm(context, query, draft, draft_id, side, team_name,
                               players, selected_ids):
    """Mark a side's XI confirmed, persist it as the user's last XI for this team,
    render the confirmed view, and post the match-ready message once both sides
    are in. Shared by the Confirm button and the one-tap "Use last XI" button.

    The caller must have already validated ``selected_ids`` (exactly 11, rules
    pass) and answered the callback query.
    """
    selection = _challenge_xi_selection(draft, side)
    selection["confirmed"] = True

    # Remember this XI so next time the same team is picked it can be reused with
    # one tap. Best-effort — a save failure must not interrupt the match flow.
    _persist_last_xi(draft, side, query.from_user.id, selected_ids)

    # This side is done; keep waiting on the other side (or stop if both are in).
    if _challenge_xi_ready(draft):
        await _disarm_selection_timer(context, draft)
    else:
        other_tg = (draft.get("target_tg_id") if side == "host"
                    else draft.get("host_tg_id"))
        await _arm_selection_timer(context, draft, [other_tg], "xi")
    # Edit is still allowed until the match-ready message is posted (both sides in).
    edit_allowed = not _challenge_xi_ready(draft) and not draft.get("match_ready_sent")
    _store_xi_message_ref(selection, getattr(query, "message", None))
    try:
        await query.edit_message_text(
            _challenge_xi_confirmed_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=(_challenge_xi_postselect_keyboard(draft_id, side, confirmed=True)
                          if edit_allowed else None),
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


async def challenge_xi_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset a side's in-progress XI selection back to empty."""
    query = update.callback_query
    try:
        _, _, draft_id, side = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid clear button.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid clear button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return
    if query.from_user.id != (draft.get(side) or {}).get("tg_id"):
        await query.answer("This XI selection is not for you.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    if selection.get("confirmed"):
        await query.answer("Your XI is confirmed — tap Edit first.", show_alert=True)
        return

    selection["player_ids"] = []
    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    await _touch_selection_timer(context, draft)
    _store_xi_message_ref(selection, getattr(query, "message", None))
    await query.answer("Selection cleared.")
    try:
        await query.edit_message_text(
            _challenge_xi_text(draft, side, team_name, players, []),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, []),
        )
    except Exception:
        logger.exception("Failed to clear challenge XI selection message")


async def challenge_xi_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-open the number picker for a side (un-confirming if needed)."""
    query = update.callback_query
    try:
        _, _, draft_id, side = query.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await query.answer("Invalid edit button.", show_alert=True)
        return
    if side not in ("host", "target"):
        await query.answer("Invalid edit button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await query.answer("This Playing XI selection is no longer active.", show_alert=True)
        return
    if query.from_user.id != (draft.get(side) or {}).get("tg_id"):
        await query.answer("This XI selection is not for you.", show_alert=True)
        return
    if draft.get("match_ready_sent") or draft.get("match_started"):
        await query.answer("Both XIs are locked — the match is starting.", show_alert=True)
        return

    selection = _challenge_xi_selection(draft, side)
    selected_ids = selection.setdefault("player_ids", [])
    was_confirmed = selection.get("confirmed")
    selection["confirmed"] = False
    # Re-arm this side's timer since we're waiting on them again.
    await _arm_selection_timer(context, draft, [(draft.get(side) or {}).get("tg_id")], "xi")
    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    _store_xi_message_ref(selection, getattr(query, "message", None))
    await query.answer("Edit your XI." if was_confirmed else "Keep editing.")
    try:
        await query.edit_message_text(
            _challenge_xi_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, selected_ids),
        )
    except Exception:
        logger.exception("Failed to re-open challenge XI selection message")


async def challenge_xi_quickselect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Power-user XI selection: a participant replies with player numbers (batting order).

    Accepts a partial pick (2–11 numbers); the full keeper/bowling-options rules
    are only enforced once exactly 11 are given, and the Confirm XI button stays
    hidden until then. A lone number is intentionally ignored so a stray digit in
    the group chat can't wipe a selection — single players are picked by button.

    This runs on every plain (non-command) text message, so it bails out fast and
    silently unless the sender is mid-XI-selection for an active draft in this chat.
    Each user only ever touches their own side's selection, so simultaneous host
    and guest replies never collide (see the concurrency note on the draft state).
    """
    message = getattr(update, "effective_message", None)
    text = (getattr(message, "text", None) or "").strip()
    if not text or text.startswith("/"):
        return
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if chat is None or user is None:
        return
    draft = _active_draft_in_chat(context.bot_data, chat.id)
    if not draft or draft.get("turn") != "complete":
        return
    side = _challenge_side_for_user(draft, user.id)
    if side is None:
        return
    if not (draft.get("xi_started") or {}).get(side):
        return
    selection = _challenge_xi_selection(draft, side)
    if selection.get("confirmed"):
        return

    tokens = [tok for tok in re.split(r"[\s,]+", text) if tok]
    if len(tokens) < 2 or not all(tok.isdigit() for tok in tokens):
        # Not a quick-select attempt — leave normal chat untouched.
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    total = len(players)
    numbers = [int(tok) for tok in tokens]

    if len(numbers) > 11:
        await message.reply_text(f"❌ That's {len(numbers)} numbers — pick at most 11.")
        return
    out_of_range = [n for n in numbers if n < 1 or n > total]
    if out_of_range:
        await message.reply_text(f"❌ Out of range: {out_of_range[0]}. Use numbers 1–{total}.")
        return
    if len(set(numbers)) != len(numbers):
        await message.reply_text("❌ No duplicates — pick different players.")
        return

    selected_ids = [int(getattr(players[n - 1], "id")) for n in numbers]
    selected_players = [players[n - 1] for n in numbers]
    # A full XI must satisfy the rules; a partial pick (<11) is accepted as-is
    # and simply won't surface the Confirm XI button until it reaches 11.
    if len(numbers) == 11:
        valid, error = _challenge_xi_validation(selected_players, *_challenge_overseas_limits(draft))
        if not valid:
            await message.reply_text(f"❌ {error}")
            return

    selection["player_ids"] = selected_ids
    await _touch_selection_timer(context, draft)
    draft_id = draft.get("draft_id")
    try:
        sent = await message.reply_text(
            _challenge_xi_text(draft, side, team_name, players, selected_ids),
            parse_mode="HTML",
            reply_markup=_challenge_xi_player_keyboard(draft_id, side, players, selected_ids),
        )
        _store_xi_message_ref(selection, sent)
    except Exception:
        logger.exception("Failed to render quick-select XI message")
    # Keep the chat tidy by removing the numbers the user typed (best effort).
    try:
        await message.delete()
    except Exception:
        pass


async def challenge_change_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/change <out> <in>` — swap a player in the XI/bench frame, editing in place.

    `out` is a Playing XI slot (1-11). `in` is a bench number (12+); the bench
    player takes that batting slot and the dropped player goes to the bench. If
    both are 1-11 the two batting positions are swapped (a reorder). Usable while
    picking and any time before the match starts.
    """
    message = getattr(update, "effective_message", None)
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if message is None or chat is None or user is None:
        return
    draft = _active_draft_in_chat(context.bot_data, chat.id)
    if not draft or draft.get("turn") != "complete":
        return
    side = _challenge_side_for_user(draft, user.id)
    if side is None:
        return
    if draft.get("match_started") or draft.get("match_launched"):
        await message.reply_text("❌ The match has started — you can't change your XI now.")
        return

    selection = _challenge_xi_selection(draft, side)
    selected_ids = [int(pid) for pid in selection.get("player_ids") or []]
    if len(selected_ids) != 11:
        await message.reply_text("❌ Pick your full Playing XI first, then use /change.")
        return

    team_name = draft.get("host_team") if side == "host" else draft.get("target_team")
    session = get_session()
    try:
        players = _challenge_team_players(session, draft, side)
    finally:
        session.close()
    pid_order = [int(getattr(p, "id")) for p in players]
    pid_map = {int(getattr(p, "id")): p for p in players}

    # XI/bench numbering: 1-11 = batting order, 12.. = bench in roster order.
    bench_ids = [pid for pid in pid_order if pid not in set(selected_ids)]
    bench_number = {12 + i: pid for i, pid in enumerate(bench_ids)}

    args = context.args if getattr(context, "args", None) is not None else (message.text or "").split()[1:]
    nums = [a for a in args if str(a).lstrip("-").isdigit()]

    def _usage_text(reason):
        return (f"❌ {reason}\n"
                "Usage: <code>/change &lt;out&gt; &lt;in&gt;</code> — e.g. <code>/change 2 13</code>\n\n"
                + _challenge_xi_confirmed_text(draft, side, team_name, players, selected_ids))

    if len(nums) != 2:
        await message.reply_text(_usage_text("Give two numbers: the XI slot to drop and the player to bring in."), parse_mode="HTML")
        return
    out_no, in_no = int(nums[0]), int(nums[1])
    if not (1 <= out_no <= 11):
        await message.reply_text(_usage_text(f"The first number must be a Playing XI slot (1–11), not {out_no}."), parse_mode="HTML")
        return

    new_ids = list(selected_ids)
    if 1 <= in_no <= 11:
        # Reorder: swap two batting positions.
        new_ids[out_no - 1], new_ids[in_no - 1] = new_ids[in_no - 1], new_ids[out_no - 1]
    elif in_no in bench_number:
        new_ids[out_no - 1] = bench_number[in_no]
    else:
        await message.reply_text(_usage_text(f"The second number must be a bench player (12–{11 + len(bench_ids)}) or an XI slot (1–11), not {in_no}."), parse_mode="HTML")
        return

    new_players = [pid_map[pid] for pid in new_ids if pid in pid_map]
    valid, error = _challenge_xi_validation(new_players, *_challenge_overseas_limits(draft))
    if not valid:
        await message.reply_text(f"❌ {error}\nNo change made.")
        return

    selection["player_ids"] = new_ids
    await _touch_selection_timer(context, draft)
    draft_id = draft.get("draft_id")
    confirmed = bool(selection.get("confirmed"))

    # If the XI was already confirmed, keep the saved "last XI" in sync with this
    # post-confirm edit so the next match reuses the swapped/reordered lineup.
    if confirmed:
        _persist_last_xi(draft, side, user.id, new_ids)

    # Edit the tracked XI message in place (no new message), falling back to a reply.
    edited = False
    msg_chat_id = selection.get("msg_chat_id")
    msg_id = selection.get("msg_id")
    rendered = _challenge_xi_confirmed_text(draft, side, team_name, players, new_ids)
    markup = (_challenge_xi_postselect_keyboard(draft_id, side, confirmed=confirmed)
              if not (draft.get("match_ready_sent") or draft.get("match_started")) else None)
    if msg_chat_id is not None and msg_id is not None:
        try:
            await context.bot.edit_message_text(
                rendered, chat_id=msg_chat_id, message_id=msg_id,
                parse_mode="HTML", reply_markup=markup,
            )
            edited = True
        except Exception:
            logger.debug("change: in-place XI edit failed", exc_info=True)
    if not edited:
        try:
            sent = await message.reply_text(rendered, parse_mode="HTML", reply_markup=markup)
            _store_xi_message_ref(selection, sent)
        except Exception:
            logger.exception("Failed to render /change XI message")
    try:
        await message.delete()
    except Exception:
        pass


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
    # Vs the bot the guest IS the bot, so the host calls the coin — and if the
    # bot wins the toss it elects bat/bowl at random (see cipl_coin_callback).
    caller = (draft.get("host") if draft.get("vs_bot")
              else draft.get("target")) or {}
    try:
        await query.edit_message_text(
            f"🪙 <b>TOSS</b>\n"
            f"{_mention(caller.get('tg_id'), caller.get('name') or 'Guest')}, "
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
