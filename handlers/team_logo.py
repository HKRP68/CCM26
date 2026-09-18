"""``/setteamlogo`` — a team crest, and the admin approval it needs first.

The crest a user uploads is drawn on every scorecard of every match their team
plays, in front of everyone in the chat, so it does not go live on upload. It
is queued as a :class:`models.TeamLogoRequest`, DM'd to every bot admin with
Approve / Reject buttons, and only applied when one of them says yes. A
rejection carries a reason, picked from a grid in one tap or typed.

Built from ``CommandHandler`` + ``CallbackQueryHandler`` + ``context.user_data``
rather than a ``ConversationHandler``, because this codebase has none — every
multi-step flow (trade, draft, XI picking, /setmilestone) is built this way.

Telegram ``file_id``s are kept in the database rather than paths, and the image
bytes go through ``asset_store``: the host filesystem is rebuilt on deploy, so
anything written only to disk is gone by the next release.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, TeamLogoRequest
from services import team_logo_service as tls
from services.activity_service import log_activity
from services.admin_ids import configured_admin_ids, configured_owner_ids, is_admin

logger = logging.getLogger(__name__)

CB = "tlogo"
# What the flow is waiting for, parked in user_data between messages. One slot
# for the uploader's image, one for an admin's typed rejection reason.
AWAIT_IMAGE = "tlogo_await_image"
AWAIT_REASON = "tlogo_await_reason"

# The review message is sent to admins while handling the *uploader's* update,
# so services/button_access.py must treat this prefix as shared — see the entry
# it has in SHARED_CALLBACK_PREFIXES. Authorisation happens here instead.
_NOT_ADMIN = "These buttons are for bot admins."


def _cb(action, request_id, value=None):
    return f"{CB}:{action}:{request_id}" + (f":{value}" if value is not None else "")


def _review_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=_cb("ok", request_id)),
        InlineKeyboardButton("❌ Reject", callback_data=_cb("no", request_id)),
    ]])


def _reason_keyboard(request_id):
    rows, row = [], []
    for key, label, _message in tls.REJECT_REASONS:
        row.append(InlineKeyboardButton(label, callback_data=_cb("r", request_id, key)))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✍️ Write my own",
                             callback_data=_cb("r", request_id, tls.CUSTOM_REASON_KEY)),
    ])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=_cb("back", request_id))])
    return InlineKeyboardMarkup(rows)


def _review_caption(request, uploader):
    who = uploader.username and f"@{uploader.username}" or (uploader.first_name or "—")
    return (
        "🛡 <b>Team logo awaiting review</b>\n\n"
        f"👤 {html.escape(str(who))} (<code>{uploader.telegram_id}</code>)\n"
        f"🏏 Team: <b>{html.escape(str(request.team_name or 'no team name'))}</b>\n"
        f"🖼 {request.width}×{request.height}, "
        f"{(request.byte_size or 0) / 1024:.0f} KB\n"
        f"🆔 Request #{request.id}"
    )


def _admin_ids():
    """Everyone who may decide. Owners are admins for this purpose."""
    return sorted(configured_admin_ids() | configured_owner_ids())


async def _dm(bot, chat_id, text, **kwargs):
    """Best-effort DM. A blocked admin must not fail the upload."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               disable_web_page_preview=True, **kwargs)
        return True
    except Exception as exc:
        logger.info("team logo DM to %s failed: %s", chat_id, exc)
        return False


async def _fan_out_review(context, request, uploader, png):
    """Send the review card to every admin. Returns how many got it.

    Each admin gets their own copy, and whoever presses first wins — the
    service refuses a second decision because the row is no longer pending, and
    the other copies are edited to say who decided.
    """
    caption = _review_caption(request, uploader)
    keyboard = _review_keyboard(request.id)
    sent = []
    for admin_id in _admin_ids():
        try:
            message = await context.bot.send_photo(
                chat_id=admin_id, photo=_fresh(png), caption=caption,
                parse_mode="HTML", reply_markup=keyboard)
            sent.append((admin_id, message.message_id))
        except Exception as exc:
            logger.info("team logo review DM to %s failed: %s", admin_id, exc)
    # Remembered so the losing copies can be tidied after a decision. bot_data
    # rather than user_data because the presser is not the uploader.
    context.bot_data.setdefault("tlogo_review_msgs", {})[request.id] = sent
    return len(sent)


