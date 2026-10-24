"""Admin clear with an expiring, caller-bound preview, backups and undo."""
import asyncio
import hashlib
import json
import logging
import secrets
import time
from contextlib import AsyncExitStack
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services import match_control_service as control

logger = logging.getLogger(__name__)
_chat_locks = {}


def get_session():
    from database import get_session as session_factory
    return session_factory()


def _match():
    from handlers import match
    return match


def _memory(context, cid):
    mh = _match()
    result = {}
    for key, value in list(context.bot_data.items()):
        if not isinstance(key, str):
            continue
        if key in (mh._cric_lobby_key(cid), f"cm_lobby_chat_{cid}") or (
            key.startswith(("ms_", "lp_", "challenge_team_draft_", "cm_lobby_"))
            and isinstance(value, dict) and value.get("chat_id") == cid
        ):
            result[key] = value
    return result


def _targets(context, cid):
    from models import Match, MatchState
    session = get_session()
    try:
        rows = session.query(Match).filter(
            Match.chat_id == cid, Match.status.in_(_match().ACTIVE_MATCH_STATUSES)).all()
        mids = {r.id for r in rows}
        # Include persisted orphan boards even when this worker has no cache.
        for state in (session.query(MatchState).join(Match, Match.id == MatchState.match_id)
                      .filter(Match.chat_id == cid).all()):
            mids.add(state.match_id)
        mem = _memory(context, cid)
        mids.update(int(k[3:]) for k in mem if k.startswith("ms_") and k[3:].isdigit())
        signature = hashlib.sha256(json.dumps({
            "rows": sorted((r.id, r.status) for r in rows),
            "mids": sorted(mids), "memory": sorted(k for k in mem if not k.startswith("ms_"))
        }, sort_keys=True).encode()).hexdigest()
        return sorted(mids), sorted(mem), signature
    finally:
        session.close()


def _dump(row):
    return {c.name: (getattr(row, c.name).isoformat()
                     if isinstance(getattr(row, c.name), datetime)
                     else getattr(row, c.name)) for c in row.__table__.columns}


def _restore(row, data):
    from sqlalchemy import DateTime
    for c in row.__table__.columns:
        if c.name in data:
            value = data[c.name]
            if value is not None and isinstance(c.type, DateTime):
                value = datetime.fromisoformat(value)
            setattr(row, c.name, value)


