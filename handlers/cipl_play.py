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

import logging
from datetime import datetime, timedelta

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
from services.miniapp_buttons import miniapp_button
from services import cipl_match
from engine.approach_modifiers import (
    BATTING_APPROACHES, BOWLING_APPROACHES,
    batting_label, bowling_label,
)
from handlers.match import _mention

logger = logging.getLogger(__name__)

CIPL_TIMEOUT = 90  # seconds before an idle pick is auto-resolved


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


async def _new_action_message(context, state, text, keyboard):
    sent = await context.bot.send_message(
        state["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    if sent and getattr(sent, "message_id", None):
        state["action_msg_id"] = sent.message_id
        state.setdefault("over_msg_ids", []).append(sent.message_id)
    return sent


async def _edit_action_message(context, state, text, keyboard):
    mid_msg = state.get("action_msg_id")
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
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
    btn = miniapp_button("📊 View Match", "scorecard",
                         is_private=bool(state.get("is_private")),
                         origin_chat_id=state.get("chat_id"))
    return [[btn]] if btn else None


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

        from services.config_service import get_challenge_max_overs
        overs = get_challenge_max_overs(session)
        settings = random_match_settings()
        match = Match(
            user1_id=host.id, user2_id=target.id, status="active",
            overs=overs, toss_winner_id=winner.id, toss_decision=decision,
            batting_first_id=bat_user.id, bowling_first_id=bowl_user.id,
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
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

    await begin_cipl_match(context, draft["chat_id"], match_obj, bat_info, bowl_info,
                           bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                           pitch_type)


# ════════════════════════════════════════════════════════════════════
# Entry point — called after the toss is decided
# ════════════════════════════════════════════════════════════════════

async def begin_cipl_match(context, chat_id, match, bat_user, bowl_user,
                           bat_xi, bowl_xi, bat_team_name, bowl_team_name,
                           pitch_type):
    state = cipl_match.build_cipl_state(
        match_id=match.id, overs=match.overs,
        bat_user_id=bat_user.id, bowl_user_id=bowl_user.id,
        bat_user_tg=bat_user.telegram_id, bowl_user_tg=bowl_user.telegram_id,
        bat_xi=bat_xi, bowl_xi=bowl_xi,
        bat_team_name=bat_team_name, bowl_team_name=bowl_team_name,
        chat_id=chat_id, pitch_type=pitch_type,
        is_private=chat_id > 0, stadium=match.stadium)
    state["user_names"] = {
        str(bat_user.telegram_id): bat_user.username or bat_user.first_name or "Player",
        str(bowl_user.telegram_id): bowl_user.username or bowl_user.first_name or "Player",
    }
    _ss(context, match.id, state, next_action=A_PICK_CIPL_BOWLER)
    await _prompt_bowler(context, match.id, state, first=True)


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
    text = (f"{_header(state)}\n\n"
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
    text = (f"{_header(state)}\n\n"
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
    note = " <i>(auto Balanced)</i>" if auto else ""
    text = (f"{_header(state)}\n\n"
            f"🎳 Bowler: <b>{state['current_bowler']['name']}</b> • "
            f"{bowling_label(state['bowling_approach'])}{note}\n"
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
    lines = [
        f"<b>End of Over {summary['over_no']}</b> — {summary['bowler']['name']}",
        f"Approaches: {bowling_label(summary['bowling_approach'])} vs "
        f"{batting_label(summary['batting_approach'])}",
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


async def _innings_break(context, mid, state):
    summary_text = _innings_scorecard(state, innings_label="1st Innings")
    cipl_match.end_first_innings(state)
    _ss(context, mid, state, next_action=A_PICK_CIPL_BOWLER)
    target = state["target"]
    text = (f"🛑 <b>Innings Break</b>\n\n{summary_text}\n\n"
            f"🎯 <b>{state['bat_team_name']}</b> need <b>{target}</b> to win "
            f"in {state['overs']} overs.")
    await context.bot.send_message(state["chat_id"], text, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(_miniapp_row(state))
                                   if _miniapp_row(state) else None)
    # Innings-1 messages are now history; start innings 2 with a clean slate.
    state["over_msg_ids"] = []
    state["action_msg_id"] = None
    await _prompt_bowler(context, mid, state, first=True)


async def _complete_match(context, mid, state):
    result = cipl_match.compute_result(state)
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
    text = (f"🏁 <b>Match Over</b>\n\n"
            f"{_innings_scorecard(state, innings_label='2nd Innings')}\n\n"
            f"{result_line}")
    await context.bot.send_message(state["chat_id"], text, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(_miniapp_row(state))
                                   if _miniapp_row(state) else None)
    _ss(context, mid, state, next_action=A_COMPLETED)
    cleanup_state(context, mid)
    release_match_lock(mid)


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
