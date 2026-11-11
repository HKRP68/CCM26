"""Captain-only live batting controls, shared by chat and the Mini App."""
import asyncio
import copy
import html
import logging

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from services import live_batting as batting
from services.match_state_store import get_match_lock

logger = logging.getLogger(__name__)


async def submit_change(context, mid, actor_tg, kind, action, *, source="chat",
                        on_accept=None):
    from handlers import cipl_play as cp
    from services import match_webapp_service as web
    lock = get_match_lock(mid)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=1)
    except asyncio.TimeoutError:
        return False, "The over is being bowled. Try again when it finishes."
    claimed = False
    try:
        claimed = web._claim_action(mid)
        if not claimed:
            return False, "The match is processing a delivery. Try again in a moment."
        state = await cp._gs(context, mid)
        if not state:
            return False, "Match not found."
        if actor_tg != state.get("bat_user_tg"):
            return False, "Only the current batting captain can change batting."
        if cp._super_over_active(context, mid):
            return False, "Finish the Super Over before changing batting."
        if cp.is_cipl_state(state):
            error = cp._pick_surface_error(state, source)
            if error:
                return False, error
        na = await cp._get_next_action(context, mid)
        changed = copy.deepcopy(state)
        try:
            if kind == "retire":
                message = batting.retire(changed, na, action.get("outRosterId"),
                                         action.get("inRosterId"), action.get("token"))
            elif kind == "order":
                message = batting.reorder(changed, na, action.get("rosterIds"),
                                          action.get("token"))
            else:
                return False, "Unknown batting change."
        except ValueError as exc:
            return False, str(exc)
        try:
            await cp._ss(context, mid, changed, next_action=na)
        except (cp.StaleMatchState, cp.MatchCleared):
            return False, "The match moved on. Reopen Manage batting and choose again."
        if on_accept is not None:
            await on_accept()
        # Keep the existing turn and its deadline. Managing the batting must
        # neither bowl a ball nor restart the opposing captain's timer.
        try:
            await context.bot.send_message(
                changed["chat_id"], "🏏 " + html.escape(message), parse_mode="HTML")
        except Exception:
            logger.exception("live batting notice failed for match %s", mid)
        return True, message
    finally:
        if claimed:
            web._release_action(mid)
        lock.release()


async def _find_match(update, context):
    from database import get_session
    from models import Match
    from handlers import cipl_play as cp
    args = list(getattr(context, "args", None) or [])
    if args:
        try:
            mid = int(args[0])
        except ValueError:
            return None, None
        return mid, await cp._gs(context, mid)
    def ids_in_chat():
        from services.match_handover import LIVE_STATUSES
        with get_session() as db:
            return [r[0] for r in db.query(Match.id).filter(
                Match.chat_id == update.effective_chat.id,
                Match.status.in_(LIVE_STATUSES)).order_by(Match.id.desc()).all()]
    for mid in await asyncio.to_thread(ids_in_chat):
        state = await cp._gs(context, mid)
        if state and state.get("bat_user_tg") == update.effective_user.id:
            return mid, state
    return None, None


def _menu(mid, opts, outgoing=None):
    tok = opts["token"]
    rows = []
    if outgoing is not None:
        text = ("🏏 <b>Choose the incoming batsman</b>\n"
                "The retired player keeps their score and can return. No wicket is added.")
        for p in opts["waiting"]:
            label = f"{p['name']} · {p['runs']} ({p['balls']})"
            if p["retired"]:
                label += " · Resume"
            rows.append([Button(label, callback_data=f"lb_r_{mid}_{tok}_{outgoing}_{p['id']}")])
        rows.append([Button("‹ Back", callback_data=f"lb_v_{mid}")])
    else:
        text = ("🏏 <b>Manage batting</b>\n"
                "Retire either batter and choose who comes in. Retired players can resume "
                "from their saved score.\n\n<b>Available batting order</b>\n")
        text += "\n".join(f"{i + 1}. {html.escape(p['name'])} · {p['runs']} ({p['balls']})"
                           + (" · Retired, can return" if p["retired"] else "")
                           for i, p in enumerate(opts["waiting"])) or "No available batsmen."
        text += "\n\nUse ↑ / ↓ to change who bats next. Changes apply to this innings only."
        if opts["waiting"]:
            for p in opts["atCrease"]:
                rows.append([Button(f"Retire {p['name']} · {p['runs']} ({p['balls']})",
                                    callback_data=f"lb_p_{mid}_{tok}_{p['id']}")])
        for i, p in enumerate(opts["waiting"]):
            moves = []
            if i:
                moves.append(Button(f"↑ {p['name']}", callback_data=f"lb_o_{mid}_{tok}_{i}_{i-1}"))
            if i < len(opts["waiting"]) - 1:
                moves.append(Button(f"↓ {p['name']}", callback_data=f"lb_o_{mid}_{tok}_{i}_{i+1}"))
            if moves:
                rows.append(moves)
    rows.append([Button("Close", callback_data=f"lb_x_{mid}")])
    return text, Markup(rows)


