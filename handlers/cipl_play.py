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

import asyncio
import html
import logging
import os
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

CIPL_TIMEOUT = int(os.getenv("CIPL_TIMEOUT_SECONDS", "90"))  # inactivity → forfeit
CIPL_REMIND = int(os.getenv("CIPL_REMIND_SECONDS", "30"))    # mention before forfeit
# Forfeiting a *live* match (failing to pick bowler / bowling or batting
# approach in time) fines the idle player and compensates the opponent.
CIPL_FORFEIT_COINS = int(os.getenv("CIPL_FORFEIT_COINS", "3000"))
CIPL_FORFEIT_GEMS = int(os.getenv("CIPL_FORFEIT_GEMS", "5"))
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


def _resolve_challenge_team_id(team_name, league_key, session):
    """Return the ChallengeTeam.id for ``team_name`` within ``league_key`` (or None)."""
    try:
        from handlers.challenge import _get_challenge_league_record
        from models import ChallengeTeam
        q = session.query(ChallengeTeam).filter(ChallengeTeam.name == team_name)
        league = _get_challenge_league_record(session, league_key)
        if league is not None:
            q = q.filter(ChallengeTeam.league_id == league.id)
        team = q.first()
        return team.id if team else None
    except Exception:
        logger.exception("cipl challenge team id resolution failed for %s", team_name)
        return None


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


def _at_mention_tg(state, tg_id, fallback="Player"):
    """Clickable '@username' mention (link text always carries the @ prefix)."""
    names = state.get("user_names") or {}
    raw = names.get(str(tg_id), fallback)
    label = raw if str(raw).startswith("@") else f"@{raw}"
    return _mention(tg_id, label)


def _team_user_mentions(state):
    """Map each CIPL team name → its captain's '@username' mention.

    Used for the 'who beat who' result line. Innings-2 state holds the chaser as
    bat_* and the defender as bowl_*, which together cover both teams."""
    return {
        state.get("bat_team_name"): _at_mention_tg(state, state.get("bat_user_tg")),
        state.get("bowl_team_name"): _at_mention_tg(state, state.get("bowl_user_tg")),
    }


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


async def _clear_action_reminder(context, state):
    """Remove the '30s left' inactivity ping once the player acts."""
    pid = state.pop("action_remind_msg_id", None)
    chat_id = state.get("chat_id")
    if pid and chat_id is not None:
        try:
            await context.bot.delete_message(chat_id, pid)
        except Exception:
            pass


