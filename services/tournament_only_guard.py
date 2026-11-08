"""Telegram gameplay gate for accounts restricted to official tournaments.

Only game actions reach the database. Shared match buttons are resolved against
the server's draft or Match row; callback text never grants tournament access.
The guard runs before the independent message-handler groups, including typed
overs, XI selections and social-game answers.
"""

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from types import SimpleNamespace

from telegram.ext import ApplicationHandlerStop

logger = logging.getLogger(__name__)

START_COMMANDS = frozenset("""
playmatch pm match wpm sim simmatch testwpm pbo bowlout catch cm
cdraft challengedraft letsplay lp lpbot lpb letsplaybot ciplbot ciplb
challengeiplbot lpmulti lpm letsplaymulti superover pso vsbot vsb wpmbot wpmb
botvsbot bvb botmatch spectate cmtours createtour cltour cltours cmulti
auctionleague al rcpl alplay simtolast alsim simset alresume skipplayer altrade
unscramble u ju su lucky7 powerplay pp score21 s21 wordchase wc bluff mole
cartel fantasy fl ipl160 16o iplsim 16-0ipl wsp wspbot wspb
""".split())
CONTINUE_COMMANDS = frozenset("""
rcl resumecl resume r continue unpause playapp appmode playchat chatmode
impact ip concussion medicalsub change handover transfermatch
""".split())
TOURNAMENT_COMMANDS = frozenset(("lptour", "lptplay"))

# Deliberately specific: a broad "cancel" substring also matches unrelated
# user-controlled data and could exempt gameplay by accident.
SAFE_CALLBACK_PREFIXES = (
    "cm_deny_", "cm_cancel_", "cl_cancel_", "cl_denymatch_", "lp_deny_",
    "lpm_cancel_", "cdc_", "matchdeny_", "pbodec_", "bvb_cancel_",
    "botmatch_cancel_", "bm_decline_", "mh_cancel_", "ct_cancel_",
    "ho_decline_", "ho_cancel_", "sop_deny_", "sos_cancel_",
    "svm_del_", "svm_delok_", "svm_no_", "endmatch_", "endmatchno_",
    "wc_end_", "vsb_cancel_", "wpmb_cancel_", "l7_cancel_", "cltset_x_", "clt_dec_",
)
SAFE_CALLBACKS = frozenset(("cric_cancel_lobby", "svm_list"))
GAME_CALLBACK_PREFIXES = (
    "lp_", "lpm_", "cl_", "cipl_", "cm_", "so_", "sop_", "sos_",
    "svm_", "ho_", "cric_", "cric_traits:", "cric_coin:", "cric_decision:",
    "matchacc_", "oversset_", "overscustom_", "toss_", "op1_", "op2_",
    "selbowl_", "bvar_", "blen_", "bspin_", "bshot_", "nbowl_", "newbat_",
    "pboacc_", "bopick_", "cdj_", "cdp_", "vsb_", "wpmb_", "bvb_",
    "botmatch_", "botdiff_", "botplay_", "mtp_", "cltplay_", "al:",
    "us_ans_", "l7_", "s21_", "bm_", "mh_", "ct_",
    "ctm_", "cto_", "tac_", "clt_acc_", "cltset_",
)
MATCH_ID_FIRST_PREFIXES = (
    "matchacc_", "oversset_", "overscustom_", "op1_", "op2_", "selbowl_",
    "bvar_", "blen_", "bspin_", "bshot_", "nbowl_", "newbat_", "pboacc_",
    "bopick_",
)


@dataclass
class Action:
    record: dict | None = None
    match_id: int | None = None
    tournament: bool = False
    bowlout_id: int | None = None
    tour_id: int | None = None
    cltour_id: int | None = None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _command(update, bot_username=None):
    message = getattr(update, "effective_message", None)
    text = (getattr(message, "text", None) or "").strip()
    if not text.startswith("/"):
        return None
    token = text.split(None, 1)[0][1:].lower()
    name, _, addressed = token.partition("@")
    if addressed and bot_username and addressed != bot_username.lower():
        return None
    if name in ("start", "s"):
        payload = text.split()[1:]
        if payload and payload[0].lower() == "ipl160":
            return "ipl160"
        if payload and payload[0].startswith("cmd_"):
            from services.dm_only import parse_start_payload
            replayed, _ = parse_start_payload(payload[0])
            if replayed in START_COMMANDS | CONTINUE_COMMANDS:
                return replayed
    return name


