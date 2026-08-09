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
import re
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
from handlers.match import BOT_TG_ID_, _mention

logger = logging.getLogger(__name__)

CIPL_TIMEOUT = int(os.getenv("CIPL_TIMEOUT_SECONDS", "120"))  # 2-min turn → forfeit
CIPL_REMIND = int(os.getenv("CIPL_REMIND_SECONDS", "90"))     # warn 30s before forfeit
# Forfeiting a *live* match (failing to pick bowler / bowling or batting
# approach in time) fines the idle player. The fine is burned, not paid to the
# opponent — see _forfeit_live_match.
CIPL_FORFEIT_COINS = int(os.getenv("CIPL_FORFEIT_COINS", "3000"))
CIPL_FORFEIT_GEMS = int(os.getenv("CIPL_FORFEIT_GEMS", "5"))
CIPL_OVERS = 20    # Challenge League / League Battle matches are always 20 overs

# Pause before the AI captain (/lpbot, /ciplbot) reveals a decision, so its
# moves read as moves rather than as the board teleporting.
BOT_THINK_DELAY = float(os.getenv("BOT_THINK_DELAY_SECONDS", "1.6"))

# Anti-blowout: drafts can produce lopsided squads, and the shared rating
# engine amplifies gaps into one-sided results. Before a match starts we pull
# each side's effective ratings a fraction of the way toward the two-team
# midpoint — shrinking the strength gap by this factor while preserving each
# squad's internal shape. 0.0 = off (raw gap), 1.0 = both teams equalised.
def _env_float(name, default, lo, hi):
    """Parse a float env var, falling back to ``default`` on anything malformed
    or non-finite, then clamp into ``[lo, hi]``. Keeps a bad env value from
    crashing import or forcing extreme gameplay."""
    import math
    try:
        v = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return max(lo, min(hi, v))


CIPL_GAP_COMPRESSION = _env_float("CIPL_GAP_COMPRESSION", 0.4, 0.0, 1.0)

# Dead zone, in rating points of squad-average gap. Below this the two XIs are
# already an even contest, so compressing them buys no fairness at all — it only
# rounds every card up or down by one and makes an 87 show as 86 for one captain
# and 88 for the other. Anything at or under this gap now plays at its true
# ratings. (At the default 0.4 factor, a 5-point gap is the first that shifts a
# card by a full point, so 5.0 is also the smallest threshold that changes
# nothing about how genuinely lopsided ties are handled.)
CIPL_GAP_DEADZONE = _env_float("CIPL_GAP_DEADZONE", 5.0, 0.0, 100.0)

# Engine rating keys the balancing pass may touch. Each has a ``card_<key>``
# twin written by services.cipl_match.cp_to_player_dict that is never modified —
# that twin is what the UI reads (services.cipl_match.display_rating).
_RATING_KEYS = ("rating", "bat_rating", "bowl_rating")


def _compress_team_gap(team_a, team_b, factor=CIPL_GAP_COMPRESSION,
                       deadzone=CIPL_GAP_DEADZONE):
    """Nudge two XIs' effective ratings toward each other by ``factor``.

    Mutates the player dicts in place (``rating``/``bat_rating``/``bowl_rating``)
    by a single per-team shift, so the weaker side is lifted and the stronger
    side eased down while every player keeps their relative standing within the
    squad. Deterministic and idempotent across both innings because the team
    means are computed from the same players each time.

    Two things this deliberately does NOT do:

    * It never touches the ``card_*`` keys, so the ratings shown in the dugout,
      the bowler picker and the scorecard stay the numbers on the squad sheet.
      Balancing is a simulation weight, not a re-rating of anybody's players.
    * It does nothing at all when the two squad averages are within ``deadzone``
      of each other — an even tie is left completely alone rather than being
      "balanced" by a rounding step in each direction.

    Returns the applied per-team shift (0.0 when the pass was skipped), which
    callers can record on the match state for display.
    """
    if factor <= 0 or not team_a or not team_b:
        return 0.0

    def _mean(team):
        vals = [float(p.get("card_rating", p.get("rating")) or 0) for p in team]
        return sum(vals) / len(vals) if vals else 0.0

    mean_a, mean_b = _mean(team_a), _mean(team_b)
    if abs(mean_a - mean_b) <= deadzone:
        return 0.0
    midpoint = (mean_a + mean_b) / 2.0

    def _shift(team, team_mean):
        delta = factor * (midpoint - team_mean)
        if abs(delta) < 1e-9:
            return
        for p in team:
            for key in _RATING_KEYS:
                # Always shift from the untouched card value, so re-running this
                # on an already-compressed XI is a no-op rather than a second
                # nudge on top of the first.
                base = p.get("card_" + key, p.get(key))
                if base is not None:
                    p[key] = int(round(max(1, min(100, float(base) + delta))))

    _shift(team_a, mean_a)
    _shift(team_b, mean_b)
    return abs(factor * (midpoint - mean_a))


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

# get_state / save_state keep the per-match snapshot in ctx.bot_data, which is
# owned by the event-loop thread and read/iterated by other handlers there
# (e.g. _find_cipl_match_in_chat, cleanup_state). They must therefore run ON the
# loop — offloading them to a worker thread would mutate bot_data off-thread and
# can race those iterations (RuntimeError: dictionary changed size during
# iteration). They stay async so call sites await them uniformly; the heavy,
# bot_data-free work (the over simulation, the match-finalisation DB batch, the
# cold-path DB reads in the middleware) is what actually moves off-loop.
async def _gs(ctx, mid):
    return _gs_store(ctx, mid)


async def _ss(ctx, mid, s, next_action=None, last_prompt_msg_id=None):
    _ss_store(ctx, mid, s, next_action=next_action,
              last_prompt_msg_id=last_prompt_msg_id)


async def _get_next_action(ctx, mid):
    """Off-loop wrapper for the blocking next_action read. Safe to offload:
    get_next_action only touches the lock-guarded shared cache and the DB — it
    never writes ctx.bot_data."""
    return await asyncio.to_thread(get_next_action, ctx, mid)


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


# ════════════════════════════════════════════════════════════════════
# Bot matches (/lpbot, /ciplbot) — unranked practice vs the AI captain
# ════════════════════════════════════════════════════════════════════

def mark_bot_match(state, bot_user_id, difficulty=None):
    """Tag a freshly built state as an unranked practice match vs the bot.

    ``stats_disabled`` is the existing gate every reward/stat path already
    respects — ``player_stats_service.persist_player_game_stats`` bails on it,
    the POTM career credit is skipped, and ``award_match_rewards_core`` is called
    with ``count_result=False`` (no coins, gems, Win/Loss, streak, season points
    or active day). ``is_bot_match`` is what drives the AI's turns and the
    no-penalty timeout; ``unranked`` is a plain label for the UI.

    The AI captain also fixes its two settings for the match here, before the
    first ball rather than mid-over, so both can be shown on the match card: the
    captaincy *style* it drew (``services.bot_captain.PERSONAS``) and the
    *difficulty* the player chose at the difficulty prompt.
    """
    state["is_bot_match"] = True
    state["bot_user_id"] = bot_user_id
    state["stats_disabled"] = True
    state["unranked"] = True
    try:
        from services.bot_captain import assign_difficulty, assign_persona
        assign_persona(state)
        assign_difficulty(state, difficulty)
    except Exception:
        logger.exception("bot captain: persona/difficulty draw failed for match %s",
                         state.get("match_id"))
    # A practice match must never touch a tournament table or a tour series.
    state["tournament_id"] = None
    state["tournament_team_by_user"] = {}
    state["reserved_fixture_id"] = None
    state["cl_tour_id"] = None
    state["cl_tour_match_id"] = None
    return state


def _is_bot_match(state):
    return bool(state) and bool(state.get("is_bot_match"))


BOT_TEAM_SUFFIX = "(Bot)"


def bot_team_name(state):
    """The team name the AI captain is playing as, or ``None``.

    In a league bot match the bot fields a real franchise, so "RCB" on the
    scorecard is indistinguishable from a human playing RCB. Resolving which
    side the bot owns is what lets every card say so.
    """
    if not _is_bot_match(state):
        return None
    bot_uid = state.get("bot_user_id")
    if bot_uid is None:
        return None
    if state.get("bat_team_id") == bot_uid:
        return state.get("bat_team_name")
    if state.get("bowl_team_id") == bot_uid:
        return state.get("bowl_team_name")
    return None


def with_bot_tag(state, name):
    """``"RCB"`` → ``"RCB (Bot)"`` when that team is the AI captain's.

    Display only — the undecorated name stays the key for POTM, results and
    anything else that matches teams by name.
    """
    bot_name = bot_team_name(state)
    if not name or not bot_name or str(name) != str(bot_name):
        return name
    if str(name).endswith(BOT_TEAM_SUFFIX):
        return name
    return f"{name} {BOT_TEAM_SUFFIX}"


def _bot_bowls(state):
    """True when the AI captain is the one who must pick this over's bowler."""
    return (_is_bot_match(state)
            and state.get("bot_user_id") is not None
            and state.get("bowl_team_id") == state.get("bot_user_id"))


