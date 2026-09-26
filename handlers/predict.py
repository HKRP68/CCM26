"""/predict — spectators back a side of the live match in this chat.

The card is posted for the whole group (its ``pred_`` buttons are shared — see
``services.button_access``). Each button is one complete prediction: a side and
a stake. ``services.prediction_service`` does every check (not your own match,
one prediction per match, enough coins, first innings only) and settles the
pool when the match ends.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, MatchState, User
from services import prediction_service as ps

logger = logging.getLogger(__name__)


def _stake_label(stake):
    return f"{stake // 1000}K" if stake >= 1000 and stake % 1000 == 0 else str(stake)


def _sides(match, state, users):
    """``[(user_id, team label)]`` for the two sides, batting side first."""
    out = []
    state = state or {}
    for side in ("bat", "bowl"):
        uid, name = state.get(f"{side}_team_id"), state.get(f"{side}_team_name")
        if uid in (match.user1_id, match.user2_id) and name:
            out.append((uid, str(name)))
    if len(out) == 2:
        return out
    out = []
    for uid in (match.user1_id, match.user2_id):
        u = users.get(uid)
        label = ((u.team_name or u.first_name or "Player") if u else "Player")
        out.append((uid, label))
    return out


def render_card(match, sides, pool, open_):
    lines = ["🔮 <b>PREDICT THE WINNER</b>",
             f"<i>Match #{match.id}</i>", "━━━━━━━━━━━━━━━"]
    total = sum(v["stake"] for v in pool.values())
    for uid, label in sides:
        p = pool.get(uid, {"stake": 0, "count": 0})
        share = f" · {round(100 * p['stake'] / total)}% of pool" if total else ""
        lines.append(f"🏏 <b>{html.escape(label)}</b> — {p['stake']:,} coins "
                     f"from {p['count']} backer{'s' if p['count'] != 1 else ''}{share}")
    lines.append(f"💰 Pool: <b>{total:,}</b> coins")
    lines.append("━━━━━━━━━━━━━━━")
    if open_:
        lines.append("Tap a side and a stake. Winners share the whole pool by "
                     f"stake, plus a {int(ps.HOUSE_BONUS * 100)}% bonus. "
                     "No winners or no result → everyone is refunded.")
        lines.append("<i>Closes at the innings break · players can't predict "
                     "their own match · one prediction each.</i>")
    else:
        lines.append("🔒 <b>Predictions are closed</b> — settled when the match ends.")
    return "\n".join(lines)


def _keyboard(match_id, sides, open_):
    rows = []
    if open_:
        for uid, label in sides:
            short = label if len(label) <= 12 else label[:11] + "…"
            rows.append([InlineKeyboardButton(f"{short} {_stake_label(s)}",
                                              callback_data=f"pred_p_{match_id}_{uid}_{s}")
                         for s in ps.STAKES])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"pred_r_{match_id}")])
    return InlineKeyboardMarkup(rows)


def _live_matches(session, chat_id):
    return (session.query(Match)
            .join(MatchState, MatchState.match_id == Match.id)
            .filter(Match.chat_id == chat_id,
                    Match.status.in_(ps._LIVE_STATUSES))
            .order_by(Match.id.desc()).limit(5).all())


def _state(context, mid):
    try:
        from services.match_state_store import get_state
        return get_state(context, mid)
    except Exception:
        logger.exception("predict: state read failed for %s", mid)
        return None


def _build(session, context, match):
    state = _state(context, match.id)
    users = {u.id: u for u in session.query(User)
             .filter(User.id.in_([match.user1_id, match.user2_id])).all()}
    sides = _sides(match, state, users)
    open_ = ps.predictions_open(state) and (match.status in ps._LIVE_STATUSES)
    pool = ps.pool(session, match.id)
    return render_card(match, sides, pool, open_), _keyboard(match.id, sides, open_)


async def predict_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/predict — open the prediction card for the live match in this chat."""
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        await update.message.reply_text(
            "🔮 Predictions happen in the group where a match is being played — "
            "send /predict there while it's live.")
        return
    session = get_session()
    try:
        matches = _live_matches(session, chat.id)
        from services.match_rewards import is_ai_user
        matches = [m for m in matches
                   if not any(is_ai_user(session.get(User, uid))
                              for uid in (m.user1_id, m.user2_id))]
        if not matches:
            await update.message.reply_text(
                "🔮 No live player-vs-player match in this chat right now.")
            return
        if len(matches) > 1 and not context.args:
            rows = []
            for m in matches:
                u1, u2 = session.get(User, m.user1_id), session.get(User, m.user2_id)
                label = (f"#{m.id}: {(u1.team_name or u1.first_name or 'P1') if u1 else 'P1'} vs "
                         f"{(u2.team_name or u2.first_name or 'P2') if u2 else 'P2'}")
                rows.append([InlineKeyboardButton(label[:60], callback_data=f"pred_r_{m.id}")])
            await update.message.reply_text("🔮 Which match?",
                                            reply_markup=InlineKeyboardMarkup(rows))
            return
        match = matches[0]
        if context.args:
            try:
                wanted = int(str(context.args[0]).lstrip("#"))
                match = next(m for m in matches if m.id == wanted)
            except (ValueError, StopIteration):
                pass
        text, kb = _build(session, context, match)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("/predict failed")
        await update.message.reply_text("❌ Couldn't open predictions.")
    finally:
        session.close()