def _registered_commands(context):
    app = getattr(context, "application", None)
    groups = getattr(app, "handlers", {}) if app else {}
    return {name for group in groups.values() for handler in group
            for name in getattr(handler, "commands", ())}


def _participants(record):
    """Internal IDs and Telegram IDs from trusted server-owned state."""
    internal, telegram = set(), set()
    if not isinstance(record, dict):
        return internal, telegram
    for key in ("user1_id", "user2_id", "host_user_id", "target_user_id",
                "challenger_user_id", "guest_user_id", "bat_team_id", "bowl_team_id",
                "host_uid", "guest_uid", "u1_id", "u2_id"):
        value = _int(record.get(key))
        if value is not None:
            internal.add(value)
    for key in ("host_tg_id", "target_tg_id", "challenger_tg_id", "guest_tg_id",
                "bat_user_tg", "bowl_user_tg", "host_id", "challenger_id"):
        value = _int(record.get(key))
        if value is not None:
            telegram.add(value)
    for key in ("host", "guest", "target"):
        value = record.get(key)
        if isinstance(value, dict):
            uid, tid = _int(value.get("user_id")), _int(value.get("tg_id"))
            if uid is not None:
                internal.add(uid)
            if tid is not None:
                telegram.add(tid)
        elif getattr(value, "id", None) is not None:
            telegram.add(value.id)
    for player in record.get("players", ()):
        value = getattr(player, "id", None)
        if value is not None:
            telegram.add(value)
    return internal, telegram


def _draft_action(draft, *, lp=False):
    draft = draft if isinstance(draft, dict) else {}
    official = (bool((draft.get("lpt") or {}).get("tournament_id")) if lp else
                bool(draft.get("is_tournament") and draft.get("tournament_id")))
    return Action(record=draft, tournament=official)


def _callback_action(data, context, chat_id):
    if data in SAFE_CALLBACKS or data.startswith(SAFE_CALLBACK_PREFIXES):
        return None
    if data == "us_join":
        return Action()
    if not data.startswith(GAME_CALLBACK_PREFIXES):
        return None
    parts = data.split("_")
    at = lambda index: parts[index] if len(parts) > index else None
    bd = context.bot_data
    if data.startswith("al:") and data.split(":")[1] in (
            "help", "hof", "card", "table", "caps", "fix", "squad", "hub",
            "st", "awards", "rtview", "pause", "quit", "quitok"):
        return None
    if data.startswith(("mtp_", "tac_")):
        return Action(tour_id=_int(at(1)))
    if data.startswith("cltplay_"):
        return Action(cltour_id=_int(at(1)))
    if data.startswith("clt_acc_"):
        return Action(cltour_id=_int(at(2)))
    if data.startswith(("ctm_", "cto_")):
        return Action(record=bd.get(f"cmt_draft_{at(1)}"))
    if data.startswith("cltset_"):
        return Action(record=bd.get(f"cltour_setup_{at(2)}"))
    if data.startswith(("cdj_", "cdp_")):
        return Action(record=bd.get(f"challenge_team_draft_{at(1)}"))
    if data.startswith("sos_"):
        return Action(record=bd.get(f"challenge_team_draft_{at(2)}"))
    if data.startswith("sop_accept_"):
        return Action(match_id=_int(at(2)))
    if data.startswith("lp_"):
        index = 3 if at(1) in ("coin", "toss") else 2
        return _draft_action(bd.get(f"lp_{at(index)}"), lp=True)
    if data.startswith("cl_"):
        return _draft_action(bd.get(f"challenge_team_draft_{at(2)}"))
    if data.startswith(("cipl_coin_", "cipl_toss_")):
        return _draft_action(bd.get(f"challenge_team_draft_{at(3)}"))
    if data.startswith("cipl_"):
        if at(1) in ("impo", "impi", "impp", "impx", "cs"):
            from services.button_access import split_owner
            prefix = f"cipl_{at(1)}_"
            _, rest = split_owner(prefix, data, separator="_")
            return Action(match_id=_int(rest.split("_", 1)[0]))
        return Action(match_id=_int(at(2)))
    if data.startswith(("so_", "svm_", "ho_", "toss_")):
        return Action(match_id=_int(at(2)))
    if data.startswith(MATCH_ID_FIRST_PREFIXES):
        if data.startswith("bopick_"):
            return Action(bowlout_id=_int(at(1)))
        if data.startswith("pboacc_"):
            return Action(record={"user1_id": _int(at(1)), "user2_id": _int(at(2))})
        return Action(match_id=_int(at(1)))
    if data.startswith("cric_"):
        return Action(record=bd.get(f"cric_lobby_{chat_id}"))
    if data.startswith("cm_"):
        index = 3 if at(1) in ("coin", "toss") else 2
        return Action(record=bd.get(f"cm_lobby_{at(index)}"))
    if data.startswith("lpm_"):
        return Action(record=bd.get(f"lp_{at(2)}"))
    if data.startswith(("mh_", "ct_", "bm_")):
        module = {"mh": "molehunt", "ct": "cartel", "bm": "bluffmaster"}[at(0)]
        loaded = sys.modules.get(f"handlers.{module}")
        lobbies = getattr(loaded, "LOBBIES", {})
        return Action(record=lobbies.get(_int(at(2)), lobbies.get(chat_id)))
    return Action()