def _bot_bats(state):
    """True when the AI captain is the one who must pick the batting approach."""
    return (_is_bot_match(state)
            and state.get("bot_user_id") is not None
            and state.get("bat_team_id") == state.get("bot_user_id"))


UNRANKED_NOTE = ("🎯 <i>Practice match — unranked. No career stats, coins, gems, "
                 "Win/Loss or streak.</i>")


def _rematch_row(state):
    """A one-tap Rematch button for a finished practice match.

    The callback carries the mode and, for a league match, the league — so the
    rematch reopens the same competition instead of silently dropping to IPL.
    """
    if state.get("is_letsplay"):
        data = "botmatch_again_lp"
    else:
        league = str(state.get("league_key") or "")[:16]
        data = f"botmatch_again_cipl_{league}" if league else "botmatch_again_cipl"
    return [[InlineKeyboardButton("🔁 Rematch the Bot", callback_data=data)]]


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
    # Remove any previous action/picker message BEFORE sending the new one. A
    # re-prompt (a /rcl resume, or the edit-in-place fallback in
    # _edit_action_message) must never leave two live pickers — each showing a
    # different scorecard and both still tappable — in the chat at once.
    prev = state.get("action_msg_id")
    if prev:
        try:
            await context.bot.delete_message(state["chat_id"], prev)
        except Exception:
            pass
        try:
            (state.get("over_msg_ids") or []).remove(prev)
        except ValueError:
            pass
        state["action_msg_id"] = None
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

    Returns a plain ``list`` of rows, never the ``InlineKeyboardMarkup`` tuple:
    callers append their own rows to it (the Rematch button on a finished
    practice match), and ``tuple + list`` raises.
    """
    try:
        from services.match_broadcast import play_match_keyboard
        kb = play_match_keyboard(
            state["match_id"], chat_id=state.get("chat_id"),
            is_private=bool(state.get("is_private")), label="📊 View Match")
        return [list(row) for row in kb.inline_keyboard] if kb else None
    except Exception:
        logger.exception("cipl view-match button build failed")
        return None


# ════════════════════════════════════════════════════════════════════
# Inactivity timer (5-min turn, reminder shortly before forfeit + fine)
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
        if await _get_next_action(context, mid) != expected:
            return  # already acted — no nag
        state = await _gs(context, mid)
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
        # A practice match against the bot costs nothing to walk away from, so
        # don't threaten a fine that will never be charged.
        if _is_bot_match(state):
            nag = (f"⏳ {_mention(idle_tg, idle_name)}, you have "
                   f"<b>{secs} seconds</b> to play your turn — or the practice "
                   f"match closes. No fine, it's just practice.")
        else:
            nag = (f"⏳ {_mention(idle_tg, idle_name)}, you have "
                   f"<b>{secs} seconds</b> to play your turn — or you forfeit "
                   f"the match (−{CIPL_FORFEIT_COINS:,} 🪙 "
                   f"−{CIPL_FORFEIT_GEMS} 💎).")
        try:
            sent = await context.bot.send_message(
                state["chat_id"], nag, parse_mode="HTML")
            state["action_remind_msg_id"] = sent.message_id
            await _ss(context, mid, state)
        except Exception:
            logger.exception("cipl reminder send failed for match %s", mid)


async def _on_timeout(context):
    d = context.job.data
    mid = d["mid"]
    expected = d["expected"]
    async with get_match_lock(mid):
        if await _get_next_action(context, mid) != expected:
            return  # the user already acted
        state = await _gs(context, mid)
        if not state:
            return
        try:
            if _is_bot_match(state):
                await _close_idle_bot_match(context, mid, state)
            else:
                await _forfeit_live_match(context, mid, state, expected)
        except Exception:
            logger.exception("cipl timeout forfeit failed for match %s", mid)


async def _close_idle_bot_match(context, mid, state):
    """Retire an abandoned practice match — no forfeit, no fine, no winner.

    There is no human opponent and nothing was ever at stake, so an idle
    /lpbot or /ciplbot match is simply packed away and the chat freed.
    """
    _cancel_timer(context, mid)
    chat_id = state.get("chat_id")

    prev = state.pop("action_remind_msg_id", None)
    if prev and chat_id is not None:
        try:
            await context.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    try:
        await _delete_prev_over(context, state)
    except Exception:
        pass

    session = get_session()
    try:
        match = session.query(Match).get(mid)
        if match and match.status not in ("completed", "abandoned"):
            match.status = "abandoned"
            match.completed_at = datetime.utcnow()
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("closing idle bot match %s failed", mid)
    finally:
        session.close()

    pinned = state.get("pinned_msg_id")
    if pinned and chat_id is not None:
        try:
            await context.bot.unpin_chat_message(chat_id, pinned)
        except Exception:
            pass

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id,
                "🤖 <b>Practice match closed</b>\n"
                "Nobody played a turn in time, so the bot has packed up. "
                "Nothing was lost — no fine, no stats.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(_rematch_row(state)))
        except Exception:
            logger.exception("bot practice-match close notice failed for %s", mid)

    await _ss(context, mid, state, next_action=A_COMPLETED)
    cleanup_state(context, mid)
    release_match_lock(mid)


async def _forfeit_live_match(context, mid, state, expected):
    """Idle player forfeits a live match: they're fined and the opponent wins.

    The fine is burned rather than handed to the winner. Paying it across
    accounts made an idle "opponent" worth farming — sit out on an alt, collect
    on the main — so the win is the reward and the fine leaves the economy.
    """
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
        # CL Tour: a forfeit decides the match, so record it for the series too
        # (otherwise the slot stays 'playing' and the tour stalls).
        if isinstance(state, dict) and state.get("cl_tour_match_id") and win_uid:
            try:
                from services.cl_tour_service import record_cl_match_result
                # Savepoint-isolate this best-effort write: if its flush fails it
                # must not leave the session inactive and break the main commit.
                with session.begin_nested():
                    record_cl_match_result(session, state["cl_tour_match_id"], win_uid)
            except Exception:
                logger.exception("CL tour forfeit recording failed for %s", mid)
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
                log_activity(session, win_user.id, "cipl_forfeit_win",
                             f"Opponent forfeited match #{mid} — win by forfeit",
                             coins_change=0, gems_change=0)
        else:
            charged_coins = charged_gems = 0
        # A forfeit doesn't record a tournament result, so free the reserved
        # fixture (live → scheduled) — otherwise that pairing could never replay.
        rfx = state.get("reserved_fixture_id") if isinstance(state, dict) else None
        if rfx:
            try:
                from services import league_schedule_service
                league_schedule_service.release_fixture(session, rfx)
            except Exception:
                logger.exception("releasing reserved fixture failed for match %s", mid)
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
                f"⚠️ Fine: −{charged_coins:,} 🪙 −{charged_gems} 💎",
                parse_mode="HTML")
        except Exception:
            logger.exception("cipl forfeit announce failed for match %s", mid)

    await _ss(context, mid, state, next_action=A_COMPLETED)
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
    # The guest calls the toss — except vs the bot, where the guest IS the bot,
    # so the caller is the (only) human in the match.
    vs_bot = bool(draft.get("vs_bot"))
    caller_tg = draft.get("host_tg_id") if vs_bot else draft.get("target_tg_id")
    if q.from_user.id != caller_tg:
        await q.answer(
            "Only you can call this toss!" if vs_bot
            else "Only the guest calls the toss!", show_alert=True)
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
    # ``won`` is from the CALLER's point of view, and the caller is the guest in
    # a normal match but the host vs the bot.
    caller_side = "host" if vs_bot else "target"
    other_side = "target" if vs_bot else "host"
    winner_side = caller_side if won else other_side
    winner_tg = draft.get(f"{winner_side}_tg_id")
    winner_name = (draft.get(winner_side) or {}).get("name", "Winner")
    caller_label = "you" if vs_bot else "guest"

    # Vs the bot, a bot toss win is decided right here — it elects bat or bowl
    # at random and the match launches. No keyboard is posted, so there is
    # nothing stale for the human to tap.
    bot_won = vs_bot and winner_side == "target"
    if bot_won:
        from services import bot_captain
        decision = bot_captain.elect_toss_decision()
        revealed = await reveal_toss_result(lambda: q.edit_message_text(
            f"🪙 The coin lands on <b>{coin.upper()}</b> — you called "
            f"<b>{call.upper()}</b>.\n\n"
            f"🤖 <b>Bot</b> won the toss and elected to "
            f"<b>{'BAT' if decision == 'bat' else 'BOWL'}</b> first.",
            parse_mode="HTML"))
        if revealed:
            draft["toss_winner_side"] = winner_side
            await _launch_after_toss(context, q, draft, draft_id,
                                     decision, winner_side)
            return
    else:
        # The reveal is the critical edit: if it fails the toss is left frozen on
        # a mid-flip frame. Retry it, and only mark the winner once it actually
        # lands — otherwise release the lock so the caller can try again.
        revealed = await reveal_toss_result(lambda: q.edit_message_text(
            f"🪙 The coin lands on <b>{coin.upper()}</b> — {caller_label} called "
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
        caller = draft.get(caller_side) or {}
        try:
            await context.bot.send_message(
                q.message.chat_id,
                f"🪙 <b>TOSS</b>\n"
                f"{_mention(caller.get('tg_id'), caller.get('name') or 'Player')}, "
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
    winner_tg = draft.get("target_tg_id") if winner_side == "target" else draft.get("host_tg_id")
    if draft.get("match_launched"):
        # `match_launched` only proves a Match row was COMMITTED — not that the
        # board ever reached the chat. Everything after that commit (the toss
        # edit, the state write, the first bowler prompt) can still fail, and
        # when it did the old code answered "Match already started" forever on a
        # match nobody could play. Check the live state before claiming it
        # started, and heal whichever half is broken.
        state = await _live_cipl_state(context, draft)
        if state is not None:
            await q.answer("Match already started — play on the board below 👇")
            # No action message means the prompt never landed (or was deleted):
            # put the board back in front of them instead of leaving them
            # tapping a dead toss message.
            if not state.get("action_msg_id"):
                await cipl_resume(context, draft["match_id"])
            return
        # Committed but never playable. Rolling that back writes to the database,
        # so it is the toss winner's tap that does it — anyone else in the chat
        # gets the same reassuring toast they'd get mid-launch.
        if q.from_user.id != winner_tg:
            await q.answer("Starting the match…")
            return
        # Clear the wreckage — the cancelled row also frees the per-chat and
        # per-player active-match gates — so the launch below can proceed.
        await _abandon_failed_launch(context, draft)
    if q.from_user.id != winner_tg:
        await q.answer("Toss winner only.", show_alert=True)
        return
    await _launch_after_toss(context, q, draft, draft_id, decision, winner_side)


async def _live_cipl_state(context, draft):
    """The saved state of this draft's launched match, or None if it isn't playable.

    "Playable" means a Challenge League state exists for the committed match id
    and the match has not already finished. Used to tell a genuine double-tap
    ("the match IS running, look below") apart from a launch that committed a
    Match row and then died before the board appeared.
    """
    mid = draft.get("match_id")
    if not mid:
        return None
    try:
        state = await _gs(context, mid)
        if not is_cipl_state(state):
            return None
        if await _get_next_action(context, mid) == A_COMPLETED:
            return None
        return state
    except Exception:
        logger.exception("cipl launch health check failed for match %s", mid)
        return None


def _cancel_orphan_match_row(mid):
    """Blocking: cancel a Match that committed but never became playable.

    Also releases anything the launch reserved on the way in — a tournament
    fixture (live → scheduled) and a CL Tour slot (playing → pending) — so the
    same pairing can be replayed instead of being burned by a launch that never
    produced a single ball.
    """
    session = get_session()
    try:
        match = session.query(Match).get(mid)
        if not match or match.status not in ("active", "playing", "in_progress"):
            return
        match.status = "cancelled"
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("cancelling orphan cipl match %s failed", mid)
    finally:
        session.close()


def _release_launch_reservations(draft):
    """Blocking: hand back the fixture / tour slot a failed launch reserved."""
    fixture_id = draft.get("reserved_fixture_id")
    tour_match_id = draft.get("cl_tour_match_id")
    if not fixture_id and not tour_match_id:
        return
    session = get_session()
    try:
        if fixture_id:
            from services import league_schedule_service
            league_schedule_service.release_fixture(session, fixture_id)
        if tour_match_id:
            from services.cl_tour_service import unlink_match_from_cl_tour
            unlink_match_from_cl_tour(session, tour_match_id)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("releasing cipl launch reservations failed")
    finally:
        session.close()


async def _abandon_failed_launch(context, draft):
    """Undo a launch that committed a Match row but never reached the board.

    Leaves the draft in the state it was in just before the winner tapped, so
    the very next Bat/Bowl tap replays the launch cleanly: the Match row is
    cancelled (freeing the per-chat and per-player active-match gates that would
    otherwise reject the retry with "A match is already active in this chat"),
    the half-written match state is dropped, and the reservations are handed
    back.
    """
    mid = draft.get("match_id")
    if mid:
        try:
            cleanup_state(context, mid)
            release_match_lock(mid)
        except Exception:
            logger.exception("clearing dead cipl state failed for match %s", mid)
        await asyncio.to_thread(_cancel_orphan_match_row, mid)
    await asyncio.to_thread(_release_launch_reservations, draft)
    draft["match_id"] = None
    draft["match_launched"] = False
    draft["launch_in_progress"] = False
    draft.pop("reserved_fixture_id", None)


async def _launch_after_toss(context, q, draft, draft_id, decision, winner_side):
    """Create the Match row and hand off to the over-by-over flow.

    Shared by the human "Bat First / Bowl First" tap and the bot's automatic
    election vs /ciplbot, so both routes get the same concurrency gates, the same
    double-tap guards and the same launch.
    """
    # Guard the bot path too: it reaches here without going through the button
    # checks above, and a hiccup-retried toss must not launch twice.
    if draft.get("launch_in_progress") or draft.get("match_launched"):
        return

    async def _fail(msg):
        """Tell the player why the launch didn't happen.

        The human Bat/Bowl route reaches here with the callback query still
        unanswered, so an alert is the right surface. The bot-elects route was
        already answered back in ``cipl_coin_callback``, where a second answer is
        rejected — without the fallback those failures would be silent and leave
        a dead toss message on screen.
        """
        try:
            await q.answer(msg, show_alert=True)
            return
        except Exception:
            logger.debug("toss failure alert rejected (query already answered)",
                         exc_info=True)
        try:
            await context.bot.send_message(draft["chat_id"], f"⚠️ {msg}")
        except Exception:
            logger.exception("toss failure notice failed for draft %s", draft_id)

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
            await _fail("Players no longer exist.")
            return

        # Final concurrency gate at the commit point: one game per chat and one
        # match per player (any game mode). Guards against a race where either
        # side started another match while this Challenge League toss was open.
        from handlers.match import (
            _active_match_in_chat, _active_match_for_user, _active_cric_match_in_chat,
        )
        if _active_match_in_chat(session, draft["chat_id"]) \
                or _active_cric_match_in_chat(session, draft["chat_id"]):
            await _fail("A match is already active in this chat.")
            return
        # The bot opponent is a single shared User row that is a participant in
        # every concurrent practice match, so checking it here would block the
        # second one. Only the humans are gated.
        human_ids = [host.id] if draft.get("vs_bot") else [host.id, target.id]
        if any(_active_match_for_user(session, uid) for uid in human_ids):
            await _fail(
                "A player is already in another active match — finish it first.")
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
            # For a fixture-gated tournament, atomically reserve this pair's open
            # fixture (scheduled → live) so a concurrent draft in another chat can't
            # play the same fixture. find_open_fixture later fills the 'live' row.
            from models import Tournament as _Tourn
            from services import league_schedule_service as _lss
            _tour = session.query(_Tourn).get(int(tid))
            if _tour and (_tour.schedule_generated or _tour.knockout_generated):
                _fx = _lss.reserve_fixture_by_names(session, tid, host_team, target_team)
                if not _fx:
                    await _fail(
                        "That fixture has already been taken or played. Restart and "
                        "pick a scheduled opponent.")
                    return
                draft["reserved_fixture_id"] = _fx.id

        bat_side = "host" if bat_is_host else "target"
        bowl_side = "target" if bat_is_host else "host"
        bat_xi = build_xi_from_draft(session, draft, bat_side)
        bowl_xi = build_xi_from_draft(session, draft, bowl_side)
        if len(bat_xi) < 2 or len(bowl_xi) < 2:
            await _fail("Playing XI missing — restart the challenge.")
            return
        # Balance the tie: compress the two squads' rating gap so a lopsided
        # draft doesn't turn into a blowout (applied once; carried into both
        # innings via the stored XIs). Card ratings are left untouched — only
        # the engine's working numbers move.
        gap_shift = _compress_team_gap(bat_xi, bowl_xi)
        if gap_shift:
            logger.info("/cipl balancing applied ±%.2f to the engine ratings of "
                        "match %s; card ratings unchanged", gap_shift, draft_id)

        # Capture all primitives BEFORE the session closes (avoid detached ORM).
        bat_info = SimpleUser(bat_user.id, bat_user.telegram_id,
                              bat_user.username, bat_user.first_name)
        bowl_info = SimpleUser(bowl_user.id, bowl_user.telegram_id,
                               bowl_user.username, bowl_user.first_name)
        winner_tg_val = winner.telegram_id

        # Challenge League is 20 units — either 20 overs (T20) or 20 sets of 5
        # balls (The Hundred). The per-league format is cached on the draft at
        # XI selection; default to the over-based format if it is missing.
        overs = CIPL_OVERS
        ball_format = draft.get("ball_format", "T20")
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
        # CL Tour: bind this Match to its series slot (marks the slot 'playing')
        # in the SAME transaction as match creation — flush to get match.id, link,
        # then commit once, so the Match and its tour slot can never diverge.
        session.flush()
        if draft.get("cl_tour_match_id"):
            from services.cl_tour_service import link_match_to_cl_tour
            if link_match_to_cl_tour(session, draft["cl_tour_match_id"], match.id) is None:
                raise RuntimeError(
                    f"CL tour match {draft['cl_tour_match_id']} not found")
        session.commit()
        launch_committed = True
        # The live Match row now exists — only now does the chat belong to an
        # active match rather than to setup, so flip `match_launched` (the flag
        # the draft expiry job keys off) here, not before the commit.
        draft["match_launched"] = True
        # Remembered so a later tap (and the failure path below) can tell whether
        # the launch actually produced a playable board — see _live_cipl_state.
        draft["match_id"] = match.id
        match_obj = SimpleMatch(match.id, match.overs, match.stadium)
        pitch_type = match.pitch_type
    except Exception:
        if session is not None:
            session.rollback()
        logger.exception("/cipl toss/launch failed")
        await _fail("Failed to start match.")
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
    try:
        # The bot-elects path already answered this query during the coin flip,
        # and a second answer on the same query is rejected.
        await q.answer()
    except Exception:
        logger.debug("toss callback answer failed (already answered)", exc_info=True)
    winner_name = (draft.get(winner_side) or {}).get("name", "Winner")
    fmt_label = "The Hundred (100 balls)" if ball_format == "The100" else f"{match_obj.overs} overs"
    unit_flow = "set by set" if ball_format == "The100" else "over by over"
    # The toss-result edit is cosmetic. If Telegram rejects it (the message was
    # deleted, a network blip, an "unmodified" reply) the match must still start
    # — an unguarded edit here used to abort the launch AFTER the Match row had
    # committed, which is exactly how a match ended up "already started" with no
    # board anywhere.
    try:
        await q.edit_message_text(
            f"✅ {_mention(winner_tg_val, winner_name)} "
            f"elected to <b>{'BAT' if decision == 'bat' else 'BOWL'}</b> first.\n"
            f"🏟️ {match_obj.stadium} • {pitch_type} pitch • {fmt_label}\n\n"
            f"The match begins below — play {unit_flow}!",
            parse_mode="HTML")
        # The toss-result message (this one) is kept; every other setup message is
        # swept away when the match starts.
        if getattr(q, "message", None) is not None:
            draft["toss_result_msg_id"] = q.message.message_id
    except Exception:
        logger.warning("/cipl toss result edit failed for match %s — "
                       "starting the match anyway", match_obj.id, exc_info=True)

    try:
        await begin_cipl_match(context, draft["chat_id"], match_obj, bat_info, bowl_info,
                               bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                               pitch_type,
                               bat_team_code=bat_team_code, bowl_team_code=bowl_team_code,
                               bat_team_emoji=bat_team_emoji, bowl_team_emoji=bowl_team_emoji,
                               draft=draft, ball_format=ball_format)
    except Exception:
        logger.exception("/cipl match hand-off failed for match %s", match_obj.id)
        await _recover_failed_handoff(context, draft, draft_id,
                                      winner_side, winner_tg_val, winner_name)


async def _recover_failed_handoff(context, draft, draft_id, winner_side,
                                  winner_tg, winner_name):
    """Rescue a launch whose Match row committed but whose board never appeared.

    Two outcomes, in order of preference:

    1. The state write survived (``begin_cipl_match`` saves before it prompts),
       so the match is playable and only the prompt is missing — ``cipl_resume``
       re-sends it and the match carries on from over 1.
    2. Nothing usable was written. Roll the launch back completely and put the
       Bat/Bowl buttons back so the winner can simply tap again, instead of
       being told "Match already started" about a match that never was.
    """
    mid = draft.get("match_id")
    chat_id = draft.get("chat_id")
    if mid and await _live_cipl_state(context, draft) is not None:
        if await cipl_resume(context, mid):
            logger.info("/cipl recovered stalled launch for match %s", mid)
            return

    await _abandon_failed_launch(context, draft)
    if chat_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id,
            f"⚠️ The match didn't start — nothing was lost.\n"
            f"{_mention(winner_tg, winner_name)}, choose again:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First",
                                     callback_data=f"cipl_toss_bat_{draft_id}_{winner_side}"),
                InlineKeyboardButton("🎳 Bowl First",
                                     callback_data=f"cipl_toss_bowl_{draft_id}_{winner_side}"),
            ]]))
    except Exception:
        logger.exception("/cipl toss re-prompt after failed launch failed "
                         "for draft %s", draft_id)


# ════════════════════════════════════════════════════════════════════
# Entry point — called after the toss is decided
# ════════════════════════════════════════════════════════════════════

async def begin_cipl_match(context, chat_id, match, bat_user, bowl_user,
                           bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                           pitch_type, bat_team_code="", bowl_team_code="",
                           bat_team_emoji="🏏", bowl_team_emoji="🏏", draft=None,
                           ball_format="T20"):
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
        conditions=(draft or {}).get("conditions"), ball_format=ball_format)
    state["user_names"] = {
        str(bat_user.telegram_id): bat_user.username or bat_user.first_name or "Player",
        str(bowl_user.telegram_id): bowl_user.username or bowl_user.first_name or "Player",
    }
    # Carry tournament identity through the match so the result is recorded against
    # the active Challenge League Tournament when it completes.
    if draft:
        state["tournament_id"] = draft.get("tournament_id")
        state["tournament_team_by_user"] = draft.get("tournament_team_by_user") or {}
        state["reserved_fixture_id"] = draft.get("reserved_fixture_id")
        # Carry CL Tour identity so the series score updates when the match ends.
        state["cl_tour_id"] = draft.get("cl_tour_id")
        state["cl_tour_match_id"] = draft.get("cl_tour_match_id")
    # /ciplbot: unranked practice, and the AI captain owns one side's turns.
    # This clears the tournament/tour identity set just above, so a practice
    # match can never be recorded against a real competition.
    if draft and draft.get("vs_bot"):
        from handlers.botlevel import level_for_match
        mark_bot_match(state, draft.get("target_user_id"),
                       difficulty=level_for_match(context.bot_data,
                                                  draft.get("host_user_id")))
        state["user_names"][str(BOT_TG_ID_)] = "🤖 Bot"
        # Remembered so the Rematch button reopens the same league.
        state["league_key"] = draft.get("league_key")
    await _ss(context, match.id, state, next_action=A_PICK_CIPL_BOWLER)
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
    overs_label = "The Hundred (100 balls)" if state.get("ball_format") == "The100" else f"{overs} overs"
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
    # Practice match: name the captaincy style the AI drew for this match, so
    # it's clear up front that the bot doesn't play every match the same way.
    bot_line = ""
    if _is_bot_match(state):
        try:
            from services.bot_captain import difficulty_label, persona_label
            bot_line = (f"🤖 <b>Bot captain:</b> {html.escape(persona_label(state))}"
                        f" • {html.escape(difficulty_label(state))}"
                        f" • <i>unranked</i>\n")
        except Exception:
            logger.exception("cipl: bot persona line failed")
    # No Team Chemistry line here: this card is Challenge League's, and a league
    # squad handed to both captains isn't a team anyone assembled. Same rule as
    # traits — see _chem_line. /letsplay's own card (letsplay._announce) does
    # carry it.
    return (
        f"🏆 <b>{bat_code}</b> 🆚 <b>{bowl_code}</b>\n"
        f"⚡ <b>High-Voltage IPL Battle</b> ⚡\n"
        f"{rule}\n"
        f"🏟️ {stadium} • {overs_label}\n"
        f"🌱 <b>Pitch:</b> {pitch}\n"
        f"{cond_line}"
        f"{bot_line}"
        f"🏏 {bat} batting first\n"
        f"{rule}\n"
        f"{bat_emoji} <b>{bat}</b>   vs   {bowl_emoji} <b>{bowl}</b>\n"
        f"📺 Live scorecard, commentary &amp; XI inside\n\n"
        f"👇 Tap <b>Watch Match</b> to follow the action live")


# ════════════════════════════════════════════════════════════════════
# Per-over prompts
# ════════════════════════════════════════════════════════════════════

def _unit_word(state, cap=False):
    """'over' (T20) or 'set' (The Hundred) — the per-turn unit of play."""
    w = "set" if state.get("ball_format") == "The100" else "over"
    return w.capitalize() if cap else w


def _progress(state):
    """Innings progress: '12.3/20' for T20, '63/100 balls' for The Hundred."""
    if state.get("ball_format") == "The100":
        return f"{cipl_match.balls_bowled(state)}/{cipl_match.total_balls(state)} balls"
    return f"{cipl_match.format_overs(state)}/{state['overs']}"


def _header(state):
    inn = state.get("innings", 1)
    line = (f"🏏 <b>{state['bat_team_name']}</b> {cipl_match.format_score(state)} "
            f"({_progress(state)})")
    c = cipl_match.chase(state)
    if c:
        line += (f"\n🎯 Need <b>{c['runs_required']}</b> off "
                 f"{c['balls_remaining']} • RRR {c['rrr']:.2f}")
    return f"Innings {inn} • {_unit_word(state, cap=True)} {state['current_over']}\n{line}"


async def _prompt_bowler(context, mid, state=None, first=False):
    if state is None:
        state = await _gs(context, mid)
    if not state:
        return
    if not first:
        await _delete_prev_over(context, state)
    if _bot_bowls(state):
        await _bot_take_the_ball(context, mid, state)
        return
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
            f"{p['name']} ({cipl_match.display_rating(p, 'bowl_rating')}) "
            f"· {left} left{tag}",
            callback_data=f"cipl_bowler_{mid}_{p['roster_id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    part_time_note = ("\n⚠️ <i>Front-line bowlers are bowled out — only part-time "
                      "bowlers (🧤) are left.</i>" if only_part_timers else "")
    text = (f"{_approach_card(state)}\n\n"
            f"🎳 {_mention_tg(state, state['bowl_user_tg'])}, pick your bowler "
            f"for {_unit_word(state)} {state['current_over']}:{part_time_note}")
    await _new_action_message(context, state, text, rows)
    await _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)
    _arm_timer(context, mid, A_PICK_CIPL_BOWLER)


async def _bot_take_the_ball(context, mid, state):
    """The AI captain picks its bowler for the over, then its approach.

    Mirrors the human path exactly — same eligibility list, same state writes —
    it just answers the prompt itself instead of posting a keyboard. No
    inactivity timer is armed: the bot is never idle, and the human's clock only
    starts when the board comes back to them.
    """
    from services import bot_captain

    # The AI captain solves a 5x5 game per candidate bowler, which is ~150ms of
    # pure Python. Cheap in absolute terms, but it must not sit on the event
    # loop while every other live match waits its turn.
    bowler = await asyncio.to_thread(bot_captain.pick_bowler, state)
    if bowler is None:
        # Nothing legal to bowl — the innings has nowhere left to go.
        logger.warning("bot captain: no bowler available for match %s — "
                       "ending the innings", mid)
        await _finish_innings(context, mid, state)
        return

    state["current_bowler"] = bowler
    await _ss(context, mid, state)
    text = (f"{_approach_card(state)}\n\n"
            f"🤖 <b>{html.escape(str(state.get('bowl_team_name', 'Bot')))}</b> "
            f"hands the ball to <b>{html.escape(str(bowler['name']))}</b> "
            f"({cipl_match.display_rating(bowler, 'bowl_rating')}) for "
            f"{_unit_word(state)} {state['current_over']}…")
    await _new_action_message(context, state, text, None)
    await asyncio.sleep(BOT_THINK_DELAY)
    await _prompt_bowl_approach(context, mid, state)


async def _prompt_bowl_approach(context, mid, state, auto=False):
    if _bot_bowls(state):
        # The bot's plan is hidden from the batting captain, exactly as a human
        # opponent's would be — it is revealed only once the over is bowled.
        from services import bot_captain
        state["bowling_approach"] = await asyncio.to_thread(
            bot_captain.pick_bowling_approach, state)
        await _ss(context, mid, state)
        await _prompt_bat_approach(context, mid, state)
        return
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
    await _ss(context, mid, state, next_action=A_PICK_BOWL_APPROACH)
    _arm_timer(context, mid, A_PICK_BOWL_APPROACH)


async def _prompt_bat_approach(context, mid, state, auto=False):
    if _bot_bats(state):
        from services import bot_captain
        state["batting_approach"] = await asyncio.to_thread(
            bot_captain.pick_batting_approach, state)
        await _ss(context, mid, state)
        await asyncio.sleep(BOT_THINK_DELAY)
        await _run_over(context, mid, state)
        return
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
    await _ss(context, mid, state, next_action=A_PICK_BAT_APPROACH)
    _arm_timer(context, mid, A_PICK_BAT_APPROACH)


# ════════════════════════════════════════════════════════════════════
# Resume (/rcl) — re-render the current over prompt from saved state
# ════════════════════════════════════════════════════════════════════

def is_cipl_state(state):
    """True if ``state`` belongs to a Challenge League over-by-over match."""
    return bool(state) and state.get("mode") == "cipl_approach"


def _innings_quota_used(state):
    """True when the current innings has nothing left to bowl.

    That is: the format's full quota of overs (20) or balls (100 = 20 sets) has
    been bowled, the side is all out, or the chase is won. Once this is true no
    further over/set may be played in this innings — the match must move to the
    innings break or the result. Never raises on a malformed state.
    """
    try:
        return bool(state) and cipl_match.is_innings_over(state)
    except Exception:
        logger.exception("innings-over check failed — treating innings as live")
        return False


def _match_row_status(mid):
    """Read Match.status from the DB (blocking — call via asyncio.to_thread)."""
    session = get_session()
    try:
        m = session.query(Match).get(mid)
        return (m.status or "") if m else None
    except Exception:
        logger.exception("match status lookup failed for %s", mid)
        return None
    finally:
        session.close()


async def _finish_innings(context, mid, state):
    """Route a finished innings to its terminal step — never another over."""
    if state.get("innings", 1) == 1:
        await _innings_break(context, mid, state)
    else:
        await _complete_match(context, mid, state)


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
        state = await _gs(context, mid)
        if not is_cipl_state(state):
            return False
        # While a Super Over is live the main over-by-over flow is suspended —
        # never re-prompt it (that could run another main-match over after the
        # regulation overs and overwrite the tie with a wrong result).
        if _super_over_active(context, mid):
            return False
        action = await _get_next_action(context, mid)
        if action == A_COMPLETED:
            # Finished match whose cleanup didn't land — clear it so /rcl stops
            # finding the corpse (and can never re-prompt an over on it).
            cleanup_state(context, mid)
            release_match_lock(mid)
            return False
        # The innings has already used its full quota (20 overs / 100 balls, all
        # out, or the chase won). /rcl must NEVER re-prompt a pick here: doing so
        # played a 21st over whenever an end-of-innings handler had failed
        # part-way (a dropped Telegram send left next_action on the consumed
        # approach pick). Drive the terminal step instead.
        if _innings_quota_used(state):
            return await _resume_finished_innings(context, mid, state)
        try:
            # Re-render the outstanding pick. Keep action_msg_id so an approach
            # resume edits the existing prompt IN PLACE (no duplicate), and a
            # bowler resume replaces it via _new_action_message (which deletes the
            # old picker first). Either way the buttons reappear even if the
            # previous prompt was deleted — the edit falls back to a fresh send.
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


async def _resume_finished_innings(context, mid, state):
    """Resume a match whose innings is over but whose end handler didn't finish.

    Innings 1 → run the innings break (which starts the chase from over 1).
    Innings 2 → the match is decided. If the DB already recorded the result, the
    stale live state is simply cleaned up (re-running the finalizer would pay the
    rewards a second time); otherwise the result is posted properly.
    Returns True — the resume was handled either way.
    """
    if state.get("innings", 1) == 1:
        logger.info("cipl resume: innings 1 quota used for match %s — "
                    "running the innings break instead of another over", mid)
        await _innings_break(context, mid, state)
        return True

    status = await asyncio.to_thread(_match_row_status, mid)
    if status in ("completed", "abandoned", "cancelled"):
        logger.info("cipl resume: match %s already %s — clearing stale state",
                    mid, status)
        try:
            await context.bot.send_message(
                state["chat_id"],
                "🏁 <b>This match is already over.</b>\n"
                "The full quota of overs has been bowled — there's nothing left "
                "to resume. Start a new one with /cipl or /letsplay.",
                parse_mode="HTML")
        except Exception:
            logger.exception("cipl resume completion notice failed for %s", mid)
        cleanup_state(context, mid)
        release_match_lock(mid)
        return True

    logger.info("cipl resume: innings 2 quota used for match %s — posting the "
                "result instead of another over", mid)
    await _complete_match(context, mid, state)
    return True


async def rcl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rcl — resume a stuck Challenge League (/cipl) match in this chat."""
    chat = update.effective_chat
    if chat is None:
        return
    cid = chat.id

    found_mid, found_state = await _find_cipl_match_in_chat(context, cid)
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
            "🏁 Nothing to resume — this match has already finished.\n"
            "Start a new one with /cipl or /letsplay. If you're still stuck in "
            "a match, ask an admin to /removematch you.")