def _clear(context, cid, mids, action_id):
    """Back up rows, fixtures, boards and drafts before the clearing commit."""
    from models import (Match, MatchState, MatchControlAction, TourMatch,
                        CLTourMatch, TournamentMatch)
    from services.match_outcome import mark_end, END_CLEARED_BY_USER
    from services.match_state_store import get_state, get_next_action, serialize_state
    from services import saved_match_service as saved
    session = get_session()
    try:
        action = session.get(MatchControlAction, action_id)
        rows = session.query(Match).filter(
            Match.id.in_(mids), Match.chat_id == cid,
            Match.status.in_(_match().ACTIVE_MATCH_STATUSES)).with_for_update().all()
        backup = {"matches": [_dump(r) for r in rows], "states": [], "links": {},
                  "memory": json.loads(json.dumps(_memory(context, cid)))}
        save_ids = []
        for mid in mids:
            state = get_state(context, mid)
            pointer = get_next_action(context, mid)
            ms = session.query(MatchState).filter_by(match_id=mid).first()
            if ms:
                data = _dump(ms)
                if state:
                    data["state_json"] = serialize_state(state)
                backup["states"].append(data)
            elif state:
                backup["states"].append({
                    "match_id": mid, "state_json": serialize_state(state),
                    "next_action": pointer or state.get("_na") or "PICK_DELIVERY",
                    "version": 0, "ball_seq": 0,
                    "last_modified": datetime.utcnow().isoformat()})
            if saved.is_saveable(state, pointer):
                # A failed snapshot aborts clearing; it never becomes best effort.
                saved.save_snapshot(mid, state, pointer, saved.REASON_CLEARED,
                                    action.user_id, session=session)
                save_ids.append(mid)
        for cls in (TourMatch, CLTourMatch, TournamentMatch):
            links = session.query(cls).filter(cls.match_id.in_(mids)).all()
            backup["links"][cls.__name__] = [_dump(r) for r in links]
            for r in links:
                r.match_id = None
                r.status = "scheduled" if cls is TournamentMatch else "pending"
                if hasattr(r, "winner_id"):
                    r.winner_id = None
                if hasattr(r, "completed_at"):
                    r.completed_at = None
        for row in rows:
            row.status = "completed"
            row.completed_at = datetime.utcnow()
            row.winner_id = row.loser_id = None
            mark_end(row, END_CLEARED_BY_USER, by_user_id=action.user_id)
        # Deleting boards in this commit makes stale guarded writes fail rather
        # than resurrecting a cleared match on another worker.
        deleted = session.query(MatchState).filter(MatchState.match_id.in_(mids)).delete(
            synchronize_session=False)
        control.finish_in_session(session, action_id, {
            "match_ids": mids, "memory_keys": sorted(backup["memory"]),
            "saved_ids": save_ids}, backup)
        session.commit()
        from services.match_heartbeat_flags import decrement_active_matches
        for _ in range(deleted):
            decrement_active_matches(context)
        return save_ids
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def clear_handler(update, context):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None:
        return
    mh = _match()
    # Private-chat owners are not group admins. Bot admins may operate anywhere.
    from handlers.cipl_pause import _is_admin
    if not _is_admin(user.id) and (chat.type not in ("group", "supergroup") or
            not await mh._is_chat_admin(context, chat, user.id)):
        await msg.reply_text("🚫 Only an admin of this group or a bot admin can clear matches.")
        return
    mids, keys, signature = _targets(context, chat.id)
    if not mids and not keys:
        await msg.reply_text("✅ No ongoing matches or setups here.")
        return
    token = secrets.token_hex(8)
    context.bot_data[f"mctrl_{token}"] = {
        "user_id": user.id, "chat_id": chat.id, "signature": signature,
        "expires": time.time() + 120}
    # Reap previews lazily, avoiding unbounded growth without a job queue.
    for key, value in list(context.bot_data.items()):
        if isinstance(key, str) and key.startswith("mctrl_") and value["expires"] < time.time():
            context.bot_data.pop(key, None)
    setups = len([k for k in keys if not k.startswith("ms_")])
    await msg.reply_text(
        f"⚠️ Clear {len(mids)} match(es) and {setups} setup/lobby record(s) in this chat?\n"
        f"Match IDs: {', '.join(map(str, mids)) or 'none'}\n\n"
        "This costs 5 ranked/Elo points and 1 of your 5 shared pause/clear uses per "
        "rolling 24 hours. A 60-second cooldown applies.\n"
        "A backup will be kept. Undo is available for 10 minutes, before new play starts.\n"
        "Confirmation expires in 2 minutes.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear these matches", callback_data=f"mctrl_yes_{token}"),
            InlineKeyboardButton("Cancel", callback_data=f"mctrl_no_{token}")]]))


