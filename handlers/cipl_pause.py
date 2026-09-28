"""Pause, save and resume over-by-over matches; move them to the Mini App.

Commands (Challenge League — tournament fixtures included — and Lets Play):

* ``/pause`` — a captain parks the live match in this chat. It is saved exactly
  as it stands (the score, the over in progress, whose pick it is) and the chat
  is free for something else.
* ``/saved`` — your saved matches, each with ▶️ Continue and 🗑 Discard.
* ``/continue [MatchId]`` — pick a saved match up again, in any group. Against
  a human the other captain taps ✅ Ready first, so nobody is put on the clock
  while they are away; a practice match against the bot resumes at once.
* ``/playapp`` / ``/playchat`` — move the live match between the chat buttons
  and the Mini App. In the Mini App each captain makes their picks privately
  and neither side's plans are shown — not to the opponent, not in the chat.

A match is also saved, not lost, when it is cleared (``/clearmatches``,
``/removematch``) or when a tournament match's turn clock runs out for the
first time (see ``handlers.cipl_play._on_timeout``). The DB side is
``services.saved_match_service``.
"""

import asyncio
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services import saved_match_service as svc
from services.match_state_store import (
    A_COMPLETED, A_PICK_CIPL_BOWLER, cleanup_state, get_match_lock,
    release_match_lock, serialize_state, write_state_guarded, _deserialize,
    REV_KEY, SAVE_OK)

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = 15          # seconds a command waits for a busy match
REQUEST_TTL = 10 * 60      # a resume request waits this long for ✅ Ready
_REQ_PREFIX = "svreq_"     # bot_data key of a pending resume request


def _cp():
    # cipl_play imports a lot; keep this module importable on its own.
    from handlers import cipl_play
    return cipl_play


def _is_admin(tg_id):
    try:
        from handlers.forward_broadcast import is_forward_admin
        return bool(is_forward_admin(tg_id))
    except Exception:
        return False


def _mention(tg_id, name):
    return f'<a href="tg://user?id={int(tg_id)}">{html.escape(str(name))}</a>'


def _name_for(state, tg_id):
    names = (state or {}).get("user_names") or {}
    return names.get(str(tg_id)) or "Player"


def _captain_tgs(state):
    return {t for t in (state.get("bat_user_tg"), state.get("bowl_user_tg"))
            if t is not None}


def _human_tgs(state):
    bot_tg = None
    if state.get("is_bot_match"):
        try:
            from handlers.match import BOT_TG_ID_
            bot_tg = BOT_TG_ID_
        except Exception:
            bot_tg = None
    return {t for t in _captain_tgs(state) if t != bot_tg and t is not None
            and int(t) > 0}


def _is_group(chat):
    return getattr(chat, "type", None) in ("group", "supergroup")


def _saved_state(row):
    try:
        return _deserialize(row["state_json"]) or {}
    except Exception:
        logger.exception("saved match %s has an unreadable state", row.get("match_id"))
        return {}


def _continue_kb(mid, with_list=True):
    row = [InlineKeyboardButton("▶️ Continue", callback_data=f"svm_go_{mid}")]
    if with_list:
        row.append(InlineKeyboardButton("📂 Saved", callback_data="svm_list"))
    return InlineKeyboardMarkup([row])


# ════════════════════════════════════════════════════════════════════
# Parking a live match
# ════════════════════════════════════════════════════════════════════