async def _find_cipl_match_in_chat(context, cid):
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
    # Run the blocking query in a worker thread so /rcl can't freeze the loop.
    def _query_open_mids():
        session = get_session()
        try:
            rows = (session.query(Match)
                    .filter(Match.chat_id == cid,
                            Match.status.in_(("playing", "active", "toss", "selecting")))
                    .order_by(Match.id.desc())
                    .all())
            return [m.id for m in rows]
        except Exception:
            logger.exception("cipl resume DB lookup failed for chat %s", cid)
            return []
        finally:
            session.close()

    mids = await asyncio.to_thread(_query_open_mids)
    for mid in mids:
        state = await _gs(context, mid)
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
        state = await _gs(context, mid)
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
        if _innings_quota_used(state):
            await q.answer("This innings is already over — the full quota of "
                           "overs has been bowled.", show_alert=True)
            return
        if await _get_next_action(context, mid) != A_PICK_CIPL_BOWLER:
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
        await _ss(context, mid, state)
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
        state = await _gs(context, mid)
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
        if _innings_quota_used(state):
            await q.answer("This innings is already over — the full quota of "
                           "overs has been bowled.", show_alert=True)
            return
        if await _get_next_action(context, mid) != A_PICK_BOWL_APPROACH:
            await q.answer("Already chosen.", show_alert=True)
            return
        if not (0 <= idx < len(BOWLING_APPROACHES)):
            await q.answer("Invalid approach.", show_alert=True)
            return
        await q.answer()
        _cancel_timer(context, mid)
        state["bowling_approach"] = BOWLING_APPROACHES[idx][0]
        await _ss(context, mid, state)
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
        state = await _gs(context, mid)
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
        if _innings_quota_used(state):
            await q.answer("This innings is already over — the full quota of "
                           "overs has been bowled.", show_alert=True)
            return
        if await _get_next_action(context, mid) != A_PICK_BAT_APPROACH:
            await q.answer("Already chosen.", show_alert=True)
            return
        if not (0 <= idx < len(BATTING_APPROACHES)):
            await q.answer("Invalid approach.", show_alert=True)
            return
        await q.answer()
        _cancel_timer(context, mid)
        state["batting_approach"] = BATTING_APPROACHES[idx][0]
        await _ss(context, mid, state)
        await _run_over(context, mid, state)