async def manage_handler(update, context):
    from handlers import cipl_play as cp
    mid, state = await _find_match(update, context)
    msg = update.effective_message
    if not state:
        await msg.reply_text("No live batting innings found. Use /managebatting or /managebatting MATCH_ID.")
        return
    if state.get("bat_user_tg") != update.effective_user.id:
        await msg.reply_text("Only the current batting captain can change batting.")
        return
    if cp.is_cipl_state(state):
        error = cp._pick_surface_error(state, "chat")
        if error:
            await msg.reply_text(error)
            return
    opts = batting.options(state, await cp._get_next_action(context, mid), can_control=True)
    if not opts["canManage"] or cp._super_over_active(context, mid):
        await msg.reply_text(opts["message"] if not opts["canManage"] else "Finish the Super Over first.")
        return
    text, markup = _menu(mid, opts)
    await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def batting_callback(update, context):
    from handlers import cipl_play as cp
    q = update.callback_query
    try:
        parts = q.data.split("_")
        kind, mid = parts[1], int(parts[2])
        state = await cp._gs(context, mid)
        if not state or state.get("bat_user_tg") != q.from_user.id:
            await q.answer("Only the current batting captain can use this.", show_alert=True)
            return
        if kind == "x":
            await q.answer()
            await q.edit_message_text("🏏 Batting controls closed.")
            return
        if cp.is_cipl_state(state):
            error = cp._pick_surface_error(state, "chat")
            if error:
                await q.answer(error, show_alert=True)
                return
        na = await cp._get_next_action(context, mid)
        opts = batting.options(state, na, can_control=True)
        if not opts["canManage"] or cp._super_over_active(context, mid):
            await q.answer(opts["message"] if not opts["canManage"] else "Finish the Super Over first.", show_alert=True)
            return
        if kind not in ("v", "n") and parts[3] != opts["token"]:
            await q.answer("The match moved on. Reopen /managebatting.", show_alert=True)
            return
        if kind in ("r", "o"):
            if kind == "r":
                action = {"token": parts[3], "outRosterId": int(parts[4]), "inRosterId": int(parts[5])}
                change = "retire"
            else:
                ids = [p["id"] for p in opts["waiting"]]
                a, b = int(parts[4]), int(parts[5])
                if min(a, b) < 0 or max(a, b) >= len(ids) or abs(a - b) != 1:
                    raise ValueError("Invalid batting position.")
                ids[a], ids[b] = ids[b], ids[a]
                action = {"token": parts[3], "rosterIds": ids}
                change = "order"
            ok, message = await submit_change(context, mid, q.from_user.id, change, action)
            if not ok:
                await q.answer(message, show_alert=True)
                return
            state = await cp._gs(context, mid)
            opts = batting.options(state, await cp._get_next_action(context, mid), can_control=True)
        await q.answer()
        text, markup = _menu(mid, opts, outgoing=int(parts[4]) if kind == "p" else None)
        if kind == "n":
            # The plan picker remains usable while this separate menu opens.
            await context.bot.send_message(state["chat_id"], text, parse_mode="HTML", reply_markup=markup)
        else:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except (ValueError, IndexError, TypeError):
        await q.answer("Invalid batting selection. Reopen /managebatting.", show_alert=True)