async def park_match(context, mid, state, reason, *, by_user_id=None,
                     match_status="paused", end_reason=None):
    """Save match ``mid`` and take it out of play. Call with the lock held.

    The chat is tidied first (the open picker, the 30-second nag and the pinned
    card go), then the snapshot is written; only once it has landed is the live
    state deleted. Returns the saved row (a dict), or None when the snapshot
    could not be written — in which case the match is left exactly as it was.
    """
    cp = _cp()
    next_action = await cp._get_next_action(context, mid)
    if not svc.is_saveable(state, next_action):
        return None
    try:
        row = await asyncio.to_thread(
            svc.save_snapshot, mid, state, next_action, reason, by_user_id)
    except Exception:
        logger.exception("saving match %s failed — leaving it live", mid)
        return None

    cp._cancel_timer(context, mid)
    try:
        await cp._clear_action_reminder(context, state)
        await cp._delete_prev_over(context, state)
    except Exception:
        logger.debug("tidying the chat for paused match %s failed", mid,
                     exc_info=True)
    pinned = state.get("pinned_msg_id")
    if pinned and state.get("chat_id") is not None:
        try:
            await context.bot.unpin_chat_message(state["chat_id"], pinned)
        except Exception:
            pass
    try:
        prev = await asyncio.to_thread(
            svc.park_match_row, mid, match_status, end_reason, by_user_id)
        if prev is not None:
            await asyncio.to_thread(_record_prev_status, mid, prev)
    except Exception:
        logger.exception("parking the Match row of %s failed", mid)
    cleanup_state(context, mid)
    release_match_lock(mid)
    return row


def _record_prev_status(mid, prev):
    from database import get_session
    from models import SavedMatch
    session = get_session()
    try:
        (session.query(SavedMatch).filter(SavedMatch.match_id == int(mid))
         .update({SavedMatch.prev_status: prev}, synchronize_session=False))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def snapshot_before_clear(context, mid, by_user_id=None):
    """Save a match that /clearmatches or /removematch is about to wipe.

    Blocking and best-effort: runs just before ``cleanup_state`` throws the live
    state away, and never stands in the way of the clear itself. Returns True
    when a resumable save was written.
    """
    from services.match_state_store import get_next_action, get_state
    try:
        state = get_state(context, mid)
        next_action = get_next_action(context, mid)
        if not svc.is_saveable(state, next_action):
            return False
        svc.save_snapshot(mid, state, next_action, svc.REASON_CLEARED, by_user_id)
        return True
    except Exception:
        logger.exception("could not save cleared match %s", mid)
        return False


async def autosave_idle_match(context, mid, state, expected):
    """A tournament turn ran out: save the match instead of forfeiting it.

    Called by the inactivity timeout with the lock held. The first time a
    tournament match runs out of time it is parked (no fine, no winner, the
    fixture kept for it) and the chat is told how to carry on; the flag it
    leaves means a second idle timeout in the same match forfeits as usual.
    Returns True when the match was saved.
    """
    cp = _cp()
    (idle_uid, idle_tg, idle_name, _w_uid, win_tg, win_name) = cp._idle_actor(
        state, expected)
    state["idle_saves"] = int(state.get("idle_saves") or 0) + 1
    state["idle_saved_tg"] = idle_tg
    from services.match_outcome import END_AUTO
    row = await park_match(context, mid, state, svc.REASON_AUTO,
                           match_status="paused", end_reason=END_AUTO)
    if row is None:
        state["idle_saves"] -= 1
        return False
    try:
        await context.bot.send_message(
            state["chat_id"],
            f"⏱ <b>Match saved — nobody lost it</b>\n"
            f"{_mention(idle_tg, idle_name)} didn't play in time, so this "
            f"tournament match has been <b>paused and saved</b> instead of "
            f"forfeited.\n📋 {html.escape(row.get('title') or '')}\n\n"
            f"▶️ Either captain can pick it up with "
            f"<code>/continue {mid}</code> — the other taps ✅ Ready.\n"
            f"⚠️ <i>This was the one free pass: time out again in this match "
            f"and it is forfeited (−{cp.CIPL_FORFEIT_COINS:,} 🪙 "
            f"−{cp.CIPL_FORFEIT_GEMS} 💎).</i>",
            parse_mode="HTML", reply_markup=_continue_kb(mid))
    except Exception:
        logger.exception("autosave notice failed for match %s", mid)
    return True