# ════════════════════════════════════════════════════════════════════
# Over execution + progression
# ════════════════════════════════════════════════════════════════════

async def _run_over(context, mid, state):
    # Hard stop on the format limit. Once the innings quota is used — 20 overs,
    # or 100 balls (20 sets) in The Hundred — no further over may be simulated,
    # no matter how we got here (a stale approach button, a /rcl on a match whose
    # end-of-innings handler failed, an inactivity auto-pick). Finish the innings
    # instead of bowling a 21st over.
    if _innings_quota_used(state):
        logger.warning(
            "cipl: blocked an over past the innings limit for match %s "
            "(innings %s, %s/%s balls, %s wickets)",
            mid, state.get("innings"), cipl_match.balls_bowled(state),
            cipl_match.total_balls(state), state.get("total_wickets"))
        await _finish_innings(context, mid, state)
        return

    _cancel_timer(context, mid)
    bowler_name = state["current_bowler"]["name"]
    await _edit_action_message(
        context, state,
        f"{_header(state)}\n\n⏳ Simulating {_unit_word(state)} {state['current_over']} — "
        f"{bowler_name} bowling…", None)

    # The over simulation is CPU-bound pure Python (6 balls + pressure/scenario
    # engines). Run it in a worker thread so it can't block the event loop — and
    # every other user's command/button — for the duration of the over.
    summary = await asyncio.to_thread(cipl_match.simulate_over, state)

    # Feed the over back to the AI captain: what the human picked, and what it
    # cost them. Later overs counter the habits this builds up. No-op (and never
    # raises) outside a bot match.
    from services.bot_captain import observe_over
    observe_over(state, summary)

    innings_over = cipl_match.is_innings_over(state)
    # Advance the resume pointer the instant the over is simulated — BEFORE the
    # (fallible) summary send / next prompt. If a send here fails, next_action
    # would otherwise still point at the just-consumed batting approach, and a
    # later /rcl would re-show the approach picker on the now-advanced scorecard
    # and re-simulate this over. For an innings-ending over the dedicated
    # handlers below set the pointer themselves, so only persist the state here.
    if innings_over:
        await _ss(context, mid, state)
    else:
        await _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)

    # The summary post is cosmetic — a failed send must not stop the innings from
    # ending below, or the match would sit on a consumed approach pick and the
    # next /rcl would bowl another over.
    try:
        await _post_tracked(context, state,
                            _render_over_summary(state, summary),
                            keyboard=_miniapp_row(state))
    except Exception:
        logger.exception("cipl over summary post failed for match %s", mid)

    if innings_over:
        await _finish_innings(context, mid, state)
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
    # Neither captain's approach is shown here, and that now includes the bot's.
    # The summary used to print "Bot's plan: ..." after every over on the grounds
    # that there was no opponent to keep it from — but the player IS the
    # opponent, and a bot whose every pick is published is a bot whose mix can be
    # written down and countered. Its plans are hidden for exactly the reason a
    # human's are.
    lines = [
        f"<b>End of {_unit_word(state, cap=True)} {summary['over_no']}</b> — {summary['bowler']['name']}",
        f"Timeline: {timeline}",
        f"This {_unit_word(state)}: <b>{summary['over_runs']}</b> run(s), "
        f"{summary['over_wickets']} wkt(s)",
        "",
        f"🏏 <b>{state['bat_team_name']}</b> {cipl_match.format_score(state)} "
        f"({_progress(state)})",
        f"• {s_line}",
        f"• {n_line}",
        f"🎳 {summary['bowler']['name']}: {summary['bowler_figures']}",
        f"{arrow} Momentum: {state['bat_team_name']}",
    ]
    # Approach Interaction System flavour — the name of the special combination
    # the two picks made, or the match-up's one-line character.
    #
    # Deliberately withheld in bot matches, for the same reason the bot's plan
    # itself is (see above): the 5x5 flavour table is a lookup, so printing
    # "The Chess Match" after the over is printing both picks, and a bot whose
    # picks are published is a bot that can be written down. Between two humans
    # the reveal is symmetric — each captain gives up exactly what they learn —
    # and it is what makes an over feel like an event rather than a number.
    if not _is_bot_match(state):
        combo, flavour = summary.get("combo"), summary.get("flavour")
        if combo:
            lines.append(f"⚔️ <b>{html.escape(combo)}</b> — "
                         f"<i>{html.escape(flavour or '')}</i>")
        elif flavour:
            lines.append(f"⚔️ <i>{html.escape(flavour)}</i>")
        carry = summary.get("carry")
        if carry and carry.get("note"):
            lines.append(f"↪️ <i>Into the next {_unit_word(state)}: "
                         f"{html.escape(carry['note'])}.</i>")
    # What the bot has *noticed* is still said out loud — that it has caught the
    # player repeating themselves is a warning, not a plan, so it keeps the mind
    # game visible without handing over the counter.
    if _is_bot_match(state):
        try:
            from services.bot_captain import take_read_note
            note = take_read_note(state)
        except Exception:
            logger.exception("cipl: bot read note failed")
            note = None
        if note:
            lines.append(f"🧠 <i>{html.escape(str(note))}</i>")
    c = cipl_match.chase(state)
    if c and c["runs_required"] > 0:
        lines.append(f"🎯 Need {c['runs_required']} off {c['balls_remaining']} "
                     f"(RRR {c['rrr']:.2f})")
        # Final-over thriller cue: at the end of the penultimate over of a live
        # chase that's still winnable, flag the drama coming down to the wire.
        if c["balls_remaining"] == 6 and 0 < c["runs_required"] <= 36:
            if c["runs_required"] / 6.0 >= 2.0:
                lines.append("🔥 DOWN TO THE WIRE — final over, needs a blitz!")
            else:
                lines.append("🔥 Final over — game in the balance!")
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
    chem = _chem_line(state)
    if chem:
        lines += [rule, chem]
    card = "\n".join(lines)
    card += _commentary_block(state)
    return card


