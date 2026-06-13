"""Telegram handlers for the Challenge League over-by-over "Approach" match.

Flow per over (two human players, in the group chat):
  1. Bowling captain picks a bowler (only Bowler/All-rounder, not last over's).
  2. Bowling captain picks a Bowling Approach.
  3. Batting captain picks a Batting Approach.
  4. The six balls are simulated (services.cipl_match.simulate_over).
  5. An over summary is posted with a Mini App link (read-only scorecard).
  6. When the next over starts, the previous over's messages are deleted.

Toss happens first, "exactly like the current system" (run_coin_toss): after the
host clicks Start, the guest calls heads/tails, the winner chooses bat or bowl,
and only then does the over-by-over flow begin in the chat.
"""

import html
import logging
from datetime import datetime, timedelta
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import ChallengePlayer, Match, User
from services.match_constants import MATCH_EXPIRE, random_match_settings
from services.match_state_store import (
    get_state as _gs_store,
    save_state as _ss_store,
    get_next_action,
    cleanup_state,
    get_match_lock,
    release_match_lock,
    A_PICK_CIPL_BOWLER,
    A_PICK_BOWL_APPROACH,
    A_PICK_BAT_APPROACH,
    A_COMPLETED,
)
from services import cipl_match
from engine.approach_modifiers import (
    BATTING_APPROACHES, BOWLING_APPROACHES,
)
from handlers.match import _mention

logger = logging.getLogger(__name__)

CIPL_TIMEOUT = 90  # seconds before an idle pick is auto-resolved
CIPL_OVERS = 20    # Challenge League / League Battle matches are always 20 overs


class SimpleUser:
    """Detached snapshot of a User so engine code never touches a closed session."""
    __slots__ = ("id", "telegram_id", "username", "first_name")

    def __init__(self, uid, tg_id, username, first_name):
        self.id = uid
        self.telegram_id = tg_id
        self.username = username
        self.first_name = first_name


class SimpleMatch:
    __slots__ = ("id", "overs", "stadium")

    def __init__(self, mid, overs, stadium):
        self.id = mid
        self.overs = overs
        self.stadium = stadium


# ════════════════════════════════════════════════════════════════════
# Team identity (coloured marker + short code for the scorecard card)
# ════════════════════════════════════════════════════════════════════

# Ordered hue buckets → coloured-circle emoji, for custom-league teams whose
# primary_color is a hex string not present in IPL_TEAM_META.
_CIRCLE_BY_HUE = [
    (15, "🔴"), (45, "🟠"), (70, "🟡"), (170, "🟢"),
    (260, "🔵"), (320, "🟣"), (360, "🔴"),
]


def _hex_to_circle(hex_color):
    """Map a ``#rrggbb`` (or ``rrggbb``) string to the nearest coloured circle."""
    s = (hex_color or "").lstrip("#").strip()
    if len(s) != 6:
        return "🏏"
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return "🏏"
    mx, mn = max(r, g, b), min(r, g, b)
    diff = mx - mn
    if diff < 0.10:  # near-greyscale → white/black circle by lightness
        return "⚪" if mx > 0.5 else "⚫"
    if mx == r:
        hue = (60 * ((g - b) / diff) + 360) % 360
    elif mx == g:
        hue = 60 * ((b - r) / diff) + 120
    else:
        hue = 60 * ((r - g) / diff) + 240
    for ceiling, emoji in _CIRCLE_BY_HUE:
        if hue <= ceiling:
            return emoji
    return "🏏"


def _resolve_team_identity(team_name, league_key, session):
    """Return ``(short_code, colour_emoji)`` for the scorecard card.

    Reuses the challenge helpers (which already know IPL teams and custom-league
    short names); falls back to a hue-mapped circle from the team's stored
    primary_color when the team isn't an IPL side.
    """
    try:
        from handlers import challenge
        code = challenge._team_short_code(team_name, league_key, session) or ""
        emoji = challenge._team_emoji(team_name)
        if emoji == "🏏" and session is not None:
            from models import ChallengeTeam
            team = (session.query(ChallengeTeam)
                    .filter(ChallengeTeam.name == team_name).first())
            if team and getattr(team, "primary_color", None):
                emoji = _hex_to_circle(team.primary_color)
        return code, emoji
    except Exception:
        logger.exception("cipl team identity resolution failed for %s", team_name)
        return "", "🏏"


# ════════════════════════════════════════════════════════════════════
# Small helpers
# ════════════════════════════════════════════════════════════════════

def _gs(ctx, mid):
    return _gs_store(ctx, mid)


def _ss(ctx, mid, s, next_action=None, last_prompt_msg_id=None):
    _ss_store(ctx, mid, s, next_action=next_action,
              last_prompt_msg_id=last_prompt_msg_id)


def _mention_tg(state, tg_id, fallback="Player"):
    names = state.get("user_names") or {}
    return _mention(tg_id, names.get(str(tg_id), fallback))


def _draft_key(draft_id):
    from handlers.challenge import _challenge_team_draft_key
    return _challenge_team_draft_key(draft_id)


def build_xi_from_draft(session, draft, side):
    """Return the selected XI for ``side`` as engine player dicts, in the
    captain's selection order (which is the batting order)."""
    from handlers.challenge import _challenge_xi_selection
    selection = _challenge_xi_selection(draft, side)
    selected_ids = [int(pid) for pid in selection.get("player_ids", [])]
    if not selected_ids:
        return []
    rows = (session.query(ChallengePlayer)
            .filter(ChallengePlayer.id.in_(selected_ids))
            .all())
    by_id = {int(r.id): r for r in rows}
    ordered = [by_id[pid] for pid in selected_ids if pid in by_id]
    return [cipl_match.cp_to_player_dict(cp) for cp in ordered]


# ════════════════════════════════════════════════════════════════════
# Message management (delete previous over, edit/send action message)
# ════════════════════════════════════════════════════════════════════

async def _delete_prev_over(context, state):
    chat_id = state["chat_id"]
    for mid_msg in state.get("over_msg_ids", []) or []:
        try:
            await context.bot.delete_message(chat_id, mid_msg)
        except Exception:
            pass
    state["over_msg_ids"] = []
    state["action_msg_id"] = None


def _with_view_match(state, keyboard):
    """Append the View Match row so EVERY over message carries the link."""
    kb = list(keyboard or [])
    extra = _miniapp_row(state)
    if extra:
        kb += extra
    return kb