async def _new_action_message(context, state, text, keyboard):
    await _clear_action_reminder(context, state)
    kb = _with_view_match(state, keyboard)
    sent = await context.bot.send_message(
        state["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    if sent and getattr(sent, "message_id", None):
        state["action_msg_id"] = sent.message_id
        state.setdefault("over_msg_ids", []).append(sent.message_id)
    return sent


async def _edit_action_message(context, state, text, keyboard):
    await _clear_action_reminder(context, state)
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
# Inactivity timer (30s reminder, then forfeit + fine)
# ════════════════════════════════════════════════════════════════════

def _cancel_timer(context, mid):
    if not getattr(context, "job_queue", None):
        return
    for name in (f"cipl_to_{mid}", f"cipl_remind_{mid}"):
        for j in context.job_queue.get_jobs_by_name(name):
            j.schedule_removal()


def _arm_timer(context, mid, expected_action):
    _cancel_timer(context, mid)
    if not getattr(context, "job_queue", None):
        return
    data = {"mid": mid, "expected": expected_action}
    if CIPL_REMIND and CIPL_REMIND < CIPL_TIMEOUT:
        context.job_queue.run_once(
            _on_remind, CIPL_REMIND, name=f"cipl_remind_{mid}", data=data)
    context.job_queue.run_once(
        _on_timeout, CIPL_TIMEOUT, name=f"cipl_to_{mid}", data=data)


def _idle_actor(state, expected):
    """Return (idle_uid, idle_tg, idle_name, win_uid, win_tg, win_name) for a
    live-match timeout, based on whose turn it is to act."""
    if expected == A_PICK_BAT_APPROACH:
        return (state.get("bat_team_id"), state.get("bat_user_tg"),
                state.get("bat_team_name", "Batting side"),
                state.get("bowl_team_id"), state.get("bowl_user_tg"),
                state.get("bowl_team_name", "Bowling side"))
    # Bowler pick or bowling-approach pick → the bowling side is on the clock.
    return (state.get("bowl_team_id"), state.get("bowl_user_tg"),
            state.get("bowl_team_name", "Bowling side"),
            state.get("bat_team_id"), state.get("bat_user_tg"),
            state.get("bat_team_name", "Batting side"))


async def _on_remind(context):
    d = context.job.data
    mid = d["mid"]
    expected = d["expected"]
    async with get_match_lock(mid):
        if get_next_action(context, mid) != expected:
            return  # already acted — no nag
        state = _gs(context, mid)
        if not state:
            return
        _, idle_tg, idle_name, _, _, _ = _idle_actor(state, expected)
        secs = max(0, CIPL_TIMEOUT - CIPL_REMIND)
        prev = state.pop("action_remind_msg_id", None)
        if prev:
            try:
                await context.bot.delete_message(state["chat_id"], prev)
            except Exception:
                pass
        try:
            sent = await context.bot.send_message(
                state["chat_id"],
                f"⏳ {_mention(idle_tg, idle_name)}, you have <b>{secs} seconds</b> "
                f"to play your turn — or you forfeit the match "
                f"(−{CIPL_FORFEIT_COINS:,} 🪙 −{CIPL_FORFEIT_GEMS} 💎).",
                parse_mode="HTML")
            state["action_remind_msg_id"] = sent.message_id
            _ss(context, mid, state)
        except Exception:
            logger.exception("cipl reminder send failed for match %s", mid)


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
            await _forfeit_live_match(context, mid, state, expected)
        except Exception:
            logger.exception("cipl timeout forfeit failed for match %s", mid)


async def _forfeit_live_match(context, mid, state, expected):
    """Idle player forfeits a live match: they're fined, the opponent wins and
    is compensated the same amount."""
    _cancel_timer(context, mid)
    (idle_uid, idle_tg, idle_name,
     win_uid, win_tg, win_name) = _idle_actor(state, expected)
    chat_id = state.get("chat_id")

    prev = state.pop("action_remind_msg_id", None)
    if prev and chat_id is not None:
        try:
            await context.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    # Remove the stale selection prompt's buttons so it can't be tapped.
    try:
        await _delete_prev_over(context, state)
    except Exception:
        pass

    from services.activity_service import log_activity
    session = get_session()
    try:
        match = session.query(Match).get(mid)
        if match and match.status not in ("completed", "abandoned"):
            match.status = "completed"
            match.completed_at = datetime.utcnow()
            match.winner_id = win_uid
            match.loser_id = idle_uid
            match.margin_type = "forfeit"
            match.margin_value = 0
        idle_user = session.query(User).get(idle_uid) if idle_uid else None
        win_user = session.query(User).get(win_uid) if win_uid else None
        if idle_user:
            charged_coins = min(idle_user.total_coins or 0, CIPL_FORFEIT_COINS)
            charged_gems = min(idle_user.total_gems or 0, CIPL_FORFEIT_GEMS)
            idle_user.total_coins = (idle_user.total_coins or 0) - charged_coins
            idle_user.total_gems = (idle_user.total_gems or 0) - charged_gems
            log_activity(session, idle_user.id, "cipl_forfeit",
                         f"Forfeited match #{mid} (inactivity): -{charged_coins} coins, -{charged_gems} gems",
                         coins_change=-charged_coins, gems_change=-charged_gems)
            if win_user:
                win_user.total_coins = (win_user.total_coins or 0) + charged_coins
                win_user.total_gems = (win_user.total_gems or 0) + charged_gems
                log_activity(session, win_user.id, "cipl_forfeit_compensation",
                             f"Opponent forfeited match #{mid}: +{charged_coins} coins, +{charged_gems} gems",
                             coins_change=charged_coins, gems_change=charged_gems)
        else:
            charged_coins = charged_gems = 0
        session.commit()
    except Exception:
        session.rollback()
        charged_coins = charged_gems = 0
        logger.exception("cipl forfeit economy failed for match %s", mid)
    finally:
        session.close()

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id,
                f"⌛ <b>Match forfeited</b>\n"
                f"{_mention(idle_tg, idle_name)} didn't play in time.\n"
                f"🏆 {_mention(win_tg, win_name)} wins!\n"
                f"⚠️ Fine: −{charged_coins:,} 🪙 −{charged_gems} 💎\n"
                f"🎁 Compensation: +{charged_coins:,} 🪙 +{charged_gems} 💎",
                parse_mode="HTML")
        except Exception:
            logger.exception("cipl forfeit announce failed for match %s", mid)

    _ss(context, mid, state, next_action=A_COMPLETED)
    cleanup_state(context, mid)
    release_match_lock(mid)


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
    if draft.get("toss_winner_side") or draft.get("coin_flipping"):
        await q.answer("Toss already done.", show_alert=True)
        return
    if q.from_user.id != draft.get("target_tg_id"):
        await q.answer("Only the guest calls the toss!", show_alert=True)
        return
    # Lock synchronously BEFORE the async coin animation. The flip plays over
    # several Telegram edits, during which the Heads/Tails buttons still look
    # tappable; without this lock a racing double-tap would spawn a second coin
    # flip and a second result keyboard for the wrong side.
    draft["coin_flipping"] = True
    try:
        await q.answer()
        from services.match_broadcast import run_coin_toss, reveal_toss_result
        coin, won = await run_coin_toss(
            lambda t: q.edit_message_text(t, parse_mode="HTML"), call)
    except Exception:
        # The flip never produced a result — release the lock so the guest can
        # call again instead of being stuck behind "Toss already done."
        draft["coin_flipping"] = False
        logger.exception("/cipl coin flip failed for draft %s", draft_id)
        await q.answer("Toss failed — call it again.", show_alert=True)
        return
    winner_side = "target" if won else "host"
    winner_tg = draft.get("target_tg_id") if won else draft.get("host_tg_id")
    winner_name = (draft.get("target") if won else draft.get("host") or {}).get("name", "Winner")
    # The reveal is the critical edit: if it fails the toss is left frozen on a
    # mid-flip frame. Retry it, and only mark the winner once it actually lands —
    # otherwise release the lock so the guest can call the toss again.
    revealed = await reveal_toss_result(lambda: q.edit_message_text(
        f"🪙 The coin lands on <b>{coin.upper()}</b> — guest called "
        f"<b>{call.upper()}</b>.\n\n"
        f"🏆 {_mention(winner_tg, winner_name)} won the toss. Choose:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First",
                                 callback_data=f"cipl_toss_bat_{draft_id}_{winner_side}"),
            InlineKeyboardButton("🎳 Bowl First",
                                 callback_data=f"cipl_toss_bowl_{draft_id}_{winner_side}"),
        ]])))
    if not revealed:
        # The animation edits already stripped the Heads/Tails keyboard, so a
        # bare alert would leave the guest with no button to retry. Clear the
        # lock and post a fresh toss-call prompt so the toss can actually resume.
        draft["coin_flipping"] = False
        logger.warning("/cipl toss reveal failed for draft %s — reprompting", draft_id)
        await q.answer("Toss hiccup — call it again below.", show_alert=True)
        target = draft.get("target") or {}
        try:
            await context.bot.send_message(
                q.message.chat_id,
                f"🪙 <b>TOSS</b>\n"
                f"{_mention(target.get('tg_id'), target.get('name') or 'Guest')}, "
                f"call the coin again:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Heads", callback_data=f"cipl_coin_heads_{draft_id}"),
                    InlineKeyboardButton("Tails", callback_data=f"cipl_coin_tails_{draft_id}"),
                ]]))
        except Exception:
            logger.exception("/cipl toss re-prompt failed for draft %s", draft_id)
        return
    draft["toss_winner_side"] = winner_side


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
    # A duplicate tap (the impatient winner double-tapping while the first tap is
    # still finalising, or tapping a stale button after the match began) must NOT
    # fire an alarming "Match already started" popup — that scary alert is exactly
    # what this fix is meant to remove. Acknowledge it with a quiet toast instead;
    # the real match is proceeding on the board below.
    if draft.get("launch_in_progress"):
        await q.answer("Starting the match…")
        return
    if draft.get("match_launched"):
        await q.answer("Match already started — play on the board below 👇")
        return
    winner_tg = draft.get("target_tg_id") if winner_side == "target" else draft.get("host_tg_id")
    if q.from_user.id != winner_tg:
        await q.answer("Toss winner only.", show_alert=True)
        return

    # Lock synchronously BEFORE the (slow) DB work that builds and commits the
    # match. Launching involves several queries + a commit, during which the
    # Bat/Bowl buttons still look tappable; without this lock an impatient winner
    # who taps again would either race a second launch or, once the first finished,
    # get the alarming "Match already started" alert even though their first tap
    # is what actually started the match.
    #
    # Use a dedicated in-progress flag rather than `match_launched`: the draft
    # expiry job (`_expire_challenge_draft`) keys off `match_launched` to decide
    # whether the chat is still owned by setup, so `match_launched` must stay
    # false until the Match row actually commits. The in-progress flag is cleared
    # in `finally`, so a failed/aborted launch can be retried.
    draft["launch_in_progress"] = True
    launch_committed = False
    # Acquire the session INSIDE the try so that if get_session() itself fails the
    # finally still runs and releases launch_in_progress — otherwise the draft
    # would be stuck rejecting every retry.
    session = None
    try:
        session = get_session()
        host = session.query(User).get(draft["host_user_id"])
        target = session.query(User).get(draft["target_user_id"])
        if not host or not target:
            await q.answer("Players no longer exist.", show_alert=True)
            return

        # Final concurrency gate at the commit point: one game per chat and one
        # match per player (any game mode). Guards against a race where either
        # side started another match while this Challenge League toss was open.
        from handlers.match import (
            _active_match_in_chat, _active_match_for_user, _active_cric_match_in_chat,
        )
        if _active_match_in_chat(session, draft["chat_id"]) \
                or _active_cric_match_in_chat(session, draft["chat_id"]):
            await q.answer("A match is already active in this chat.", show_alert=True)
            return
        if _active_match_for_user(session, host.id) \
                or _active_match_for_user(session, target.id):
            await q.answer(
                "A player is already in another active match — finish it first.",
                show_alert=True)
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

        # ── Challenge League Tournament tagging ──
        # An official tournament match is recorded against the active tournament.
        # The same two teams may meet any number of times in a tournament.
        tournament_id = None
        if draft.get("is_tournament") and draft.get("tournament_id"):
            tid = draft.get("tournament_id")
            host_cid = _resolve_challenge_team_id(host_team, league_key, session)
            target_cid = _resolve_challenge_team_id(target_team, league_key, session)
            tournament_id = tid
            draft["tournament_team_by_user"] = {host.id: host_cid, target.id: target_cid}

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
        # Conditions were generated alongside the Pitch Report at pitch selection.
        # Reuse their weather/temperature so the live match matches the report.
        conditions = draft.get("conditions") or {}
        match = Match(
            user1_id=host.id, user2_id=target.id, status="active",
            overs=overs, toss_winner_id=winner.id, toss_decision=decision,
            batting_first_id=bat_user.id, bowling_first_id=bowl_user.id,
            stadium=settings["stadium"], pitch_type=chosen_pitch,
            weather=conditions.get("weather") or settings["weather"],
            temperature=conditions.get("temperature") or settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=draft["chat_id"], created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=MATCH_EXPIRE),
            tournament_id=tournament_id,
        )
        session.add(match)
        session.commit()
        launch_committed = True
        # The live Match row now exists — only now does the chat belong to an
        # active match rather than to setup, so flip `match_launched` (the flag
        # the draft expiry job keys off) here, not before the commit.
        draft["match_launched"] = True
        match_obj = SimpleMatch(match.id, match.overs, match.stadium)
        pitch_type = match.pitch_type
    except Exception:
        if session is not None:
            session.rollback()
        logger.exception("/cipl toss/launch failed")
        await q.answer("Failed to start match.", show_alert=True)
        return
    finally:
        if session is not None:
            session.close()
        # Always release the synchronous double-tap lock. On success the match is
        # now guarded by `match_launched`; on failure (validation rejected it, a
        # player vanished, an exception, …) clearing it lets the toss winner tap
        # again instead of being stuck behind "Match already started".
        draft["launch_in_progress"] = False

    # The live Match row now exists, so the chat is guarded by the active-match
    # checks. Release the team-selection lock here (held through the toss) so the
    # hand-off is seamless and the chat isn't double-locked.
    try:
        from handlers.challenge import _release_draft_chat_lock
        _release_draft_chat_lock(context.bot_data, draft)
    except Exception:
        logger.debug("challenge draft lock release failed", exc_info=True)
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
        bat_team_emoji=bat_team_emoji, bowl_team_emoji=bowl_team_emoji,
        conditions=(draft or {}).get("conditions"))
    state["user_names"] = {
        str(bat_user.telegram_id): bat_user.username or bat_user.first_name or "Player",
        str(bowl_user.telegram_id): bowl_user.username or bowl_user.first_name or "Player",
    }
    # Carry tournament identity through the match so the result is recorded against
    # the active Challenge League Tournament when it completes.
    if draft:
        state["tournament_id"] = draft.get("tournament_id")
        state["tournament_team_by_user"] = draft.get("tournament_team_by_user") or {}
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
    # Compact live-conditions strip (weather · temp · dew) from the Pitch Report.
    cond = state.get("conditions") or {}
    cond_line = ""
    if cond.get("weather"):
        bits = [f"🌤️ {html.escape(str(cond['weather']))}"]
        if cond.get("temperature") is not None:
            bits.append(f"🌡️ {cond['temperature']}°C")
        if cond.get("dew"):
            bits.append(f"❄️ {html.escape(str(cond['dew']))} dew")
        cond_line = f"{'  ·  '.join(bits)}\n"
    return (
        f"🏆 <b>{bat_code}</b> 🆚 <b>{bowl_code}</b>\n"
        f"⚡ <b>High-Voltage IPL Battle</b> ⚡\n"
        f"{rule}\n"
        f"🏟️ {stadium} • {overs} overs\n"
        f"🌱 <b>Pitch:</b> {pitch}\n"
        f"{cond_line}"
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
    # When the front-line attack is exhausted, eligible_bowlers falls back to
    # part-time batsmen — flag them in the picker so the captain knows.
    only_part_timers = bool(elig) and all(
        cipl_match.is_part_time_bowler(p) for p in elig)
    rows, row = [], []
    for p in elig:
        tag = " 🧤" if cipl_match.is_part_time_bowler(p) else ""
        left = cipl_match.overs_left(state, p)
        row.append(InlineKeyboardButton(
            f"{p['name']} ({p.get('bowl_rating', 0)}) · {left} left{tag}",
            callback_data=f"cipl_bowler_{mid}_{p['roster_id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    part_time_note = ("\n⚠️ <i>Front-line bowlers are bowled out — only part-time "
                      "bowlers (🧤) are left.</i>" if only_part_timers else "")
    text = (f"{_approach_card(state)}\n\n"
            f"🎳 {_mention_tg(state, state['bowl_user_tg'])}, pick your bowler "
            f"for over {state['current_over']}:{part_time_note}")
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
# Resume (/rcl) — re-render the current over prompt from saved state
# ════════════════════════════════════════════════════════════════════

def is_cipl_state(state):
    """True if ``state`` belongs to a Challenge League over-by-over match."""
    return bool(state) and state.get("mode") == "cipl_approach"


def _super_over_active(context, mid):
    """True while a Super Over is running for this match.

    The main /cipl / /letsplay over-by-over flow is suspended for the duration:
    its callbacks and /rcl must refuse to act so they can't run another
    main-match over and replace the tie with a wrong result.
    """
    return bool(context.bot_data.get(f"so_{mid}"))


async def cipl_resume(context, mid, state=None):
    """Re-render the current Challenge League prompt from saved state.

    A Challenge League match drives itself through three picks per over
    (bowler → bowling approach → batting approach). If the flow stalls midway —
    a dropped button, a transient send failure — this re-sends the prompt for
    whichever pick is outstanding so the match continues from exactly where it
    left off. It NEVER falls through to the regular-match delivery renderer
    (which would spam "Couldn't show delivery buttons. Retrying automatically…").

    Returns True if a prompt was re-sent, False otherwise.
    """
    # Hold the per-match lock so a resume can't interleave with a captain
    # callback or the inactivity timer (both of which lock) and rewind the flow
    # to an older action. Re-read state under the lock for the same reason.
    async with get_match_lock(mid):
        state = _gs(context, mid)
        if not is_cipl_state(state):
            return False
        # While a Super Over is live the main over-by-over flow is suspended —
        # never re-prompt it (that could run another main-match over after the
        # regulation overs and overwrite the tie with a wrong result).
        if _super_over_active(context, mid):
            return False
        action = get_next_action(context, mid)
        if action == A_COMPLETED:
            return False
        try:
            # Force a fresh action message so the buttons reappear even if the
            # previous prompt message was deleted or is no longer editable.
            state["action_msg_id"] = None
            if action == A_PICK_BOWL_APPROACH and state.get("current_bowler"):
                await _prompt_bowl_approach(context, mid, state)
            elif action == A_PICK_BAT_APPROACH and state.get("current_bowler"):
                await _prompt_bat_approach(context, mid, state)
            else:
                # A_PICK_CIPL_BOWLER, an unknown action, or a missing bowler all
                # resume cleanly from the start of the over (bowler selection).
                await _prompt_bowler(context, mid, state, first=True)
            return True
        except Exception:
            logger.exception("cipl_resume failed for match %s", mid)
            return False


async def rcl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rcl — resume a stuck Challenge League (/cipl) match in this chat."""
    chat = update.effective_chat
    if chat is None:
        return
    cid = chat.id

    found_mid, found_state = _find_cipl_match_in_chat(context, cid)
    if found_mid is None:
        await update.message.reply_text(
            "❌ No active Challenge League or Lets Play match in this chat to "
            "resume.\nStart one with /cipl or /letsplay.")
        return

    if _super_over_active(context, found_mid):
        # The main match is over and a Super Over is live — re-render its prompt
        # (selection or current ball) just like /resume, instead of touching the
        # suspended main flow.
        from handlers.super_over import resume_super_over
        await update.message.reply_text(
            "🔄 <b>Resuming Super Over…</b>", parse_mode="HTML")
        ok = await resume_super_over(context, found_mid)
        if not ok:
            await update.message.reply_text(
                "⚠️ Couldn't re-show the Super Over right now. Please try again, "
                "or ask an admin to /removematch you if it stays stuck.")
        return

    # Label the resume after whichever mode this match is (Lets Play vs cipl).
    _label = "Lets Play" if found_state.get("is_letsplay") else "Challenge League"

    # Only the two captains in this match may resume it — otherwise any group
    # member could reset another match's prompt/timer flow.
    requester = update.effective_user.id if update.effective_user else None
    captains = {found_state.get("bat_user_tg"), found_state.get("bowl_user_tg")}
    if requester not in captains:
        await update.message.reply_text(
            "❌ Only the two captains in this match can use /rcl to resume it.")
        return

    await update.message.reply_text(
        f"🔄 <b>Resuming {_label} match…</b>", parse_mode="HTML")
    ok = await cipl_resume(context, found_mid, found_state)
    if not ok:
        await update.message.reply_text(
            "⚠️ Couldn't resume — the match may have already finished.\n"
            "If it stays stuck, ask an admin to /removematch you.")


def _find_cipl_match_in_chat(context, cid):
    """Return (match_id, state) for a live Challenge League match in ``cid``.

    Checks the in-memory state cache first (picking the most recent match by id),
    then falls back to the DB so a match that survived a restart can still be
    resumed. Returns (None, None) if none.
    """
    best_mid, best_state = None, None
    for k, v in list(context.bot_data.items()):
        if (isinstance(k, str) and k.startswith("ms_")
                and isinstance(v, dict)
                and v.get("chat_id") == cid
                and is_cipl_state(v)):
            try:
                kid = int(k.split("_", 1)[1])
            except (ValueError, IndexError):
                continue
            if best_mid is None or kid > best_mid:
                best_mid, best_state = kid, v
    if best_mid is not None:
        return best_mid, best_state
    # DB fallback — find unfinished matches in this chat and rehydrate state.
    session = get_session()
    try:
        rows = (session.query(Match)
                .filter(Match.chat_id == cid,
                        Match.status.in_(("playing", "active", "toss", "selecting")))
                .order_by(Match.id.desc())
                .all())
        mids = [m.id for m in rows]
    except Exception:
        logger.exception("cipl resume DB lookup failed for chat %s", cid)
        mids = []
    finally:
        session.close()
    for mid in mids:
        state = _gs(context, mid)
        if is_cipl_state(state):
            return mid, state
    return None, None


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
        if _super_over_active(context, mid):
            await q.answer("🔥 Super Over in progress — the main match is over.",
                           show_alert=True)
            return
        if q.from_user.id != state["bowl_user_tg"]:
            await q.answer("Only the bowling captain picks the bowler.", show_alert=True)
            return
        if get_next_action(context, mid) != A_PICK_CIPL_BOWLER:
            await q.answer("Bowler already chosen.", show_alert=True)
            return
        # Enforce eligibility server-side too: a stale button (or tampered
        # callback data) must not bypass the quota / no-back-to-back / part-time
        # rules the picker applies.
        allowed = {p["roster_id"] for p in cipl_match.eligible_bowlers(state)}
        if rid not in allowed:
            await q.answer("That bowler isn't eligible for this over.", show_alert=True)
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
        if _super_over_active(context, mid):
            await q.answer("🔥 Super Over in progress — the main match is over.",
                           show_alert=True)
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
        if _super_over_active(context, mid):
            await q.answer("🔥 Super Over in progress — the main match is over.",
                           show_alert=True)
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
    # Reveal any player traits that fired this over (/letsplay only — Challenge
    # League players carry no traits, so these lists are always empty there).
    ta = summary.get("traits_activated") or {}
    bowl_t = ta.get("bowl") or []
    bat_t = ta.get("bat") or []
    if bowl_t or bat_t:
        lines.append("")
        if bowl_t:
            lines.append("🎳 Traits: " + ", ".join(html.escape(t) for t in bowl_t))
        if bat_t:
            lines.append("🏏 Traits: " + ", ".join(html.escape(t) for t in bat_t))
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
    "new_bowler": "🎳", "returning_bowler": "🎳", "new_batsman": "🏏",
}

# Card-type commentary entries the Mini App renders as rich cards. The chat
# already posts its own end-of-over summary message, so these are skipped in the
# expandable per-over commentary block to avoid duplicate / empty lines.
_CMT_SKIP_IN_BLOCK = {"wicket", "end_of_over", "over_complete"}


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
        # a paired "wicket" summary card; rich Mini App cards (end_of_over /
        # over_complete) are handled by the chat's own summary message. Skip all
        # of these so the Telegram block keeps one clean line per event.
        if etype in _CMT_SKIP_IN_BLOCK:
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
    # A tied match triggers a Super Over (interactive, user-vs-user, ball by
    # ball) for /cipl, /c[league] and /letsplay — they all reach here. The
    # Match row stays 'active' until the Super Over decides a winner.
    if result["tie"]:
        try:
            # Mention the Super Over in the Mini App commentary feed so spectators
            # know the tie is being resolved (the chat already announces it).
            cipl_match._push_card(state, {
                "type": "new_bowler",
                "text": (f"🤝 Scores level at {cipl_match.format_score(state)} — "
                         f"it's a SUPER OVER! 🔥"),
            })
            _ss(context, mid, state)
            from handlers.super_over import start_super_over
            if await start_super_over(context, mid, state):
                return
        except Exception:
            logger.exception("Super Over kickoff failed for match %s — "
                             "falling back to a tied result", mid)
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

        # Record the official tournament result (standings + per-player stats).
        # No-op for casual Challenge League matches (no tournament_id in state).
        # A tie reaching here did NOT go to a Super Over, so it is recorded as a tie.
        try:
            if state.get("tournament_id"):
                from services import tournament_service
                tournament_service.record_tournament_match(session, state)
        except Exception:
            logger.exception("tournament match recording failed for %s", mid)

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("cipl match finalization failed for match %s", mid)
    finally:
        session.close()

    # "Who beat who" line uses clickable @user mentions alongside the team
    # names: @winner (Team) beat @loser (Team).
    mentions = _team_user_mentions(state)
    if result["tie"]:
        result_line = "🤝 <b>Match Tied!</b>"
    else:
        win_m = mentions.get(result["winner"], f"<b>{result['winner']}</b>")
        lose_m = mentions.get(result["loser"], result["loser"])
        result_line = (f"🏆 {win_m} ({result['winner']}) beat "
                       f"{lose_m} ({result['loser']}) "
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
        # Pillow rendering is CPU-bound and synchronous — run it off the event
        # loop so finishing one Challenge League match doesn't freeze every
        # other user's buttons (and other live matches) while the card renders.
        img = await asyncio.to_thread(_build_cipl_summary_image, state, result)
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