def _chem_line(state):
    """``🧪 CHEM  MI 🟩 88  ·  CSK 🟨 74`` for the board, or '' if it doesn't apply.

    Traits announce themselves over by over; Team Chemistry — the other thing
    the player builds their XI around — was only ever visible before the toss.
    This puts both sides' numbers on the same board, on the same 30-100 scale
    /cmuchem and /pxi use, so a squad decision and its match are finally
    readable together.

    Scoped exactly like traits: **only sides playing their own roster XI**. This
    board is shared with Challenge League, whose squads are league rosters
    handed to both captains rather than cards anyone collected — chemistry is a
    property of the team you assembled, so scoring a squad you were dealt would
    be meaningless. Renders nothing, rather than a wrong number, whenever a side
    can't be scored.
    """
    if not state.get("is_letsplay"):
        return ""
    try:
        from services import chemistry
        bat = chemistry.live_badge(state.get("bat_xi") or [])
        bowl = chemistry.live_badge(state.get("bowl_xi") or [])
    except Exception:
        logger.exception("letsplay chemistry line failed")
        return ""
    if not bat or not bowl:
        return ""
    bat_code = html.escape(str(state.get("bat_team_code")
                               or state.get("bat_team_name") or "Bat"))
    bowl_code = html.escape(str(state.get("bowl_team_code")
                                or state.get("bowl_team_name") or "Bowl"))
    # Only the colour + number: the board is already dense, and the /100 is
    # implied by the scale players know from /cmuchem.
    return (f"🧪 CHEM  {bat_code} {bat.split('/')[0]}  ·  "
            f"{bowl_code} {bowl.split('/')[0]}")


