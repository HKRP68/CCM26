"""Community mini-game Telegram surface: launch, announce, answer, reveal.

Message sending lives here; game logic lives in
:mod:`services.community_game_service`. The group answer router consumes
messages only while a game is live in the Official GC and never raises
``ApplicationHandlerStop`` — ordinary chat and the other social games keep
working alongside it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from services import community_game_service as G
from services import community_config_service as C
from services import community_rich as CR
from services import rich_message as R

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────

def _official_group_id():
    try:
        from services.gc_gate import official_group_id
        return official_group_id()
    except Exception:
        return None


def _user_for(session, tg_user):
    from models import User
    if tg_user is None:
        return None
    return (session.query(User)
            .filter(User.telegram_id == tg_user.id).first())


async def _send_card_photo(context, chat_id, player_id, *, blur=None,
                           spoiler=False, caption=None):
    """Render and send a player's card. Returns the sent message or None."""
    from database import get_session
    from models import Player
    session = get_session()
    try:
        player = session.get(Player, player_id) if player_id else None
        if player is None:
            return None
        raw = G.card_bytes(player)
        if not raw:
            return None
        data = raw
        if blur:
            data = G.blur_card_bytes(raw, blur) or raw
        bio = BytesIO(data)
        bio.name = "card.png"
        return await context.bot.send_photo(
            chat_id=chat_id, photo=bio, caption=caption, parse_mode="HTML",
            has_spoiler=bool(spoiler))
    except TelegramError:
        logger.info("card photo send failed for chat %s", chat_id, exc_info=True)
        return None
    except Exception:
        logger.exception("card photo render/send failed")
        return None
    finally:
        session.close()


async def _send_game_message(context, chat_id, game_type, payload, player_id):
    intro = CR.game_intro_html(game_type, payload)
    try:
        if game_type == "mystery":
            blur = int(payload.get("question", {}).get("blur", 16))
            msg = await _send_card_photo(context, chat_id, player_id,
                                         blur=blur, caption=intro)
            if msg is None:  # no card → text fallback
                msg = await context.bot.send_message(chat_id, intro, parse_mode="HTML")
            return msg
        if game_type == "fastestfingers":
            msg = await _send_card_photo(context, chat_id, player_id,
                                         spoiler=True, caption=intro)
            if msg is None:
                msg = await context.bot.send_message(chat_id, intro, parse_mode="HTML")
            return msg
        return await context.bot.send_message(chat_id, intro, parse_mode="HTML")
    except TelegramError:
        logger.info("game intro send failed in chat %s", chat_id, exc_info=True)
        return None


# ── Launch ──────────────────────────────────────────────────────────────

async def launch_and_announce(context, chat_id, game_type, *, source="auto",
                              launched_by=None, difficulty=None,
                              exclude_player_ids=None):
    """Generate, persist and announce one game. Returns (game_id, error)."""
    from database import get_session
    session = get_session()
    payload = None
    game_id = None
    try:
        cfg = C.get_config(session, fresh=True)
        if not cfg.get("enabled"):
            return None, "disabled"
        if not C.game_enabled(game_type, session):
            return None, "game_disabled"
        payload = G.generate_payload(session, game_type, cfg,
                                     exclude_ids=exclude_player_ids,
                                     difficulty=difficulty)
        if payload is None:
            return None, "no_payload"
        game = G.create_game(session, chat_id, payload, source=source,
                             launched_by=launched_by)
        if game is None:
            return None, "active_exists"
        session.commit()
        game_id = game.id
        player_id = game.player_id
    except Exception:
        session.rollback()
        logger.exception("launch_and_announce failed")
        return None, "error"
    finally:
        session.close()

    # Announce (outside the DB session).
    msg = await _send_game_message(context, chat_id, game_type, payload, player_id)

    # Persist the message id + schedule the timely reveal.
    if msg is not None:
        session = get_session()
        try:
            from models import CommunityGame
            g = session.get(CommunityGame, game_id)
            if g is not None:
                g.message_id = getattr(msg, "message_id", None)
                session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    try:
        if context.job_queue is not None:
            dur = int(payload.get("duration", 60))
            context.job_queue.run_once(
                _reveal_job, when=dur + 1,
                data={"game_id": game_id},
                name=f"community_reveal_{game_id}")
    except Exception:
        logger.debug("reveal job schedule failed (sweeper will handle)", exc_info=True)

    return game_id, None


# ── Answer router (group text) ──────────────────────────────────────────