def saves_on_timeout(state):
    """Does an idle turn in this match save it rather than forfeit it?

    Tournament fixtures (Challenge League and Lets Play), once per match. A
    forfeit there records no tournament result anyway — the fixture just goes
    back on the schedule to be played again from ball one — so saving it loses
    nothing and keeps the 12 overs already played. CL Tour matches still
    forfeit: a forfeit decides the series game.
    """
    return (bool(state.get("tournament_id"))
            and not state.get("is_bot_match")
            and not state.get("cl_tour_match_id")
            and int(state.get("idle_saves") or 0) < 1)


# ════════════════════════════════════════════════════════════════════
# /pause
# ════════════════════════════════════════════════════════════════════

async def _find_live(context, update):
    """(mid, state) of the match a command is about, or (None, reason)."""
    cp = _cp()
    chat = update.effective_chat
    args = list(getattr(context, "args", None) or [])
    if args:
        mid = cp._parse_match_id(args[0])
        if mid is None:
            return None, "ℹ️ Usage: the command alone, or with a match id."
        state = await cp._gs(context, mid)
        if not cp.is_cipl_state(state):
            return None, f"❌ Match #{mid} isn't a live Challenge League or Lets Play match."
        return mid, state
    mid, state = await cp._find_cipl_match_in_chat(context, chat.id)
    if mid is None:
        return None, ("❌ No live Challenge League or Lets Play match in this chat.\n"
                      "Your saved matches: /saved")
    return mid, state