async def _new_action_message(context, state, text, keyboard):
    kb = _with_view_match(state, keyboard)
    sent = await context.bot.send_message(
        state["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    if sent and getattr(sent, "message_id", None):
        state["action_msg_id"] = sent.message_id
        state.setdefault("over_msg_ids", []).append(sent.message_id)
    return sent


async def _edit_action_message(context, state, text, keyboard):
    mid_msg = state.get("action_msg_id")
    kb = _with_view_match(state, keyboard)
    markup = InlineKeyboardMarkup(kb) if kb else None
    if mid_msg:
        try:
            await context.bot.edit_message_text(
                text, chat_id=state["chat_id"], message_id=mid_msg,
                parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await _new_action_message(context, state, text, keyboard)


async def _post_tracked(context, state, text, keyboard=None):
    """Post an over message (e.g. the summary) tracked for deletion next over."""
    sent = await context.bot.send_message(
        state["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    if sent and getattr(sent, "message_id", None):
        state.setdefault("over_msg_ids", []).append(sent.message_id)
    return sent


def _miniapp_row(state):
    """The "View Match" button opens the SAME cricket arena Mini App used by
    /wpm and /wpmbot (scorecard, over-by-over commentary, playing XI), targeting
    THIS match_id — not just the bare app — so spectators land on the right board.
    """
    try:
        from services.match_broadcast import play_match_keyboard
        kb = play_match_keyboard(
            state["match_id"], chat_id=state.get("chat_id"),
            is_private=bool(state.get("is_private")), label="📊 View Match")
        return kb.inline_keyboard if kb else None
    except Exception:
        logger.exception("cipl view-match button build failed")
        return None


# ════════════════════════════════════════════════════════════════════
# Inactivity timer (auto-pick Balanced / top bowler)
# ════════════════════════════════════════════════════════════════════

def _cancel_timer(context, mid):
    if not getattr(context, "job_queue", None):
        return
    for j in context.job_queue.get_jobs_by_name(f"cipl_to_{mid}"):
        j.schedule_removal()


def _arm_timer(context, mid, expected_action):
    _cancel_timer(context, mid)
    if not getattr(context, "job_queue", None):
        return
    context.job_queue.run_once(
        _on_timeout, CIPL_TIMEOUT, name=f"cipl_to_{mid}",
        data={"mid": mid, "expected": expected_action})


async def _on_timeout(context):
    d = context.job.data
    mid = d["mid"]
    expected = d["expected"]
    async with get_match_lock(mid):
        if get_next_action(context, mid) != expected:
            return  # the user already acted
        state = _gs(context, mid)
        if not state:
            return
        try:
            if expected == A_PICK_CIPL_BOWLER:
                elig = cipl_match.eligible_bowlers(state)
                if not elig:
                    return
                state["current_bowler"] = elig[0]
                _ss(context, mid, state)
                await _prompt_bowl_approach(context, mid, state, auto=True)
            elif expected == A_PICK_BOWL_APPROACH:
                state["bowling_approach"] = "balanced"
                _ss(context, mid, state)
                await _prompt_bat_approach(context, mid, state, auto=True)
            elif expected == A_PICK_BAT_APPROACH:
                state["batting_approach"] = "balanced"
                _ss(context, mid, state)
                await _run_over(context, mid, state)
        except Exception:
            logger.exception("cipl timeout auto-pick failed for match %s", mid)


# ════════════════════════════════════════════════════════════════════
# Toss (run "exactly like the current system")
# ════════════════════════════════════════════════════════════════════

async def cipl_coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cipl_coin_{heads|tails}_{draft_id} — the guest calls the toss."""
    q = update.callback_query
    try:
        _, _, call, draft_id = q.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await q.answer("Invalid toss call.", show_alert=True)
        return
    draft = context.bot_data.get(_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await q.answer("This toss is no longer active.", show_alert=True)
        return
    if call not in ("heads", "tails"):
        await q.answer("Invalid call.", show_alert=True)
        return
    if draft.get("toss_winner_side"):
        await q.answer("Toss already done.", show_alert=True)
        return
    if q.from_user.id != draft.get("target_tg_id"):
        await q.answer("Only the guest calls the toss!", show_alert=True)
        return
    await q.answer()
    from services.match_broadcast import run_coin_toss
    coin, won = await run_coin_toss(
        lambda t: q.edit_message_text(t, parse_mode="HTML"), call)
    winner_side = "target" if won else "host"
    draft["toss_winner_side"] = winner_side
    winner_tg = draft.get("target_tg_id") if won else draft.get("host_tg_id")
    winner_name = (draft.get("target") if won else draft.get("host") or {}).get("name", "Winner")
    await q.edit_message_text(
        f"🪙 The coin lands on <b>{coin.upper()}</b> — guest called "
        f"<b>{call.upper()}</b>.\n\n"
        f"🏆 {_mention(winner_tg, winner_name)} won the toss. Choose:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First",
                                 callback_data=f"cipl_toss_bat_{draft_id}_{winner_side}"),
            InlineKeyboardButton("🎳 Bowl First",
                                 callback_data=f"cipl_toss_bowl_{draft_id}_{winner_side}"),
        ]]))


async def cipl_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cipl_toss_{bat|bowl}_{draft_id}_{winner_side} — winner elects, then the
    over-by-over match begins in the chat."""
    q = update.callback_query
    try:
        _, _, decision, draft_id, winner_side = q.data.split("_")
        draft_id = int(draft_id)
    except Exception:
        await q.answer("Invalid toss decision.", show_alert=True)
        return
    draft = context.bot_data.get(_draft_key(draft_id))
    if not draft or draft.get("turn") != "complete":
        await q.answer("This toss is no longer active.", show_alert=True)
        return
    if decision not in ("bat", "bowl"):
        await q.answer("Invalid decision.", show_alert=True)
        return
    if draft.get("match_launched"):
        await q.answer("Match already started.", show_alert=True)
        return
    winner_tg = draft.get("target_tg_id") if winner_side == "target" else draft.get("host_tg_id")
    if q.from_user.id != winner_tg:
        await q.answer("Toss winner only.", show_alert=True)
        return

    session = get_session()
    try:
        host = session.query(User).get(draft["host_user_id"])
        target = session.query(User).get(draft["target_user_id"])
        if not host or not target:
            await q.answer("Players no longer exist.", show_alert=True)
            return

        winner = target if winner_side == "target" else host
        loser = host if winner_side == "target" else target
        if decision == "bat":
            bat_user, bowl_user = winner, loser
        else:
            bowl_user, bat_user = winner, loser

        host_id = host.id
        host_team = draft.get("host_team")
        target_team = draft.get("target_team")
        bat_is_host = bat_user.id == host_id
        bat_team_name = host_team if bat_is_host else target_team
        bowl_team_name = target_team if bat_is_host else host_team

        # Coloured marker + short code for the broadcast-style scorecard card.
        league_key = draft.get("league_key")
        bat_team_code, bat_team_emoji = _resolve_team_identity(
            bat_team_name, league_key, session)
        bowl_team_code, bowl_team_emoji = _resolve_team_identity(
            bowl_team_name, league_key, session)

        bat_side = "host" if bat_is_host else "target"
        bowl_side = "target" if bat_is_host else "host"
        bat_xi = build_xi_from_draft(session, draft, bat_side)
        bowl_xi = build_xi_from_draft(session, draft, bowl_side)
        if len(bat_xi) < 2 or len(bowl_xi) < 2:
            await q.answer("Playing XI missing — restart the challenge.", show_alert=True)
            return

        # Capture all primitives BEFORE the session closes (avoid detached ORM).
        bat_info = SimpleUser(bat_user.id, bat_user.telegram_id,
                              bat_user.username, bat_user.first_name)
        bowl_info = SimpleUser(bowl_user.id, bowl_user.telegram_id,
                               bowl_user.username, bowl_user.first_name)
        winner_tg_val = winner.telegram_id

        # Challenge League / League Battle is always a 20-over contest.
        overs = CIPL_OVERS
        settings = random_match_settings()
        # Honour the host's chosen pitch (selected during setup); fall back to the
        # randomised surface only when no pitch was picked.
        chosen_pitch = draft.get("pitch_type") or settings["pitch_type"]
        match = Match(
            user1_id=host.id, user2_id=target.id, status="active",
            overs=overs, toss_winner_id=winner.id, toss_decision=decision,
            batting_first_id=bat_user.id, bowling_first_id=bowl_user.id,
            stadium=settings["stadium"], pitch_type=chosen_pitch,
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=draft["chat_id"], created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(match)
        session.commit()
        match_obj = SimpleMatch(match.id, match.overs, match.stadium)
        pitch_type = match.pitch_type
    except Exception:
        session.rollback()
        logger.exception("/cipl toss/launch failed")
        await q.answer("Failed to start match.", show_alert=True)
        return
    finally:
        session.close()

    draft["match_launched"] = True
    await q.answer()
    winner_name = (draft.get(winner_side) or {}).get("name", "Winner")
    await q.edit_message_text(
        f"✅ {_mention(winner_tg_val, winner_name)} "
        f"elected to <b>{'BAT' if decision == 'bat' else 'BOWL'}</b> first.\n"
        f"🏟️ {match_obj.stadium} • {pitch_type} pitch • {match_obj.overs} overs\n\n"
        f"The match begins below — play over by over!",
        parse_mode="HTML")
    # The toss-result message (this one) is kept; every other setup message is
    # swept away when the match starts.
    if getattr(q, "message", None) is not None:
        draft["toss_result_msg_id"] = q.message.message_id

    await begin_cipl_match(context, draft["chat_id"], match_obj, bat_info, bowl_info,
                           bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                           pitch_type,
                           bat_team_code=bat_team_code, bowl_team_code=bowl_team_code,
                           bat_team_emoji=bat_team_emoji, bowl_team_emoji=bowl_team_emoji,
                           draft=draft)


# ════════════════════════════════════════════════════════════════════
# Entry point — called after the toss is decided
# ════════════════════════════════════════════════════════════════════

async def begin_cipl_match(context, chat_id, match, bat_user, bowl_user,
                           bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                           pitch_type, bat_team_code="", bowl_team_code="",
                           bat_team_emoji="🏏", bowl_team_emoji="🏏", draft=None):
    state = cipl_match.build_cipl_state(
        match_id=match.id, overs=match.overs,
        bat_user_id=bat_user.id, bowl_user_id=bowl_user.id,
        bat_user_tg=bat_user.telegram_id, bowl_user_tg=bowl_user.telegram_id,
        bat_xi=bat_xi, bowl_xi=bowl_xi,
        bat_team_name=bat_team_name, bowl_team_name=bowl_team_name,
        chat_id=chat_id, pitch_type=pitch_type,
        is_private=chat_id > 0, stadium=match.stadium,
        bat_team_code=bat_team_code, bowl_team_code=bowl_team_code,
        bat_team_emoji=bat_team_emoji, bowl_team_emoji=bowl_team_emoji)
    state["user_names"] = {
        str(bat_user.telegram_id): bat_user.username or bat_user.first_name or "Player",
        str(bowl_user.telegram_id): bowl_user.username or bowl_user.first_name or "Player",
    }
    _ss(context, match.id, state, next_action=A_PICK_CIPL_BOWLER)
    # Clear the pre-match setup chatter (keep the toss result) and pin a polished
    # announcement carrying the Watch Match button.
    await _cleanup_setup_and_announce(context, state, draft)
    await _prompt_bowler(context, match.id, state, first=True)


async def _cleanup_setup_and_announce(context, state, draft):
    """Delete the challenge setup messages (all but the toss result) and post +
    pin the 'Watch X vs Y' announcement with the Mini App Watch Match button."""
    chat_id = state["chat_id"]
    keep = (draft or {}).get("toss_result_msg_id")
    for mid_msg in list((draft or {}).get("setup_msg_ids", []) or []):
        if mid_msg == keep:
            continue
        try:
            await context.bot.delete_message(chat_id, mid_msg)
        except Exception:
            pass  # message too old / not deletable — leave it

    text = _match_start_announcement(state)
    kb = _miniapp_row(state)
    try:
        sent = await context.bot.send_message(
            chat_id, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    except Exception:
        logger.exception("cipl match-start announcement failed")
        return
    if not (sent and getattr(sent, "message_id", None)):
        return
    state["pinned_msg_id"] = sent.message_id
    try:
        await context.bot.pin_chat_message(
            chat_id, sent.message_id, disable_notification=True)
    except Exception:
        # No pin rights (bot not an admin) — the announcement still stands.
        logger.info("cipl announcement pin skipped (no rights) for chat %s", chat_id)


def _match_start_announcement(state):
    """Polished 'Watch CSK 🆚 MI — High-Voltage IPL Battle' card for the pin."""
    bat = html.escape(str(state.get("bat_team_name", "Team A")))
    bowl = html.escape(str(state.get("bowl_team_name", "Team B")))
    bat_code = html.escape(str(state.get("bat_team_code") or "")) or bat
    bowl_code = html.escape(str(state.get("bowl_team_code") or "")) or bowl
    bat_emoji = state.get("bat_team_emoji", "🏏")
    bowl_emoji = state.get("bowl_team_emoji", "🏏")
    stadium = html.escape(str(state.get("stadium") or "Neutral Venue"))
    pitch = html.escape(str(state.get("pitch_type") or "Hard"))
    overs = state.get("overs", 20)
    rule = "━" * 15
    return (
        f"🏆 <b>{bat_code}</b> 🆚 <b>{bowl_code}</b>\n"
        f"⚡ <b>High-Voltage IPL Battle</b> ⚡\n"
        f"{rule}\n"
        f"🏟️ {stadium} • {overs} overs\n"
        f"🌱 <b>Pitch:</b> {pitch}\n"
        f"🏏 {bat} batting first\n"
        f"{rule}\n"
        f"{bat_emoji} <b>{bat}</b>   vs   {bowl_emoji} <b>{bowl}</b>\n"
        f"📺 Live scorecard, commentary &amp; XI inside\n\n"
        f"👇 Tap <b>Watch Match</b> to follow the action live")


# ════════════════════════════════════════════════════════════════════
# Per-over prompts
# ════════════════════════════════════════════════════════════════════

def _header(state):
    inn = state.get("innings", 1)
    line = (f"🏏 <b>{state['bat_team_name']}</b> {cipl_match.format_score(state)} "
            f"({cipl_match.format_overs(state)}/{state['overs']})")
    c = cipl_match.chase(state)
    if c:
        line += (f"\n🎯 Need <b>{c['runs_required']}</b> off "
                 f"{c['balls_remaining']} • RRR {c['rrr']:.2f}")
    return f"Innings {inn} • Over {state['current_over']}\n{line}"


async def _prompt_bowler(context, mid, state=None, first=False):
    if state is None:
        state = _gs(context, mid)
    if not state:
        return
    if not first:
        await _delete_prev_over(context, state)
    elig = cipl_match.eligible_bowlers(state)
    rows, row = [], []
    for p in elig:
        row.append(InlineKeyboardButton(
            f"{p['name']} ({p.get('bowl_rating', 0)})",
            callback_data=f"cipl_bowler_{mid}_{p['roster_id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    text = (f"{_approach_card(state)}\n\n"
            f"🎳 {_mention_tg(state, state['bowl_user_tg'])}, pick your bowler "
            f"for over {state['current_over']}:")
    await _new_action_message(context, state, text, rows)
    _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)
    _arm_timer(context, mid, A_PICK_CIPL_BOWLER)


async def _prompt_bowl_approach(context, mid, state, auto=False):
    bowler = state["current_bowler"]
    rows = [[InlineKeyboardButton(f"{emoji} {label}",
                                  callback_data=f"cipl_bowlapp_{mid}_{idx}")]
            for idx, (_, emoji, label) in enumerate(BOWLING_APPROACHES)]
    note = " <i>(auto-picked bowler)</i>" if auto else ""
    text = (f"{_approach_card(state)}\n\n"
            f"🎳 Bowler: <b>{bowler['name']}</b>{note}\n"
            f"{_mention_tg(state, state['bowl_user_tg'])}, choose your "
            f"<b>Bowling Approach</b>:")
    await _edit_action_message(context, state, text, rows)
    _ss(context, mid, state, next_action=A_PICK_BOWL_APPROACH)
    _arm_timer(context, mid, A_PICK_BOWL_APPROACH)


async def _prompt_bat_approach(context, mid, state, auto=False):
    rows = [[InlineKeyboardButton(f"{emoji} {label}",
                                  callback_data=f"cipl_batapp_{mid}_{idx}")]
            for idx, (_, emoji, label) in enumerate(BATTING_APPROACHES)]
    # The bowling captain's chosen Bowling Approach is hidden here so the batting
    # captain (the opponent) can't read the bowling plan before picking their own.
    text = (f"{_approach_card(state)}\n\n"
            f"🎳 Bowler: <b>{state['current_bowler']['name']}</b>\n"
            f"🏏 {_mention_tg(state, state['bat_user_tg'])}, choose your "
            f"<b>Batting Approach</b>:")
    await _edit_action_message(context, state, text, rows)
    _ss(context, mid, state, next_action=A_PICK_BAT_APPROACH)
    _arm_timer(context, mid, A_PICK_BAT_APPROACH)


# ════════════════════════════════════════════════════════════════════
# Callbacks
# ════════════════════════════════════════════════════════════════════

async def cipl_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, _, mid, rid = q.data.split("_")
        mid, rid = int(mid), int(rid)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    async with get_match_lock(mid):
        state = _gs(context, mid)
        if not state:
            await q.answer("Match not found.", show_alert=True)
            return
        if q.from_user.id != state["bowl_user_tg"]:
            await q.answer("Only the bowling captain picks the bowler.", show_alert=True)
            return
        if get_next_action(context, mid) != A_PICK_CIPL_BOWLER:
            await q.answer("Bowler already chosen.", show_alert=True)
            return
        bowler = cipl_match.find_player(state["bowl_xi"], rid)
        if not bowler:
            await q.answer("Bowler not available.", show_alert=True)
            return
        await q.answer()
        _cancel_timer(context, mid)
        state["current_bowler"] = bowler
        _ss(context, mid, state)
        await _prompt_bowl_approach(context, mid, state)


async def cipl_bowlapp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, _, mid, idx = q.data.split("_")
        mid, idx = int(mid), int(idx)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    async with get_match_lock(mid):
        state = _gs(context, mid)
        if not state:
            await q.answer("Match not found.", show_alert=True)
            return
        if q.from_user.id != state["bowl_user_tg"]:
            await q.answer("Only the bowling captain picks this.", show_alert=True)
            return
        if get_next_action(context, mid) != A_PICK_BOWL_APPROACH:
            await q.answer("Already chosen.", show_alert=True)
            return
        if not (0 <= idx < len(BOWLING_APPROACHES)):
            await q.answer("Invalid approach.", show_alert=True)
            return
        await q.answer()
        _cancel_timer(context, mid)
        state["bowling_approach"] = BOWLING_APPROACHES[idx][0]
        _ss(context, mid, state)
        await _prompt_bat_approach(context, mid, state)


async def cipl_batapp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, _, mid, idx = q.data.split("_")
        mid, idx = int(mid), int(idx)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    async with get_match_lock(mid):
        state = _gs(context, mid)
        if not state:
            await q.answer("Match not found.", show_alert=True)
            return
        if q.from_user.id != state["bat_user_tg"]:
            await q.answer("Only the batting captain picks this.", show_alert=True)
            return
        if get_next_action(context, mid) != A_PICK_BAT_APPROACH:
            await q.answer("Already chosen.", show_alert=True)
            return
        if not (0 <= idx < len(BATTING_APPROACHES)):
            await q.answer("Invalid approach.", show_alert=True)
            return
        await q.answer()
        _cancel_timer(context, mid)
        state["batting_approach"] = BATTING_APPROACHES[idx][0]
        _ss(context, mid, state)
        await _run_over(context, mid, state)


# ════════════════════════════════════════════════════════════════════
# Over execution + progression
# ════════════════════════════════════════════════════════════════════

async def _run_over(context, mid, state):
    _cancel_timer(context, mid)
    bowler_name = state["current_bowler"]["name"]
    await _edit_action_message(
        context, state,
        f"{_header(state)}\n\n⏳ Simulating over {state['current_over']} — "
        f"{bowler_name} bowling…", None)

    summary = cipl_match.simulate_over(state)
    _ss(context, mid, state)

    await _post_tracked(context, state, _render_over_summary(state, summary),
                        keyboard=_miniapp_row(state))

    if cipl_match.is_innings_over(state):
        if state["innings"] == 1:
            await _innings_break(context, mid, state)
        else:
            await _complete_match(context, mid, state)
    else:
        await _prompt_bowler(context, mid, state)


def _render_over_summary(state, summary):
    timeline = " ".join(cipl_match._SYM.get(_sym_key(s), s)
                        for s in summary["over_timeline"]) or "—"
    striker = state["batting_order"][state["striker_idx"]]
    non_striker = state["batting_order"][state["non_striker_idx"]]
    bs = state["bat_stats"]
    s_line = _bat_line(striker, bs)
    n_line = _bat_line(non_striker, bs)
    mo = summary["momentum_shift"]
    arrow = "📈" if mo > 1 else ("📉" if mo < -1 else "➖")
    # Bowling/Batting approaches are deliberately NOT shown in the public over
    # summary — revealing them would let the opponent read each captain's plan.
    lines = [
        f"<b>End of Over {summary['over_no']}</b> — {summary['bowler']['name']}",
        f"Timeline: {timeline}",
        f"This over: <b>{summary['over_runs']}</b> run(s), "
        f"{summary['over_wickets']} wkt(s)",
        "",
        f"🏏 <b>{state['bat_team_name']}</b> {cipl_match.format_score(state)} "
        f"({cipl_match.format_overs(state)}/{state['overs']})",
        f"• {s_line}",
        f"• {n_line}",
        f"🎳 {summary['bowler']['name']}: {summary['bowler_figures']}",
        f"{arrow} Momentum: {state['bat_team_name']}",
    ]
    c = cipl_match.chase(state)
    if c and c["runs_required"] > 0:
        lines.append(f"🎯 Need {c['runs_required']} off {c['balls_remaining']} "
                     f"(RRR {c['rrr']:.2f})")
    return "\n".join(lines)


def _sym_key(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        return s


def _bat_line(player, bat_stats):
    st = bat_stats.get(str(player["roster_id"]), {})
    star = "" if st.get("out") else "*"
    return (f"{player['name']} {st.get('runs', 0)}{star} "
            f"({st.get('balls', 0)}b, {st.get('fours', 0)}×4, {st.get('sixes', 0)}×6)")


# ════════════════════════════════════════════════════════════════════
# Broadcast-style scorecard card (shown on the approach prompts)
# ════════════════════════════════════════════════════════════════════

# Commentary entry type → ball emoji for the expandable commentary block.
_CMT_EMOJI = {
    "dot": "0️⃣", "one": "1️⃣", "two": "2️⃣", "three": "3️⃣",
    "four": "4️⃣", "six": "6️⃣", "wicket": "⭕", "extra": "↔️",
}


def _compact_bat_line(player, bat_stats):
    """``Rohit Sharma 56(27)*`` — runs(balls), trailing ``*`` while not out."""
    st = bat_stats.get(str(player["roster_id"]), {})
    star = "" if st.get("out") else "*"
    return (f"{html.escape(str(player['name']))} "
            f"{st.get('runs', 0)}({st.get('balls', 0)}){star}")


def _compact_bowler_figs(bws):
    """``1/23 (2)`` — wickets/runs (overs)."""
    overs = f"{bws['balls'] // 6}.{bws['balls'] % 6}" if bws['balls'] % 6 else str(bws['balls'] // 6)
    return f"{bws.get('wickets', 0)}/{bws.get('runs', 0)} ({overs})"


def _over_emoji_strip(state):
    """Last completed over's deliveries as run/wicket emojis (—— on first over)."""
    tl = state.get("last_over_timeline") or []
    if not tl:
        return "—"
    return "".join(cipl_match._SYM.get(_sym_key(s), s) for s in tl)


def _commentary_block(state):
    """Last over's ball-by-ball as an expandable Telegram quote (newest first)."""
    entries = state.get("last_over_commentary") or []
    if not entries:
        return ""
    lines = []
    for e in reversed(entries):
        etype = e.get("type")
        # A wicket is emitted as a ball row (rich commentary, carries the W) plus
        # a paired "wicket" summary card. Render only the ball row here so the
        # Telegram block keeps a single line per delivery.
        if etype == "wicket":
            continue
        emoji = _CMT_EMOJI.get(etype, "")
        if etype == "ball" and e.get("isWicket"):
            emoji = "⭕"
        text = html.escape(str(e.get("text", "")))
        over = html.escape(str(e.get("over", "")))
        lines.append(f"{over} {text} {emoji}".rstrip())
    body = "\n".join(lines)
    return f'\n🟩 <b>COMMENTARY</b>\n<blockquote expandable>"{body}"</blockquote>'


def _crr_line(state):
    """``CRR - 10.36`` (1st innings) or ``CRR - .. | RRR - .. | Need R off B`` (2nd)."""
    line = f"CRR - {cipl_match.current_run_rate(state):.2f}"
    c = cipl_match.chase(state)
    if c and c["runs_required"] > 0:
        line += (f" | RRR - {c['rrr']:.2f} | "
                 f"Need {c['runs_required']} off {c['balls_remaining']}")
    return line


def _approach_card(state):
    """Full broadcast-style scorecard card used on the approach-select prompts."""
    inn = state.get("innings", 1)
    bat_name = html.escape(str(state["bat_team_name"]))
    bat_emoji = state.get("bat_team_emoji", "🏏")
    bat_code = html.escape(str(state.get("bat_team_code") or "")) or bat_name
    bowl_emoji = state.get("bowl_team_emoji", "🏏")
    bowl_code = html.escape(str(state.get("bowl_team_code") or "")) or html.escape(
        str(state["bowl_team_name"]))

    striker = state["batting_order"][state["striker_idx"]]
    non_striker = state["batting_order"][state["non_striker_idx"]]
    bs = state["bat_stats"]
    rule = "—" * 28

    lines = [
        f"Innings {inn} | <b>{bat_name}</b> | Bat",
        rule,
        f"{bat_emoji} {bat_code} - <b>{cipl_match.format_score(state)}</b> - "
        f"{cipl_match.format_overs(state)}",
        "",
        f"🔹 {_compact_bat_line(striker, bs)}",
        f"      {_compact_bat_line(non_striker, bs)}",
        rule,
        _crr_line(state),
        rule,
        f"{bowl_emoji} {bowl_code} | Bowl | {_over_emoji_strip(state)}",
    ]
    bowler = state.get("current_bowler")
    if bowler:
        bws = state["bowl_stats"].get(str(bowler["roster_id"]))
        if bws:
            lines += [rule,
                      f"{html.escape(str(bowler['name']))} - {_compact_bowler_figs(bws)}"]
    card = "\n".join(lines)
    card += _commentary_block(state)
    return card


async def _innings_break(context, mid, state):
    # Clear the last over of innings 1 (its "simulating…" prompt + summary) so the
    # break message is the only thing left from innings 1 — even if the innings
    # ended on the final ball or an all-out.
    await _delete_prev_over(context, state)

    summary_text = _innings_scorecard(state, innings_label="1st Innings")
    cipl_match.end_first_innings(state)
    _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)
    target = state["target"]
    text = (f"🛑 <b>Innings Break</b>\n\n{summary_text}\n\n"
            f"🎯 <b>{state['bat_team_name']}</b> need <b>{target}</b> to win "
            f"in {state['overs']} overs.")
    kb = _miniapp_row(state)
    sent = await context.bot.send_message(
        state["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    # Keep the break card up through the first over of the chase (the target also
    # rides along in every over header), then let the normal previous-over delete
    # sweep it away the moment over 2 begins — so the chat doesn't accumulate it.
    state["over_msg_ids"] = [sent.message_id] if sent and getattr(sent, "message_id", None) else []
    state["action_msg_id"] = None
    await _prompt_bowler(context, mid, state, first=True)


async def _complete_match(context, mid, state):
    # Clear the final over's "simulating…" prompt + summary before the result is
    # posted — even when the chase is won mid-over, the last over message goes.
    await _delete_prev_over(context, state)
    _ss(context, mid, state)

    result = cipl_match.compute_result(state)
    # Per-over Win/Loss prize handed out below (None on a tie / award failure).
    prize_info = None
    # Persist career stats + finalize Match row.
    session = get_session()
    try:
        try:
            from services.player_stats_service import persist_player_game_stats
            persist_player_game_stats(session, state)
        except Exception:
            logger.exception("cipl stat persistence failed for match %s", mid)

        match = session.query(Match).get(mid)
        if match:
            match.status = "completed"
            match.completed_at = datetime.utcnow()
            match.inn1_runs = state.get("inn1_runs")
            match.inn1_wickets = state.get("inn1_wickets")
            match.inn2_runs = state.get("total_runs")
            match.inn2_wickets = state.get("total_wickets")
            if not result["tie"]:
                # innings-2 batting side = state['bat_team_id'] (chaser)
                chaser_uid = state["bat_team_id"]
                defender_uid = state["bowl_team_id"]
                won_by_chaser = result["margin_type"] == "wickets"
                match.winner_id = chaser_uid if won_by_chaser else defender_uid
                match.loser_id = defender_uid if won_by_chaser else chaser_uid
                match.margin_type = result["margin_type"]
                match.margin_value = result["margin"]

                # Per-over Win/Loss prize — the user who won/lost the match with
                # their League team gets coins/gems. Uses the same website-tunable
                # economy as /wpm, /cm, /vsbot and /playmatch
                # (config_service.match_*_per_over via award_match_rewards_core).
                try:
                    from services.match_rewards import award_match_rewards_core
                    overs = state.get("overs") or CIPL_OVERS
                    w_coins, w_gems, l_coins, l_gems = award_match_rewards_core(
                        session, match.winner_id, match.loser_id, overs,
                        is_vsbot=False)
                    prize_info = {
                        "w_coins": w_coins, "w_gems": w_gems,
                        "l_coins": l_coins, "l_gems": l_gems,
                    }
                except Exception:
                    logger.exception("cipl prize award failed for match %s", mid)

        # Snapshot the final scorecard + Arena board so the "View Match" Mini App
        # stays viewable after the live state is cleaned up below (same mechanism
        # /wpm uses). Must run while the live state still exists.
        try:
            if result["tie"]:
                result_text = "Match Tied"
            else:
                result_text = (f"{result['winner']} beat {result['loser']} by "
                               f"{result['margin']} {result['margin_type']}")
            from services.match_webapp_service import save_final_scorecard
            save_final_scorecard(session, mid, result_text=result_text)
        except Exception:
            logger.exception("cipl final scorecard snapshot failed for match %s", mid)

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("cipl match finalization failed for match %s", mid)
    finally:
        session.close()

    if result["tie"]:
        result_line = "🤝 <b>Match Tied!</b>"
    else:
        result_line = (f"🏆 <b>{result['winner']}</b> beat {result['loser']} "
                       f"by {result['margin']} {result['margin_type']}!")

    # Win/result message FIRST, then the Match Summary image — same order and
    # card as /wpm, /wpmbot and /cm. The match-end recap body sits inside an
    # expandable quote so the chat stays tidy.
    body = (f"{_innings_scorecard(state, innings_label='2nd Innings')}\n\n"
            f"{result_line}")
    if prize_info:
        body += (
            f"\n\n💰 <b>Prizes</b>\n"
            f"🏆 {result['winner']}: +{prize_info['w_coins']:,} coins, "
            f"+{prize_info['w_gems']} 💎\n"
            f"🤝 {result['loser']}: +{prize_info['l_coins']:,} coins, "
            f"+{prize_info['l_gems']} 💎")
    text = f"🏁 <b>Match Over</b>\n<blockquote expandable>{body}</blockquote>"
    miniapp_row = _miniapp_row(state)
    await context.bot.send_message(state["chat_id"], text, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(miniapp_row)
                                   if miniapp_row else None)
    try:
        img = _build_cipl_summary_image(state, result)
        if img:
            # The Spectate / View Match button rides on the scorecard image too,
            # so anyone can open this exact match in the Mini App.
            await context.bot.send_photo(
                state["chat_id"], photo=BytesIO(img),
                caption=f"🏆 <b>Match Summary</b> — {result_line}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(miniapp_row)
                if miniapp_row else None)
    except Exception:
        logger.exception("cipl match summary image failed for match %s", mid)

    # Unpin the match-start announcement now that the match is over.
    pinned = state.get("pinned_msg_id")
    if pinned:
        try:
            await context.bot.unpin_chat_message(state["chat_id"], pinned)
        except Exception:
            pass

    _ss(context, mid, state, next_action=A_COMPLETED)
    cleanup_state(context, mid)
    release_match_lock(mid)


def _summary_rows(bat_stats, bat_xi, bowl_stats, bowl_xi):
    """Top batters/bowlers for one innings, in the match-summary card format."""
    bats = []
    for p in bat_xi or []:
        st = bat_stats.get(str(p["roster_id"]), {})
        if st.get("balls", 0) > 0 or st.get("out"):
            bats.append({"name": p["name"], "runs": st.get("runs", 0),
                         "balls": st.get("balls", 0), "out": st.get("out", False)})
    bats.sort(key=lambda b: b["runs"], reverse=True)
    bowls = []
    for p in bowl_xi or []:
        st = bowl_stats.get(str(p["roster_id"]), {})
        balls = st.get("balls", 0)
        if balls > 0:
            bowls.append({"name": p["name"], "wickets": st.get("wickets", 0),
                          "runs": st.get("runs", 0),
                          "overs": f"{balls // 6}.{balls % 6}"})
    bowls.sort(key=lambda b: b["wickets"], reverse=True)
    return bats[:4], bowls[:4]


def _cipl_calc_potm(state, winner_name=None):
    """Player of the Match for a finished /cipl match, by impact points.

    Mirrors handlers/match.py:_calc_potm but reads CIPL state, whose batting/
    bowling stats are keyed by ``str(roster_id)``. Returns
    ``(name, stats_string, team)`` — all "" / None-safe — for the summary card.

    Batting impact: runs + 4s + 2·6s + strike-rate & milestone bonuses.
    Bowling impact: 25·wickets + economy·overs + milestone bonuses.

    POTM rule: every winning-team player gets a flat +15 impact bonus, then the
    award goes to the highest adjusted-impact player overall — except when the
    top two are within 50 points, in which case a winning-team player among them
    is preferred. ``winner_name`` of ``None`` (or a non-team value such as
    "Match Tied") falls back to the plain overall highest impact.
    """
    if winner_name and winner_name not in (
            state.get("bat_team_name"), state.get("inn1_team"),
            state.get("inn1_bat_team"), state.get("bowl_team_name")):
        # A tie / unknown winner sentinel — treat as no winner.
        winner_name = None
    def _bat_impact(bs):
        if not bs or bs.get("balls", 0) == 0:
            return 0
        runs = bs.get("runs", 0)
        balls = bs.get("balls", 1)
        impact = runs + bs.get("fours", 0) + bs.get("sixes", 0) * 2
        sr = (runs / balls) * 100 if balls else 0
        if sr >= 150:
            impact += 10
        elif sr >= 130:
            impact += 5
        if runs >= 100:
            impact += 30
        elif runs >= 50:
            impact += 15
        return impact

    def _bowl_impact(bws):
        if not bws or bws.get("balls", 0) == 0:
            return 0
        balls = bws.get("balls", 1)
        overs = balls / 6
        econ = (bws.get("runs", 0) / balls) * 6 if balls else 0
        impact = bws.get("wickets", 0) * 25 + (8 - econ) * overs * 2
        if bws.get("wickets", 0) >= 5:
            impact += 30
        elif bws.get("wickets", 0) >= 3:
            impact += 15
        return max(0, impact)

    # After the match, innings == 2: the side now batting (bat_team_name) chased,
    # and it is the side that BOWLED in innings 1. Read the archived first-innings
    # team names directly so each player is attributed to the correct side.
    inn1_bat_team = (state.get("inn1_bat_team") or state.get("inn1_team")
                     or state.get("bowl_team_name", ""))
    inn1_bowl_team = state.get("inn1_bowl_team") or state.get("bat_team_name", "")
    inn2_bat_team = state.get("bat_team_name", "")
    inn2_bowl_team = state.get("bowl_team_name", "")

    players = {}  # roster_id -> {name, team, bat, bowl, bat_impact, bowl_impact}

    def _add(xi, stats, *, team, is_bat):
        for p in xi or []:
            rid = p["roster_id"]
            st = (stats or {}).get(str(rid), {})
            entry = players.setdefault(
                rid, {"name": p.get("name", "Player"), "team": team,
                      "bat": {}, "bowl": {}, "bat_impact": 0, "bowl_impact": 0})
            if is_bat:
                entry["bat"] = st
                entry["bat_impact"] += _bat_impact(st)
                entry["team"] = team  # batting team is the player's own side
            else:
                entry["bowl"] = st
                entry["bowl_impact"] += _bowl_impact(st)

    _add(state.get("inn1_bat_xi"), state.get("inn1_bat_stats"),
         team=inn1_bat_team, is_bat=True)
    _add(state.get("inn1_bowl_xi"), state.get("inn1_bowl_stats"),
         team=inn1_bowl_team, is_bat=False)
    _add(state.get("bat_xi"), state.get("bat_stats"),
         team=inn2_bat_team, is_bat=True)
    _add(state.get("bowl_xi"), state.get("bowl_stats"),
         team=inn2_bowl_team, is_bat=False)

    # ── POTM scoring & selection ──────────────────────────────────────
    #  • Every player on the WINNING team gets a flat +15 impact bonus.
    #  • POTM is the highest adjusted-impact player overall.
    #  • Tie-break: if the top two are within 50 points of each other, prefer
    #    the one from the winning team.
    WINNING_TEAM_BONUS = 15

    for data in players.values():
        base = data["bat_impact"] + data["bowl_impact"]
        if winner_name and data.get("team") == winner_name:
            base += WINNING_TEAM_BONUS
        data["impact"] = base

    if not players:
        return None, "", ""

    ranked = sorted(players.values(), key=lambda d: d["impact"], reverse=True)
    best = ranked[0]
    if winner_name and len(ranked) >= 2:
        second = ranked[1]
        if (best["impact"] - second["impact"]) <= 50:
            # Within 50 points — prefer a winning-team player from the top two.
            winners_in_top2 = [d for d in (best, second)
                               if d.get("team") == winner_name]
            if winners_in_top2:
                best = max(winners_in_top2, key=lambda d: d["impact"])

    parts = []
    bs, bws = best["bat"], best["bowl"]
    if bs.get("balls", 0) > 0:
        parts.append(f"{bs.get('runs', 0)}({bs.get('balls', 0)})")
    if bws.get("balls", 0) > 0:
        ov = bws["balls"] // 6
        rem = bws["balls"] % 6
        ovr = f"{ov}.{rem}" if rem else str(ov)
        parts.append(f"{bws.get('wickets', 0)}/{bws.get('runs', 0)} ({ovr})")
    return best["name"], " | ".join(parts), best["team"]


def _build_cipl_summary_image(state, result):
    """Render the shared post-match summary card from the finished /cipl state."""
    try:
        from services.match_summary_card import generate_match_summary
    except Exception:
        logger.exception("match summary card unavailable for cipl")
        return None

    # Use the SAME admin-configured scorecard text settings as /wpm, /vsbot and
    # /wpmbot so the batsman-name font (and every other label) renders at the
    # same size here — without this the card falls back to the smaller defaults.
    try:
        from services.config_service import get_config
        text_settings = get_config().get("scorecard_text_settings")
    except Exception:
        logger.exception("cipl summary text settings load failed")
        text_settings = None

    inn1_bats, inn1_bowls = _summary_rows(
        state.get("inn1_bat_stats", {}), state.get("inn1_bat_xi", []),
        state.get("inn1_bowl_stats", {}), state.get("inn1_bowl_xi", []))
    inn2_bats, inn2_bowls = _summary_rows(
        state.get("bat_stats", {}), state.get("bat_xi", []),
        state.get("bowl_stats", {}), state.get("bowl_xi", []))

    if result["tie"]:
        winner_name, margin_text = "Match Tied", "Match Tied"
    else:
        winner_name = result["winner"]
        margin_text = f"won by {result['margin']} {result['margin_type']}"

    inn1_team = state.get("inn1_bat_team", state.get("bowl_team_name", "Team 1"))
    inn2_team = state.get("bat_team_name", "Team 2")

    # Player of the Match — populates the card's POTM footer (without this it
    # renders a blank "—"). Defensive: never let a POTM error drop the card.
    try:
        potm_name, potm_stats, potm_team = _cipl_calc_potm(
            state, None if result["tie"] else winner_name)
    except Exception:
        logger.exception("cipl POTM calculation failed for match %s", state.get("match_id"))
        potm_name, potm_stats, potm_team = None, None, None

    return generate_match_summary(
        inn1_team=inn1_team,
        inn1_runs=state.get("inn1_runs", 0),
        inn1_wickets=state.get("inn1_wickets", 0),
        inn1_overs=state.get("inn1_overs", "0.0"),
        inn2_team=inn2_team,
        inn2_runs=state.get("total_runs", 0),
        inn2_wickets=state.get("total_wickets", 0),
        inn2_overs=cipl_match.format_overs(state),
        winner_name=winner_name,
        win_margin_text=margin_text,
        overs_total=state.get("overs", 0),
        stadium=state.get("stadium"),
        potm_name=potm_name,
        potm_stats=potm_stats,
        potm_team=potm_team,
        top_per_team={
            "inn1": {"team": inn1_team, "batters": inn1_bats, "bowlers": inn1_bowls},
            "inn2": {"team": inn2_team, "batters": inn2_bats, "bowlers": inn2_bowls},
        },
        match_no=state.get("match_id"),
        text_settings=text_settings,
    )


def _innings_scorecard(state, innings_label=""):
    """Compact scorecard for the innings currently in ``state``."""
    lines = [f"<b>{state['bat_team_name']}</b> — {cipl_match.format_score(state)} "
             f"({cipl_match.format_overs(state)} ov){'  · ' + innings_label if innings_label else ''}"]
    bs = state["bat_stats"]
    batted = [p for p in state["batting_order"]
              if bs.get(str(p["roster_id"]), {}).get("balls", 0) > 0
              or bs.get(str(p["roster_id"]), {}).get("out")]
    for p in batted[:7]:
        lines.append(f"  {_bat_line(p, bs)}")
    # Top bowlers
    bowl = state["bowl_stats"]
    ranked = sorted(
        [p for p in state["bowl_xi"] if bowl.get(str(p["roster_id"]), {}).get("balls", 0) > 0],
        key=lambda p: bowl[str(p["roster_id"])].get("wickets", 0), reverse=True)
    if ranked:
        lines.append("🎳 " + " | ".join(
            f"{p['name']} {cipl_match._bowler_figures(bowl[str(p['roster_id'])])}"
            for p in ranked[:3]))
    return "\n".join(lines)