def _text_action(update, context):
    """Classify only text that an active gameplay text handler will consume."""
    message = getattr(update, "effective_message", None)
    text = (getattr(message, "text", None) or "").strip()
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if not user or not chat:
        return None
    if getattr(message, "web_app_data", None):
        return Action()  # Fantasy team submissions arrive as WEB_APP_DATA.
    if not text or text.startswith("/"):
        return None
    bd = context.bot_data
    mid = bd.get(f"awaiting_overs_{user.id}")
    overs_text = text.lower().replace("overs", "").replace("over", "").strip()
    if mid and _int(overs_text) is not None and 1 <= int(overs_text) <= 20:
        return Action(match_id=_int(mid))
    # Quick-select only consumes a participant's numeric XI reply.
    if re.fullmatch(r"[\d,\s]+", text) and len(re.findall(r"\d+", text)) >= 2:
        did = bd.get(f"cl_draft_chat_{chat.id}")
        draft = bd.get(f"challenge_team_draft_{did}")
        side = ("host" if isinstance(draft, dict) and draft.get("host_tg_id") == user.id
                else "target" if isinstance(draft, dict) and draft.get("target_tg_id") == user.id
                else None)
        selection = (draft.get("xi_selections") or {}).get(side, {}) if isinstance(draft, dict) else {}
        if (isinstance(draft, dict) and side and draft.get("turn") == "complete"
                and draft.get("toss_decision") and (draft.get("xi_started") or {}).get(side)
                and not selection.get("confirmed")):
            return _draft_action(draft)
    wc = sys.modules.get("handlers.wordchase")
    game = getattr(wc, "GAMES", {}).get(chat.id)
    if game and game.get("guessing_enabled") and game.get("host_id") != user.id:
        return Action(record=game)
    for module, membership in (("molehunt", "players"), ("cartel", "alive")):
        loaded = sys.modules.get(f"handlers.{module}")
        lobby = getattr(loaded, "LOBBIES", {}).get(chat.id)
        if not lobby or lobby.get("state") != "CLUE_PHASE" or len(text.split()) != 1:
            continue
        members = {getattr(p, "id", p) for p in lobby.get(membership, ())}
        if user.id in members:
            return Action(record=lobby)
    bluff = sys.modules.get("handlers.bluffmaster")
    bid = getattr(bluff, "USER_CHAT", {}).get(user.id)
    lobby = getattr(bluff, "LOBBIES", {}).get(bid)
    if (lobby and lobby.get("state") == "QUIZ_PHASE"
            and lobby.get("submissions", {}).get(user.id, {}).get("type") == "answer_pending"):
        return Action(record=lobby)
    return None