async def _acquire(mid):
    lock = get_match_lock(mid)
    try:
        await asyncio.wait_for(lock.acquire(), LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        return None
    return lock


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pause — save the live match in this chat and stop the clock."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    cp = _cp()
    mid, state = await _find_live(context, update)
    if mid is None:
        await msg.reply_text(state, parse_mode="HTML")
        return
    if user.id not in _captain_tgs(state) and not _is_admin(user.id):
        await msg.reply_text("❌ Only the two captains in this match can pause it.")
        return
    if cp._super_over_active(context, mid):
        await msg.reply_text("🔥 A Super Over is on — it can't be paused. Finish it!")
        return

    lock = await _acquire(mid)
    if lock is None:
        await msg.reply_text("⏳ The match is in the middle of an over — try "
                             "/pause again in a few seconds.")
        return
    try:
        state = await cp._gs(context, mid)
        if not cp.is_cipl_state(state):
            await msg.reply_text("🏁 That match has already finished.")
            return
        if await cp._get_next_action(context, mid) == A_COMPLETED:
            await msg.reply_text("🏁 That match has already finished.")
            return
        by_uid = cp._user_id_for_tg(state, user.id)
        row = await park_match(context, mid, state, svc.REASON_PAUSED,
                               by_user_id=by_uid)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass
    if row is None:
        await msg.reply_text("⚠️ Couldn't save the match right now — it is "
                             "still live. Please try again.")
        return

    others = [t for t in _human_tgs(state) if t != user.id]
    who = _mention(user.id, _name_for(state, user.id) if user.id in
                   _captain_tgs(state) else (user.first_name or "Admin"))
    tail = ""
    if others:
        tail = (f"\n\n{', '.join(_mention(t, _name_for(state, t)) for t in others)}"
                f" — nothing is lost and nobody is on the clock.")
    await msg.reply_text(
        f"⏸️ <b>Match paused &amp; saved</b> (#{mid})\n"
        f"📋 {html.escape(row.get('title') or '')}\n"
        f"Paused by {who}.{tail}\n\n"
        f"▶️ Carry on any time with <code>/continue {mid}</code> — in this "
        f"group or another one. It picks up from exactly this ball.",
        parse_mode="HTML", reply_markup=_continue_kb(mid))


# ════════════════════════════════════════════════════════════════════
# /saved and /continue
# ════════════════════════════════════════════════════════════════════

def _saved_list_view(rows):
    if not rows:
        return ("📂 <b>No saved matches.</b>\n"
                "Pause a live match with /pause and it will wait here for you.",
                None)
    lines = ["📂 <b>Your saved matches</b>", ""]
    kb = []
    for r in rows:
        label = svc.REASON_LABELS.get(r.get("reason"), "💾 Saved")
        when = r.get("saved_at")
        when_txt = when.strftime("%d %b %H:%M UTC") if when else ""
        lines.append(f"<b>#{r['match_id']}</b> · {label} · <i>{when_txt}</i>\n"
                     f"   {html.escape(r.get('title') or '')}")
        kb.append([
            InlineKeyboardButton(f"▶️ Continue #{r['match_id']}",
                                 callback_data=f"svm_go_{r['match_id']}"),
            InlineKeyboardButton("🗑", callback_data=f"svm_del_{r['match_id']}"),
        ])
    lines += ["", "<i>Continue picks the match up from the very ball it "
                  "stopped on. Against a human, the other captain taps "
                  "✅ Ready first.</i>"]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def saved_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/saved — list your resumable matches."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    rows = await asyncio.to_thread(svc.list_saved, user.id)
    text, kb = _saved_list_view(rows)
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def continue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/continue [MatchId] — resume a saved match in this chat."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if msg is None or user is None or chat is None:
        return
    cp = _cp()
    args = list(getattr(context, "args", None) or [])
    if args:
        mid = cp._parse_match_id(args[0])
        if mid is None:
            await msg.reply_text("ℹ️ Usage: <code>/continue &lt;MatchId&gt;</code> "
                                 "— or /saved to pick one.", parse_mode="HTML")
            return
    else:
        rows = await asyncio.to_thread(svc.list_saved, user.id)
        here = [r for r in rows if r.get("chat_id") == chat.id]
        pick = here if len(here) == 1 else (rows if len(rows) == 1 else None)
        if not pick:
            text, kb = _saved_list_view(rows)
            await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
            return
        mid = pick[0]["match_id"]
    text = await request_resume(context, chat, user, mid)
    if text:
        await msg.reply_text(text, parse_mode="HTML")


def _chat_busy(chat_id, human_uids):
    """Why a match can't be resumed in ``chat_id`` right now, or None."""
    from database import get_session
    from models import Match
    from sqlalchemy import or_
    from handlers.match import ACTIVE_MATCH_STATUSES, _active_match_in_chat
    session = get_session()
    try:
        if _active_match_in_chat(session, chat_id) is not None:
            return ("🚫 A match is already going on in this chat. Finish it "
                    "(or /pause it) first.")
        if human_uids:
            busy = (session.query(Match)
                    .filter(Match.status.in_(ACTIVE_MATCH_STATUSES),
                            or_(Match.user1_id.in_(human_uids),
                                Match.user2_id.in_(human_uids)))
                    .first())
            if busy is not None:
                return ("🚫 One of the captains is in another match right now "
                        f"(#{busy.id}). Finish or /pause that one first.")
        return None
    finally:
        session.close()


async def request_resume(context, chat, user, mid):
    """Start resuming saved match ``mid`` in ``chat`` on ``user``'s request.

    Returns a message to show the requester, or None when the request itself
    posted what there is to say (the ✅ Ready card, or the resumed match).
    """
    row = await asyncio.to_thread(svc.get_saved, mid)
    if row is None or row.get("status") != svc.ST_SAVED:
        return f"❌ There's no saved match #{mid} to continue. See /saved."
    state = _saved_state(row)
    if not state:
        return "⚠️ That save can't be read — it can't be resumed."
    captains = _captain_tgs(state)
    admin = _is_admin(user.id)
    if user.id not in captains and not admin:
        return "❌ Only the two captains in that match can continue it."
    humans = _human_tgs(state)
    if len(humans) > 1 and not _is_group(chat):
        return ("👥 Continue a match against another player in a group, so "
                "both captains can see it.")
    busy = await asyncio.to_thread(_chat_busy, chat.id,
                                   svc.human_user_ids(state))
    if busy:
        return busy

    opponents = [t for t in humans if t != user.id]
    if not opponents or (admin and user.id not in captains):
        # Against the bot, or an admin putting it back: nobody to wait for.
        return await do_resume(context, mid, chat.id, by_tg=user.id)

    key = f"{_REQ_PREFIX}{mid}"
    context.bot_data[key] = {"chat_id": chat.id, "by": user.id,
                             "opp": opponents[0], "at": time.time()}
    opp = opponents[0]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ready", callback_data=f"svm_ok_{mid}"),
        InlineKeyboardButton("❌ Not now", callback_data=f"svm_no_{mid}"),
    ]])
    await context.bot.send_message(
        chat.id,
        f"▶️ <b>Continue match #{mid}?</b>\n"
        f"📋 {html.escape(row.get('title') or '')}\n\n"
        f"{_mention(user.id, _name_for(state, user.id))} wants to carry on. "
        f"{_mention(opp, _name_for(state, opp))}, tap <b>✅ Ready</b> when "
        f"you're here — the clock starts the moment you do.\n"
        f"<i>This request expires in {REQUEST_TTL // 60} minutes.</i>",
        parse_mode="HTML", reply_markup=kb)
    return None