async def _innings_break(context, mid, state):
    # Clear the last over of innings 1 (its "simulating…" prompt + summary) so the
    # break message is the only thing left from innings 1 — even if the innings
    # ended on the final ball or an all-out.
    await _delete_prev_over(context, state)

    summary_text = _innings_scorecard(state, innings_label="1st Innings")
    cipl_match.end_first_innings(state)
    await _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)
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
    await _ss(context, mid, state)

    result = cipl_match.compute_result(state)
    # A tied match triggers a Super Over (interactive, user-vs-user, ball by
    # ball) for /cipl, /c[league] and /letsplay — they all reach here. The
    # Match row stays 'active' until the Super Over decides a winner.
    # A practice match vs the bot stops at the tie: the Super Over is an
    # interactive two-human flow (handlers/super_over.py) with no AI captain,
    # so there is nobody to play the bot's half of it.
    if result["tie"] and not _is_bot_match(state):
        try:
            # Mention the Super Over in the Mini App commentary feed so spectators
            # know the tie is being resolved (the chat already announces it).
            cipl_match._push_card(state, {
                "type": "new_bowler",
                "text": (f"🤝 Scores level at {cipl_match.format_score(state)} — "
                         f"it's a SUPER OVER! 🔥"),
            })
            await _ss(context, mid, state)
            from handlers.super_over import start_super_over
            if await start_super_over(context, mid, state):
                return
        except Exception:
            logger.exception("Super Over kickoff failed for match %s — "
                             "falling back to a tied result", mid)
    # Persist career stats + finalize the Match row. This is a batch of blocking
    # DB work (stat persistence, Match update, reward payout, scorecard snapshot,
    # tournament recording) — run it all in a worker thread so completing one
    # Challenge League match doesn't freeze the event loop (and every other
    # user's command/button) for the duration of the commit.
    def _finalize_match_db():
        # Per-over Win/Loss prize handed out below (None on a tie / award failure).
        prize = None
        session = get_session()
        try:
            try:
                from services.player_stats_service import persist_player_game_stats
                persist_player_game_stats(session, state)
            except Exception:
                logger.exception("cipl stat persistence failed for match %s", mid)

            # Player-of-the-Match career credit. ``persist_player_game_stats``
            # covers batting/bowling but not the POTM award, so bump it here
            # (parity with the chat/webapp finalize) — otherwise /statscl always
            # reports POTM(s): 0 for Challenge League players.
            try:
                # A /letsplay mismatch flags the state ``stats_disabled`` (anti
                # stat-farming) — skip the POTM career credit too, in step with
                # persist_player_game_stats. Tournament matches never set it.
                if not state.get("stats_disabled"):
                    winner_for_potm = None if result["tie"] else result.get("winner")
                    best = _cipl_potm_select(state, winner_for_potm)
                    if best and best.get("player_id"):
                        _record_cipl_potm(session, state, best)
            except Exception:
                logger.exception("cipl POTM career credit failed for match %s", mid)

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
                        # A /letsplay mismatch flagged for anti stat-farming
                        # counts for nothing — no coins/gems, no W/L, no streak
                        # (award returns zeros). Tournaments never set the flag.
                        w_coins, w_gems, l_coins, l_gems = award_match_rewards_core(
                            session, match.winner_id, match.loser_id, overs,
                            is_vsbot=False,
                            count_result=not state.get("stats_disabled"))
                        prize = {
                            "w_coins": w_coins, "w_gems": w_gems,
                            "l_coins": l_coins, "l_gems": l_gems,
                        }
                    except Exception:
                        logger.exception("cipl prize award failed for match %s", mid)

            # Record the official tournament result (standings + per-player stats).
            # No-op for casual Challenge League matches (no tournament_id in state).
            # A tie reaching here did NOT go to a Super Over, so it is recorded as a tie.
            try:
                if state.get("tournament_id"):
                    from services import tournament_service
                    tournament_service.record_tournament_match(session, state)
            except Exception:
                logger.exception("tournament match recording failed for %s", mid)

            # Record the CL Tour series result. A tie reaching here means no Super
            # Over ran (it would have returned earlier), so record None — which
            # resets the slot to pending/replayable rather than leaving it stuck
            # 'playing'. A decided match records the winner.
            try:
                if state.get("cl_tour_match_id"):
                    from services.cl_tour_service import record_cl_match_result
                    winner_for_tour = None if result["tie"] else match.winner_id
                    # Savepoint-isolate: a flush failure here must not poison the
                    # main finalization commit below.
                    with session.begin_nested():
                        record_cl_match_result(
                            session, state["cl_tour_match_id"], winner_for_tour)
            except Exception:
                logger.exception("CL tour result recording failed for %s", mid)

            # ── Match-end quest tracking ──
            # /letsplay and Challenge League finish here, and until now neither
            # fired a single quest event — so a player could win ten /lp matches
            # and watch "Win 3 matches" sit at 0/3. Fire the same shared tracker
            # the in-chat and Mini App finalizes use. It gates itself: a practice
            # match vs the bot or a Team Overall mismatch is flagged
            # ``stats_disabled`` and comes back out having changed nothing.
            try:
                from services.quest_service import track_user_match_quests
                quest_winner = (None if result["tie"] or not match
                                else match.winner_id)
                for uid in (state.get("inn1_bat_team_id"),
                            state.get("inn1_bowl_team_id")):
                    if not uid:
                        continue
                    qu = session.query(User).get(uid)
                    track_user_match_quests(
                        session, state, qu,
                        bool(quest_winner) and uid == quest_winner,
                        False, quest_winner)
            except Exception:
                logger.exception("cipl match-end quest tracking failed for %s", mid)

            session.commit()
        except Exception:
            session.rollback()
            logger.exception("cipl match finalization failed for match %s", mid)
        finally:
            session.close()
        return prize

    prize_info = await asyncio.to_thread(_finalize_match_db)

    # Snapshot the final scorecard + Arena board ON the event loop. This persists
    # a MatchScorecard row AND schedules the Telegram text-archive upload, which
    # relies on asyncio.get_running_loop() — so it must NOT run in a worker thread
    # (there get_running_loop() raises and the MatchNo<id>.txt upload is silently
    # skipped). The single-row write is cheap; it must run while the live state
    # still exists (before the cleanup below).
    try:
        if result["tie"]:
            result_text = "Match Tied"
        else:
            result_text = (f"{result['winner']} beat {result['loser']} by "
                           f"{result['margin']} {result['margin_type']}")
        # POTM for the archived MatchNo<id>.txt. Computed here rather than
        # reusing the card's copy below because the card is rendered later, in a
        # worker thread, and the archive is written from this block — the
        # selection is a cheap pure read of the finished state either way.
        try:
            archive_potm = _cipl_calc_potm(
                state, None if result["tie"] else result.get("winner"))
        except Exception:
            logger.exception("cipl archive POTM failed for match %s", mid)
            archive_potm = None
        from services.match_webapp_service import save_final_scorecard
        sc_session = get_session()
        try:
            save_final_scorecard(sc_session, mid, result_text=result_text,
                                 potm=archive_potm)
            sc_session.commit()
        except Exception:
            sc_session.rollback()
            raise
        finally:
            sc_session.close()
    except Exception:
        logger.exception("cipl final scorecard snapshot failed for match %s", mid)

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
    # Build the end-of-match keyboard defensively: this runs BEFORE the result
    # message and the Match Summary card, so anything raising here would cost
    # the player both of them (and leave the live state uncleaned).
    try:
        miniapp_row = list(_miniapp_row(state) or [])
        if _is_bot_match(state):
            text += f"\n\n{UNRANKED_NOTE}"
            miniapp_row += _rematch_row(state)
    except Exception:
        logger.exception("cipl end-of-match keyboard build failed for match %s", mid)
        miniapp_row = []
    # The result is already committed at this point — a failed send must not
    # abort the function, or the live state would stay parked on the last
    # (consumed) approach pick and /rcl would bowl an over past the limit.
    try:
        await context.bot.send_message(state["chat_id"], text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(miniapp_row)
                                       if miniapp_row else None)
    except Exception:
        logger.exception("cipl result message failed for match %s", mid)
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
                caption=_summary_caption(state, result_line),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(miniapp_row)
                if miniapp_row else None)
        else:
            logger.warning("cipl match summary image came back empty for match %s", mid)
    except Exception:
        logger.exception("cipl match summary image failed for match %s", mid)

    # The post-match analysis file. Everything above answers "who won"; this
    # answers "why" — phase splits, the worm, both momentum tracks, the live win
    # probability, and the over-by-over record of the approach duel, which
    # nothing else keeps. Sent last so a failure here costs only the file.
    #
    # It has to be built while the live state is still up (cleanup_state below
    # takes it away), but the render itself is pure string work, so the state
    # read stays on the loop and only the rendering goes to a thread.
    try:
        await _send_match_analysis(context, mid, state, result, miniapp_row)
    except Exception:
        logger.exception("cipl match analysis failed for match %s", mid)

    # Unpin the match-start announcement now that the match is over.
    pinned = state.get("pinned_msg_id")
    if pinned:
        try:
            await context.bot.unpin_chat_message(state["chat_id"], pinned)
        except Exception:
            pass

    await _ss(context, mid, state, next_action=A_COMPLETED)
    cleanup_state(context, mid)
    release_match_lock(mid)