async def clear_callback(update, context):
    q = update.callback_query
    if q is None or q.message is None:
        return
    _, choice, token = q.data.split("_", 2)
    key = f"mctrl_{token}"
    preview = context.bot_data.get(key)
    if not preview or preview["expires"] < time.time():
        await q.answer("Confirmation expired. Run /clearmatches again.", show_alert=True)
        return
    if q.from_user.id != preview["user_id"] or q.message.chat.id != preview["chat_id"]:
        await q.answer("Only the admin who requested this can confirm it.", show_alert=True)
        return
    context.bot_data.pop(key, None)
    if choice != "yes":
        await q.answer("Cancelled.")
        await q.edit_message_text("Clear cancelled. No usage or rating penalty.")
        return
    await q.answer()
    mh = _match()
    from handlers.cipl_pause import _is_admin
    chat = q.message.chat
    if not _is_admin(q.from_user.id) and (chat.type not in ("group", "supergroup") or
            not await mh._is_chat_admin(context, chat, q.from_user.id)):
        await q.edit_message_text("🚫 You no longer have permission to clear this group.")
        return
    action_id = None
    try:
        async with _chat_locks.setdefault(chat.id, asyncio.Lock()):
            mids, keys, signature = _targets(context, chat.id)
            if signature != preview["signature"]:
                raise control.ControlDenied("The matches or setups changed. Run /clearmatches again.")
            async with AsyncExitStack() as stack:
                for mid in mids:
                    lock = mh.get_match_lock(mid)
                    await asyncio.wait_for(lock.acquire(), 15)
                    stack.callback(lock.release)
                if _targets(context, chat.id)[2] != signature:
                    raise control.ControlDenied("The matches changed. Run /clearmatches again.")
                if not _is_admin(q.from_user.id) and not await mh._is_chat_admin(
                        context, chat, q.from_user.id):
                    raise control.ControlDenied("You no longer have permission to clear this group.")
                from handlers import cipl_play
                if any(cipl_play._super_over_active(context, mid) for mid in mids):
                    raise control.ControlDenied("A Super Over is active. Finish it before clearing.")
                action_id = await asyncio.to_thread(
                    control.reserve, q.from_user.id, chat.id, "clearmatches")
                # The match locks are held while the backup and mutation commit.
                saved_ids = _clear(context, chat.id, mids, action_id)
                mh._clear_chat_memory(context, chat.id)
                for mid in mids:
                    cipl_play._cancel_timer(context, mid)
                    mh._cancel_all_match_timers(context, mid)
                    mh.cleanup_state(context, mid)
        await q.edit_message_text(
            f"🧹 Cleared {len(mids)} match(es). Ranked/Elo: −5 points.\n"
            f"Recorded as action #{action_id}. Undo within 10 minutes: /undoclear {action_id}\n"
            "Undo keeps the usage and rating penalty." + mh._saved_note(saved_ids),
            parse_mode="HTML")
    except (control.ControlDenied, asyncio.TimeoutError) as exc:
        await q.edit_message_text(str(exc) or "Match busy. Try /clearmatches again shortly.")
    except Exception:
        logger.exception("Safe clear failed in chat %s", chat.id)
        await q.message.reply_text("⚠️ Could not complete clearing. Check /controlhistory before retrying.")
    finally:
        if action_id is not None:
            await asyncio.to_thread(control.fail, action_id, "Clear did not complete")


