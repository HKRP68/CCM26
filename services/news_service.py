"""CMU News — the articles in the Mini App's community strip.

Three ways a story gets here:

* **Admins** publish from the website (``/news``). Live immediately.
* **Players** submit from the Mini App. Held ``pending`` until a bot admin
  approves it from their DM or the website — the same rule team logos follow
  (see services/team_logo_service.py), because an article is shown to every
  player who opens the app.
* **The game** writes its own (:func:`auto_story`) when something worth
  shouting about happens: a tournament champion, a record auction buy, the
  season's winners. These are best-effort by construction — a news failure
  must never fail the match or payout that triggered it.

Services never commit; the caller does. Images are re-encoded to JPEG and kept
in ``stored_assets`` through the caller's session (the reason is the same as
``team_logo_service._store``: a second pooled session mid-transaction is the
pool-exhaustion trap).
"""

import hashlib
import io
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"

SOURCE_ADMIN = "admin"
SOURCE_USER = "user"
SOURCE_AUTO = "auto"

FEED_LIMIT = 5
# "Today's CMU News": the unread badge only counts the last day's stories, so
# someone who never opens the section is not nagged by a 40-unread pill.
UNREAD_WINDOW = timedelta(hours=24)

HEADLINE_MIN, HEADLINE_MAX = 8, 160
BODY_MIN, BODY_MAX = 30, 5000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_MAX_WIDTH = 1280
# Every submission is a DM an admin has to action.
DAILY_SUBMISSION_CAP = 3

REACTIONS = ("👍", "🔥", "😂", "😮")

REJECT_REASONS = {
    "spam": "Looks like spam or advertising.",
    "rude": "Contains abusive or inappropriate content.",
    "dup": "This story has already been covered.",
    "low": "Needs more detail — please expand the article and try again.",
}

# Auto-story kinds, with the label the website shows beside each switch.
AUTO_KINDS = {
    "tournament_champion": "🏆 Tournament champions",
    "auction_record": "💰 Record auction buys",
    "auction_complete": "🔨 Auction wrap-ups",
    "season_winners": "📅 Monthly season winners",
    "ranked_season": "🥇 Ranked ladder champions",
    "hall_of_fame": "🌟 Hall of Fame records",
}

_ASSET_PREFIX = "data/news"


# ── Settings ────────────────────────────────────────────────────────────────

def _config(db):
    from models import GameConfig
    try:
        return db.query(GameConfig).first()
    except Exception:
        logger.exception("news: could not read game config")
        return None


def settings(db):
    cfg = _config(db)
    raw_kinds = getattr(cfg, "news_auto_kinds", None) if cfg else None
    if raw_kinds is None:
        kinds = set(AUTO_KINDS)
    else:
        kinds = {k.strip() for k in raw_kinds.split(",") if k.strip() in AUTO_KINDS}
    return {
        "submit_reward_coins": int(getattr(cfg, "news_submit_reward_coins", 100) or 0)
        if cfg else 100,
        "auto_publish": bool(getattr(cfg, "news_auto_publish", True)) if cfg else True,
        "auto_announce": bool(getattr(cfg, "news_auto_announce", False)) if cfg else False,
        "auto_kinds": kinds,
    }


def save_settings(db, *, submit_reward_coins, auto_publish, auto_announce, auto_kinds):
    from models import GameConfig
    cfg = db.query(GameConfig).first()
    if cfg is None:
        cfg = GameConfig()
        db.add(cfg)
    cfg.news_submit_reward_coins = max(0, int(submit_reward_coins or 0))
    cfg.news_auto_publish = bool(auto_publish)
    cfg.news_auto_announce = bool(auto_announce)
    cfg.news_auto_kinds = ",".join(k for k in AUTO_KINDS if k in set(auto_kinds))


# ── Images ──────────────────────────────────────────────────────────────────