async def _send_match_analysis(context, mid, state, result, keyboard_rows=None):
    """Render the match analysis report and send it into the match chat.

    Shared by the main finish (:func:`_complete_match`) and the Super Over one,
    so a match decided in a Super Over is not the single case that silently gets
    no file. Best-effort throughout: every failure is logged and swallowed, and
    a missing section is dropped rather than faked, because a match that has
    already been paid out must never be held up by its own paperwork.
    """
    from services.match_analysis import analysis_filename, build_match_analysis_html

    filename = analysis_filename(mid)
    result = result or {}
    # Read on the loop (this can reach the state cache / DB), render off it.
    try:
        from services.match_webapp_service import build_scorecard
        scorecard = build_scorecard(mid, None)
    except Exception:
        logger.exception("cipl analysis scorecard build failed for match %s", mid)
        scorecard = None
    try:
        potm = _cipl_calc_potm(state, None if result.get("tie") else result.get("winner"))
    except Exception:
        logger.exception("cipl analysis POTM failed for match %s", mid)
        potm = None

    html = await asyncio.to_thread(
        build_match_analysis_html, state, result, scorecard, potm)
    if not html:
        logger.warning("cipl match analysis came back empty for match %s", mid)
        return

    # File it in the storage channel next to MatchNo<id>.txt as well, so the
    # analysis survives the chat it was posted in.
    try:
        from services import tg_storage_service
        if tg_storage_service.is_configured():
            asyncio.get_running_loop().create_task(
                tg_storage_service.upload_text_async(
                    html, filename,
                    caption=f"📊 Match analysis · Match {mid}"))
    except Exception:
        logger.exception("cipl analysis archive failed for match %s", mid)

    doc = BytesIO(html.encode("utf-8"))
    doc.name = filename
    await context.bot.send_document(
        state["chat_id"], document=doc, filename=filename,
        caption=("📊 <b>Full Match Analysis</b>\n"
                 "<blockquote expandable>Phase-by-phase splits, the worm, "
                 "momentum &amp; pressure, win probability, partnerships and the "
                 "over-by-over approach duel. Open it in your browser."
                 "</blockquote>"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None)


def _summary_rows(bat_stats, bat_xi, bowl_stats, bowl_xi, bpu=6):
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
            # The Hundred shows bowler workload in balls; T20 in overs.balls.
            overs = f"{balls}b" if bpu != 6 else f"{balls // 6}.{balls % 6}"
            bowls.append({"name": p["name"], "wickets": st.get("wickets", 0),
                          "runs": st.get("runs", 0), "overs": overs})
    bowls.sort(key=lambda b: b["wickets"], reverse=True)
    return bats[:4], bowls[:4]


def _cipl_potm_select(state, winner_name=None):
    """Pick the Player of the Match for a finished /cipl match, by impact points.

    Mirrors handlers/match.py:_calc_potm but reads CIPL state, whose batting/
    bowling stats are keyed by ``str(roster_id)``. Returns the winning player's
    data dict (``{"rid", "name", "team", "bat", "bowl", ...}``) or ``None`` when
    no one batted or bowled.

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
                rid, {"rid": rid, "player_id": p.get("player_id"),
                      "name": p.get("name", "Player"), "team": team,
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
        return None

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
    return best


def _cipl_calc_potm(state, winner_name=None):
    """POTM name/stats/team for the summary card. See ``_cipl_potm_select``.

    Returns ``(name, stats_string, team)`` — all "" / None-safe.
    """
    best = _cipl_potm_select(state, winner_name)
    if not best:
        return None, "", ""
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


def _record_cipl_potm(session, state, best):
    """Bump the POTM winner's career PlayerGameStats.potm for a /cipl match.

    Finds the POTM player's owning side from the innings XI lists (the same
    team_id → user_id mapping ``persist_player_game_stats`` uses), then keys the
    award by the master ``player_id`` so /statscl's cross-league aggregate sees it.
    """
    from models import PlayerGameStats
    rid = best.get("rid")
    pid = best.get("player_id")
    if rid is None or not pid:
        return
    owner_uid = None
    for xi, owner in (
        (state.get("inn1_bat_xi"), state.get("inn1_bat_team_id")),
        (state.get("inn1_bowl_xi"), state.get("inn1_bowl_team_id")),
        (state.get("bat_xi"), state.get("bat_team_id")),
        (state.get("bowl_xi"), state.get("bowl_team_id")),
    ):
        if owner and any(p.get("roster_id") == rid for p in (xi or [])):
            owner_uid = owner
            break
    if not owner_uid:
        return
    row = (session.query(PlayerGameStats)
           .filter(PlayerGameStats.user_id == owner_uid,
                   PlayerGameStats.player_id == pid).first())
    if row:
        row.potm = (row.potm or 0) + 1
    else:
        session.add(PlayerGameStats(user_id=owner_uid, player_id=pid, potm=1))
    # Hand the result to the quest tracker, which fires 'career_potm' when the
    # winner is somebody's own Career Player (parity with the Mini App finalize).
    state["potm_player_id"] = pid
    state["potm_owner_user_id"] = owner_uid


def _summary_caption(state, result_line):
    """Caption under the Match Summary card.

    The result, the venue, and — for a practice match — which captaincy style
    the bot was playing, so the player can see it really does change match to
    match. Never raises: a caption is not worth losing the card over.
    """
    lines = [f"🏆 <b>Match Summary</b> — {result_line}"]
    try:
        stadium = state.get("stadium")
        if stadium:
            # Match the start card's wording: a 100-ball match is not "20 overs".
            overs_label = ("The Hundred (100 balls)"
                           if state.get("ball_format") == "The100"
                           else f"{state.get('overs', CIPL_OVERS)} overs")
            lines.append(f"🏟️ {html.escape(str(stadium))} • {overs_label}")
        if _is_bot_match(state):
            from services.bot_captain import persona_label
            lines.append(f"🤖 Bot captain: <b>{html.escape(persona_label(state))}</b> "
                         f"• <i>unranked practice</i>")
    except Exception:
        logger.exception("cipl summary caption build failed for match %s",
                         state.get("match_id"))
    return "\n".join(lines)


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

    _bpu = cipl_match.balls_per_unit(state)
    inn1_bats, inn1_bowls = _summary_rows(
        state.get("inn1_bat_stats", {}), state.get("inn1_bat_xi", []),
        state.get("inn1_bowl_stats", {}), state.get("inn1_bowl_xi", []), bpu=_bpu)
    inn2_bats, inn2_bowls = _summary_rows(
        state.get("bat_stats", {}), state.get("bat_xi", []),
        state.get("bowl_stats", {}), state.get("bowl_xi", []), bpu=_bpu)

    if result["tie"]:
        winner_name, margin_text = "Match Tied", "Match Tied"
    else:
        winner_name = result["winner"]
        # A caller with no run/wicket margin to quote (e.g. a Super Over decided
        # on boundary countback) passes the finished phrase itself, so the card
        # never has to render something like "won by 0 sixes".
        margin_text = (result.get("margin_text")
                       or f"won by {result['margin']} {result['margin_type']}")

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

    is_hundred = cipl_match.is_hundred(state)
    if is_hundred:
        inn1_overs_val = re.match(r"(\d+)", str(state.get("inn1_overs", "0")))
        inn1_overs_val = int(inn1_overs_val.group(1)) if inn1_overs_val else 0
        inn2_overs_val = cipl_match.balls_bowled(state)
        overs_total_val = cipl_match.total_balls(state)
    else:
        inn1_overs_val = state.get("inn1_overs", "0.0")
        inn2_overs_val = cipl_match.format_overs(state)
        overs_total_val = state.get("overs", 0)

    # Mark the AI captain's side on the card itself. In a league bot match the
    # bot fields a real franchise, so without this "RCB won by 8 wickets" reads
    # exactly like a result against a human. Applied only here, at render time —
    # POTM and the result logic above still key off the plain team name.
    inn1_label = with_bot_tag(state, inn1_team)
    inn2_label = with_bot_tag(state, inn2_team)

    return generate_match_summary(
        inn1_team=inn1_label,
        inn1_runs=state.get("inn1_runs", 0),
        inn1_wickets=state.get("inn1_wickets", 0),
        inn1_overs=inn1_overs_val,
        inn2_team=inn2_label,
        inn2_runs=state.get("total_runs", 0),
        inn2_wickets=state.get("total_wickets", 0),
        inn2_overs=inn2_overs_val,
        winner_name=with_bot_tag(state, winner_name),
        win_margin_text=margin_text,
        overs_total=overs_total_val,
        is_hundred=is_hundred,
        stadium=state.get("stadium"),
        potm_name=potm_name,
        potm_stats=potm_stats,
        potm_team=with_bot_tag(state, potm_team),
        top_per_team={
            "inn1": {"team": inn1_label, "batters": inn1_bats, "bowlers": inn1_bowls},
            "inn2": {"team": inn2_label, "batters": inn2_bats, "bowlers": inn2_bowls},
        },
        match_no=state.get("match_id"),
        text_settings=text_settings,
    )


def _innings_scorecard(state, innings_label=""):
    """Compact scorecard for the innings currently in ``state``."""
    ov_suffix = "" if state.get("ball_format") == "The100" else " ov"
    lines = [f"<b>{state['bat_team_name']}</b> — {cipl_match.format_score(state)} "
             f"({cipl_match.format_overs(state)}{ov_suffix}){'  · ' + innings_label if innings_label else ''}"]
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