async def do_resume(context, mid, chat_id, by_tg=None):
    """Put saved match ``mid`` back in play in ``chat_id`` and re-show its pick.

    Returns a message for the requester on failure, None on success (the chat
    then has the "resumed" card and the outstanding prompt).
    """
    cp = _cp()
    lock = await _acquire(mid)
    if lock is None:
        return "⏳ That match is busy — try again in a few seconds."
    try:
        if not await asyncio.to_thread(svc.claim, mid):
            return "❌ That match has already been continued or discarded. See /saved."
        row = await asyncio.to_thread(svc.get_saved, mid)
        state = _saved_state(row or {})
        if not state:
            await asyncio.to_thread(svc.void, mid)
            return "⚠️ That save can't be read — it can't be resumed."
        try:
            await asyncio.to_thread(svc.revive_match, mid, chat_id, state)
        except svc.ReviveError as exc:
            await asyncio.to_thread(svc.void if exc.permanent else svc.unclaim, mid)
            return f"❌ {html.escape(exc.message)}"
        except Exception:
            logger.exception("reviving match %s failed", mid)
            await asyncio.to_thread(svc.unclaim, mid)
            return "⚠️ Couldn't restore that match right now — please try again."

        state = svc.clean_state(state)
        state["chat_id"] = chat_id
        state["is_private"] = chat_id > 0
        state["prompt_delivered"] = False
        state["resumed_count"] = int(state.get("resumed_count") or 0) + 1
        next_action = (row or {}).get("next_action") or A_PICK_CIPL_BOWLER
        state[REV_KEY] = int(state.get(REV_KEY) or 0) + 1
        snapshot = serialize_state(state)
        result = await asyncio.to_thread(
            write_state_guarded, mid, snapshot, state[REV_KEY], next_action,
            None, True)
        if result.get("status") != SAVE_OK:
            logger.error("resumed match %s could not be written back (%s)",
                         mid, result.get("status"))
            try:
                await asyncio.to_thread(svc.park_match_row, mid)
            except Exception:
                logger.exception("re-parking match %s failed", mid)
            await asyncio.to_thread(svc.unclaim, mid)
            return "⚠️ Couldn't restore that match right now — please try again."
        try:
            from services.match_heartbeat_flags import increment_active_matches
            increment_active_matches(context)
        except Exception:
            pass
        context.bot_data.pop(f"{_REQ_PREFIX}{mid}", None)
        await asyncio.to_thread(svc.finish_claim, mid)
        state = await cp._gs(context, mid) or state

        # A fresh card for the chat: where the match stands and, in a Mini
        # App match, the button to play it.
        where = ("📱 Picks are made in the Mini App — plans stay private."
                 if cp._app_mode(state) else "💬 Picks are made with the chat buttons.")
        try:
            kb = cp._miniapp_row(state)
            sent = await context.bot.send_message(
                chat_id,
                f"▶️ <b>Match resumed</b> (#{mid})\n"
                f"{cp._competition_line(state) or ''}\n"
                f"🏏 <b>{html.escape(str(state.get('bat_team_name')))}</b> "
                f"{cp.cipl_match.format_score(state)} ({cp._progress(state)})\n"
                f"{where}\n<i>Need a break? /pause saves it again.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb) if kb else None)
            state["pinned_msg_id"] = sent.message_id
            try:
                await context.bot.pin_chat_message(
                    chat_id, sent.message_id, disable_notification=True)
            except Exception:
                pass
            await cp._ss(context, mid, state)
        except Exception:
            logger.exception("resume card failed for match %s", mid)
        try:
            await cp._resume_locked(context, mid)
        except Exception:
            logger.exception("re-prompting resumed match %s failed", mid)
            cp._schedule_cipl_recovery(context, mid)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass
    return None


# ════════════════════════════════════════════════════════════════════
# Buttons: svm_go_ / svm_ok_ / svm_no_ / svm_del_ / svm_delok_ / svm_list
# ════════════════════════════════════════════════════════════════════

async def saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    user = q.from_user
    data = q.data or ""
    parts = data.split("_")
    action = parts[1] if len(parts) > 1 else ""
    mid = None
    if len(parts) > 2:
        try:
            mid = int(parts[2])
        except ValueError:
            await q.answer("Invalid button.", show_alert=True)
            return

    if action == "list":
        await q.answer()
        rows = await asyncio.to_thread(svc.list_saved, user.id)
        text, kb = _saved_list_view(rows)
        await context.bot.send_message(q.message.chat_id, text,
                                       parse_mode="HTML", reply_markup=kb)
        return

    if mid is None:
        await q.answer("Invalid button.", show_alert=True)
        return

    if action == "go":
        await q.answer()
        text = await request_resume(context, q.message.chat, user, mid)
        if text:
            await context.bot.send_message(q.message.chat_id, text, parse_mode="HTML")
        return

    if action in ("ok", "no"):
        req = context.bot_data.get(f"{_REQ_PREFIX}{mid}")
        if not req or time.time() - req.get("at", 0) > REQUEST_TTL:
            context.bot_data.pop(f"{_REQ_PREFIX}{mid}", None)
            await q.answer("This request has expired — /continue again.",
                           show_alert=True)
            return
        if user.id not in (req.get("opp"), req.get("by")) and not _is_admin(user.id):
            await q.answer("This is for the two captains of that match.",
                           show_alert=True)
            return
        if action == "no":
            context.bot_data.pop(f"{_REQ_PREFIX}{mid}", None)
            await q.answer("Okay — it stays saved.")
            try:
                await q.edit_message_text(
                    f"⏸️ Match #{mid} stays saved. <code>/continue {mid}</code> "
                    f"whenever you're both ready.", parse_mode="HTML")
            except Exception:
                pass
            return
        if user.id != req.get("opp") and not _is_admin(user.id):
            await q.answer("Waiting for your opponent to tap ✅ Ready.",
                           show_alert=True)
            return
        await q.answer("Resuming…")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        text = await do_resume(context, mid, req["chat_id"], by_tg=user.id)
        if text:
            await context.bot.send_message(req["chat_id"], text, parse_mode="HTML")
        return

    if action in ("del", "delok"):
        row = await asyncio.to_thread(svc.get_saved, mid)
        if row is None or row.get("status") != svc.ST_SAVED:
            await q.answer("That save is already gone.", show_alert=True)
            return
        if user.id not in (row.get("user1_tg"), row.get("user2_tg")) \
                and not _is_admin(user.id):
            await q.answer("Only the two captains can discard it.", show_alert=True)
            return
        if action == "del":
            await q.answer()
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, discard", callback_data=f"svm_delok_{mid}"),
                InlineKeyboardButton("↩️ Keep it", callback_data="svm_list"),
            ]])
            await context.bot.send_message(
                q.message.chat_id,
                f"🗑 Discard saved match #{mid} for good?\n"
                f"📋 {html.escape(row.get('title') or '')}\n"
                f"<i>It can't be continued afterwards. A tournament fixture goes "
                f"back on the schedule to be played from scratch.</i>",
                parse_mode="HTML", reply_markup=kb)
            return
        uid = None
        state = _saved_state(row)
        try:
            uid = _cp()._user_id_for_tg(state, user.id)
        except Exception:
            pass
        ok = await asyncio.to_thread(svc.discard, mid, uid)
        await q.answer("Discarded." if ok else "Already gone.")
        try:
            await q.edit_message_text(
                f"🗑 Saved match #{mid} discarded." if ok else
                f"Saved match #{mid} was already gone.")
        except Exception:
            pass
        return

    await q.answer()


