"""Telegram side of CMU News and polls: admin review DMs and channel posts.

Plain Bot API calls over ``requests`` rather than python-telegram-bot, because
the callers are the website (a Flask thread) and the game-side auto-story hooks,
neither of which has a running bot ``context``. Every function is best-effort
and never raises: a Telegram hiccup must not fail a publish that already
committed.
"""

import html
import json
import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)


def _token():
    return os.getenv("BOT_TOKEN", "").strip()


def _post(method, data, files=None, timeout=12):
    token = _token()
    if not token:
        return None
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/{method}"
        if files:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=data, timeout=timeout)
        body = resp.json()
        if not body.get("ok"):
            logger.warning("telegram %s failed: %s", method, body.get("description"))
        return body
    except Exception:
        logger.exception("telegram %s failed", method)
        return None


def _send(chat_id, text, *, markup=None, photo=None, photo_type="image/jpeg"):
    if photo:
        data = {"chat_id": chat_id, "caption": text[:1024], "parse_mode": "HTML"}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        ext = "png" if photo_type == "image/png" else "jpg"
        return _post("sendPhoto", data, files={"photo": (f"news.{ext}", photo, photo_type)})
    data = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if markup:
        data["reply_markup"] = markup
    return _post("sendMessage", data)


def _open_button(label, tab):
    """URL button that opens the Mini App on ``tab`` (works in groups/channels)."""
    try:
        from services.miniapp_buttons import miniapp_deep_link
        link = miniapp_deep_link(tab)
    except Exception:
        link = None
    if not link:
        return None
    return {"inline_keyboard": [[{"text": label, "url": link}]]}


def _preview(body, limit=500):
    body = (body or "").strip()
    return body if len(body) <= limit else body[:limit].rsplit(" ", 1)[0] + "…"


# ── Admin review ────────────────────────────────────────────────────────────

def review_markup(article_id):
    return {"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": f"news:ap:{article_id}"},
         {"text": "❌ Reject", "callback_data": f"news:rj:{article_id}"}],
    ]}


def review_caption(article):
    return (f"📰 <b>News submission #{article.id}</b>\n"
            f"By {html.escape(article.author_name or 'a player')}"
            f" (<code>{article.author_telegram_id or '-'}</code>)\n"
            f"Reward on approval: {int(article.reward_coins or 0)} coins\n\n"
            f"<b>{html.escape(article.headline)}</b>\n\n"
            f"{html.escape(_preview(article.body, 700))}")


def send_review_to_admins(article_id):
    """DM every bot admin a review card for a pending article."""
    from database import get_session
    from models import NewsArticle
    from services.admin_ids import configured_admin_ids
    from services import news_service
    db = get_session()
    try:
        article = db.get(NewsArticle, article_id)
        if article is None or article.status != news_service.STATUS_PENDING:
            return 0
        photo, ptype = news_service.image_bytes(db, article)
        caption = review_caption(article)
        sent = 0
        for admin_id in sorted(configured_admin_ids()):
            body = _send(admin_id, caption, markup=review_markup(article.id),
                         photo=photo, photo_type=ptype or "image/jpeg")
            if body and body.get("ok"):
                sent += 1
        return sent
    except Exception:
        logger.exception("news: review fan-out failed for #%s", article_id)
        return 0
    finally:
        db.close()


def notify_author(article, *, approved, paid=0, note=None):
    if not article.author_telegram_id:
        return
    if approved:
        text = (f"🎉 Your story <b>{html.escape(article.headline)}</b> is now live "
                f"in CMU News!" + (f"\n💰 +{paid} coins added to your balance." if paid else ""))
        markup = _open_button("📰 Read it in the Mini App", f"news_{article.id}")
    else:
        text = (f"📰 Your story <b>{html.escape(article.headline)}</b> was not approved."
                + (f"\nReason: {html.escape(note)}" if note else ""))
        markup = None
    _send(article.author_telegram_id, text, markup=markup)


def notify_author_by_id(article_id, approved, paid=0):
    """Thread-safe wrapper: re-reads the article in its own session."""
    from database import get_session
    from models import NewsArticle
    db = get_session()
    try:
        article = db.get(NewsArticle, article_id)
        if article is not None:
            notify_author(article, approved=approved, paid=paid,
                          note=article.review_note)
    except Exception:
        logger.exception("news: author notify failed for #%s", article_id)
    finally:
        db.close()


# ── Channel / group announcements ───────────────────────────────────────────

def _targets(db):
    from services.referral_service import get_branding
    branding = get_branding(db)
    out = []
    for key in ("channel_username", "group_username"):
        name = branding.get(key)
        if name:
            out.append(f"@{name}")
    return out


def announce_article(article_id):
    """Post a published article to the branding channel and group."""
    from database import get_session
    from models import NewsArticle
    from services import news_service
    db = get_session()
    try:
        article = db.get(NewsArticle, article_id)
        if article is None or article.status != news_service.STATUS_PUBLISHED:
            return 0
        photo, ptype = news_service.image_bytes(db, article)
        text = (f"📰 <b>CMU NEWS</b>\n\n<b>{html.escape(article.headline)}</b>\n\n"
                f"{html.escape(_preview(article.body, 600 if photo else 1500))}")
        markup = _open_button("📖 Read full story", f"news_{article.id}")
        sent = 0
        for chat in _targets(db):
            body = _send(chat, text, markup=markup, photo=photo,
                         photo_type=ptype or "image/jpeg")
            if body and body.get("ok"):
                sent += 1
        return sent
    except Exception:
        logger.exception("news: announce failed for #%s", article_id)
        return 0
    finally:
        db.close()


def announce_poll(poll_id):
    from database import get_session
    from models import Poll
    from services import poll_service
    db = get_session()
    try:
        poll = db.get(Poll, poll_id)
        if poll is None:
            return 0
        options = "\n".join(f"• {html.escape(o)}" for o in poll_service.options(poll))
        reward = (f"\n\n🪙 Vote and earn <b>{poll.reward_coins}</b> coins!"
                  if poll.reward_coins else "")
        text = (f"🗳 <b>NEW POLL</b>\n\n<b>{html.escape(poll.question)}</b>\n\n{options}"
                f"{reward}\n\n⏳ Closes {poll.ends_at + timedelta(hours=5, minutes=30):%d %b, %I:%M %p} IST")
        markup = _open_button("🗳 Vote in the Mini App", f"poll_{poll.id}")
        sent = 0
        for chat in _targets(db):
            body = _send(chat, text, markup=markup)
            if body and body.get("ok"):
                sent += 1
        return sent
    except Exception:
        logger.exception("news: poll announce failed for #%s", poll_id)
        return 0
    finally:
        db.close()


def in_background(func, *args):
    """Run ``func(*args)`` on a daemon thread — Flask requests never wait on Telegram."""
    import threading
    try:
        threading.Thread(target=func, args=args, daemon=True).start()
    except Exception:
        logger.exception("news: could not spawn %s", getattr(func, "__name__", func))