def _fresh(png):
    """A new BytesIO per send — a failed upload consumes the buffer."""
    import io
    return io.BytesIO(png)


# ══════════════════════════════════════════════════════════════════════
# /setteamlogo
# ══════════════════════════════════════════════════════════════════════

async def setteamlogo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    tg_user = update.effective_user
    if message is None or tg_user is None:
        return
    if message.chat and message.chat.type != "private":
        await message.reply_text(
            "📩 Send /setteamlogo to me in a private chat — I need you to "
            "upload an image, and a group is not the place for it.")
        return

    arg = " ".join(context.args or []).strip().lower()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await message.reply_text("❌ Do /debut first!")
            return

        if arg in ("remove", "clear", "delete", "off"):
            removed = tls.remove_logo(session, user)
            log_activity(session, user.id, "teamlogo", "Team logo removed")
            session.commit()
            await message.reply_text(
                "🗑 Your team logo is removed. Your scorecards will show your "
                "team's initials again." if removed
                else "You do not have a team logo set.")
            return

        pending = tls.pending_request(session, user.id)
        if pending is not None:
            await message.reply_text(
                "⏳ <b>You already have a logo waiting for approval.</b>\n\n"
                f"Request #{pending.id}, sent "
                f"{pending.created_at:%d %b %H:%M} UTC.\n"
                "You will get a message here either way.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🚫 Withdraw it", callback_data=_cb("cancel", pending.id))]]))
            return

        blocked = tls.submission_block(session, user.id)
        if blocked:
            await message.reply_text(f"🛑 {blocked[1]}")
            return

        photo = _photo_from(message) or _photo_from(message.reply_to_message)
        if photo is None:
            context.user_data[AWAIT_IMAGE] = True
            current = ("\n\n🖼 You have a logo set already — a new one replaces "
                       "it once approved." if user.team_logo_asset_key else "")
            await message.reply_text(
                "🏏 <b>Set your team logo</b>\n\n"
                "Send me the image now, as a photo or a file.\n\n"
                "• PNG, JPG or WEBP\n"
                f"• at least {tls.MIN_DIM}×{tls.MIN_DIM}, square-ish works best\n"
                f"• up to {tls.MAX_BYTES // 1024 // 1024} MB\n"
                "• your own artwork only\n\n"
                "A bot admin checks it before it goes on your scorecards."
                f"{current}\n\nSend /cancel to stop.",
                parse_mode="HTML")
            return

        await _accept_upload(update, context, session, user, photo)
    except Exception:
        session.rollback()
        logger.exception("setteamlogo failed for %s", tg_user.id)
        await message.reply_text("⚠️ Something went wrong. Try again.")
    finally:
        session.close()


def _photo_from(message):
    """The best image on a message: the largest photo size, or an image file."""
    if message is None:
        return None
    if message.photo:
        return message.photo[-1]
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc
    return None