async def community_game_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Consume answers for the active game in the Official GC, if any."""
    try:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or chat.type == "private":
            return
        text = message.text
        if not text or text.lstrip().startswith("/"):
            return
        # Edited messages/forwards are never answers.
        if getattr(message, "forward_origin", None) or getattr(message, "forward_date", None):
            return
        if not C.is_enabled():
            return
        official = _official_group_id()
        if official is None or chat.id != official:
            return
        if not G.has_active_game(chat.id):
            return

        server_now = datetime.utcnow()
        from database import get_session
        session = get_session()
        try:
            game = G.active_game_for_chat(session, chat.id)
            if game is None:
                G.mark_inactive(chat.id)
                return
            tg_user = update.effective_user
            user = _user_for(session, tg_user)
            if user is None or user.is_banned:
                return  # only registered, non-banned managers may play
            game_id = game.id
            result = G.submit_answer(
                session, game_id, user.id, tg_user.id, text,
                server_received_at=server_now,
                fuzzy=bool(C.get("fuzzy_matching", False, session=session)))
            # The "won" path commits inside submit_answer; persist the
            # attempt/participant mutations for every other path too.
            if result.get("status") not in ("won", "ignored"):
                session.commit()
        finally:
            session.close()

        await _handle_answer_result(update, context, result)
    except Exception:
        logger.exception("community_game_answer_handler failed (non-fatal)")


async def _handle_answer_result(update, context, result):
    status = result.get("status")
    message = update.effective_message
    if status == "won":
        await _announce_winner(context, update.effective_chat.id, result, update)
    elif status == "wordle":
        await _send_wordle_feedback(update, context, result)
    elif status == "exhausted":
        try:
            await message.reply_text(
                f"❌ You've used all {result.get('max_attempts')} attempts for this round.")
        except TelegramError:
            pass
    # wrong / ignored / closed / expired → stay silent to avoid flooding.


async def _send_wordle_feedback(update, context, result):
    """DM the Wordle feedback so the group isn't flooded; fall back to a short
    group reply only if the DM fails."""
    fb = result.get("feedback", "")
    attempts = result.get("attempts")
    maxa = result.get("max_attempts")
    body = f"🧩 <b>Cricket Wordle</b>\n{fb}\nAttempt {attempts}/{maxa}"
    user = update.effective_user
    try:
        await context.bot.send_message(user.id, body, parse_mode="HTML")
    except TelegramError:
        try:
            await update.effective_message.reply_text(
                f"{fb}  ({attempts}/{maxa}) — open a DM with me for private feedback.")
        except TelegramError:
            pass


async def _announce_winner(context, chat_id, result, update=None):
    game = result.get("game")
    award = result.get("award", {})
    from services.gc_gate import official_group_id  # noqa
    winner_name = _winner_display(update)
    label = G.GAME_LABEL.get(game.game_type, game.game_type)
    blocks, html = CR.winner_card(
        game_label=label, winner_name=winner_name,
        answer_name=result.get("answer_name") or "",
        response_ms=result.get("response_ms"),
        participants=result.get("participants"), award=award)
    try:
        await R.send_rich_message(context.bot, chat_id, blocks, html)
    except Exception:
        try:
            await context.bot.send_message(chat_id, html, parse_mode="HTML")
        except TelegramError:
            pass
    # Reveal the original card where there is a featured player.
    if game.player_id:
        await _send_card_photo(context, chat_id, game.player_id,
                               caption=f"✅ {CR.esc(result.get('answer_name') or '')}")


def _winner_display(update):
    u = update.effective_user if update else None
    if u is None:
        return "Winner"
    if u.username:
        return f"@{u.username}"
    return u.first_name or f"Manager {u.id}"


# ── Reveal / timeout ──────────────────────────────────────────────────

async def _reveal_job(context):
    data = getattr(context.job, "data", {}) or {}
    await reveal_if_due(context, data.get("game_id"))


async def reveal_if_due(context, game_id, *, now=None):
    """Expire an overdue, unwon game and post the answer. Row-locked so the
    run_once job and the scheduler sweep can't both post."""
    if not game_id:
        return
    from database import get_session
    from models import CommunityGame
    now = now or datetime.utcnow()
    session = get_session()
    reveal = None
    try:
        game = (session.query(CommunityGame)
                .filter(CommunityGame.id == game_id)
                .with_for_update().first())
        if game is None or game.status != "active":
            return
        if now < game.deadline:
            return
        import json
        ans = {}
        try:
            ans = json.loads(game.answer_json or "{}")
        except (ValueError, TypeError):
            ans = {}
        G.expire_game(session, game, now=now)
        session.commit()
        reveal = {"chat_id": game.chat_id, "game_type": game.game_type,
                  "answer_name": ans.get("name") or ans.get("word") or "",
                  "player_id": game.player_id}
    except Exception:
        session.rollback()
        logger.exception("reveal_if_due failed for game %s", game_id)
        return
    finally:
        session.close()

    if reveal is None:
        return
    label = G.GAME_LABEL.get(reveal["game_type"], reveal["game_type"])
    blocks, html = CR.timeout_card(game_label=label, answer_name=reveal["answer_name"])
    try:
        await R.send_rich_message(context.bot, reveal["chat_id"], blocks, html)
    except Exception:
        try:
            await context.bot.send_message(reveal["chat_id"], html, parse_mode="HTML")
        except TelegramError:
            pass
    if reveal["player_id"]:
        await _send_card_photo(context, reveal["chat_id"], reveal["player_id"],
                               caption=f"✅ {CR.esc(reveal['answer_name'])}")