# ════════════════════════════════════════════════════════════════════
# /playapp and /playchat — move a live match between chat and Mini App
# ════════════════════════════════════════════════════════════════════

async def _switch_mode(update, context, mode):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    cp = _cp()
    mid, state = await _find_live(context, update)
    if mid is None:
        await msg.reply_text(state, parse_mode="HTML")
        return
    if user.id not in _captain_tgs(state) and not _is_admin(user.id):
        await msg.reply_text("❌ Only the two captains in this match can move it.")
        return
    if cp._super_over_active(context, mid):
        await msg.reply_text("🔥 A Super Over is on — finish it where it is.")
        return
    if mode == "app" and not cp._miniapp_row(state):
        await msg.reply_text("📱 The Mini App isn't available on this bot right now.")
        return

    lock = await _acquire(mid)
    if lock is None:
        await msg.reply_text("⏳ The over is being bowled — try again in a few seconds.")
        return
    try:
        state = await cp._gs(context, mid)
        if not cp.is_cipl_state(state):
            await msg.reply_text("🏁 That match has already finished.")
            return
        current = "app" if state.get("play_mode") == "app" else "chat"
        if current == mode:
            await msg.reply_text(
                "📱 This match is already played in the Mini App." if mode == "app"
                else "💬 This match is already played with the chat buttons.")
            return
        state["play_mode"] = mode
        cp._cancel_timer(context, mid)
        await cp._ss(context, mid, state)
        if mode == "app":
            text = ("📱 <b>Match moved to the Mini App</b>\n"
                    "Each captain now makes their picks privately in the app — "
                    "bowler, plan, new batsman and Impact Player. Neither side's "
                    "plans are shown: not to the opponent, not in this chat. "
                    "The chat keeps the score and the over summaries.\n"
                    "<i>/playchat moves it back.</i>")
        else:
            text = ("💬 <b>Match moved back to the chat</b>\n"
                    "Picks are made with the chat buttons again, and each over's "
                    "match-up is shown in the summary.\n"
                    "<i>/playapp moves it to the Mini App.</i>")
        kb = cp._miniapp_row(state) if mode == "app" else None
        await msg.reply_text(text, parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        await cp._resume_locked(context, mid)
    except Exception:
        logger.exception("switching match %s to %s failed", mid, mode)
        await cp._recover_from_error(context, mid, "play mode switch")
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


async def playapp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/playapp — play the live match in the Mini App, plans kept private."""
    await _switch_mode(update, context, "app")


async def playchat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/playchat — play the live match with the chat buttons again."""
    await _switch_mode(update, context, "chat")