async def _accept_upload(update, context, session, user, photo):
    """Download, validate, queue, and fan out to the admins."""
    message = update.effective_message
    context.user_data.pop(AWAIT_IMAGE, None)
    try:
        handle = await context.bot.get_file(photo.file_id)
        raw = bytes(await handle.download_as_bytearray())
    except Exception:
        logger.exception("team logo download failed for %s", user.telegram_id)
        await message.reply_text("⚠️ I could not download that. Try again.")
        return

    result = tls.submit_logo(session, user, raw, file_id=photo.file_id)
    if not result["ok"]:
        if result["error"] == "pending":
            await message.reply_text(
                "⏳ You already have a logo waiting for approval.")
        elif result["error"] in ("daily_cap", "cooldown", "held"):
            await message.reply_text(f"🛑 {result['message']}")
        else:
            await message.reply_text(f"❌ {result.get('message', 'That did not work.')}")
        return

    request = result["request"]
    log_activity(session, user.id, "teamlogo", f"Logo submitted (#{request.id})")
    session.commit()

    reached = await _fan_out_review(context, request, user, result["png"])
    if reached:
        await message.reply_text(
            "✅ <b>Sent for approval.</b>\n\n"
            "A bot admin will check it shortly. You will get a message here "
            "either way — and if it is turned down, I will tell you why.",
            parse_mode="HTML")
    else:
        # The row is queued and /logoqueue will find it, so this is a delay
        # rather than a loss — say so rather than implying it vanished.
        await message.reply_text(
            "✅ <b>Sent for approval.</b>\n\n"
            "I could not reach an admin right now, so this may take a little "
            "longer. It is safely in the queue.", parse_mode="HTML")