def _continuation_action(session, update, context, command):
    from sqlalchemy import or_
    from models import Match, MatchState, SavedMatch
    message = update.effective_message
    args = (getattr(message, "text", None) or "").split()[1:]
    explicit_id_commands = CONTINUE_COMMANDS - {
        "impact", "ip", "concussion", "medicalsub", "change", "handover", "transfermatch"}
    if args and command in explicit_id_commands:
        value = args[0].lstrip("#").lstrip("Mm").lstrip("#")
        return Action(match_id=int(value) if value.isdigit() else None)
    chat_id = getattr(update.effective_chat, "id", None)
    tg_id = getattr(update.effective_user, "id", None)
    if command == "change":
        # The router gives Lets Play priority, then the active CL draft.
        for key, draft in context.bot_data.items():
            if (isinstance(key, str) and key.startswith("lp_")
                    and isinstance(draft, dict) and draft.get("chat_id") == chat_id
                    and draft.get("status") in ("pitch", "showxi", "toss")):
                return _draft_action(draft, lp=True)
        did = context.bot_data.get(f"cl_draft_chat_{chat_id}")
        draft = context.bot_data.get(f"challenge_team_draft_{did}")
        if isinstance(draft, dict) and tg_id in _participants(draft)[1]:
            return _draft_action(draft)
        return Action()
    if command in ("continue", "unpause"):
        rows = (session.query(SavedMatch)
                .filter(SavedMatch.status == "saved",
                        or_(SavedMatch.user1_tg == tg_id, SavedMatch.user2_tg == tg_id))
                .order_by(SavedMatch.saved_at.desc()).limit(10).all())
        here = [row for row in rows if row.chat_id == chat_id]
        selected = here if len(here) == 1 else rows if len(rows) == 1 else []
        return Action(match_id=selected[0].match_id if selected else None)
    # Mirror the live-match router: its most recent cached CIPL state wins,
    # followed by persisted CIPL states. Never let an unrelated row exempt a
    # command that will actually act on a different casual match.
    cached = [(int(key[3:]), state) for key, state in context.bot_data.items()
              if isinstance(key, str) and key.startswith("ms_") and key[3:].isdigit()
              and isinstance(state, dict) and state.get("chat_id") == chat_id
              and state.get("mode") == "cipl_approach"]
    if cached:
        mid, state = max(cached, key=lambda item: item[0])
        return Action(record=state, match_id=mid)
    rows = (session.query(Match, MatchState)
             .join(MatchState, MatchState.match_id == Match.id)
             .filter(Match.chat_id == chat_id,
                     Match.status.in_(("playing", "active", "toss", "selecting")))
             .order_by(Match.id.desc()).all())
    for match, saved_state in rows:
        state = json.loads(saved_state.state_json)
        if state.get("mode") == "cipl_approach":
            return Action(record=state, match_id=match.id)
    if command in ("resume", "r"):
        for prefix in ("so_", "ms_"):
            for key, state in context.bot_data.items():
                if (isinstance(key, str) and key.startswith(prefix)
                        and key[len(prefix):].isdigit() and isinstance(state, dict)
                        and state.get("chat_id") == chat_id):
                    return Action(record=state, match_id=int(key[len(prefix):]))
    return Action()


def _dynamic_command_action(session, command):
    # Mirror the handler's route precedence, including dynamic <league>bot.
    from handlers.ciplbot import league_key_from_bot_command
    from handlers.challenge import (is_tournament_command,
                                    is_tournament_view_command, is_fixtures_command,
                                    is_challenge_league_command,
                                    is_super_over_league_command)
    if league_key_from_bot_command(command, session)[0]:
        return Action()
    if is_tournament_command(command, session) is not None:
        return Action(tournament=True)
    if (is_tournament_view_command(command, session) is not None
            or is_fixtures_command(command, session) is not None):
        return None
    if (is_challenge_league_command(command, session)[0]
            or is_super_over_league_command(command, session)[0]):
        return Action()
    return None