def _undo(context, cid, action_id):
    from models import (Match, MatchState, MatchControlAction, TourMatch,
                        CLTourMatch, TournamentMatch, SavedMatch)
    from services.match_state_store import _cache_evict, _deserialize
    session = get_session()
    try:
        action = session.query(MatchControlAction).filter_by(id=action_id).with_for_update().first()
        if (action is None or action.chat_id != cid or action.command != "clearmatches"
                or action.outcome != "success" or not action.backup_json or action.undone_at):
            raise control.ControlDenied("No restorable clear action with that ID in this chat.")
        if datetime.utcnow() - action.finished_at > control.UNDO_TTL:
            raise control.ControlDenied("Undo expired. Saved over-by-over matches can still use /continue.")
        if _targets(context, cid)[:2] != ([], []):
            raise control.ControlDenied("This chat already has a match or setup. Undo would overwrite it.")
        backup = json.loads(action.backup_json)
        mids = sorted({data["match_id"] for data in backup["states"]}
                      | {data["id"] for data in backup["matches"]})
        if session.query(SavedMatch).filter(
                SavedMatch.match_id.in_(mids),
                SavedMatch.status.in_(("resuming", "resumed"))).first():
            raise control.ControlDenied("A cleared match is already resuming or resumed. Undo refused.")
        for data in backup["matches"]:
            row = session.query(Match).filter_by(id=data["id"]).with_for_update().first()
            if (row is None or row.status != "completed" or row.end_reason != "cleared_by_user"
                    or row.ended_by_id != action.user_id):
                raise control.ControlDenied("A cleared match has changed or resumed. Undo refused.")
            _restore(row, data)
        for name, cls in (("TourMatch", TourMatch), ("CLTourMatch", CLTourMatch),
                          ("TournamentMatch", TournamentMatch)):
            for data in backup["links"][name]:
                row = session.query(cls).filter_by(id=data["id"]).with_for_update().first()
                if row is None or row.match_id is not None or row.status not in ("scheduled", "pending"):
                    raise control.ControlDenied("A fixture has been reused. Undo refused.")
                _restore(row, data)
        for data in backup["states"]:
            if session.query(MatchState).filter_by(match_id=data["match_id"]).first():
                raise control.ControlDenied("A match board has already been restored elsewhere.")
            row = MatchState()
            # Another chat may have reused the deleted board's primary key.
            _restore(row, {k: v for k, v in data.items() if k != "id"})
            row.version = (row.version or 0) + 1
            session.add(row)
        session.query(SavedMatch).filter(SavedMatch.match_id.in_(mids)).update(
            {SavedMatch.status: "resumed"}, synchronize_session=False)
        action.undone_at = datetime.utcnow()
        session.commit()
        from services.match_heartbeat_flags import increment_active_matches
        for _ in backup["states"]:
            increment_active_matches(context)
        for mid in mids:
            _cache_evict(mid)
        context.bot_data.update(backup["memory"])
        for data in backup["states"]:
            context.bot_data[f"ms_{data['match_id']}"] = _deserialize(data["state_json"])
        from handlers.letsplay import _rearm_setup_timeout
        for key, draft in backup["memory"].items():
            if key.startswith("lp_") and isinstance(draft, dict) and draft.get("invite_id"):
                _rearm_setup_timeout(context, draft["invite_id"])
        return mids
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def undoclear_handler(update, context):
    from handlers.cipl_pause import _is_admin
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return
    if not _is_admin(user.id) and (chat.type not in ("group", "supergroup") or
            not await _match()._is_chat_admin(context, chat, user.id)):
        await update.effective_message.reply_text("🚫 Only a group admin or bot admin can undo a clear.")
        return
    try:
        if len(context.args) != 1:
            raise ValueError()
        action_id = int(context.args[0])
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Usage: /undoclear <action ID>")
        return
    try:
        async with _chat_locks.setdefault(chat.id, asyncio.Lock()):
            mids = _undo(context, chat.id, action_id)
        await update.effective_message.reply_text(
            "↩️ Clear undone. Usage and the −5 Elo penalty remain. Use /resume to reopen match buttons.")
        for mid in mids:
            from handlers import cipl_play
            from services.match_state_store import get_state
            state = get_state(context, mid)
            if cipl_play.is_cipl_state(state):
                cipl_play._schedule_cipl_recovery(context, mid)
            elif state:
                _match()._schedule_recovery(context, mid, "undo clear")
    except control.ControlDenied as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        logger.exception("Undo clear failed")
        await update.effective_message.reply_text("⚠️ Could not restore the clear. Try again shortly.")


async def history_handler(update, context):
    from handlers.cipl_pause import _is_admin
    tg_id = update.effective_user.id
    if context.args:
        if not _is_admin(tg_id):
            await update.effective_message.reply_text("Only bot admins can view another user's history.")
            return
        try:
            tg_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /controlhistory [Telegram user ID]")
            return
    rows = await asyncio.to_thread(control.history, tg_id)
    lines = [f"Pause / clear history for {tg_id} (latest 10):"]
    for row in rows:
        lines.append(f"#{row['id']} · {row['created_at']:%d %b %H:%M} UTC · "
                     f"/{row['command']} · {row['outcome']} · Elo {row['rating_delta']:+d} · "
                     f"chat {row['chat_id']}" + (" · undone" if row["undone_at"] else ""))
    await update.effective_message.reply_text("\n".join(lines) if rows else "No pause/clear records yet.")


async def access_handler(update, context):
    from handlers.cipl_pause import _is_admin
    if not _is_admin(update.effective_user.id):
        await update.effective_message.reply_text("🚫 Bot admins only.")
        return
    try:
        tg_id = int(context.args[0])
        mode = context.args[1].lower()
        if mode not in ("block", "unblock") or len(context.args) != 2:
            raise ValueError()
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Usage: /controlaccess <Telegram user ID> block|unblock")
        return
    await asyncio.to_thread(control.set_blocked, tg_id, mode == "block")
    await update.effective_message.reply_text(f"Pause/clear access for {tg_id}: {mode}ed.")