async def team_logo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The private-chat catcher for an awaited image or a typed reject reason.

    Returns quickly on a miss so it never starves the other text handlers
    sharing this update.
    """
    message = update.effective_message
    tg_user = update.effective_user
    if message is None or tg_user is None:
        return
    data = context.user_data or {}

    pending_reason = data.get(AWAIT_REASON)
    if pending_reason and message.text:
        await _take_typed_reason(update, context, pending_reason, message.text)
        return

    if not data.get(AWAIT_IMAGE):
        return
    text = (message.text or "").strip().lower()
    if text in ("/cancel", "cancel"):
        context.user_data.pop(AWAIT_IMAGE, None)
        await message.reply_text("Cancelled. Nothing was sent for approval.")
        return
    photo = _photo_from(message)
    if photo is None:
        if message.text:
            await message.reply_text(
                "Send the image itself, as a photo or a file. "
                "Or /cancel to stop.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            context.user_data.pop(AWAIT_IMAGE, None)
            await message.reply_text("❌ Do /debut first!")
            return
        await _accept_upload(update, context, session, user, photo)
    except Exception:
        session.rollback()
        logger.exception("team logo upload failed for %s", tg_user.id)
        await message.reply_text("⚠️ Something went wrong. Try again.")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# The admin side
# ══════════════════════════════════════════════════════════════════════

async def team_logo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 3:
        await query.answer()
        return
    action, raw_id = parts[1], parts[2]
    value = parts[3] if len(parts) > 3 else None
    try:
        request_id = int(raw_id)
    except (TypeError, ValueError):
        await query.answer()
        return

    session = get_session()
    try:
        request = tls.get_request(session, request_id)
        if request is None:
            await query.answer("That request is gone.", show_alert=True)
            return

        if action == "cancel":
            await _withdraw(query, session, request)
            return
        if not is_admin(query.from_user.id):
            await query.answer(_NOT_ADMIN, show_alert=True)
            return
        if action == "ok":
            await _approve(query, context, session, request)
        elif action == "no":
            await _offer_reasons(query, request)
        elif action == "back":
            await query.answer()
            await _safe_markup(query, _review_keyboard(request.id))
        elif action == "r":
            await _reject_with(query, context, session, request, value)
        else:
            await query.answer()
    except Exception:
        session.rollback()
        logger.exception("tlogo callback failed (action=%s)", action)
        try:
            await query.answer("Something went wrong.", show_alert=True)
        except Exception:
            pass
    finally:
        session.close()


async def _safe_markup(query, markup):
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception:
        pass


async def _withdraw(query, session, request):
    """The uploader pulling their own request back."""
    if query.from_user.id != request.telegram_id:
        await query.answer("That is not your request.", show_alert=True)
        return
    result = tls.cancel_request(session, request)
    if not result["ok"]:
        await query.answer("That has already been decided.", show_alert=True)
        return
    session.commit()
    await query.answer("Withdrawn.")
    try:
        await query.edit_message_text(
            "🚫 Withdrawn. Run /setteamlogo whenever you want to try again.")
    except Exception:
        pass


async def _approve(query, context, session, request):
    result = tls.approve_request(session, request, reviewer=_reviewer(query))
    if not result["ok"]:
        await query.answer("Already decided.", show_alert=True)
        await _settle(context, request, f"Already decided ({request.status}).")
        return
    session.commit()
    await query.answer("Approved.")
    delivered = await _dm(
        context.bot, request.telegram_id,
        "🎉 <b>Your team logo is approved!</b>\n\n"
        "It will appear on your team's scorecards from your next match — and "
        "on the cards of matches you have already played, next time they are "
        "shown.")
    await _settle(context, request,
                  f"✅ Approved by {html.escape(_reviewer(query))}."
                  + ("" if delivered else " (Could not DM the owner.)"))


async def _offer_reasons(query, request):
    await query.answer()
    try:
        await query.edit_message_reply_markup(
            reply_markup=_reason_keyboard(request.id))
    except Exception:
        await query.answer("Could not open the reasons — try again.",
                           show_alert=True)


async def _reject_with(query, context, session, request, reason_key):
    if reason_key == tls.CUSTOM_REASON_KEY:
        context.user_data[AWAIT_REASON] = request.id
        await query.answer()
        await _dm(context.bot, query.from_user.id,
                  f"✍️ Type the reason for turning down request "
                  f"#{request.id}.\n\nIt is sent to the owner exactly as you "
                  f"write it. Send /cancel to stop.")
        return

    entry = tls.REJECT_REASON_MAP.get(reason_key)
    if entry is None:
        await query.answer()
        return
    await _finish_rejection(query, context, session, request, entry[1])


async def _take_typed_reason(update, context, request_id, text):
    """The admin's free-text reason, arriving as the next private message."""
    message = update.effective_message
    if text.strip().lower() in ("/cancel", "cancel"):
        context.user_data.pop(AWAIT_REASON, None)
        await message.reply_text("Cancelled — the request is still pending.")
        return
    if not is_admin(update.effective_user.id):
        context.user_data.pop(AWAIT_REASON, None)
        return

    session = get_session()
    try:
        request = tls.get_request(session, request_id)
        if request is None:
            context.user_data.pop(AWAIT_REASON, None)
            await message.reply_text("That request is gone.")
            return
        reviewer = _name_of(update.effective_user)
        result = tls.reject_request(session, request, reviewer=reviewer,
                                    note=text.strip())
        if not result["ok"]:
            context.user_data.pop(AWAIT_REASON, None)
            await message.reply_text("That request has already been decided.")
            return
        session.commit()
        context.user_data.pop(AWAIT_REASON, None)
        delivered = await _notify_rejection(context, request, text.strip())
        await message.reply_text(
            f"❌ Request #{request.id} rejected."
            + (" Owner notified." if delivered
               else " ⚠️ Could not DM the owner."))
        await _settle(context, request, f"❌ Rejected by {html.escape(reviewer)}.")
    except Exception:
        session.rollback()
        logger.exception("typed rejection failed for request %s", request_id)
        await message.reply_text("⚠️ Something went wrong.")
    finally:
        session.close()


async def _finish_rejection(query, context, session, request, note):
    reviewer = _reviewer(query)
    result = tls.reject_request(session, request, reviewer=reviewer, note=note)
    if not result["ok"]:
        await query.answer("Already decided.", show_alert=True)
        await _settle(context, request, f"Already decided ({request.status}).")
        return
    session.commit()
    await query.answer("Rejected.")
    delivered = await _notify_rejection(context, request, note)
    await _settle(context, request,
                  f"❌ Rejected by {html.escape(reviewer)}."
                  + ("" if delivered else " (Could not DM the owner.)"))