def _target_filters(update):
    """Opponents named in replies, text mentions or @username arguments."""
    from sqlalchemy import func
    from models import User
    filters = []
    message = getattr(update, "effective_message", None)
    reply = getattr(message, "reply_to_message", None)
    replied_user = getattr(reply, "from_user", None)
    if replied_user and getattr(replied_user, "id", None):
        filters.append(User.telegram_id == replied_user.id)
    for entity in getattr(message, "entities", ()) or ():
        target = getattr(entity, "user", None)
        if target and getattr(target, "id", None):
            filters.append(User.telegram_id == target.id)
    args = (getattr(message, "text", None) or "").split()[1:]
    for arg in args:
        if re.fullmatch(r"@[A-Za-z0-9_]+", arg):
            filters.append(func.lower(User.username) == arg[1:].lower())
    # Numeric target IDs are accepted by several match handlers. Do not treat
    # overs (1–100) as Telegram users.
    if args and args[0].isdigit() and int(args[0]) > 100:
        filters.append(User.telegram_id == int(args[0]))
    return filters


def _check_action(session, action, update):
    from sqlalchemy import or_
    from models import Bowlout, CLTour, Match, Tour, User
    from services.tournament_only_service import (
        is_restricted, is_tournament_match, restriction_message, restriction_alert,
        OPPONENT_NOTICE)

    if action.tournament:
        return None
    internal, telegram = _participants(action.record)
    for model, identifier in ((Tour, action.tour_id), (CLTour, action.cltour_id)):
        if identifier is not None:
            tour = session.get(model, identifier)
            if tour is not None:
                internal.update((tour.user1_id, tour.user2_id))
    if action.bowlout_id is not None:
        bowlout = session.get(Bowlout, action.bowlout_id)
        if bowlout is not None:
            internal.update((bowlout.user1_id, bowlout.user2_id))
            if bowlout.match_id and is_tournament_match(session, bowlout.match_id):
                return None
    if action.match_id is not None:
        match = session.get(Match, action.match_id)
        if match is not None:
            if is_tournament_match(session, match):
                return None
            internal.update(uid for uid in (match.user1_id, match.user2_id) if uid is not None)
    tg = getattr(update.effective_user, "id", None)
    if tg is not None:
        telegram.add(tg)
    filters = _target_filters(update)
    if telegram:
        filters.append(User.telegram_id.in_(telegram))
    if internal:
        filters.append(User.id.in_(internal))
    if not filters:
        return None
    for user in session.query(User).filter(or_(*filters)).all():
        if not is_restricted(user, session=session):
            continue
        if user.telegram_id == tg:
            return (restriction_alert(user, session) if getattr(update, "callback_query", None)
                    else restriction_message(user, session=session))
        return OPPONENT_NOTICE
    return None


def _decision(update, context, command, action):
    from database import get_session
    session = get_session()
    try:
        if command in CONTINUE_COMMANDS:
            action = _continuation_action(session, update, context, command)
        elif command and action is None:
            action = _dynamic_command_action(session, command)
        return _check_action(session, action, update) if action is not None else None
    finally:
        session.close()


async def tournament_only_guard(update, context):
    """PTB TypeHandler middleware; stop a denied gameplay update in all groups."""
    if not getattr(update, "effective_user", None):
        return
    query = getattr(update, "callback_query", None)
    try:
        # Classification is part of the gate too: an invalid/stale callback
        # must not throw past it and let a later handler group play the game.
        snapshot = SimpleNamespace(bot_data=dict(getattr(context, "bot_data", {}) or {}))
        bot_username = getattr(getattr(context, "bot", None), "username", None)
        command = _command(update, bot_username)
        if query:
            action = _callback_action(str(query.data or ""), snapshot,
                                      getattr(update.effective_chat, "id", None))
            if action is None:
                return
        elif command:
            if command in TOURNAMENT_COMMANDS:
                return
            action = Action() if command in START_COMMANDS else None
            if command not in START_COMMANDS | CONTINUE_COMMANDS:
                if command in _registered_commands(context):
                    return
        else:
            action = _text_action(update, snapshot)
            if action is None:
                return
        denied = await asyncio.to_thread(_decision, update, snapshot, command, action)
    except Exception:
        logger.exception("Tournament-only gameplay access check failed")
        denied = "⚠️ Unable to verify game access right now. Please try again."
    if denied is None:
        return
    try:
        if query:
            await query.answer(denied[:200], show_alert=True)
        elif getattr(update, "effective_message", None):
            await update.effective_message.reply_text(denied, parse_mode=None)
    finally:
        raise ApplicationHandlerStop