async def predict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """pred_p_{mid}_{uid}_{stake} places a prediction; pred_r_{mid} refreshes."""
    q = update.callback_query
    parts = (q.data or "").split("_")
    session = get_session()
    try:
        if len(parts) >= 3 and parts[1] == "r":
            match = session.get(Match, int(parts[2]))
            if match is None:
                await q.answer("That match is gone.", show_alert=True)
                return
            await q.answer()
            text, kb = _build(session, context, match)
            try:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass   # "message is not modified" — nothing changed
            return
        if len(parts) != 5 or parts[1] != "p":
            await q.answer("Invalid button.", show_alert=True)
            return
        mid, pick, stake = int(parts[2]), int(parts[3]), int(parts[4])
        if stake not in ps.STAKES:
            await q.answer("Invalid stake.", show_alert=True)
            return
        match = session.get(Match, mid)
        user = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        state = _state(context, mid)
        try:
            ps.place(session, match, user, pick, stake, state=state)
            session.commit()
        except ps.PredictionError as e:
            session.rollback()
            await q.answer(str(e), show_alert=True)
            return
        users = {u.id: u for u in session.query(User)
                 .filter(User.id.in_([match.user1_id, match.user2_id])).all()}
        label = dict(_sides(match, state, users)).get(pick, "that side")
        await q.answer(f"🔮 {stake:,} coins on {label}. Good luck!", show_alert=True)
        text, kb = _build(session, context, match)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("predict callback failed")
        try:
            await q.answer("❌ Something went wrong.", show_alert=True)
        except Exception:
            pass
    finally:
        session.close()


async def prediction_sweep_job(context):
    """Background job: settle/refund predictions on matches that ended elsewhere."""
    import asyncio

    def _run():
        session = get_session()
        try:
            out = ps.sweep(session)
            session.commit()
            return [(m.chat_id, m.id, s) for m, s in out]
        except Exception:
            session.rollback()
            logger.exception("prediction sweep failed")
            return []
        finally:
            session.close()

    for chat_id, mid, summary in await asyncio.to_thread(_run):
        if not chat_id or not summary:
            continue
        if summary.get("refunded"):
            text = (f"🔮 Match #{mid}: {summary['settled']} prediction(s) refunded — "
                    f"{summary.get('reason') or 'no result'}.")
        else:
            text = (f"🔮 Match #{mid} predictions settled: {summary['winners']} of "
                    f"{summary['settled']} called it · paid {summary['paid']:,} coins.")
        try:
            await context.bot.send_message(chat_id, text)
        except Exception:
            logger.info("prediction sweep notice failed for chat %s", chat_id)