async def _notify_rejection(context, request, note):
    return await _dm(
        context.bot, request.telegram_id,
        "❌ <b>Your team logo was not approved.</b>\n\n"
        f"<b>Reason:</b> {html.escape(str(note))}\n\n"
        "Run /setteamlogo to send a different one whenever you like.")


async def _settle(context, request, footer):
    """Close out every admin's copy of a decided request."""
    sent = (context.bot_data.get("tlogo_review_msgs") or {}).pop(request.id, [])
    for chat_id, message_id in sent:
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=f"🛡 Request #{request.id}\n{footer}",
                parse_mode="HTML", reply_markup=None)
        except Exception:
            pass


def _reviewer(query):
    return _name_of(query.from_user)


def _name_of(tg_user):
    if tg_user is None:
        return "admin"
    return (tg_user.username and f"@{tg_user.username}") or \
        (tg_user.first_name or str(tg_user.id))


# ══════════════════════════════════════════════════════════════════════
# /logoqueue — the recovery path when a DM was missed
# ══════════════════════════════════════════════════════════════════════

async def logounhold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``/logounhold <telegram id>`` — let a held user try again.

    Three rejections in a row pauses someone's uploads. This is the way back,
    and it is deliberately a command rather than an automatic expiry: a hold
    means an admin decided three times, so a human should decide to lift it.
    """
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update.effective_user.id):
        await message.reply_text("That command is bot-admin only.")
        return
    if not context.args:
        await message.reply_text(
            "Usage: /logounhold &lt;telegram id&gt;\n"
            "The id is on the review card, under the uploader's name.",
            parse_mode="HTML")
        return
    try:
        target = int(str(context.args[0]).lstrip("@"))
    except (TypeError, ValueError):
        await message.reply_text("That is not a Telegram id.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == target).first()
        if user is None:
            await message.reply_text("No account with that Telegram id.")
            return
        cleared = tls.clear_hold(session, user.id)
        session.commit()
        if not cleared:
            await message.reply_text("That user is not on hold.")
            return
        delivered = await _dm(
            context.bot, target,
            "✅ <b>You can send a team logo again.</b>\n\n"
            "Run /setteamlogo whenever you are ready — please read the earlier "
            "reasons first.")
        await message.reply_text(
            f"✅ Hold lifted for <code>{target}</code>."
            + (" User notified." if delivered else " ⚠️ Could not DM them."),
            parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("logounhold failed for %s", target)
        await message.reply_text("⚠️ Something went wrong.")
    finally:
        session.close()


async def logoqueue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None or not is_admin(update.effective_user.id):
        await message.reply_text("That command is bot-admin only.")
        return

    session = get_session()
    try:
        requests = tls.list_requests(session, status=tls.STATUS_PENDING, limit=10)
        if not requests:
            await message.reply_text("✅ Nothing waiting — the logo queue is empty.")
            return
        total = tls.pending_count(session)
        await message.reply_text(
            f"🛡 <b>{total} logo{'s' if total != 1 else ''} waiting.</b>"
            + ("\nShowing the oldest 10." if total > len(requests) else ""),
            parse_mode="HTML")
        for request in requests:
            uploader = (session.query(User)
                        .filter(User.id == request.user_id).first())
            if uploader is None:
                continue
            png = tls.logo_bytes_for_key(request.asset_key)
            caption = _review_caption(request, uploader)
            try:
                if png:
                    await context.bot.send_photo(
                        chat_id=message.chat_id, photo=_fresh(png),
                        caption=caption, parse_mode="HTML",
                        reply_markup=_review_keyboard(request.id))
                else:
                    await message.reply_text(
                        caption + "\n\n⚠️ The image is missing from storage.",
                        parse_mode="HTML",
                        reply_markup=_review_keyboard(request.id))
            except Exception:
                logger.exception("logoqueue could not show request %s", request.id)
    except Exception:
        logger.exception("logoqueue failed")
        await message.reply_text("⚠️ Something went wrong.")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# /previewsummary — see the card without playing a match
# ══════════════════════════════════════════════════════════════════════

async def previewsummary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render a summary card from canned data, with the caller's own crest.

    Worth a command because the alternatives are both bad: play a whole match,
    or change the Scorecard Designer and hope. It draws through the same
    ``scorecard_delivery`` path a real match uses, so what comes back is what a
    match would produce — current theme, current logos, current text settings.
    """
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update.effective_user.id):
        await message.reply_text("That command is bot-admin only.")
        return

    session = get_session()
    try:
        viewer = (session.query(User)
                  .filter(User.telegram_id == update.effective_user.id).first())
        team = (viewer.team_name if viewer and viewer.team_name
                else "Rome Gladiators")
    finally:
        session.close()

    payload = {
        "inn1_team": team, "inn1_runs": 156, "inn1_wickets": 7,
        "inn1_overs": "20",
        "inn2_team": "Mumbai Marathas", "inn2_runs": 158, "inn2_wickets": 4,
        "inn2_overs": "16.5",
        "winner_name": "Mumbai Marathas", "win_margin_text": "by 6 wickets",
        "overs_total": 20, "match_no": 98,
        "stadium": "Singapore National Stadium",
        "potm_name": "Ben Stokes", "potm_team": "Mumbai Marathas",
        "potm_stats": "52* (31)", "potm_runs": 52, "potm_balls": 31,
        "potm_fours": 4, "potm_sixes": 2, "potm_sr": 167.7,
        "top_per_team": {
            "inn1": {"team": team,
                     "batters": [
                         {"name": "Shimron Hetmyer", "runs": 33, "balls": 22, "out": True},
                         {"name": "Venkatesh Iyer", "runs": 29, "balls": 15, "out": False},
                         {"name": "Glenn Maxwell", "runs": 27, "balls": 18, "out": True},
                         {"name": "Nitish Rana", "runs": 22, "balls": 14, "out": True}],
                     "bowlers": [
                         {"name": "Ben Stokes", "wickets": 4, "runs": 27, "overs": "4"},
                         {"name": "Sikandar Raza", "wickets": 1, "runs": 15, "overs": "2"},
                         {"name": "Mitchell Starc", "wickets": 1, "runs": 18, "overs": "4"},
                         {"name": "Jason Holder", "wickets": 1, "runs": 32, "overs": "4"}]},
            "inn2": {"team": "Mumbai Marathas",
                     "batters": [
                         {"name": "Ben Stokes", "runs": 52, "balls": 31, "out": False},
                         {"name": "Ravindra Jadeja", "runs": 29, "balls": 18, "out": True},
                         {"name": "Joe Root", "runs": 27, "balls": 17, "out": True},
                         {"name": "Suryakumar Yadav", "runs": 18, "balls": 9, "out": True}],
                     "bowlers": [
                         {"name": "James Anderson", "wickets": 1, "runs": 20, "overs": "4"},
                         {"name": "Glenn Maxwell", "wickets": 1, "runs": 21, "overs": "4"},
                         {"name": "Jason Holder", "wickets": 1, "runs": 43, "overs": "3"},
                         {"name": "Sikandar Raza", "wickets": 1, "runs": 28, "overs": "3.5"}]},
        },
    }

    try:
        from services import scorecard_delivery
        png = await scorecard_delivery.render_card_async(
            scorecard_delivery.CARD_SUMMARY, payload)
    except Exception:
        logger.exception("previewsummary render failed")
        png = None
    if not png:
        await message.reply_text(
            "⚠️ The card did not render. Check the logs — a missing font or a "
            "bad Scorecard Designer value would do it.")
        return
    await context.bot.send_photo(
        chat_id=message.chat_id, photo=_fresh(png),
        caption=(f"🖼 Preview — canned data, live theme.\n"
                 f"Innings 1 is <b>{html.escape(team)}</b>, so its crest is "
                 f"yours if you have one approved."),
        parse_mode="HTML")