def normalise_image(raw):
    """Validate an upload and re-encode it as a JPEG no wider than 1280px.

    Re-encoding drops EXIF (phone photos carry GPS) and animation frames.
    Raises ValueError with a message meant for the uploader.
    """
    from PIL import Image
    if not raw:
        raise ValueError("The image came through empty — try again.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"The image is {len(raw) / 1024 / 1024:.1f} MB. "
                         f"The limit is {MAX_IMAGE_BYTES // 1024 // 1024} MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise ValueError("That does not look like an image. Use a JPG or PNG.")
    if min(image.size) < 120:
        raise ValueError("The image is too small — use one at least 120px on each side.")
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (17, 24, 39))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    if image.width > IMAGE_MAX_WIDTH:
        ratio = IMAGE_MAX_WIDTH / image.width
        image = image.resize((IMAGE_MAX_WIDTH, max(1, int(image.height * ratio))),
                             Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _store_image(db, key, data, content_type, uploaded_by=None):
    from models import StoredAsset
    row = db.query(StoredAsset).filter(StoredAsset.key == key).first()
    if row is None:
        row = StoredAsset(key=key)
        db.add(row)
    row.filename = key.rsplit("/", 1)[-1]
    row.content_type = content_type
    row.data = data
    row.byte_size = len(data)
    row.sha256 = hashlib.sha256(data).hexdigest()
    if uploaded_by:
        row.updated_by = str(uploaded_by)[:100]


def attach_image(db, article, data, *, content_type="image/jpeg", uploaded_by=None):
    """Store already-normalised image bytes for ``article`` (which needs an id)."""
    if not data:
        return
    if article.id is None:
        db.flush()
    ext = "png" if content_type == "image/png" else "jpg"
    key = f"{_ASSET_PREFIX}/{article.id}.{ext}"
    _store_image(db, key, data, content_type, uploaded_by)
    article.image_key = key


def image_bytes(db, article):
    """Return ``(bytes, content_type)`` for an article's image, or ``(None, None)``."""
    from models import StoredAsset
    if not article or not article.image_key:
        return None, None
    row = db.query(StoredAsset).filter(StoredAsset.key == article.image_key).first()
    if row is None or not row.data:
        return None, None
    return row.data, row.content_type or "image/jpeg"


def _drop_image(db, article):
    from models import StoredAsset
    if article.image_key:
        (db.query(StoredAsset).filter(StoredAsset.key == article.image_key)
         .delete(synchronize_session=False))
        article.image_key = None


# ── Validation ──────────────────────────────────────────────────────────────

def _clean(headline, body):
    headline = " ".join((headline or "").split())
    body = (body or "").replace("\r\n", "\n").strip()
    if len(headline) < HEADLINE_MIN:
        raise ValueError(f"Headline needs at least {HEADLINE_MIN} characters.")
    if len(headline) > HEADLINE_MAX:
        raise ValueError(f"Headline is too long (max {HEADLINE_MAX}).")
    if len(body) < BODY_MIN:
        raise ValueError(f"Article needs at least {BODY_MIN} characters.")
    if len(body) > BODY_MAX:
        raise ValueError(f"Article is too long (max {BODY_MAX}).")
    return headline, body


def _author_name(user):
    try:
        from services.display_name import manager_name
        return manager_name(user)[:80]
    except Exception:
        return (getattr(user, "first_name", None) or getattr(user, "username", None)
                or "Manager")[:80]


# ── Writing ─────────────────────────────────────────────────────────────────

def create_admin_article(db, *, headline, body, image=None, image_type="image/jpeg",
                         pinned=False, admin_name="admin"):
    from models import NewsArticle
    headline, body = _clean(headline, body)
    now = datetime.utcnow()
    article = NewsArticle(headline=headline, body=body, status=STATUS_PUBLISHED,
                          source=SOURCE_ADMIN, author_name="CMU Desk",
                          reviewed_by=admin_name, is_pinned=bool(pinned),
                          created_at=now, published_at=now, decided_at=now)
    db.add(article)
    db.flush()
    if image:
        attach_image(db, article, image, content_type=image_type, uploaded_by=admin_name)
    return article


def submission_block(db, user):
    """Return a reason the user cannot submit right now, or None."""
    from models import NewsArticle
    since = datetime.utcnow() - timedelta(days=1)
    recent = (db.query(NewsArticle)
              .filter(NewsArticle.author_user_id == user.id,
                      NewsArticle.source == SOURCE_USER,
                      NewsArticle.created_at >= since)
              .count())
    if recent >= DAILY_SUBMISSION_CAP:
        return (f"You can submit {DAILY_SUBMISSION_CAP} stories a day. "
                "Please try again tomorrow.")
    return None


def submit_user_article(db, user, *, headline, body, image_raw=None):
    """Create a pending article from a player. Raises ValueError for the user."""
    from models import NewsArticle
    headline, body = _clean(headline, body)
    blocked = submission_block(db, user)
    if blocked:
        raise ValueError(blocked)
    image = normalise_image(image_raw) if image_raw else None
    article = NewsArticle(headline=headline, body=body, status=STATUS_PENDING,
                          source=SOURCE_USER, author_user_id=user.id,
                          author_telegram_id=user.telegram_id,
                          author_name=_author_name(user),
                          reward_coins=settings(db)["submit_reward_coins"])
    db.add(article)
    db.flush()
    if image:
        attach_image(db, article, image, uploaded_by=f"user:{user.telegram_id}")
    return article


def approve(db, article, *, reviewer=None, reward_coins=None):
    """Publish a pending article and pay its author once.

    Returns the coins paid (0 when none), or None when the article was not
    pending — so the bot and the website can race without double-publishing.
    """
    from models import User
    if article is None or article.status != STATUS_PENDING:
        return None
    now = datetime.utcnow()
    article.status = STATUS_PUBLISHED
    article.published_at = now
    article.decided_at = now
    article.reviewed_by = (reviewer or "admin")[:80]
    if reward_coins is not None:
        article.reward_coins = max(0, int(reward_coins))
    paid = 0
    if (article.source == SOURCE_USER and article.author_user_id
            and not article.reward_paid and (article.reward_coins or 0) > 0):
        user = db.get(User, article.author_user_id)
        if user is not None:
            paid = int(article.reward_coins)
            user.total_coins = (user.total_coins or 0) + paid
            try:
                from services.activity_service import log_activity
                log_activity(db, user.id, "news_reward",
                             f"News approved: {article.headline[:80]}",
                             coins_change=paid)
            except Exception:
                logger.exception("news: activity log failed")
    article.reward_paid = True
    return paid


def reject(db, article, *, reviewer=None, note=None):
    """Reject a pending article. Returns False if it was not pending."""
    if article is None or article.status != STATUS_PENDING:
        return False
    article.status = STATUS_REJECTED
    article.decided_at = datetime.utcnow()
    article.reviewed_by = (reviewer or "admin")[:80]
    article.review_note = (note or "")[:300] or None
    _drop_image(db, article)
    return True


def reason_text(key_or_text):
    return REJECT_REASONS.get(key_or_text, key_or_text)


def set_status(db, article, status):
    if status not in (STATUS_PUBLISHED, STATUS_ARCHIVED):
        raise ValueError("bad status")
    article.status = status
    if status == STATUS_PUBLISHED and not article.published_at:
        article.published_at = datetime.utcnow()


def delete(db, article):
    from models import NewsRead, NewsReaction
    _drop_image(db, article)
    for model in (NewsRead, NewsReaction):
        (db.query(model).filter(model.article_id == article.id)
         .delete(synchronize_session=False))
    db.delete(article)


# ── Reading ─────────────────────────────────────────────────────────────────

def _time_key(article):
    return article.published_at or article.created_at or datetime.min


def _card(article, *, is_read=False):
    return {
        "id": article.id,
        "headline": article.headline,
        "has_image": bool(article.image_key),
        "image_url": f"/api/news/{article.id}/image" if article.image_key else None,
        "published_at": (_time_key(article)).isoformat() + "Z",
        "source": article.source,
        "kind": article.kind,
        "author": article.author_name,
        "pinned": bool(article.is_pinned),
        "is_read": bool(is_read),
        "views": int(article.view_count or 0),
    }


def unread_count(db, user):
    from models import NewsArticle, NewsRead
    since = datetime.utcnow() - UNREAD_WINDOW
    read_ids = db.query(NewsRead.article_id).filter(NewsRead.user_id == user.id)
    return (db.query(NewsArticle)
            .filter(NewsArticle.status == STATUS_PUBLISHED,
                    NewsArticle.published_at >= since,
                    ~NewsArticle.id.in_(read_ids))
            .count())


def list_feed(db, user, limit=FEED_LIMIT):
    from models import NewsArticle, NewsRead
    rows = (db.query(NewsArticle)
            .filter(NewsArticle.status == STATUS_PUBLISHED)
            .order_by(NewsArticle.is_pinned.desc(), NewsArticle.published_at.desc(),
                      NewsArticle.id.desc())
            .limit(limit).all())
    ids = [r.id for r in rows]
    read = set()
    if ids:
        read = {aid for (aid,) in db.query(NewsRead.article_id)
                .filter(NewsRead.user_id == user.id, NewsRead.article_id.in_(ids))}
    return {"articles": [_card(r, is_read=r.id in read) for r in rows],
            "unread_count": unread_count(db, user)}


def reaction_counts(db, article_id):
    from sqlalchemy import func
    from models import NewsReaction
    counts = dict(db.query(NewsReaction.emoji, func.count(NewsReaction.id))
                  .filter(NewsReaction.article_id == article_id)
                  .group_by(NewsReaction.emoji).all())
    return {e: int(counts.get(e, 0)) for e in REACTIONS}


def get_article(db, user, article_id):
    """Full article for the reader; marks it read (first view counts once)."""
    from models import NewsArticle, NewsRead, NewsReaction
    article = db.get(NewsArticle, int(article_id))
    if article is None or article.status != STATUS_PUBLISHED:
        return None
    seen = (db.query(NewsRead)
            .filter(NewsRead.article_id == article.id, NewsRead.user_id == user.id)
            .first())
    if seen is None:
        db.add(NewsRead(article_id=article.id, user_id=user.id))
        article.view_count = (article.view_count or 0) + 1
        db.flush()
    mine = (db.query(NewsReaction.emoji)
            .filter(NewsReaction.article_id == article.id,
                    NewsReaction.user_id == user.id)
            .scalar())
    data = _card(article, is_read=True)
    data.update({
        "body": article.body,
        "reactions": reaction_counts(db, article.id),
        "my_reaction": mine,
        "unread_count": unread_count(db, user),
    })
    return data


def react(db, user, article_id, emoji):
    """Set, change, or (same emoji again) clear the user's reaction."""
    from models import NewsArticle, NewsReaction
    if emoji not in REACTIONS:
        raise ValueError("Unknown reaction.")
    article = db.get(NewsArticle, int(article_id))
    if article is None or article.status != STATUS_PUBLISHED:
        raise ValueError("Article not found.")
    row = (db.query(NewsReaction)
           .filter(NewsReaction.article_id == article.id,
                   NewsReaction.user_id == user.id)
           .first())
    mine = emoji
    if row is None:
        db.add(NewsReaction(article_id=article.id, user_id=user.id, emoji=emoji))
    elif row.emoji == emoji:
        db.delete(row)
        mine = None
    else:
        row.emoji = emoji
    db.flush()
    return {"reactions": reaction_counts(db, article.id), "my_reaction": mine}


def pending_count(db):
    from models import NewsArticle
    try:
        return db.query(NewsArticle).filter(NewsArticle.status == STATUS_PENDING).count()
    except Exception:
        return 0


# ── Auto stories ────────────────────────────────────────────────────────────

def auto_story(db, kind, dedupe_key, headline, body, *, kicker=None, image_png=None,
               render_banner=True):
    """Write a game-generated story. Never raises; returns the article or None.

    Runs in a savepoint so a failure here rolls back only the story, never the
    caller's match result or payout. ``dedupe_key`` is unique, so a hook that
    fires twice (a retry, a double callback) writes one story.
    """
    from models import NewsArticle
    try:
        conf = settings(db)
        if kind not in AUTO_KINDS or kind not in conf["auto_kinds"]:
            return None
        if dedupe_key and (db.query(NewsArticle.id)
                           .filter(NewsArticle.dedupe_key == dedupe_key).first()):
            return None
        headline = " ".join((headline or "").split())[:HEADLINE_MAX]
        body = (body or "").strip()[:BODY_MAX] or headline
        if image_png is None and render_banner:
            try:
                from services.news_banner import render_banner as _render
                image_png = _render(kind, headline, kicker=kicker)
            except Exception:
                logger.exception("news: banner render failed for %s", kind)
                image_png = None
        now = datetime.utcnow()
        publish = conf["auto_publish"]
        with db.begin_nested():
            article = NewsArticle(
                headline=headline, body=body, source=SOURCE_AUTO, kind=kind,
                dedupe_key=(dedupe_key or None) and dedupe_key[:120],
                status=STATUS_PUBLISHED if publish else STATUS_PENDING,
                author_name="CMU Newsroom",
                reviewed_by="auto" if publish else None,
                published_at=now if publish else None,
                decided_at=now if publish else None,
            )
            db.add(article)
            db.flush()
            if image_png:
                attach_image(db, article, image_png, content_type="image/png",
                             uploaded_by="auto")
                db.flush()
        logger.info("news: auto story #%s (%s) %s", article.id, kind, headline)
        if publish and conf["auto_announce"]:
            _queue_announce(article.id)
        return article
    except Exception:
        logger.exception("news: auto story %s failed", kind)
        return None


def _queue_announce(article_id):
    """Announce after the caller commits: a short delay then a fresh read."""
    import threading

    def _later():
        import time
        time.sleep(3)
        try:
            from services.news_announce import announce_article
            announce_article(article_id)
        except Exception:
            logger.exception("news: auto announce failed")

    try:
        threading.Thread(target=_later, daemon=True).start()
    except Exception:
        logger.exception("news: could not spawn announce thread")
