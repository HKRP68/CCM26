"""Admin panel for Cricket Bot — manage players (CRUD).
Shares the same database as the bot. Any changes here reflect in the bot instantly.
"""

import os
import io
import csv
import json
import zipfile
import html as html_lib
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("admin")
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, send_file
from sqlalchemy import func, or_, desc, asc
from dotenv import load_dotenv

load_dotenv()

# ── Import shared DB and models ─────────────────────────────────────
from database import get_session, init_db
from services.telegram_user_service import user_lookup_filter
from models import (Player, User, Trade, UserStats, UserRoster, ActivityLog,
                    PlayerGameStats, AdminLog, Match, UserAchievement,
                    Trait, PlayerTrait, TraitInventory, TraitMarket, TraitDaily,
                    BotTeam, BotTeamPlayer,
                    Quest, UserQuestProgress,
                    UserReport,
                    CommentaryEntry,
                    NotificationSchedule, NotificationLog,
                    ClaimRarityTier, GameConfig,
                    MessageTemplate,
                    GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase)

app = Flask(__name__)

# ── Secret key — MUST be stable across restarts ───────────────────────
# Why: Flask signs session cookies with this. CSRF tokens are stored in
# the session. If the secret changes between requests, ALL sessions become
# invalid → "CSRF session token is missing" errors on every form submit.
#
# Render free tier spins down idle services; every wake-up would have
# generated a fresh os.urandom value, breaking all logged-in sessions.
#
# Resolution order:
#   1. ADMIN_SECRET env var (best — set this in production)
#   2. Derive from BOT_TOKEN hash (stable as long as BOT_TOKEN is stable)
#   3. Fresh random (last resort — logs a loud warning)
import hashlib as _hashlib
_admin_secret = os.getenv("ADMIN_SECRET", "").strip()
if _admin_secret:
    app.secret_key = _admin_secret
elif os.getenv("BOT_TOKEN", "").strip():
    # Derive a stable 32-byte hex secret from the bot token. Anyone with the
    # bot token could compute this, but they could also do far worse with it
    # directly — acceptable fallback. Far better than fresh random per restart.
    _seed = "cricmaster-admin-secret-v1::" + os.getenv("BOT_TOKEN", "")
    app.secret_key = _hashlib.sha256(_seed.encode()).hexdigest()
    import logging as _l
    _l.getLogger("admin").warning(
        "ADMIN_SECRET env var not set — derived from BOT_TOKEN hash. "
        "For production, set ADMIN_SECRET to a random 32+ char string."
    )
else:
    app.secret_key = os.urandom(24).hex()
    import logging as _l
    _l.getLogger("admin").error(
        "CRITICAL: Neither ADMIN_SECRET nor BOT_TOKEN is set! "
        "Sessions and CSRF will break on restart. Set ADMIN_SECRET now."
    )

# ── Security configuration ────────────────────────────────────────────
# Cookie hardening: HttpOnly stops JS reading the cookie, SameSite mitigates
# CSRF, Secure-only when running over HTTPS (production).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("ADMIN_HTTPS", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),  # idle timeout
    WTF_CSRF_TIME_LIMIT=86400,  # 24h CSRF token validity
)

# CSRF protection for all POST/PUT/DELETE forms
try:
    from flask_wtf.csrf import CSRFProtect, generate_csrf, CSRFError
    csrf = CSRFProtect(app)

    @app.context_processor
    def _inject_csrf():
        # Makes csrf_token() available in every template
        return {"csrf_token": generate_csrf}

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(e):
        """Show a friendly message and bounce back to a fresh page.

        Common cause: session expired (idle timeout, server restart, or
        user had a stale tab open across a deploy). Bouncing to /login
        with a clear message lets them re-authenticate cleanly instead of
        seeing a raw 400.
        """
        import logging
        logging.getLogger("admin").warning(
            f"CSRF error on {request.path}: {e.description}"
        )
        from flask import flash, redirect, url_for, session as _sess
        # Clear stale session — they need a fresh one
        _sess.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("login"))
except ImportError:
    # flask-wtf not available — log a warning, fall back to no-op
    import logging
    logging.getLogger("admin").warning(
        "flask-wtf not installed; CSRF protection DISABLED. "
        "Add 'flask-wtf' to requirements.txt to enable.")
    csrf = None


def csrf_exempt(view):
    """Mark a Flask view as exempt from CSRF protection.

    Used for Mini App API endpoints which authenticate via Telegram's
    initData HMAC signature instead of CSRF tokens.
    """
    if csrf is not None:
        return csrf.exempt(view)
    return view


ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
# If ADMIN_PASSWORD_HASH is set (bcrypt hash format starting with $2b$), it
# takes priority over the plaintext ADMIN_PASSWORD. Generate one with:
#   python3 -c "import bcrypt; print(bcrypt.hashpw(b'mypassword', bcrypt.gensalt()).decode())"
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

PER_PAGE = 30

# ── Login rate limiting (in-memory, per-process) ──────────────────────
# Locks login for an IP after too many failed attempts. Reset every hour.
_LOGIN_ATTEMPTS = {}  # {ip: [count, first_attempt_dt]}
_LOGIN_LOCKOUT_THRESHOLD = 5
_LOGIN_LOCKOUT_WINDOW_SEC = 600  # 10 minutes
_LOGIN_LOCKOUT_DURATION_SEC = 1800  # 30 minutes


@app.template_filter("fromjson")
def _fromjson_filter(s):
    """Parse a JSON string in templates. Returns [] on failure."""
    if not s:
        return []
    try:
        import json as _j
        v = _j.loads(s)
        return v if v is not None else []
    except Exception:
        return []


# All stored datetimes are UTC. The website displays everything in IST.
from datetime import timedelta as _timedelta
_IST_OFFSET = _timedelta(hours=5, minutes=30)


@app.template_filter("ist")
def _ist_filter(dt, fmt="%Y-%m-%d %H:%M"):
    """Convert a naive-UTC datetime to IST and format it.
    Usage in templates: {{ some_dt | ist }} or {{ some_dt | ist('%d %b %Y, %I:%M %p') }}
    """
    if not dt:
        return "—"
    try:
        return (dt + _IST_OFFSET).strftime(fmt) + " IST"
    except Exception:
        return str(dt)


@app.template_filter("ist_short")
def _ist_short_filter(dt):
    """IST without the ' IST' suffix, for compact columns."""
    if not dt:
        return "—"
    try:
        return (dt + _IST_OFFSET).strftime("%d %b %H:%M")
    except Exception:
        return str(dt)


@app.template_filter("ist_input")
def _ist_input_filter(dt):
    """UTC datetime → IST value for a <input type=datetime-local> (no suffix)."""
    if not dt:
        return ""
    try:
        return (dt + _IST_OFFSET).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


@app.errorhandler(500)
@app.errorhandler(Exception)
def _handle_internal_error(e):
    """Catch-all error handler — shows traceback inline so issues are debuggable.
    HTTP exceptions (including 400 CSRF errors and 401/403/404) keep their
    original status code and don't get a traceback shown to the user."""
    # HTTP exceptions: pass through with their actual status
    try:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
    except Exception:
        pass

    import traceback
    tb = traceback.format_exc()
    try:
        import logging
        logging.getLogger("admin").error(f"Internal error on {request.path}: {tb}")
    except Exception:
        pass
    safe_tb = tb.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html><head><title>Error</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#0f1419; color:#dde; padding:2rem; max-width:1100px; margin:0 auto; }}
  pre {{ background:#1a1f24; padding:1rem; border-radius:8px; overflow-x:auto; font-size:.85rem; line-height:1.4; }}
  .err {{ color:#ff6b6b; font-weight:600; font-size:1.1rem; margin-bottom:.5rem; }}
  a {{ color:#4cafef; }}
</style></head><body>
<h1>⚠️ Internal Error</h1>
<div class="err">{type(e).__name__}: {str(e)[:300]}</div>
<p>Path: <code>{request.path}</code></p>
<p>Send this traceback to fix the bug:</p>
<pre>{safe_tb}</pre>
<p><a href="/">← Back to dashboard</a></p>
</body></html>""", 500


# ── Helpers ──────────────────────────────────────────────────────────

def tier_css(rating: int) -> str:
    if rating >= 95:   return "legendary"
    elif rating >= 90: return "epic"
    elif rating >= 85: return "rare"
    elif rating >= 80: return "uncommon"
    elif rating >= 70: return "common"
    else:              return "basic"


def log_admin(db, action, target_type=None, target_id=None, target_name=None, detail=None):
    """Write an entry to the admin log."""
    try:
        ip = request.remote_addr if request else None
        entry = AdminLog(
            action=action, target_type=target_type, target_id=target_id,
            target_name=target_name, detail=detail, ip_address=ip,
        )
        db.add(entry)
    except Exception:
        pass


def _verify_password(submitted: str) -> bool:
    """Check the password against (in priority order):
      1. ADMIN_PASSWORD_HASH bcrypt hash, if set
      2. ADMIN_PASSWORD plaintext
    """
    if not submitted:
        return False
    if ADMIN_PASSWORD_HASH and ADMIN_PASSWORD_HASH.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(submitted.encode("utf-8"),
                                  ADMIN_PASSWORD_HASH.encode("utf-8"))
        except Exception:
            return False
    # Constant-time comparison even for plaintext path
    import hmac
    return hmac.compare_digest(submitted, ADMIN_PASSWORD)


def _client_ip():
    """Best-effort client IP including proxy header."""
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "unknown")


def _is_login_locked(ip: str):
    """Returns (locked: bool, retry_after_seconds: int)."""
    rec = _LOGIN_ATTEMPTS.get(ip)
    if not rec:
        return (False, 0)
    count, first_at = rec
    age_sec = (datetime.utcnow() - first_at).total_seconds()
    # Window expired — reset
    if age_sec > _LOGIN_LOCKOUT_DURATION_SEC:
        _LOGIN_ATTEMPTS.pop(ip, None)
        return (False, 0)
    if count >= _LOGIN_LOCKOUT_THRESHOLD:
        retry = int(_LOGIN_LOCKOUT_DURATION_SEC - age_sec)
        return (True, max(1, retry))
    return (False, 0)


def _record_login_failure(ip: str):
    rec = _LOGIN_ATTEMPTS.get(ip)
    if rec:
        count, first_at = rec
        age_sec = (datetime.utcnow() - first_at).total_seconds()
        if age_sec > _LOGIN_LOCKOUT_WINDOW_SEC:
            # Stale window — restart counter
            _LOGIN_ATTEMPTS[ip] = [1, datetime.utcnow()]
        else:
            _LOGIN_ATTEMPTS[ip] = [count + 1, first_at]
    else:
        _LOGIN_ATTEMPTS[ip] = [1, datetime.utcnow()]


def _record_login_success(ip: str):
    _LOGIN_ATTEMPTS.pop(ip, None)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        # Idle timeout check: if last activity is older than the configured
        # lifetime, log out. Using session cookie expiry alone isn't enough
        # because Flask only refreshes the cookie on response.
        last_seen = session.get("last_seen")
        if last_seen:
            try:
                ts = datetime.fromisoformat(last_seen)
                if (datetime.utcnow() - ts).total_seconds() > 86400:
                    session.clear()
                    flash("Session expired. Please log in again.", "info")
                    return redirect(url_for("login"))
            except Exception:
                pass
        session["last_seen"] = datetime.utcnow().isoformat()
        session.permanent = True  # uses PERMANENT_SESSION_LIFETIME
        return f(*args, **kwargs)
    return decorated


# ── Auth ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = _client_ip()
    locked, retry = _is_login_locked(ip)
    if locked:
        flash(f"Too many failed attempts. Try again in {retry // 60}m {retry % 60}s.", "error")
        return render_template("login.html"), 429

    if request.method == "POST":
        submitted = request.form.get("password", "")
        if _verify_password(submitted):
            _record_login_success(ip)
            session.clear()  # rotate session ID on login
            session["admin"] = True
            session["last_seen"] = datetime.utcnow().isoformat()
            session.permanent = True
            try:
                # Log success (best-effort)
                db = get_session()
                try:
                    log_admin(db, "login_success", "auth", 0, ip, "Admin login")
                    db.commit()
                finally:
                    db.close()
            except Exception:
                pass
            return redirect(url_for("dashboard"))
        # Failed
        _record_login_failure(ip)
        try:
            db = get_session()
            try:
                log_admin(db, "login_fail", "auth", 0, ip,
                          f"Failed login attempt (count: {_LOGIN_ATTEMPTS.get(ip, [0])[0]})")
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        flash("Wrong password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Dashboard ────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    db = get_session()
    try:
        from models import Match
        now = datetime.utcnow()

        # Operations counts
        live_matches = db.query(func.count(Match.id)).filter(
            Match.status == "active").scalar() or 0
        pending_matches = db.query(func.count(Match.id)).filter(
            Match.status == "pending").scalar() or 0
        today_0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        completed_today = db.query(func.count(Match.id)).filter(
            Match.status == "completed", Match.completed_at >= today_0).scalar() or 0

        total_users = db.query(func.count(User.id)).scalar() or 0
        dau = db.query(func.count(User.id)).filter(
            User.last_match_date >= now - timedelta(days=1)).scalar() or 0
        wau = db.query(func.count(User.id)).filter(
            User.last_match_date >= now - timedelta(days=7)).scalar() or 0
        new_today = db.query(func.count(User.id)).filter(
            User.created_at >= today_0).scalar() or 0
        new_this_week = db.query(func.count(User.id)).filter(
            User.created_at >= now - timedelta(days=7)).scalar() or 0
        banned = db.query(func.count(User.id)).filter(
            User.is_banned == True).scalar() or 0

        # 14-day user growth + match-activity sparklines
        growth, match_activity = [], []
        for i in range(13, -1, -1):
            day_start = (now - timedelta(days=i)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            growth.append({
                "label": day_start.strftime("%d %b"),
                "count": db.query(func.count(User.id)).filter(
                    User.created_at >= day_start, User.created_at < day_end).scalar() or 0,
            })
            match_activity.append({
                "label": day_start.strftime("%d %b"),
                "count": db.query(func.count(Match.id)).filter(
                    Match.completed_at >= day_start, Match.completed_at < day_end).scalar() or 0,
            })
        mg = max((g["count"] for g in growth), default=1) or 1
        for g in growth: g["pct"] = round(g["count"] / mg * 100)
        mm = max((m["count"] for m in match_activity), default=1) or 1
        for m in match_activity: m["pct"] = round(m["count"] / mm * 100)

        # Recent admin actions + top users
        from models import AdminLog
        recent_logs = (db.query(AdminLog).order_by(AdminLog.timestamp.desc())
                        .limit(10).all())
        top_users = (db.query(User).order_by(User.matches_won.desc())
                      .limit(5).all())

        # Player snapshot (kept compact)
        total_players = db.query(func.count(Player.id)).scalar() or 0
        active_players = db.query(func.count(Player.id)).filter(
            Player.is_active == True).scalar() or 0
        total_trades = db.query(func.count(Trade.id)).scalar() or 0

        stats = {
            "live_matches": live_matches, "pending_matches": pending_matches,
            "completed_today": completed_today,
            "total_users": total_users, "dau": dau, "wau": wau,
            "new_today": new_today, "new_this_week": new_this_week,
            "banned": banned,
            "total_players": total_players, "active_players": active_players,
            "total_trades": total_trades,
        }

        # Rating distribution (compact)
        tier_defs = [
            ("95-100", "legendary", "#e6ac00", 95, 100),
            ("90-94",  "epic",      "#9b59b6", 90, 94),
            ("85-89",  "rare",      "#2980b9", 85, 89),
            ("80-84",  "uncommon",  "#27ae60", 80, 84),
            ("75-79",  "common",    "#7f8c8d", 75, 79),
            ("70-74",  "common",    "#7f8c8d", 70, 74),
            ("65-69",  "basic",     "#95a5a6", 65, 69),
            ("60-64",  "basic",     "#95a5a6", 60, 64),
            ("55-59",  "basic",     "#95a5a6", 55, 59),
            ("50-54",  "basic",     "#bdc3c7", 50, 54),
        ]
        max_count = 1; tiers = []
        for label, css, color, lo, hi in tier_defs:
            count = db.query(func.count(Player.id)).filter(
                Player.rating >= lo, Player.rating <= hi).scalar()
            max_count = max(max_count, count)
            tiers.append({"label": label, "css": css, "color": color,
                          "count": count, "pct": 0})
        for t in tiers:
            t["pct"] = round(t["count"] / max_count * 100) if max_count else 0

        countries = (db.query(Player.country, func.count(Player.id).label("count"))
                      .group_by(Player.country).order_by(func.count(Player.id).desc())
                      .limit(10).all())
        countries = [{"country": c, "count": n} for c, n in countries]

        # Maintenance state (for the dashboard banner)
        try:
            from services.maintenance_service import is_maintenance_active
            from services.config_service import get_config as _gc
            _cfg = _gc()
            maint_active = is_maintenance_active(_cfg)
            maint_until = _cfg.get("maintenance_until")
        except Exception:
            maint_active = False
            maint_until = None

        return render_template("dashboard.html", stats=stats, tiers=tiers,
                               countries=countries, growth=growth,
                               match_activity=match_activity,
                               recent_logs=recent_logs, top_users=top_users,
                               maint_active=maint_active, maint_until=maint_until)
    finally:
        db.close()


# ── Player list ──────────────────────────────────────────────────────

@app.route("/players")
@login_required
def players_list():
    db = get_session()
    try:
        q = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        country_filter = request.args.get("country", "").strip()
        rating_range = request.args.get("rating_range", "").strip()
        bat_hand = request.args.get("bat_hand", "").strip()
        bowl_hand = request.args.get("bowl_hand", "").strip()
        is_active = request.args.get("is_active", "").strip()
        buypl_filter = request.args.get("buypl", "").strip()
        # Numeric ranges
        rating_min = _parse_int(request.args.get("rating_min", ""))
        rating_max = _parse_int(request.args.get("rating_max", ""))
        bat_rating_min = _parse_int(request.args.get("bat_rating_min", ""))
        bat_rating_max = _parse_int(request.args.get("bat_rating_max", ""))
        bowl_rating_min = _parse_int(request.args.get("bowl_rating_min", ""))
        bowl_rating_max = _parse_int(request.args.get("bowl_rating_max", ""))
        version_mode = request.args.get("version_mode", "").strip()
        version_filter = request.args.get("version", "").strip()
        sort = request.args.get("sort", "rating_desc").strip()
        page = max(1, int(request.args.get("page", 1)))

        # Rating range map: label -> (min, max)
        RANGE_MAP = {
            "95-100": (95, 100),
            "90-94":  (90, 94),
            "85-89":  (85, 89),
            "80-84":  (80, 84),
            "75-79":  (75, 79),
            "70-74":  (70, 74),
            "65-69":  (65, 69),
            "60-64":  (60, 64),
            "55-59":  (55, 59),
            "50-54":  (50, 54),
        }

        query = db.query(Player)

        if q:
            query = query.filter(Player.name.ilike(f"%{q}%"))
        if category:
            query = query.filter(Player.category == category)
        if country_filter:
            query = query.filter(Player.country == country_filter)
        if rating_range and rating_range in RANGE_MAP:
            r_min, r_max = RANGE_MAP[rating_range]
            query = query.filter(Player.rating >= r_min, Player.rating <= r_max)
        if rating_min is not None:
            query = query.filter(Player.rating >= rating_min)
        if rating_max is not None:
            query = query.filter(Player.rating <= rating_max)
        if bat_rating_min is not None:
            query = query.filter(Player.bat_rating >= bat_rating_min)
        if bat_rating_max is not None:
            query = query.filter(Player.bat_rating <= bat_rating_max)
        if bowl_rating_min is not None:
            query = query.filter(Player.bowl_rating >= bowl_rating_min)
        if bowl_rating_max is not None:
            query = query.filter(Player.bowl_rating <= bowl_rating_max)
        if bat_hand:
            query = query.filter(Player.bat_hand == bat_hand)
        if bowl_hand:
            query = query.filter(Player.bowl_hand == bowl_hand)
        if is_active == "1":
            query = query.filter(Player.is_active == True)
        elif is_active == "0":
            query = query.filter(Player.is_active == False)
        if buypl_filter == "blocked":
            query = query.filter(Player.restricted_from_buypl == True)
        elif buypl_filter == "available":
            query = query.filter(
                (Player.restricted_from_buypl == False) |
                (Player.restricted_from_buypl.is_(None))
            )
        if version_mode == "base":
            query = query.filter(Player.parent_player_id.is_(None))
        elif version_mode == "version":
            query = query.filter(Player.parent_player_id.isnot(None))
        if version_filter:
            query = query.filter(Player.version == version_filter)

        # Sorting
        sort_map = {
            "name_asc": (Player.name.asc(),),
            "name_desc": (Player.name.desc(),),
            "rating_desc": (Player.rating.desc(), Player.name.asc()),
            "rating_asc": (Player.rating.asc(), Player.name.asc()),
            "category_asc": (Player.category.asc(), Player.rating.desc()),
            "country_asc": (Player.country.asc(), Player.rating.desc()),
            "bat_rating_desc": (Player.bat_rating.desc(), Player.name.asc()),
            "bat_rating_asc": (Player.bat_rating.asc(), Player.name.asc()),
            "bowl_rating_desc": (Player.bowl_rating.desc(), Player.name.asc()),
            "bowl_rating_asc": (Player.bowl_rating.asc(), Player.name.asc()),
        }
        order_by = sort_map.get(sort, sort_map["rating_desc"])
        query = query.order_by(*order_by)

        total = query.count()
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)

        players = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
        for p in players:
            p._tier_css = tier_css(p.rating)

        categories = [r[0] for r in db.query(Player.category).distinct().order_by(Player.category).all()]
        countries = [r[0] for r in db.query(Player.country).distinct().order_by(Player.country).all()]
        versions = [r[0] for r in (db.query(Player.version)
                                   .filter(Player.version.isnot(None))
                                   .distinct().order_by(Player.version).all())
                    if r[0]]

        # Set of player IDs with active custom images (for 🎨 indicator)
        from services.player_image_service import list_players_with_custom_images
        custom_image_ids = list_players_with_custom_images(db)

        # Build active filter chips
        active_filters = []
        if q: active_filters.append(("Search", q))
        if category: active_filters.append(("Category", category))
        if country_filter: active_filters.append(("Country", country_filter))
        if rating_range: active_filters.append(("Rating tier", rating_range))
        if rating_min is not None or rating_max is not None:
            active_filters.append(("Rating", f"{rating_min or 0}–{rating_max or 100}"))
        if bat_rating_min is not None or bat_rating_max is not None:
            active_filters.append(("Bat", f"{bat_rating_min or 0}–{bat_rating_max or 100}"))
        if bowl_rating_min is not None or bowl_rating_max is not None:
            active_filters.append(("Bowl", f"{bowl_rating_min or 0}–{bowl_rating_max or 100}"))
        if bat_hand: active_filters.append(("Bat hand", bat_hand))
        if bowl_hand: active_filters.append(("Bowl hand", bowl_hand))
        if is_active == "1": active_filters.append(("Status", "Active"))
        elif is_active == "0": active_filters.append(("Status", "Inactive"))
        if version_mode == "base": active_filters.append(("Type", "Base only"))
        elif version_mode == "version": active_filters.append(("Type", "Versions only"))
        if version_filter: active_filters.append(("Version", version_filter))
        if buypl_filter == "blocked": active_filters.append(("/buypl", "Blocked"))
        elif buypl_filter == "available": active_filters.append(("/buypl", "Available"))

        download_args = request.args.to_dict(flat=True)
        download_args.pop("page", None)
        download_args.pop("format", None)
        download_urls = {
            "csv": url_for("players_download", **download_args, format="csv"),
            "json": url_for("players_download", **download_args, format="json"),
            "xlsx": url_for("players_download", **download_args, format="xlsx"),
        }

        return render_template(
            "players.html",
            players=players, total=total, page=page, total_pages=total_pages,
            q=q, category=category, country_filter=country_filter,
            rating_range=rating_range,
            rating_ranges=list(RANGE_MAP.keys()),
            bat_hand=bat_hand, bowl_hand=bowl_hand, is_active=is_active,
            buypl_filter=buypl_filter,
            sort=sort,
            categories=categories, countries=countries, versions=versions,
            custom_image_ids=custom_image_ids, download_urls=download_urls,
            active_filters=active_filters,
            rating_min=rating_min, rating_max=rating_max,
            bat_rating_min=bat_rating_min, bat_rating_max=bat_rating_max,
            bowl_rating_min=bowl_rating_min, bowl_rating_max=bowl_rating_max,
            version_mode=version_mode, version_filter=version_filter,
        )
    finally:
        db.close()


# ── Download all players as CSV ──────────────────────────────────────

@app.route("/players/download")
@login_required
def players_download():
    db = get_session()
    try:
        export_format = (request.args.get("format") or "csv").strip().lower()
        if export_format in ("excel", "xls"):
            export_format = "xlsx"
        if export_format not in ("csv", "json", "xlsx"):
            export_format = "csv"

        RANGE_MAP = {
            "95-100": (95, 100), "90-94": (90, 94), "85-89": (85, 89),
            "80-84": (80, 84), "75-79": (75, 79), "70-74": (70, 74),
            "65-69": (65, 69), "60-64": (60, 64), "55-59": (55, 59), "50-54": (50, 54),
        }

        filters = _parse_player_filter_args(request.args)
        query = _apply_player_filters(db.query(Player), filters, RANGE_MAP)
        players = query.order_by(Player.rating.desc(), Player.name.asc(), Player.id.asc()).all()
        rows = [_player_export_dict(p) for p in players]

        log_admin(db, "players_download", detail=f"Downloaded {len(players)} players as {export_format.upper()}")
        db.commit()

        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        if export_format == "json":
            payload = json.dumps(rows, ensure_ascii=False, indent=2)
            return Response(
                payload,
                mimetype="application/json",
                headers={"Content-Disposition": f"attachment; filename=players_{timestamp}.json"},
            )

        if export_format == "xlsx":
            workbook = _build_players_xlsx(rows)
            return send_file(
                workbook,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=f"players_{timestamp}.xlsx",
            )

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=(list(rows[0].keys()) if rows else [
            "id", "name", "rating", "category", "country", "bat_hand",
            "bowl_hand", "bowl_style", "bat_rating", "bowl_rating",
            "version", "parent_player_id", "is_active", "restricted_from_buypl",
        ]))
        writer.writeheader()
        writer.writerows(rows)
        csv_data = output.getvalue()
        output.close()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=players_{timestamp}.csv"},
        )
    finally:
        db.close()


# ── Bulk upload players via CSV text ─────────────────────────────────

@app.route("/players/bulk-upload", methods=["GET", "POST"])
@login_required
def players_bulk_upload():
    if request.method == "POST":
        csv_text = request.form.get("csv_text", "").strip()
        if not csv_text:
            flash("Please paste some CSV data.", "error")
            return redirect(url_for("players_bulk_upload"))

        db = get_session()
        try:
            added = 0; skipped = 0; errors = []
            added_by_version = {}  # for the success flash
            # Cache of {name: base_player_id} for Base rows added in this batch,
            # so a Legend row appearing AFTER its Base in the same upload can
            # still link parent_player_id correctly (the Base row isn't queryable
            # via db.query until flushed, and we want to avoid flushing on every
            # row for performance).
            batch_base_ids = {}
            # Parse CSV
            reader = csv.reader(io.StringIO(csv_text))
            rows = list(reader)

            # Detect if first row is a header
            has_header = False
            if rows and rows[0]:
                first = rows[0][0].strip().lower()
                if first in ("name", "player", "player_name", "player name"):
                    has_header = True

            start_idx = 1 if has_header else 0

            for i, row in enumerate(rows[start_idx:], start=start_idx + 1):
                if not row or not row[0].strip():
                    continue
                try:
                    # Format: Name, Rating, Category, Country, Bat Hand, Bowl Hand,
                    #         Bowl Style, Bat Rating, Bowl Rating, Version, Is Active
                    # Min required: Name. Version + Is Active are optional (default Base, 1).
                    cols = [c.strip() for c in row]
                    while len(cols) < 11:
                        cols.append("")

                    name = cols[0]
                    if not name:
                        continue

                    # Version — column 10 (index 9). Default "Base" if empty/missing.
                    # Strip surrounding quotes some spreadsheet exports add.
                    version_raw = cols[9].strip().strip('"').strip("'")
                    version = version_raw if version_raw else "Base"

                    # Is Active — column 11 (index 10). "0", "false", "no" → inactive.
                    is_active_raw = cols[10].strip().lower()
                    if is_active_raw in ("0", "false", "no", "n", "inactive"):
                        is_active = False
                    else:
                        is_active = True  # default

                    # Duplicate check by (name, version) pair — multiple versions
                    # of the same player are allowed by design.
                    existing = (db.query(Player)
                                .filter(Player.name == name,
                                        Player.version == version)
                                .first())
                    if existing:
                        skipped += 1
                        continue

                    try:
                        rating = int(cols[1]) if cols[1] else 70
                    except ValueError:
                        rating = 70

                    category = cols[2] if cols[2] else "Batsman"
                    # Normalize category
                    cat_map = {
                        "bat": "Batsman", "batsman": "Batsman", "bats": "Batsman",
                        "bowl": "Bowler", "bowler": "Bowler",
                        "wk": "Wicket Keeper", "keeper": "Wicket Keeper",
                        "wicket keeper": "Wicket Keeper", "wicketkeeper": "Wicket Keeper",
                        "ar": "All-rounder", "all-rounder": "All-rounder",
                        "allrounder": "All-rounder", "all rounder": "All-rounder",
                    }
                    category = cat_map.get(category.lower(), category)

                    country = cols[3] if cols[3] else "Unknown"
                    bat_hand = cols[4] if cols[4] else "Right"
                    bowl_hand = cols[5] if cols[5] else "Right"
                    bowl_style = cols[6] if cols[6] else "Medium Pacer"

                    try:
                        bat_rating = int(cols[7]) if cols[7] else rating
                    except ValueError:
                        bat_rating = rating
                    try:
                        bowl_rating = int(cols[8]) if cols[8] else rating
                    except ValueError:
                        bowl_rating = rating

                    # Normalize hand
                    hand_map = {"r": "Right", "right": "Right", "l": "Left", "left": "Left",
                                "rh": "Right", "lh": "Left"}
                    bat_hand = hand_map.get(bat_hand.lower(), bat_hand)
                    bowl_hand = hand_map.get(bowl_hand.lower(), bowl_hand)

                    # For variant versions (non-Base): try to link to base card via
                    # parent_player_id. Look up the existing Base row with same name.
                    # Check both the DB (committed) and this batch's pending Base rows.
                    parent_player_id = None
                    if version.lower() != "base":
                        # First, check batch cache (rows added in this loop)
                        if name in batch_base_ids:
                            parent_player_id = batch_base_ids[name]
                        else:
                            # Then check the DB for already-committed Base rows
                            base = (db.query(Player)
                                    .filter(Player.name == name,
                                            Player.version == "Base")
                                    .first())
                            if base:
                                parent_player_id = base.id
                        # If still none, parent_player_id stays None. The version
                        # still saves with the right `version` value. Caller can
                        # upload Base first if they want the link.

                    p = Player(
                        name=name, rating=rating, category=category, country=country,
                        bat_hand=bat_hand, bowl_hand=bowl_hand, bowl_style=bowl_style,
                        bat_rating=bat_rating, bowl_rating=bowl_rating,
                        version=version, parent_player_id=parent_player_id,
                        is_active=is_active,
                    )
                    db.add(p)
                    # If this is a Base row, flush so its id is set, then cache
                    # it for any subsequent variant rows in this batch.
                    if version.lower() == "base":
                        db.flush()
                        batch_base_ids[name] = p.id
                    added += 1
                    added_by_version[version] = added_by_version.get(version, 0) + 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)[:80]}")

            db.commit()

            # Second pass: link parent_player_id for any variants that
            # imported BEFORE their Base row (export sorts by rating desc, so
            # high-rated variants like Legend can appear before the Base row
            # in a round-trip).
            try:
                orphans = (db.query(Player)
                           .filter(Player.version != "Base",
                                   Player.parent_player_id.is_(None))
                           .all())
                linked = 0
                for variant in orphans:
                    base = (db.query(Player)
                            .filter(Player.name == variant.name,
                                    Player.version == "Base")
                            .first())
                    if base and base.id != variant.id:
                        variant.parent_player_id = base.id
                        linked += 1
                if linked:
                    db.commit()
            except Exception:
                logger.exception("Second-pass parent link failed (non-fatal)")

            log_admin(db, "bulk_upload",
                      detail=f"Added {added} (by version: {added_by_version}), "
                             f"skipped {skipped} duplicates, {len(errors)} errors")
            db.commit()

            msg = f"✅ Added {added} players"
            if len(added_by_version) > 1 or (added_by_version and "Base" not in added_by_version):
                breakdown = ", ".join(f"{v}: {n}" for v, n in sorted(added_by_version.items()))
                msg += f" ({breakdown})"
            if skipped: msg += f" · Skipped {skipped} duplicates"
            if errors: msg += f" · {len(errors)} errors"
            flash(msg, "success")
            if errors:
                for e in errors[:5]:
                    flash(e, "error")

            return redirect(url_for("players_list"))
        except Exception as e:
            db.rollback()
            flash(f"Upload failed: {e}", "error")
            return redirect(url_for("players_bulk_upload"))
        finally:
            db.close()

    return render_template("bulk_upload.html")


# ── Admin activity log ───────────────────────────────────────────────

@app.route("/logs")
@login_required
def admin_logs():
    db = get_session()
    try:
        action_filter = request.args.get("action", "").strip()
        page = max(1, int(request.args.get("page", 1)))

        query = db.query(AdminLog)
        if action_filter:
            query = query.filter(AdminLog.action == action_filter)

        total = query.count()
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)

        logs = (query.order_by(AdminLog.timestamp.desc())
                .offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())

        actions = [r[0] for r in db.query(AdminLog.action).distinct().all()]

        return render_template("admin_logs.html",
                               logs=logs, total=total, page=page,
                               total_pages=total_pages, actions=actions,
                               action_filter=action_filter)
    finally:
        db.close()


# ── Add player ───────────────────────────────────────────────────────

@app.route("/players/add", methods=["GET", "POST"])
@login_required
def player_add():
    if request.method == "POST":
        db = get_session()
        try:
            name = request.form["name"].strip()
            existing = db.query(Player).filter(Player.name == name).first()
            if existing:
                flash(f"Player '{name}' already exists", "error")
                return redirect(url_for("player_add"))

            player = Player(
                name=name,
                rating=int(request.form["rating"]),
                category=request.form["category"],
                country=request.form["country"].strip(),
                version=request.form.get("version", "Base card").strip() or "Base card",
                bat_hand=request.form.get("bat_hand", "Right"),
                bowl_hand=request.form.get("bowl_hand", "Right"),
                bowl_style=request.form.get("bowl_style", "Medium Pacer"),
                bat_rating=int(request.form.get("bat_rating", 50)),
                bowl_rating=int(request.form.get("bowl_rating", 50)),
                bat_avg=float(request.form.get("bat_avg", 0)),
                strike_rate=float(request.form.get("strike_rate", 0)),
                runs=int(request.form.get("runs", 0)),
                centuries=int(request.form.get("centuries", 0)),
                bowl_avg=float(request.form.get("bowl_avg", 0)),
                economy=float(request.form.get("economy", 0)),
                wickets=int(request.form.get("wickets", 0)),
                is_active=request.form.get("is_active", "1") == "1",
                restricted_from_buypl=request.form.get("restricted_from_buypl") == "1",
            )
            db.add(player)
            db.flush()
            portrait_file = request.files.get("player_image")
            if portrait_file and portrait_file.filename:
                from services.player_portrait_service import save_player_portrait
                ok, msg = save_player_portrait(player, portrait_file.read(), portrait_file.filename)
                if not ok:
                    raise ValueError(msg)
            log_admin(db, "player_add", target_type="player", target_id=player.id,
                      target_name=name, detail=f"Rating {player.rating}, {player.category}, {player.country}")
            db.commit()
            flash(f"Player '{name}' created (rating {player.rating})", "success")
            return redirect(url_for("players_list"))
        except Exception as e:
            db.rollback()
            flash(f"Error: {e}", "error")
            return redirect(url_for("player_add"))
        finally:
            db.close()

    return render_template("player_form.html", player=None)


# ── Edit player ──────────────────────────────────────────────────────

@app.route("/players/<int:player_id>/edit", methods=["GET", "POST"])
@login_required
def player_edit(player_id):
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            flash("Player not found", "error")
            return redirect(url_for("players_list"))

        if request.method == "POST":
            old_rating = player.rating
            old_name = player.name
            player.name = request.form["name"].strip()
            player.rating = int(request.form["rating"])
            player.category = request.form["category"]
            player.country = request.form["country"].strip()
            player.version = request.form.get("version", "Base card").strip() or "Base card"
            player.bat_hand = request.form.get("bat_hand", "Right")
            player.bowl_hand = request.form.get("bowl_hand", "Right")
            player.bowl_style = request.form.get("bowl_style", "Medium Pacer")
            player.bat_rating = int(request.form.get("bat_rating", 50))
            player.bowl_rating = int(request.form.get("bowl_rating", 50))
            player.bat_avg = float(request.form.get("bat_avg", 0))
            player.strike_rate = float(request.form.get("strike_rate", 0))
            player.runs = int(request.form.get("runs", 0))
            player.centuries = int(request.form.get("centuries", 0))
            player.bowl_avg = float(request.form.get("bowl_avg", 0))
            player.economy = float(request.form.get("economy", 0))
            player.wickets = int(request.form.get("wickets", 0))
            player.is_active = request.form.get("is_active", "1") == "1"
            # Restricted-from-/buypl toggle
            player.restricted_from_buypl = (
                request.form.get("restricted_from_buypl") == "1"
            )

            from services.player_portrait_service import (
                remove_player_portrait, save_player_portrait,
            )
            if request.form.get("remove_player_image") == "1":
                remove_player_portrait(player)
            portrait_file = request.files.get("player_image")
            if portrait_file and portrait_file.filename:
                ok, msg = save_player_portrait(player, portrait_file.read(), portrait_file.filename)
                if not ok:
                    raise ValueError(msg)

            changes = []
            if old_name != player.name:
                changes.append(f"name: {old_name} → {player.name}")
            if old_rating != player.rating:
                changes.append(f"rating: {old_rating} → {player.rating}")
            detail = "; ".join(changes) if changes else "Edit"
            log_admin(db, "player_edit", target_type="player", target_id=player.id,
                      target_name=player.name, detail=detail)
            db.commit()
            # Bust caches — the player row changed
            try:
                from services.player_cache import invalidate as _inv_pc
                from services.card_generator import invalidate_card_cache
                _inv_pc()
                invalidate_card_cache(player.id)
            except Exception:
                pass
            flash(f"Player '{player.name}' updated", "success")
            return redirect(url_for("players_list"))

        from services.player_image_service import get_metadata
        image_meta = get_metadata(db, player.id)
        # Versions: this player's siblings (if it's a variant) or children (if it's a base)
        from services.version_service import get_all_versions
        base_id = player.parent_player_id or player.id
        versions = get_all_versions(db, base_id)
        from services.player_portrait_service import has_player_portrait
        return render_template("player_form.html", player=player,
                               image_meta=image_meta, versions=versions, base_id=base_id,
                               has_player_portrait=has_player_portrait(player))
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
        return redirect(url_for("players_list"))
    finally:
        db.close()


@app.route("/players/<int:player_id>/card-preview")
@login_required
def admin_player_card_preview(player_id):
    """Serve the generated card preview, including portrait-or-shadow artwork."""
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            return "Player not found", 404
        from services.card_generator import generate_card
        image_bytes = generate_card(player)
        if not image_bytes:
            return "Could not generate card preview", 500
        return send_file(io.BytesIO(image_bytes), mimetype="image/png",
                         download_name=f"player-card-{player_id}.png")
    finally:
        db.close()


@app.route("/players/<int:player_id>/image/upload", methods=["POST"])
@login_required
def admin_player_image_upload(player_id):
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            flash("Player not found.", "error")
            return redirect(url_for("players_list"))
        file = request.files.get("image_file")
        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(url_for("player_edit", player_id=player_id))
        try:
            file_bytes = file.read()
        except Exception as e:
            flash(f"Error reading file: {e}", "error")
            return redirect(url_for("player_edit", player_id=player_id))

        from services.player_image_service import save_custom_image
        ok, msg = save_custom_image(
            db, player_id, file_bytes, file.filename,
            label=request.form.get("label", "").strip() or None,
            uploaded_by=session.get("admin_user", "admin"),
        )
        if ok:
            db.commit()
            log_admin(db, "player_image_upload", "player", player_id, player.name,
                      f"Uploaded custom image ({len(file_bytes)//1024} KB)")
            db.commit()
            flash(f"✅ Custom image uploaded for {player.name}.", "success")
        else:
            db.rollback()
            flash(msg, "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("player_edit", player_id=player_id))


@app.route("/players/<int:player_id>/image/remove", methods=["POST"])
@login_required
def admin_player_image_remove(player_id):
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            return redirect(url_for("players_list"))
        from services.player_image_service import remove_custom_image
        ok, msg = remove_custom_image(db, player_id)
        if ok:
            db.commit()
            log_admin(db, "player_image_remove", "player", player_id, player.name,
                      "Removed custom image")
            db.commit()
            flash(f"Custom image removed for {player.name}.", "info")
        else:
            flash(msg, "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("player_edit", player_id=player_id))


@app.route("/players/<int:player_id>/image/toggle", methods=["POST"])
@login_required
def admin_player_image_toggle(player_id):
    db = get_session()
    try:
        from services.player_image_service import toggle_active
        ok, new_state = toggle_active(db, player_id)
        if ok:
            db.commit()
            flash(f"Custom image is now {'active' if new_state else 'inactive'}.", "info")
        else:
            flash("No custom image set.", "error")
    finally:
        db.close()
    return redirect(url_for("player_edit", player_id=player_id))


@app.route("/players/<int:player_id>/image/preview")
@login_required
def admin_player_image_preview(player_id):
    """Serve the custom image bytes (for inline preview in admin)."""
    from services.player_image_service import get_custom_image_bytes
    img = get_custom_image_bytes(player_id)
    if not img:
        from flask import abort
        abort(404)
    from flask import Response
    return Response(img, mimetype="image/png")


# ═══════════════════════════════════════════════════════════════════════
# PLAYER VERSIONS — alternate cosmetic editions of a base player
# Backend: each version is a Player row with parent_player_id set + a
# PlayerImage row attached. Stats are inherited from the base player.
# ═══════════════════════════════════════════════════════════════════════

@app.route("/players/<int:base_id>/versions/new", methods=["POST"])
@login_required
def admin_player_version_new(base_id):
    """Create a new version of a base player. Requires a custom image upload."""
    db = get_session()
    try:
        base = db.query(Player).get(base_id)
        if not base:
            flash("Base player not found.", "error")
            return redirect(url_for("players_list"))

        # Disallow creating a version of a version
        if base.parent_player_id:
            flash("Can't add a version to a variant. Pick the base player instead.", "error")
            return redirect(url_for("player_edit", player_id=base.parent_player_id))

        version_label = (request.form.get("version_label") or "").strip()
        if not version_label:
            flash("Version label required (e.g. 'Gold', 'World Cup 2023').", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        file = request.files.get("version_image")
        if not file or not file.filename:
            flash("⚠️ A custom card image is required to create a new version.", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        try:
            file_bytes = file.read()
        except Exception as e:
            flash(f"Error reading file: {e}", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        # Validate the image FIRST — don't create a Player row if image is bad
        from services.player_image_service import (
            ALLOWED_EXT, MAX_BYTES, MIN_DIM, _ext_from_filename,
        )
        ext = _ext_from_filename(file.filename)
        if ext not in ALLOWED_EXT:
            flash(f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}", "error")
            return redirect(url_for("player_edit", player_id=base_id))
        if len(file_bytes) > MAX_BYTES:
            flash(f"File too large. Max {MAX_BYTES//1024//1024} MB.", "error")
            return redirect(url_for("player_edit", player_id=base_id))
        try:
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(file_bytes))
            img.verify()
            img = Image.open(_io.BytesIO(file_bytes))
            if img.size[0] < MIN_DIM or img.size[1] < MIN_DIM:
                flash(f"Image too small ({img.size[0]}×{img.size[1]}). Min {MIN_DIM}×{MIN_DIM}.", "error")
                return redirect(url_for("player_edit", player_id=base_id))
        except Exception as e:
            flash(f"Not a valid image: {e}", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        # Make sure label is unique among existing versions of this base
        existing_labels = set()
        # Base player itself implicitly has label "Base"
        existing_labels.add("Base")
        for v in (db.query(Player)
                  .filter(Player.parent_player_id == base_id).all()):
            existing_labels.add((v.version or "").lower())
        if version_label.lower() in {l.lower() for l in existing_labels}:
            flash(f"Version label '{version_label}' already exists. Use a unique name.", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        # Create the variant Player row.
        # Override fields if admin provided them; fall back to base otherwise.
        def _f(form_key, default):
            """Take form value if non-empty, else default."""
            v = (request.form.get(form_key) or "").strip()
            return v if v else default

        try:
            v_rating = int(_f("rating", str(base.rating)))
            v_bat_rating = int(_f("bat_rating", str(base.bat_rating)))
            v_bowl_rating = int(_f("bowl_rating", str(base.bowl_rating)))
            v_bat_avg = float(_f("bat_avg", str(getattr(base, "bat_avg", 0.0) or 0.0)))
            v_strike_rate = float(_f("strike_rate", str(getattr(base, "strike_rate", 0.0) or 0.0)))
            v_bowl_avg = float(_f("bowl_avg", str(getattr(base, "bowl_avg", 0.0) or 0.0)))
            v_economy = float(_f("economy", str(getattr(base, "economy", 0.0) or 0.0)))
            v_runs = int(_f("runs", str(getattr(base, "runs", 0) or 0)))
            v_centuries = int(_f("centuries", str(getattr(base, "centuries", 0) or 0)))
            v_wickets = int(_f("wickets", str(getattr(base, "wickets", 0) or 0)))
        except ValueError as ve:
            flash(f"Invalid number in form: {ve}", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        variant = Player(
            name=base.name,
            country=_f("country", base.country),
            category=_f("category", base.category),
            rating=v_rating,
            bat_rating=v_bat_rating,
            bowl_rating=v_bowl_rating,
            bat_hand=_f("bat_hand", base.bat_hand),
            bowl_hand=_f("bowl_hand", base.bowl_hand),
            bowl_style=_f("bowl_style", base.bowl_style),
            bat_avg=v_bat_avg,
            strike_rate=v_strike_rate,
            bowl_avg=v_bowl_avg,
            economy=v_economy,
            runs=v_runs,
            centuries=v_centuries,
            wickets=v_wickets,
            image_url=getattr(base, "image_url", None),
            is_active=True,
            version=version_label,
            parent_player_id=base.id,
        )
        db.add(variant)
        db.flush()

        # Save the custom image for the new variant Player row
        from services.player_image_service import save_custom_image
        ok, msg = save_custom_image(
            db, variant.id, file_bytes, file.filename,
            label=version_label,
            uploaded_by=session.get("admin_user", "admin"),
        )
        if not ok:
            db.rollback()
            flash(f"Error saving image: {msg}", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        db.commit()
        log_admin(db, "player_version_create", "player", variant.id,
                  f"{variant.name} ({version_label})",
                  f"Created version '{version_label}' of {base.name}")
        db.commit()

        # Bust caches so bot picks up the new variant
        try:
            from services.player_cache import invalidate as _inv_pc
            from services.card_generator import invalidate_card_cache
            _inv_pc()
            invalidate_card_cache()
        except Exception:
            pass

        flash(f"✅ Created version '{version_label}' of {base.name}.", "success")
        return redirect(url_for("player_edit", player_id=base_id))
    except Exception as e:
        db.rollback()
        # Detect the specific unique-constraint case so the admin gets a clear
        # actionable message instead of a raw psycopg2 traceback.
        err_str = str(e).lower()
        if "uniqueviolation" in err_str or ("unique" in err_str and "name" in err_str):
            flash(
                "Database has a legacy unique-on-name constraint blocking versions. "
                "Restart the app once to run the auto-migration that drops it. "
                f"(Underlying error: {type(e).__name__})",
                "error",
            )
        else:
            flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("player_edit", player_id=base_id))


@app.route("/players/<int:version_id>/versions/delete", methods=["POST"])
@login_required
def admin_player_version_delete(version_id):
    """Delete a variant Player. Refuses if the version is owned by anyone."""
    db = get_session()
    try:
        v = db.query(Player).get(version_id)
        if not v or not v.parent_player_id:
            flash("Not a valid variant.", "error")
            return redirect(url_for("players_list"))
        base_id = v.parent_player_id

        # Refuse if any user owns this variant
        from models import UserRoster
        owners = db.query(UserRoster).filter(UserRoster.player_id == version_id).count()
        if owners > 0:
            flash(f"Cannot delete: {owners} user(s) own this version. Deactivate instead.", "error")
            return redirect(url_for("player_edit", player_id=base_id))

        # Delete attached image
        from services.player_image_service import remove_custom_image
        try:
            remove_custom_image(db, version_id)
        except Exception:
            pass
        # Delete attached PlayerVersion (if exists) is now redundant since we don't use it

        version_name = v.version or "variant"
        db.delete(v)
        db.commit()
        log_admin(db, "player_version_delete", "player", version_id,
                  f"version {version_name}", f"Deleted version of player_id={base_id}")
        db.commit()

        try:
            from services.player_cache import invalidate as _inv_pc
            from services.card_generator import invalidate_card_cache
            _inv_pc()
            invalidate_card_cache()
        except Exception:
            pass

        flash(f"✅ Version '{version_name}' deleted.", "info")
        return redirect(url_for("player_edit", player_id=base_id))
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("players_list"))


# ── Delete player ────────────────────────────────────────────────────

@app.route("/players/<int:player_id>/delete", methods=["POST"])
@login_required
def player_delete(player_id):
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            flash("Player not found", "error")
            return redirect(url_for("players_list"))

        name = player.name
        rating = player.rating
        log_admin(db, "player_delete", target_type="player", target_id=player.id,
                  target_name=name, detail=f"Rating {rating}, {player.category}")
        db.delete(player)
        db.commit()
        try:
            from services.player_cache import invalidate as _inv_pc
            from services.card_generator import invalidate_card_cache
            _inv_pc()
            invalidate_card_cache(player_id)
        except Exception:
            pass
        flash(f"Player '{name}' deleted", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error deleting player: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("players_list"))


# ── Toggle active/inactive ──────────────────────────────────────────

@app.route("/players/<int:player_id>/toggle", methods=["POST"])
@login_required
def player_toggle(player_id):
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if player:
            player.is_active = not player.is_active
            status = "activated" if player.is_active else "deactivated"
            log_admin(db, "player_toggle", target_type="player", target_id=player.id,
                      target_name=player.name, detail=f"Player {status}")
            db.commit()
            try:
                from services.player_cache import invalidate as _inv_pc
                _inv_pc()
            except Exception:
                pass
            flash(f"Player '{player.name}' {status}", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(request.referrer or url_for("players_list"))


@app.route("/players/<int:player_id>/toggle-buypl", methods=["POST"])
@login_required
def player_toggle_buypl(player_id):
    """Toggle restricted_from_buypl — blocks /buypl direct-name purchase for
    this player. Player remains available via market, packs, trades, etc."""
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if player:
            player.restricted_from_buypl = not (player.restricted_from_buypl or False)
            status = ("🚫 BLOCKED from /buypl" if player.restricted_from_buypl
                      else "✅ available via /buypl")
            log_admin(db, "player_toggle_buypl", target_type="player",
                      target_id=player.id, target_name=player.name,
                      detail=f"restricted_from_buypl → {player.restricted_from_buypl}")
            db.commit()
            try:
                from services.player_cache import invalidate as _inv_pc
                _inv_pc()
            except Exception:
                pass
            flash(f"<b>{player.name}</b> is now {status}", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(request.referrer or url_for("players_list"))


@app.route("/players/bulk-toggle-buypl", methods=["POST"])
@login_required
def players_bulk_toggle_buypl():
    """Bulk-set restricted_from_buypl on a list of player IDs."""
    db = get_session()
    try:
        action = (request.form.get("action") or "block").strip()
        ids_raw = request.form.get("player_ids", "") or ""
        try:
            ids = [int(x.strip()) for x in ids_raw.split(",") if x.strip()]
        except ValueError:
            flash("Bad player_ids", "error")
            return redirect(request.referrer or url_for("players_list"))
        if not ids:
            flash("No players selected", "error")
            return redirect(request.referrer or url_for("players_list"))

        new_val = (action == "block")
        n = (db.query(Player).filter(Player.id.in_(ids))
             .update({Player.restricted_from_buypl: new_val},
                     synchronize_session=False))
        db.commit()
        log_admin(db, "bulk_toggle_buypl", detail=(
            f"Set restricted_from_buypl={new_val} on {n} players"))
        db.commit()
        try:
            from services.player_cache import invalidate as _inv_pc
            _inv_pc()
        except Exception:
            pass
        label = "🚫 blocked from /buypl" if new_val else "✅ unblocked"
        flash(f"{n} players {label}", "success")
    except Exception as e:
        db.rollback()
        logger.exception("bulk_toggle_buypl failed")
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(request.referrer or url_for("players_list"))


# ── User management ──────────────────────────────────────────────────

@app.route("/users")
@login_required
def users_list():
    db = get_session()
    try:
        # ── Filter params ─────────────────────────────────────────────
        q = request.args.get("q", "").strip()
        # Numeric range filters
        coins_min = _parse_int(request.args.get("coins_min", ""))
        coins_max = _parse_int(request.args.get("coins_max", ""))
        gems_min = _parse_int(request.args.get("gems_min", ""))
        gems_max = _parse_int(request.args.get("gems_max", ""))
        roster_min = _parse_int(request.args.get("roster_min", ""))
        roster_max = _parse_int(request.args.get("roster_max", ""))
        # Win-rate filter (computed)
        wins_min = _parse_int(request.args.get("wins_min", ""))
        wins_max = _parse_int(request.args.get("wins_max", ""))
        played_min = _parse_int(request.args.get("played_min", ""))
        # Joined-date filter
        joined_after = request.args.get("joined_after", "").strip()
        joined_before = request.args.get("joined_before", "").strip()
        # Activity status
        activity = request.args.get("activity", "").strip()  # 'active' | 'inactive' | ''
        # Sort
        sort = request.args.get("sort", "created_desc")
        # Pagination
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(10, min(100, int(request.args.get("per_page", 20))))

        query = db.query(User)

        if q:
            query = query.filter(user_lookup_filter(q))

        if coins_min is not None: query = query.filter(User.total_coins >= coins_min)
        if coins_max is not None: query = query.filter(User.total_coins <= coins_max)
        if gems_min is not None: query = query.filter(User.total_gems >= gems_min)
        if gems_max is not None: query = query.filter(User.total_gems <= gems_max)
        if roster_min is not None: query = query.filter(User.roster_count >= roster_min)
        if roster_max is not None: query = query.filter(User.roster_count <= roster_max)
        if wins_min is not None: query = query.filter(User.matches_won >= wins_min)
        if wins_max is not None: query = query.filter(User.matches_won <= wins_max)
        if played_min is not None: query = query.filter(User.matches_played >= played_min)

        if joined_after:
            try:
                d = datetime.strptime(joined_after, "%Y-%m-%d")
                query = query.filter(User.created_at >= d)
            except ValueError: pass
        if joined_before:
            try:
                d = datetime.strptime(joined_before, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(User.created_at < d)
            except ValueError: pass

        if activity == "active":
            # Played a match in last 7 days
            cutoff = datetime.utcnow() - timedelta(days=7)
            query = query.filter(User.last_match_date >= cutoff)
        elif activity == "inactive":
            # No match in 30+ days, or never played
            cutoff = datetime.utcnow() - timedelta(days=30)
            query = query.filter(
                (User.last_match_date < cutoff) | (User.last_match_date.is_(None))
            )

        # Sort
        sort_map = {
            "created_desc": User.created_at.desc(),
            "created_asc": User.created_at.asc(),
            "username_asc": User.username.asc(),
            "username_desc": User.username.desc(),
            "coins_desc": User.total_coins.desc(),
            "coins_asc": User.total_coins.asc(),
            "gems_desc": User.total_gems.desc(),
            "gems_asc": User.total_gems.asc(),
            "roster_desc": User.roster_count.desc(),
            "roster_asc": User.roster_count.asc(),
            "wins_desc": User.matches_won.desc(),
            "wins_asc": User.matches_won.asc(),
            "played_desc": User.matches_played.desc(),
            "last_match_desc": User.last_match_date.desc().nullslast(),
        }
        query = query.order_by(sort_map.get(sort, User.created_at.desc()))

        total = query.count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        users = query.offset((page - 1) * per_page).limit(per_page).all()

        # Attach streak count + win%
        for u in users:
            st = db.query(UserStats).filter(UserStats.user_id == u.id).first()
            u._streak = f"{st.streak_count}/14" if st else "0/14"
            u._winpct = (round(u.matches_won * 100.0 / u.matches_played, 1)
                          if u.matches_played else 0.0)

        # CSV export — same query, no pagination
        if request.args.get("export") == "csv":
            return _users_csv_export(db, query)

        # Build a "filters_active" summary for the chips
        active_filters = []
        if q: active_filters.append(("Search", q, "q"))
        if coins_min is not None or coins_max is not None:
            label = f"{coins_min if coins_min is not None else '0'}-{coins_max if coins_max is not None else '∞'}"
            active_filters.append(("Coins", label, "coins"))
        if gems_min is not None or gems_max is not None:
            label = f"{gems_min if gems_min is not None else '0'}-{gems_max if gems_max is not None else '∞'}"
            active_filters.append(("Gems", label, "gems"))
        if roster_min is not None or roster_max is not None:
            label = f"{roster_min if roster_min is not None else '0'}-{roster_max if roster_max is not None else '25'}"
            active_filters.append(("Roster", label, "roster"))
        if wins_min is not None or wins_max is not None:
            label = f"{wins_min if wins_min is not None else '0'}-{wins_max if wins_max is not None else '∞'}"
            active_filters.append(("Wins", label, "wins"))
        if played_min is not None:
            active_filters.append(("Played", f"≥{played_min}", "played"))
        if joined_after: active_filters.append(("Joined after", joined_after, "joined_after"))
        if joined_before: active_filters.append(("Joined before", joined_before, "joined_before"))
        if activity: active_filters.append(("Activity", activity, "activity"))

        return render_template("users.html", users=users, total=total, page=page,
                               total_pages=total_pages, per_page=per_page,
                               q=q, sort=sort, active_filters=active_filters,
                               filters={
                                   "coins_min": coins_min, "coins_max": coins_max,
                                   "gems_min": gems_min, "gems_max": gems_max,
                                   "roster_min": roster_min, "roster_max": roster_max,
                                   "wins_min": wins_min, "wins_max": wins_max,
                                   "played_min": played_min,
                                   "joined_after": joined_after,
                                   "joined_before": joined_before,
                                   "activity": activity,
                               })
    finally:
        db.close()


def _parse_int(s):
    """Parse a request arg to int, returns None for blank/invalid."""
    if not s: return None
    try: return int(s)
    except (ValueError, TypeError): return None



def _parse_player_filter_args(args):
    """Collect player list/export filters from request args."""
    return {
        "q": args.get("q", "").strip(),
        "category": args.get("category", "").strip(),
        "country_filter": args.get("country", "").strip(),
        "rating_range": args.get("rating_range", "").strip(),
        "bat_hand": args.get("bat_hand", "").strip(),
        "bowl_hand": args.get("bowl_hand", "").strip(),
        "is_active": args.get("is_active", "").strip(),
        "buypl_filter": args.get("buypl", "").strip(),
        "version_mode": args.get("version_mode", "").strip(),
        "version_filter": args.get("version", "").strip(),
        "rating_min": _parse_int(args.get("rating_min", "")),
        "rating_max": _parse_int(args.get("rating_max", "")),
        "bat_rating_min": _parse_int(args.get("bat_rating_min", "")),
        "bat_rating_max": _parse_int(args.get("bat_rating_max", "")),
        "bowl_rating_min": _parse_int(args.get("bowl_rating_min", "")),
        "bowl_rating_max": _parse_int(args.get("bowl_rating_max", "")),
    }


def _apply_player_filters(query, filters, range_map):
    """Apply all filters used by the Players tab to a Player query."""
    q = filters["q"]
    if q:
        query = query.filter(Player.name.ilike(f"%{q}%"))
    if filters["category"]:
        query = query.filter(Player.category == filters["category"])
    if filters["country_filter"]:
        query = query.filter(Player.country == filters["country_filter"])
    rating_range = filters["rating_range"]
    if rating_range and rating_range in range_map:
        r_min, r_max = range_map[rating_range]
        query = query.filter(Player.rating >= r_min, Player.rating <= r_max)
    if filters["rating_min"] is not None:
        query = query.filter(Player.rating >= filters["rating_min"])
    if filters["rating_max"] is not None:
        query = query.filter(Player.rating <= filters["rating_max"])
    if filters["bat_rating_min"] is not None:
        query = query.filter(Player.bat_rating >= filters["bat_rating_min"])
    if filters["bat_rating_max"] is not None:
        query = query.filter(Player.bat_rating <= filters["bat_rating_max"])
    if filters["bowl_rating_min"] is not None:
        query = query.filter(Player.bowl_rating >= filters["bowl_rating_min"])
    if filters["bowl_rating_max"] is not None:
        query = query.filter(Player.bowl_rating <= filters["bowl_rating_max"])
    if filters["bat_hand"]:
        query = query.filter(Player.bat_hand == filters["bat_hand"])
    if filters["bowl_hand"]:
        query = query.filter(Player.bowl_hand == filters["bowl_hand"])
    if filters["is_active"] == "1":
        query = query.filter(Player.is_active == True)
    elif filters["is_active"] == "0":
        query = query.filter(Player.is_active == False)
    if filters["buypl_filter"] == "blocked":
        query = query.filter(Player.restricted_from_buypl == True)
    elif filters["buypl_filter"] == "available":
        query = query.filter(
            (Player.restricted_from_buypl == False) |
            (Player.restricted_from_buypl.is_(None))
        )
    if filters["version_mode"] == "base":
        query = query.filter(Player.parent_player_id.is_(None))
    elif filters["version_mode"] == "version":
        query = query.filter(Player.parent_player_id.isnot(None))
    if filters["version_filter"]:
        query = query.filter(Player.version == filters["version_filter"])
    return query


def _player_export_dict(player):
    return {
        "id": player.id,
        "name": player.name,
        "rating": player.rating,
        "category": player.category,
        "country": player.country,
        "bat_hand": player.bat_hand,
        "bowl_hand": player.bowl_hand,
        "bowl_style": player.bowl_style,
        "bat_rating": player.bat_rating,
        "bowl_rating": player.bowl_rating,
        "version": player.version or "Base",
        "parent_player_id": player.parent_player_id,
        "is_active": bool(player.is_active),
        "restricted_from_buypl": bool(getattr(player, "restricted_from_buypl", False)),
    }


def _build_players_xlsx(rows):
    """Create a minimal XLSX workbook using only the Python stdlib."""
    headers = list(rows[0].keys()) if rows else [
        "id", "name", "rating", "category", "country", "bat_hand",
        "bowl_hand", "bowl_style", "bat_rating", "bowl_rating",
        "version", "parent_player_id", "is_active", "restricted_from_buypl",
    ]

    def cell(col_idx, row_idx, value):
        col = ""
        n = col_idx
        while n:
            n, rem = divmod(n - 1, 26)
            col = chr(65 + rem) + col
        ref = f"{col}{row_idx}"
        if value is None:
            return f'<c r="{ref}"/>'
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}"><v>{value}</v></c>'
        escaped = html_lib.escape(str(value), quote=False)
        return f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    sheet_rows = []
    sheet_rows.append('<row r="1">' + ''.join(cell(i + 1, 1, h) for i, h in enumerate(headers)) + '</row>')
    for row_idx, row in enumerate(rows, start=2):
        sheet_rows.append('<row r="{}">'.format(row_idx) + ''.join(
            cell(i + 1, row_idx, row.get(h)) for i, h in enumerate(headers)
        ) + '</row>')
    worksheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{}</sheetData></worksheet>'.format(''.join(sheet_rows))
    workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Players" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)
    out.seek(0)
    return out

def _users_csv_export(db, base_query):
    """Stream a CSV of users matching the filter query (no pagination)."""
    import csv, io
    from flask import Response
    rows = base_query.all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["telegram_id", "username", "first_name", "coins", "gems",
                "quest_points", "roster", "matches_played", "matches_won",
                "matches_lost", "win_pct", "streak", "best_streak",
                "last_match", "joined"])
    for u in rows:
        st = db.query(UserStats).filter(UserStats.user_id == u.id).first()
        streak = st.streak_count if st else 0
        win_pct = (round(u.matches_won * 100.0 / u.matches_played, 1)
                    if u.matches_played else 0)
        w.writerow([
            u.telegram_id, u.username or "", u.first_name or "",
            u.total_coins, u.total_gems, u.quest_points or 0,
            u.roster_count, u.matches_played or 0, u.matches_won or 0,
            u.matches_lost or 0, win_pct, streak, u.best_streak or 0,
            u.last_match_date.strftime("%Y-%m-%d %H:%M") if u.last_match_date else "",
            u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
        ])
    return Response(buf.getvalue(),
                    mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=users_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"})


@app.route("/users/<int:user_id>")
@login_required
def user_detail(user_id):
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash("User not found", "error")
            return redirect(url_for("users_list"))

        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()

        roster = (
            db.query(UserRoster, Player)
            .join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user.id)
            .order_by(Player.rating.desc())
            .all()
        )
        for _, p in roster:
            p._tier_css = tier_css(p.rating)

        activities = (
            db.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id)
            .order_by(ActivityLog.created_at.desc())
            .limit(50)
            .all()
        )

        # Active matches the user is in (any non-completed/abandoned status)
        from models import Match
        active_matches = (
            db.query(Match)
            .filter(((Match.user1_id == user.id) | (Match.user2_id == user.id))
                    & (Match.status.in_(("active", "in_progress", "playing", "pending"))))
            .order_by(Match.id.desc())
            .all()
        )
        # Map opponents
        match_meta = []
        for m in active_matches:
            opp_id = m.user2_id if m.user1_id == user.id else m.user1_id
            opp = db.query(User).get(opp_id) if opp_id else None
            match_meta.append({
                "match": m,
                "opp_name": (opp.first_name or opp.username) if opp else "Bot/Unknown",
                "opp_username": (opp.username if opp else "bot"),
            })

        # Recent finished matches (last 10)
        recent_matches = (
            db.query(Match)
            .filter(((Match.user1_id == user.id) | (Match.user2_id == user.id))
                    & (Match.status == "completed"))
            .order_by(Match.id.desc())
            .limit(10).all()
        )

        # Trade counts
        from models import Trade
        pending_trades = (db.query(Trade)
                          .filter(((Trade.initiator_id == user.id) |
                                   (Trade.receiver_id == user.id))
                                  & (Trade.status == "pending"))
                          .count())

        # Quest progress for this user — current daily + monthly periods
        from services.quest_service import (
            daily_period_key, monthly_period_key,
        )
        from models import Quest, UserQuestProgress
        today_key = daily_period_key()
        month_key = monthly_period_key()
        quest_rows = (db.query(UserQuestProgress, Quest)
                      .join(Quest, Quest.id == UserQuestProgress.quest_id)
                      .filter(UserQuestProgress.user_id == user.id,
                              ((Quest.quest_type == "daily") &
                               (UserQuestProgress.period_key == today_key)) |
                              ((Quest.quest_type == "monthly") &
                               (UserQuestProgress.period_key == month_key)))
                      .order_by(Quest.quest_type, Quest.sort_order, Quest.id).all())
        quest_progress = [
            {"progress": p, "quest": q,
             "pct": min(100, int(100 * p.progress / max(1, q.target_count)))}
            for p, q in quest_rows
        ]

        player_categories = [
            row[0] for row in (
                db.query(Player.category)
                .filter(Player.category.isnot(None), Player.category != "")
                .distinct()
                .order_by(Player.category.asc())
                .all()
            )
        ]

        return render_template("user_detail.html", user=user, stats=stats,
                               roster=roster, activities=activities,
                               active_matches=match_meta,
                               recent_matches=recent_matches,
                               pending_trades=pending_trades,
                               quest_progress=quest_progress,
                               today_key=today_key, month_key=month_key,
                               player_categories=player_categories,
                               rating_options=range(50, 101))
    finally:
        db.close()


# ─── Edit user QP ────────────────────────────────────────────────────
@app.route("/users/<int:user_id>/edit_qp", methods=["POST"])
@login_required
def admin_edit_qp(user_id):
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash(f"User {user_id} not found", "error")
            return redirect(url_for("users_list"))
        try:
            action = (request.form.get("action") or "set").strip()
            amount = int(request.form.get("amount") or 0)
            current = user.quest_points or 0
            if action == "set":
                user.quest_points = max(0, amount)
                msg = f"Set QP to {user.quest_points}"
            elif action == "add":
                user.quest_points = max(0, current + amount)
                msg = f"Added {amount} QP → {user.quest_points}"
            elif action == "subtract":
                user.quest_points = max(0, current - amount)
                msg = f"Subtracted {amount} QP → {user.quest_points}"
            else:
                flash(f"Unknown action: {action}", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            db.commit()
            log_admin(db, "edit_qp", target_type="user",
                      target_id=user.id, target_name=user.first_name or "",
                      detail=f"{action} {amount} (was {current})")
            db.commit()
            flash(f"✅ {msg}", "success")
        except (ValueError, TypeError) as e:
            flash(f"Invalid amount: {e}", "error")
        except Exception as e:
            db.rollback()
            logger.exception("Edit QP failed")
            flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


# ─── Send a direct message to a user (via bot) ───────────────────────
@app.route("/users/<int:user_id>/message", methods=["POST"])
@login_required
def admin_message_user(user_id):
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash(f"User {user_id} not found", "error")
            return redirect(url_for("users_list"))
        text = (request.form.get("message") or "").strip()
        if not text:
            flash("Message is empty", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if len(text) > 4000:
            text = text[:4000]

        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            flash("BOT_TOKEN not configured — can't send.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        import requests
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": user.telegram_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                flash(f"✅ Message sent to {user.first_name or user.telegram_id}", "success")
                log_admin(db, "message_user", target_type="user",
                          target_id=user.id, target_name=user.first_name or "",
                          detail=text[:200])
                db.commit()
            else:
                desc = data.get("description", "unknown error")
                # Common: user hasn't started the bot, or blocked it
                flash(f"❌ Telegram refused: {desc}", "error")
        except Exception as e:
            logger.exception("send message failed")
            flash(f"❌ Send failed: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


# ─── Reset user — wipe all data ──────────────────────────────────────
@app.route("/users/<int:user_id>/reset", methods=["POST"])
@login_required
def admin_reset_user(user_id):
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash(f"User {user_id} not found", "error")
            return redirect(url_for("users_list"))

        confirmation = (request.form.get("confirmation") or "").strip()
        expected = f"RESET {user_id}"
        if confirmation != expected:
            flash(f"❌ Confirmation phrase wrong. Type exactly: <code>{expected}</code>",
                  "error")
            return redirect(url_for("user_detail", user_id=user_id))

        try:
            orig_state = {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "qp": user.quest_points or 0,
                "roster_count": user.roster_count or 0,
                "matches_played": user.matches_played or 0,
                "first_name": user.first_name or "",
                "username": user.username or "",
            }

            from models import (
                UserRoster, UserStats, ActivityLog, UserQuestProgress,
                UserAchievement, Trade, PlayerTrait, TraitInventory,
            )

            deleted_counts = {}
            for model, attr in [
                (UserRoster, "user_id"),
                (UserStats, "user_id"),
                (ActivityLog, "user_id"),
                (UserQuestProgress, "user_id"),
                (UserAchievement, "user_id"),
                (TraitInventory, "user_id"),
            ]:
                try:
                    n = (db.query(model)
                           .filter(getattr(model, attr) == user_id)
                           .delete(synchronize_session=False))
                    deleted_counts[model.__tablename__] = n
                except Exception as e:
                    deleted_counts[model.__tablename__] = f"ERR:{e}"

            try:
                n = (db.query(PlayerTrait)
                       .filter(PlayerTrait.user_id == user_id)
                       .delete(synchronize_session=False))
                deleted_counts["player_traits"] = n
            except Exception as e:
                deleted_counts["player_traits"] = f"ERR:{e}"

            try:
                n = (db.query(Trade)
                        .filter((Trade.from_user_id == user_id) |
                                (Trade.to_user_id == user_id))
                        .delete(synchronize_session=False))
                deleted_counts["trades"] = n
            except Exception as e:
                deleted_counts["trades"] = f"ERR:{e}"

            # Reset User counters
            user.total_coins = 0
            user.total_gems = 0
            user.quest_points = 0
            user.roster_count = 0
            user.matches_played = 0
            user.matches_won = 0
            user.matches_lost = 0
            if hasattr(user, "win_streak"):
                user.win_streak = 0
            if hasattr(user, "trade_count"):
                user.trade_count = 0

            new_stats = UserStats(user_id=user_id)
            db.add(new_stats)
            db.commit()

            log_admin(db, "reset_user", target_type="user",
                      target_id=user.id,
                      target_name=user.first_name or user.username or "",
                      detail=(f"RESET — was: coins={orig_state['coins']:,}, "
                              f"gems={orig_state['gems']}, QP={orig_state['qp']}, "
                              f"roster={orig_state['roster_count']}, "
                              f"matches={orig_state['matches_played']}. "
                              f"Deleted: {deleted_counts}"))
            db.commit()
            flash(f"✅ Reset complete for <b>{orig_state['first_name'] or orig_state['username'] or user_id}</b>. "
                  f"User must /debut again to play.",
                  "success")
        except Exception as e:
            db.rollback()
            logger.exception("Reset user failed")
            flash(f"Error during reset: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/quest_progress/<int:progress_id>/edit",
           methods=["POST"])
@login_required
def admin_user_quest_progress_edit(user_id, progress_id):
    """Manually adjust a UserQuestProgress.progress value. Auto-flips
    completed=True if progress >= target.
    """
    db = get_session()
    try:
        from models import UserQuestProgress, Quest
        uqp = db.query(UserQuestProgress).get(progress_id)
        if not uqp or uqp.user_id != user_id:
            flash("Progress entry not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        new_progress = max(0, int(request.form.get("progress", 0) or 0))
        q = db.query(Quest).get(uqp.quest_id)
        if not q:
            flash("Quest not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        # Cap at target
        new_progress = min(new_progress, q.target_count)
        old = uqp.progress
        uqp.progress = new_progress
        # Reset / set completion based on new progress
        if new_progress >= q.target_count:
            if not uqp.completed:
                uqp.completed = True
                uqp.completed_at = datetime.utcnow()
        else:
            uqp.completed = False
            uqp.completed_at = None
            # Don't undo a claim — but if admin sets back below target, we DO
            # warn it was previously claimed
        uqp.last_updated = datetime.utcnow()
        db.commit()
        log_admin(db, "quest_progress_edit", "user", user_id, q.name,
                  f"Quest '{q.name}' progress {old}→{new_progress}/{q.target_count}")
        db.commit()
        flash(f"✅ Progress for '{q.name}' set to {new_progress}/{q.target_count}.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id) + "#quest-progress")


@app.route("/users/<int:user_id>/match/<int:match_id>/force_end", methods=["POST"])
@login_required
def admin_force_end_match(user_id, match_id):
    """Admin-cancel a stuck match. Cleans up MatchState + sets Match.status='abandoned'."""
    db = get_session()
    try:
        from models import Match, MatchState
        match = db.query(Match).get(match_id)
        if not match:
            flash("Match not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        # Verify the user is actually in this match (safety check)
        if match.user1_id != user_id and match.user2_id != user_id:
            flash("This user isn't part of that match.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        match.status = "abandoned"
        # Drop the match state so the bot doesn't try to revive it
        ms = db.query(MatchState).filter(MatchState.match_id == match_id).first()
        if ms:
            db.delete(ms)
        db.commit()

        # Drop the in-process lock + cached state so heartbeat won't pick it up
        try:
            from services.match_state_store import release_match_lock, cleanup_state
            release_match_lock(match_id)
            cleanup_state(None, match_id)
        except Exception:
            pass

        log_admin(db, "match_force_end", "match", match_id,
                  f"match #{match_id}",
                  f"Force-ended for user_id={user_id}")
        db.commit()
        flash(f"✅ Match #{match_id} force-ended. User can start a new one.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/cancel_pending_trades", methods=["POST"])
@login_required
def admin_cancel_pending_trades(user_id):
    """Cancel all pending trades for a user (helps unstick edge cases)."""
    db = get_session()
    try:
        from models import Trade
        n = (db.query(Trade)
             .filter(((Trade.initiator_id == user_id) |
                      (Trade.receiver_id == user_id))
                     & (Trade.status == "pending"))
             .update({"status": "cancelled"}))
        db.commit()
        log_admin(db, "trades_cancel", "user", user_id, f"user_id={user_id}",
                  f"Cancelled {n} pending trade(s)")
        db.commit()
        flash(f"✅ Cancelled {n} pending trade(s).", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/edit-purse", methods=["POST"])
@login_required
def user_edit_purse(user_id):
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if user:
            old_coins = user.total_coins
            old_gems = user.total_gems
            user.total_coins = int(request.form.get("coins", user.total_coins))
            user.total_gems = int(request.form.get("gems", user.total_gems))
            # Log admin action
            from services.activity_service import log_activity
            log_activity(db, user.id, "admin_edit",
                         f"Admin set coins {old_coins:,}→{user.total_coins:,}, gems {old_gems}→{user.total_gems}",
                         coins_change=user.total_coins - old_coins,
                         gems_change=user.total_gems - old_gems)
            log_admin(db, "purse_edit", target_type="user", target_id=user.id,
                      target_name=user.username or user.first_name,
                      detail=f"coins {old_coins:,}→{user.total_coins:,}, gems {old_gems}→{user.total_gems}")
            db.commit()
            flash(f"Updated: {user.total_coins:,} coins, {user.total_gems} gems", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/ban", methods=["POST"])
@login_required
def user_ban(user_id):
    """Ban or unban a user. The bot's middleware refuses banned users."""
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash("User not found", "error")
            return redirect(url_for("users_list"))
        action = request.form.get("action", "ban")
        if action == "ban":
            reason = (request.form.get("reason", "") or "").strip()[:500]
            user.is_banned = True
            user.ban_reason = reason or None
            user.banned_at = datetime.utcnow()
            log_admin(db, "user_ban", target_type="user", target_id=user.id,
                      target_name=user.username or user.first_name,
                      detail=f"Reason: {reason or '—'}")
            db.commit()
            flash(f"🚫 Banned {user.username or user.first_name}", "info")
        elif action == "unban":
            user.is_banned = False
            user.ban_reason = None
            user.banned_at = None
            log_admin(db, "user_unban", target_type="user", target_id=user.id,
                      target_name=user.username or user.first_name,
                      detail="unbanned")
            db.commit()
            flash(f"✅ Unbanned {user.username or user.first_name}", "success")
    except Exception as e:
        db.rollback()
        logger.exception("Ban action failed")
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/bulk-action", methods=["POST"])
@login_required
def users_bulk_action():
    """Apply a bulk grant to all users matching the supplied filter params.

    Supported actions:
      - grant_coins (amount=int)
      - grant_gems (amount=int)
      - grant_quest_points (amount=int)

    Caps absurd amounts and excludes banned users. Up to 5,000 users per call.
    """
    action = (request.form.get("action") or "").strip()
    if action not in ("grant_coins", "grant_gems", "grant_quest_points"):
        flash("Unknown bulk action.", "error")
        return redirect(url_for("users_list"))

    try:
        amount = int(request.form.get("amount", "0"))
    except (ValueError, TypeError):
        flash("Amount must be a number.", "error")
        return redirect(url_for("users_list"))
    if amount == 0:
        flash("Amount cannot be zero.", "error")
        return redirect(url_for("users_list"))

    cap = {"grant_coins": 5_000_000, "grant_gems": 5_000,
           "grant_quest_points": 50_000}[action]
    if abs(amount) > cap:
        flash(f"Amount too large (max ±{cap:,}).", "error")
        return redirect(url_for("users_list"))

    db = get_session()
    try:
        from services.activity_service import log_activity as _log_act

        # Rebuild the same filter the users_list page used
        q = request.form.get("q", "").strip()
        query = db.query(User)
        if q:
            query = query.filter(user_lookup_filter(q))
        for k, col in (
            ("coins_min", User.total_coins), ("gems_min", User.total_gems),
            ("roster_min", User.roster_count), ("wins_min", User.matches_won),
            ("played_min", User.matches_played),
        ):
            v = _parse_int(request.form.get(k, ""))
            if v is not None: query = query.filter(col >= v)
        for k, col in (
            ("coins_max", User.total_coins), ("gems_max", User.total_gems),
            ("roster_max", User.roster_count), ("wins_max", User.matches_won),
        ):
            v = _parse_int(request.form.get(k, ""))
            if v is not None: query = query.filter(col <= v)

        joined_after = request.form.get("joined_after", "").strip()
        joined_before = request.form.get("joined_before", "").strip()
        if joined_after:
            try:
                d = datetime.strptime(joined_after, "%Y-%m-%d")
                query = query.filter(User.created_at >= d)
            except ValueError: pass
        if joined_before:
            try:
                d = datetime.strptime(joined_before, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(User.created_at < d)
            except ValueError: pass

        activity = request.form.get("activity", "").strip()
        now_ = datetime.utcnow()
        if activity == "active":
            query = query.filter(User.last_match_date >= now_ - timedelta(days=7))
        elif activity == "inactive":
            cutoff = now_ - timedelta(days=30)
            query = query.filter(
                (User.last_match_date < cutoff) | (User.last_match_date.is_(None)))

        # Exclude banned users from bulk grants
        query = query.filter((User.is_banned == False) | (User.is_banned.is_(None)))

        affected = query.all()
        if len(affected) > 5000:
            flash(f"Filter matches {len(affected)} users — too many. Narrow first.", "error")
            return redirect(url_for("users_list"))

        n = 0
        for u in affected:
            if action == "grant_coins":
                u.total_coins = max(0, (u.total_coins or 0) + amount)
                _log_act(db, u.id, "admin_grant",
                         f"Admin bulk-granted {amount:+,} coins",
                         coins_change=amount)
            elif action == "grant_gems":
                u.total_gems = max(0, (u.total_gems or 0) + amount)
                _log_act(db, u.id, "admin_grant",
                         f"Admin bulk-granted {amount:+,} gems",
                         gems_change=amount)
            elif action == "grant_quest_points":
                u.quest_points = max(0, (u.quest_points or 0) + amount)
                _log_act(db, u.id, "admin_grant",
                         f"Admin bulk-granted {amount:+,} quest points")
            n += 1

        log_admin(db, "bulk_grant", target_type="user_filter", target_id=None,
                  target_name=f"{n} users",
                  detail=f"{action}={amount:+,}")
        db.commit()
        flash(f"✅ Applied {amount:+,} {action.replace('grant_', '').replace('_', ' ')} to {n} users.", "success")
    except Exception as e:
        db.rollback()
        logger.exception("Bulk grant failed")
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    # Preserve filter state on redirect
    return redirect(url_for("users_list", **{
        k: request.form.get(k, "") for k in (
            "q", "coins_min", "coins_max", "gems_min", "gems_max",
            "roster_min", "roster_max", "wins_min", "wins_max",
            "played_min", "joined_after", "joined_before", "activity")
        if request.form.get(k, "").strip()
    }))


@app.route("/users/<int:user_id>/reset-cooldowns", methods=["POST"])
@login_required
def user_reset_cooldowns(user_id):
    db = get_session()
    try:
        stats = db.query(UserStats).filter(UserStats.user_id == user_id).first()
        if stats:
            stats.last_claim = None
            stats.last_daily = None
            stats.last_gspin = None
            from services.activity_service import log_activity
            log_activity(db, user_id, "admin_reset", "Admin reset all cooldowns")
            u = db.query(User).get(user_id)
            log_admin(db, "cooldown_reset", target_type="user", target_id=user_id,
                      target_name=(u.username or u.first_name) if u else str(user_id),
                      detail="Reset claim/daily/gspin cooldowns")
            db.commit()
            flash("All cooldowns reset", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/add-player", methods=["POST"])
@login_required
def user_add_player(user_id):
    db = get_session()
    try:
        player_name = request.form.get("player_name", "").strip()
        rating_raw = request.form.get("overall_rating", "").strip()
        category = request.form.get("category", "").strip()

        user = db.query(User).get(user_id)
        if not user:
            flash("User not found", "error")
            return redirect(url_for("users_list"))

        query = db.query(Player).filter(Player.is_active == True)
        filters = []
        if player_name:
            query = query.filter(Player.name.ilike(f"%{player_name}%"))
            filters.append(f"name contains '{player_name}'")
        if rating_raw:
            try:
                rating = int(rating_raw)
            except ValueError:
                flash("Overall rating must be a number between 50 and 100", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            if rating < 50 or rating > 100:
                flash("Overall rating must be between 50 and 100", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            query = query.filter(Player.rating == rating)
            filters.append(f"{rating} OVR")
        if category:
            query = query.filter(Player.category == category)
            filters.append(category)

        if not filters:
            flash("Enter a player name, rating, or category to add a player", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        player = query.order_by(Player.rating.desc(), Player.name.asc()).first()
        if not player:
            flash(f"No active player found for {' + '.join(filters)}", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        entry = UserRoster(user_id=user.id, player_id=player.id, acquired_date=datetime.utcnow())
        db.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
        from services.activity_service import log_activity
        log_activity(db, user.id, "admin_add", f"Admin added {player.name} ({player.rating} OVR)",
                     player_name=player.name, player_rating=player.rating)
        db.commit()
        flash(f"Added {player.name} ({player.rating} OVR, {player.category}) to roster", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/users/<int:user_id>/remove-player/<int:roster_id>", methods=["POST"])
@login_required
def user_remove_player(user_id, roster_id):
    db = get_session()
    try:
        entry = db.query(UserRoster).filter(UserRoster.id == roster_id, UserRoster.user_id == user_id).first()
        if entry:
            player = db.query(Player).get(entry.player_id)
            name = player.name if player else "Unknown"
            db.delete(entry)
            user = db.query(User).get(user_id)
            if user:
                user.roster_count = max(0, user.roster_count - 1)
            from services.activity_service import log_activity
            log_activity(db, user_id, "admin_remove", f"Admin removed {name}",
                         player_name=name)
            db.commit()
            flash(f"Removed {name} from roster", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("user_detail", user_id=user_id))


# ── Seed database ────────────────────────────────────────────────────

@app.route("/reset-schema", methods=["POST"])
@login_required
def reset_schema():
    try:
        from database import reset_db
        reset_db()
        flash("Database schema reset! All tables recreated. You can now seed players.", "success")
    except Exception as e:
        flash(f"Reset failed: {e}", "error")
    return redirect(url_for("seed_database"))


@app.route("/seed", methods=["GET", "POST"])
@login_required
def seed_database():
    if request.method == "GET":
        return render_template("seed.html")

    db = get_session()
    try:
        count = db.query(func.count(Player.id)).scalar()
        if count > 0:
            flash(f"Database already has {count:,} players. Clear them first or just add more.", "info")
    finally:
        db.close()

    # Try file upload first
    uploaded = request.files.get("jsonfile")
    if uploaded and uploaded.filename:
        try:
            import json
            raw_data = json.load(uploaded)
            added = _seed_from_json(raw_data)
            breakdown = getattr(_seed_from_json, "last_breakdown", {})
            msg = f"✅ Seeded {added:,} players from uploaded file!"
            if breakdown:
                detail = ", ".join(f"{v}: {n}" for v, n in sorted(breakdown.items()))
                msg += f" ({detail})"
            flash(msg, "success")
            return redirect(url_for("dashboard"))
        except Exception as e:
            flash(f"Upload seed failed: {e}", "error")
            return redirect(url_for("seed_database"))

    # Try from data/players.json on disk
    data_path = os.path.join(os.path.dirname(__file__), "data", "players.json")
    if os.path.exists(data_path):
        try:
            import json
            with open(data_path) as f:
                raw_data = json.load(f)
            added = _seed_from_json(raw_data)
            breakdown = getattr(_seed_from_json, "last_breakdown", {})
            msg = f"✅ Seeded {added:,} players from data/players.json!"
            if breakdown:
                detail = ", ".join(f"{v}: {n}" for v, n in sorted(breakdown.items()))
                msg += f" ({detail})"
            flash(msg, "success")
            return redirect(url_for("dashboard"))
        except Exception as e:
            flash(f"File seed failed: {e}", "error")
    else:
        flash("data/players.json not found. Upload the JSON file instead.", "error")

    return redirect(url_for("seed_database"))


@app.route("/clear-players", methods=["POST"])
@login_required
def clear_players():
    db = get_session()
    try:
        count = db.query(Player).delete()
        db.commit()
        flash(f"Deleted {count:,} players.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("seed_database"))


# ═══════════════════════════════════════════════════════════════════════
# TRAIT ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/traits")
@login_required
def admin_traits_list():
    db = get_session()
    try:
        traits = db.query(Trait).order_by(Trait.category, Trait.name).all()
        # Stats: equipped count + inventory count per trait
        stats = {}
        for t in traits:
            equipped = db.query(PlayerTrait).filter(PlayerTrait.trait_id == t.id).count()
            inventory = db.query(TraitInventory).filter(TraitInventory.trait_id == t.id).count()
            stats[t.id] = {"equipped": equipped, "inventory": inventory}
        return render_template("admin_traits.html", traits=traits, stats=stats)
    finally:
        db.close()


@app.route("/traits/<int:trait_id>/edit", methods=["GET", "POST"])
@login_required
def admin_trait_edit(trait_id):
    db = get_session()
    try:
        t = db.query(Trait).get(trait_id)
        if not t:
            flash("Trait not found.", "error")
            return redirect(url_for("admin_traits_list"))

        if request.method == "POST":
            t.name = request.form.get("name", t.name).strip()
            t.category = request.form.get("category", t.category).strip()
            t.description = request.form.get("description", t.description).strip()
            t.emoji = request.form.get("emoji", t.emoji).strip() or "✨"
            t.effect_key = request.form.get("effect_key", t.effect_key).strip()
            t.is_active = bool(request.form.get("is_active"))
            db.commit()
            log_admin(db, "trait_edit", "trait", t.id, t.name,
                       f"Edited trait {t.name}")
            db.commit()
            flash(f"Trait '{t.name}' updated.", "info")
            return redirect(url_for("admin_traits_list"))

        return render_template("admin_trait_form.html", trait=t)
    finally:
        db.close()


@app.route("/traits/<int:trait_id>/toggle", methods=["POST"])
@login_required
def admin_trait_toggle(trait_id):
    db = get_session()
    try:
        t = db.query(Trait).get(trait_id)
        if t:
            t.is_active = not t.is_active
            db.commit()
            log_admin(db, "trait_toggle", "trait", t.id, t.name,
                       f"Set is_active={t.is_active}")
            db.commit()
            flash(f"Trait '{t.name}' is now {'active' if t.is_active else 'inactive'}.", "info")
    finally:
        db.close()
    return redirect(url_for("admin_traits_list"))


@app.route("/users/<int:user_id>/traits")
@login_required
def admin_user_traits(user_id):
    """View a single user's equipped + inventory traits."""
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("users_list"))

        equipped = (db.query(PlayerTrait, Trait, UserRoster, Player)
                    .join(Trait, PlayerTrait.trait_id == Trait.id)
                    .join(UserRoster, PlayerTrait.roster_id == UserRoster.id)
                    .join(Player, UserRoster.player_id == Player.id)
                    .filter(PlayerTrait.user_id == user_id)
                    .order_by(UserRoster.order_position).all())

        inventory = (db.query(TraitInventory, Trait)
                     .join(Trait, TraitInventory.trait_id == Trait.id)
                     .filter(TraitInventory.user_id == user_id).all())

        all_traits = db.query(Trait).filter(Trait.is_active == True).order_by(Trait.category, Trait.name).all()

        return render_template("admin_user_traits.html",
                               user=user, equipped=equipped, inventory=inventory,
                               get_all_traits=all_traits)
    finally:
        db.close()


@app.route("/users/<int:user_id>/traits/grant", methods=["POST"])
@login_required
def admin_grant_trait(user_id):
    """Admin override: grant a trait directly to user inventory at chosen level."""
    db = get_session()
    try:
        user = db.query(User).get(user_id)
        trait_id = int(request.form.get("trait_id", 0))
        level = max(1, min(5, int(request.form.get("level", 1))))
        trait = db.query(Trait).get(trait_id)
        if not (user and trait):
            flash("Invalid user or trait.", "error")
            return redirect(url_for("admin_user_traits", user_id=user_id))
        inv = TraitInventory(user_id=user_id, trait_id=trait_id, level=level)
        db.add(inv)
        db.commit()
        log_admin(db, "trait_grant", "user", user_id, user.username or "",
                   f"Granted {trait.name} Lv.{level}")
        db.commit()
        flash(f"Granted {trait.name} Lv.{level} to {user.username}.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_user_traits", user_id=user_id))


@app.route("/users/<int:user_id>/traits/revoke/<int:player_trait_id>", methods=["POST"])
@login_required
def admin_revoke_player_trait(user_id, player_trait_id):
    db = get_session()
    try:
        pt = db.query(PlayerTrait).get(player_trait_id)
        if pt and pt.user_id == user_id:
            trait = db.query(Trait).get(pt.trait_id)
            db.delete(pt)
            db.commit()
            log_admin(db, "trait_revoke", "user", user_id, "",
                       f"Revoked {trait.name}")
            db.commit()
            flash(f"Revoked {trait.name}.", "info")
    finally:
        db.close()
    return redirect(url_for("admin_user_traits", user_id=user_id))


@app.route("/users/<int:user_id>/traits/del-inv/<int:inv_id>", methods=["POST"])
@login_required
def admin_delete_inventory(user_id, inv_id):
    db = get_session()
    try:
        inv = db.query(TraitInventory).get(inv_id)
        if inv and inv.user_id == user_id:
            db.delete(inv)
            db.commit()
            flash("Inventory entry removed.", "info")
    finally:
        db.close()
    return redirect(url_for("admin_user_traits", user_id=user_id))


def _normalise_category(raw):
    low = raw.strip().lower()
    if low == "batsman": return "Batsman"
    if low == "bowler": return "Bowler"
    if low == "all-rounder": return "All-rounder"
    if low in ("wicketkeeper", "wicket keeper", "wk"): return "Wicket Keeper"
    return raw.strip().title()

def _parse_bowl_style(raw):
    low = raw.strip().lower().replace("\n", "")
    if "leg" in low: return "Leg Spinner"
    if "off" in low: return "Off Spinner"
    if "fast" in low and "medium" not in low: return "Fast"
    return "Medium Pacer"

def _seed_from_json(raw_data):
    """Seed players from parsed JSON list. Returns count added.

    Robust to field-name variants:
      - "Player Name" / "Name"
      - "Version" / "Version " (trailing space) / "version"
      - "overall all" / "Overall" / "Rating"
    And to value variants:
      - "Base card" / "base" / empty → all normalize to "Base"
      - Trailing whitespace stripped from version
    """
    import random
    db = get_session()
    added = 0
    added_by_version = {}
    # Track Base rows added in this batch so variant rows in the same upload
    # can link parent_player_id without an extra DB round-trip per row.
    batch_base_ids = {}

    def _pick(entry, *keys, default=""):
        """Get first non-empty value from any of the supplied keys."""
        for k in keys:
            if k in entry:
                v = entry[k]
                if v is None:
                    continue
                if isinstance(v, str):
                    v = v.strip()
                    if v:
                        return v
                else:
                    return v
        return default

    def _normalize_version(raw):
        """Strip whitespace, collapse 'Base card'/'base'/empty → 'Base'."""
        if not raw:
            return "Base"
        s = str(raw).strip()
        if not s:
            return "Base"
        # Treat "Base", "base", "Base card", "base card" all as canonical "Base"
        low = s.lower()
        if low in ("base", "base card"):
            return "Base"
        # Collapse interior whitespace runs (so "Star  Card" → "Star Card")
        s = " ".join(s.split())
        return s

    try:
        # Track existing (name, version) pairs so we skip exact duplicates only
        # — multiple versions of same player are allowed.
        existing_pairs = set()
        for n, v in db.query(Player.name, Player.version).all():
            existing_pairs.add((n, v or "Base"))

        for entry in raw_data:
            # Accept multiple possible key spellings
            name = _pick(entry, "Player Name", "Name", "name", "player_name")
            if not name:
                continue
            name = str(name).strip()
            if not name:
                continue

            # Version: handle the "Version " (trailing space) typo in the
            # original file as well as the cleaner "Version" key.
            version_raw = _pick(entry, "Version", "Version ", "version",
                                "Card", "card_version", default="")
            version = _normalize_version(version_raw)

            # Duplicate check by (name, version) — multiple versions allowed
            if (name, version) in existing_pairs:
                continue

            # Rating — try several keys
            rating_raw = _pick(entry, "overall all", "Overall", "overall",
                               "Rating", "rating", default=0)
            try:
                rating = int(rating_raw)
            except (ValueError, TypeError):
                continue
            if rating < 50:
                continue
            if rating > 100:
                rating = 100

            category = _normalise_category(
                _pick(entry, "Category", "category", default="Batsman"))

            bat_style_raw = _pick(entry, "Batting Style", "batting_style",
                                  "Bat Hand", default="Right")
            bat_hand = "Left" if "left" in str(bat_style_raw).lower() else "Right"

            bowl_raw = _pick(entry, "Bowling Style", "bowling_style",
                             "Bowl Style", default="Right arm medium fast")
            bowl_hand = "Left" if "left" in str(bowl_raw).lower() else "Right"
            bowl_style = _parse_bowl_style(bowl_raw)

            country = str(_pick(entry, "Country", "country",
                                default="Unknown")).strip() or "Unknown"

            try:
                bat_rating = int(_pick(entry, "Batting Rating", "bat_rating",
                                       "Bat Rating", default=0))
            except (ValueError, TypeError):
                bat_rating = 0
            try:
                bowl_rating = int(_pick(entry, "Bowling Rating", "bowl_rating",
                                        "Bowl Rating", default=0))
            except (ValueError, TypeError):
                bowl_rating = 0

            # parent_player_id linking for non-Base versions
            parent_player_id = None
            if version != "Base":
                if name in batch_base_ids:
                    parent_player_id = batch_base_ids[name]
                else:
                    base = (db.query(Player)
                            .filter(Player.name == name,
                                    Player.version == "Base")
                            .first())
                    if base:
                        parent_player_id = base.id

            player = Player(
                name=name, version=version, rating=rating, category=category,
                country=country, bat_hand=bat_hand, bowl_hand=bowl_hand,
                bowl_style=bowl_style, bat_rating=bat_rating, bowl_rating=bowl_rating,
                parent_player_id=parent_player_id,
                bat_avg=0, strike_rate=0, runs=0, centuries=0,
                bowl_avg=0, economy=0, wickets=0, is_active=True,
            )
            db.add(player)
            # If this is a Base row, flush so it's queryable as a parent
            # for any subsequent variant rows in this batch.
            if version == "Base":
                db.flush()
                batch_base_ids[name] = player.id
            existing_pairs.add((name, version))
            added += 1
            added_by_version[version] = added_by_version.get(version, 0) + 1

        db.commit()

        # Second pass: link parent_player_id for any orphan variants (Legend
        # rows that came before their Base row in the upload).
        try:
            orphans = (db.query(Player)
                       .filter(Player.version != "Base",
                               Player.parent_player_id.is_(None))
                       .all())
            linked = 0
            for variant in orphans:
                base = (db.query(Player)
                        .filter(Player.name == variant.name,
                                Player.version == "Base")
                        .first())
                if base and base.id != variant.id:
                    variant.parent_player_id = base.id
                    linked += 1
            if linked:
                db.commit()
        except Exception:
            logger.exception("Second-pass parent link failed (non-fatal)")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    # Stash breakdown on the function so the caller can show it.
    _seed_from_json.last_breakdown = added_by_version
    return added


@app.route("/health")
def health():
    """Cheap health check — does NOT touch the database.

    Use this for external pingers (UptimeRobot, Render health checks, etc.)
    that just need to know the process is alive. Hitting /status or / will
    wake Neon's compute every time you ping; hitting /health doesn't.
    """
    return "ok", 200, {"Content-Type": "text/plain"}


# ═══════════════════════════════════════════════════════════════════════
# TELEGRAM MINI APP
# ═══════════════════════════════════════════════════════════════════════
# Serves a single-page web app at /webapp that runs inside Telegram's
# WebView. All /api/webapp/* endpoints require valid Telegram initData
# in the Authorization header. CSRF is exempt — Telegram's HMAC signature
# is the authentication.

def _webapp_auth(allow_not_debuted=False):
    """Verify Telegram initData from Authorization header.

    Returns ((db, user, tg_id), None, None) on success. ``user`` is only
    allowed to be ``None`` when ``allow_not_debuted`` is true, which lets the
    init endpoint render the setup screen without exposing gameplay APIs.
    Returns (None, None, (error_dict, http_status)) on failure.
    """
    import os as _os
    bot_token = _os.getenv("BOT_TOKEN", "")
    if not bot_token:
        return None, None, ({"ok": False, "error": "bot_token_missing"}, 500)

    init_data = request.headers.get("Authorization", "") or ""
    if init_data.startswith("tma "):
        init_data = init_data[4:]

    # Dev/test bypass: only honored when WEBAPP_DEV_MODE=1
    if init_data.startswith("DEV_") and _os.getenv("WEBAPP_DEV_MODE") == "1":
        try:
            tg_id = int(init_data[4:])
        except ValueError:
            return None, None, ({"ok": False, "error": "bad_dev_id"}, 401)
    else:
        from services.webapp_auth import verify_init_data, get_user_id
        verified = verify_init_data(init_data, bot_token)
        if not verified:
            return None, None, ({"ok": False, "error": "bad_init_data"}, 401)
        tg_id = get_user_id(verified)
        if not tg_id:
            return None, None, ({"ok": False, "error": "no_user_id"}, 401)

    db = get_session()
    user = db.query(User).filter(User.telegram_id == tg_id).first()
    if not user and not allow_not_debuted:
        db.close()
        return None, tg_id, ({"ok": False, "error": "not_debuted",
                              "message": "Complete your debut to unlock the Mini App."}, 403)
    return (db, user, tg_id), None, None


@app.route("/webapp")
def webapp():
    """Serve the Mini App HTML."""
    return render_template("webapp.html")


@app.route("/api/webapp/init", methods=["POST"])
@csrf_exempt
def webapp_init():
    """Dashboard endpoint."""
    auth, tg_id, err = _webapp_auth(allow_not_debuted=True)
    if err:
        return err
    db, user, tg_id = auth
    try:
        if not user:
            return {
                "ok": True,
                "needs_setup": True,
                "setup": {
                    "bot_username": os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
                    "starter_squad": {
                        "total": 11, "batsmen": 5, "bowlers": 3,
                        "all_rounders": 2, "wicket_keepers": 1,
                        "standard_rating": "72-80", "star_rating": "83-85",
                    },
                },
            }
        from models import UserStats
        from services import adsgram_service, quota_service as _quota_service
        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        from config import GSPIN_COOLDOWN, DAILY_COOLDOWN
        from services.command_config_service import get_cooldown
        gspin_quota = _quota_service.get_quota_status(stats, "spin", session=db)
        daily_quota = _quota_service.get_quota_status(stats, "daily", session=db)
        gspin_ready = not gspin_quota["all_used"]
        gspin_remaining = gspin_quota["cycle_reset_in"] if gspin_quota["all_used"] else 0
        daily_ready = not daily_quota["all_used"]
        daily_remaining = daily_quota["cycle_reset_in"] if daily_quota["all_used"] else 0
        effective_gspin_cd = get_cooldown(db, "gspin", GSPIN_COOLDOWN)
        effective_daily_cd = get_cooldown(db, "daily", DAILY_COOLDOWN)
        played = user.matches_played or 0
        won = user.matches_won or 0
        win_rate = round(won / played * 100, 1) if played else 0
        # Resume affordance: is this user in a live Mini-App match right now?
        live_match = None
        try:
            from models import Match as _M
            lm = (db.query(_M)
                  .filter(((_M.user1_id == user.id) | (_M.user2_id == user.id)),
                          _M.status == "playing")
                  .order_by(_M.id.desc()).first())
            if lm:
                from services.match_webapp_access import get_state as _gst
                st = _gst(lm.id)
                if st and st.get("played_via") == "webapp":
                    live_match = {"match_id": lm.id,
                                  "bat_team": st.get("bat_team_name", ""),
                                  "bowl_team": st.get("bowl_team_name", "")}
        except Exception:
            logger.exception("live_match lookup failed (non-fatal)")
        return {
            "ok": True,
            "live_match": live_match,
            "user": {
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "first_name": user.first_name or "",
                "username": user.username or "",
                "team_name": user.team_name or "",
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
                "roster_count": user.roster_count or 0,
                "matches_played": played,
                "matches_won": won,
                "matches_lost": user.matches_lost or 0,
                "win_rate": win_rate,
                "streak": stats.streak_count if stats else 0,
            },
            "gspin": {
                "ready": gspin_ready,
                "cooldown_remaining": gspin_remaining,
                "cooldown_total": effective_gspin_cd,
                "quota": gspin_quota,
            },
            "daily": {
                "ready": daily_ready,
                "cooldown_remaining": daily_remaining,
                "cooldown_total": effective_daily_cd,
                "quota": daily_quota,
            },
            "onboarding": {
                "xi_set": (db.query(UserRoster)
                           .filter(UserRoster.user_id == user.id,
                                   UserRoster.order_position <= 11).count() == 11),
                "first_reward_claimed": bool(stats and (stats.last_claim or stats.last_daily)),
                "casual_match_played": bool((user.quick_matches_played or 0) > 0),
                "club_joined": bool(user.club_id),
            },
            "adsgram": {
                "configured": adsgram_service.is_configured(),
                "block_id": adsgram_service.get_block_id(),
            },
            "branding": _get_branding_safe(db),
            "login_streak": _touch_login_safe(db, user, stats),
            "season": _get_season_safe(db, user),
            "events": _get_events_safe(db),
        }
    except Exception as e:
        logger.exception("webapp_init failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


def _get_season_safe(db, user):
    try:
        from services.season_service import get_season_info
        info = get_season_info(db, user)
        db.commit()
        return info
    except Exception:
        db.rollback()
        logger.exception("season info failed")
        return None


def _get_events_safe(db):
    try:
        from services.event_service import get_events_for_display
        return get_events_for_display(db)
    except Exception:
        logger.exception("events display failed")
        return []


def _touch_login_safe(db, user, stats):
    """Advance login streak on app open; return status. Never raises."""
    try:
        from services.login_streak_service import touch_login_streak
        status = touch_login_streak(db, stats)
        db.commit()
        return status
    except Exception:
        db.rollback()
        logger.exception("login streak touch failed")
        return None


def _get_branding_safe(db):
    """Wrapper that returns empty branding if service errors (defensive)."""
    try:
        from services.referral_service import get_branding
        return get_branding(db)
    except Exception:
        return {"channel_username": "", "channel_label": "", "channel_link": "",
                "group_username": "", "group_label": "", "group_link": "",
                "tagline": "", "has_any": False}


def _format_miniapp_suggestion_message(message):
    """Prefix Mini App feedback so it is easy to spot in the reports inbox."""
    return f"[Mini App Suggestion] {message}"


@app.route("/api/webapp/suggestion", methods=["POST"])
@csrf_exempt
def webapp_suggestion():
    """Accept Mini App suggestion-box messages into the website reports inbox."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if len(message) < 5:
            return {"ok": False, "message": "Please write at least 5 characters."}, 400
        if len(message) > 1000:
            return {"ok": False, "message": "Suggestion is too long (max 1000 characters)."}, 400

        cutoff = datetime.utcnow() - timedelta(days=1)
        recent_count = (db.query(UserReport)
                          .filter(UserReport.user_id == user.id,
                                  UserReport.message.like("[Mini App Suggestion]%"),
                                  UserReport.created_at >= cutoff)
                          .count())
        if recent_count >= 5:
            return {"ok": False,
                    "message": "You have sent 5 suggestions in the last 24 hours. Please wait before sending more."}, 429

        report = UserReport(
            user_id=user.id,
            message=_format_miniapp_suggestion_message(message),
            is_read=False,
            is_resolved=False,
        )
        db.add(report)
        db.commit()

        logger.info(
            "Mini App suggestion from user %s saved as report #%s",
            user.id,
            report.id,
        )
        return {
            "ok": True,
            "saved": True,
            "message": "Suggestion added to the website report box — thank you!",
            "report_id": report.id,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_suggestion failed")
        return {"ok": False, "message": "Could not send suggestion. Please try again.", "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/search", methods=["POST"])
@csrf_exempt
def webapp_search():
    """Search players. Body: {q, min_rating, max_rating, category, limit}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or "").strip()
        min_r = int(data.get("min_rating") or 0)
        max_r = int(data.get("max_rating") or 100)
        category = (data.get("category") or "").strip()
        limit = min(50, int(data.get("limit") or 20))

        query = db.query(Player).filter(Player.is_active == True)
        if q:
            like = f"%{q}%"
            query = query.filter(Player.name.ilike(like))
        if min_r > 0:
            query = query.filter(Player.rating >= min_r)
        if max_r < 100:
            query = query.filter(Player.rating <= max_r)
        if category:
            query = query.filter(Player.category == category)

        players = query.order_by(Player.rating.desc()).limit(limit).all()

        from models import UserRoster
        owned_ids = {p[0] for p in (
            db.query(UserRoster.player_id)
              .filter(UserRoster.user_id == user.id).all())}

        from config import get_buy_value
        return {
            "ok": True,
            "results": [{
                "id": p.id,
                "name": p.name,
                "rating": p.rating,
                "category": p.category,
                "country": p.country,
                "version": p.version or "Base",
                "bat_rating": p.bat_rating or 0,
                "bowl_rating": p.bowl_rating or 0,
                "price": get_buy_value(p.rating),
                "owned": p.id in owned_ids,
                "blocked": bool(getattr(p, "restricted_from_buypl", False)),
            } for p in players],
        }
    except Exception as e:
        logger.exception("webapp_search failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/player/<int:player_id>", methods=["POST"])
@csrf_exempt
def webapp_player_detail(player_id):
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        p = db.query(Player).get(player_id)
        if not p or not p.is_active:
            return {"ok": False, "error": "not_found"}, 404
        from services.version_service import user_owns_any_version
        from config import get_buy_value, get_sell_value
        owns_any = user_owns_any_version(db, user.id, p.id)
        return {
            "ok": True,
            "player": {
                "id": p.id,
                "name": p.name,
                "rating": p.rating,
                "category": p.category,
                "country": p.country,
                "version": p.version or "Base",
                "bat_hand": p.bat_hand,
                "bowl_hand": p.bowl_hand,
                "bowl_style": p.bowl_style,
                "bat_rating": p.bat_rating or 0,
                "bowl_rating": p.bowl_rating or 0,
                "price": get_buy_value(p.rating),
                "sell_value": get_sell_value(p.rating),
                "owned_any_version": owns_any,
                "blocked": bool(getattr(p, "restricted_from_buypl", False)),
            },
        }
    except Exception as e:
        logger.exception("webapp_player_detail failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/buy/<int:player_id>", methods=["POST"])
@csrf_exempt
def webapp_buy(player_id):
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from datetime import datetime as _dt
        from models import UserRoster
        from services.version_service import user_owns_any_version
        from config import get_buy_value, MAX_ROSTER

        p = db.query(Player).get(player_id)
        if not p or not p.is_active:
            return {"ok": False, "error": "not_found"}, 404
        if getattr(p, "restricted_from_buypl", False):
            return {"ok": False, "error": "blocked",
                    "message": f"{p.name} is Not available to Buy via the Mini App."}, 403
        if user_owns_any_version(db, user.id, p.id):
            return {"ok": False, "error": "already_owned",
                    "message": f"You already own {p.name}."}, 400
        if (user.roster_count or 0) >= MAX_ROSTER:
            return {"ok": False, "error": "roster_full",
                    "message": f"Roster full ({MAX_ROSTER}/{MAX_ROSTER}). Release someone first."}, 400
        price = get_buy_value(p.rating)
        if (user.total_coins or 0) < price:
            return {"ok": False, "error": "insufficient_coins",
                    "message": f"Need {price:,} 🪙, you have {user.total_coins:,}."}, 400

        user.total_coins = (user.total_coins or 0) - price
        entry = UserRoster(
            user_id=user.id, player_id=p.id,
            order_position=(user.roster_count or 0) + 1,
            acquired_date=_dt.utcnow(),
        )
        db.add(entry)
        db.flush()
        user.roster_count = (user.roster_count or 0) + 1

        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "buy",
                         f"[MiniApp] Bought {p.name} ({p.rating} OVR) for {price:,}",
                         coins_change=-price,
                         player_name=p.name, player_rating=p.rating)
        except Exception:
            pass
        try:
            from services.undo_service import record_buy
            record_buy(db, user.id,
                       roster_id=entry.id, player_id=p.id,
                       player_name=p.name, rating=p.rating, price=price)
        except Exception:
            pass

        db.commit()
        return {
            "ok": True,
            "purchased": {"name": p.name, "rating": p.rating, "price": price},
            "balance": user.total_coins,
            "roster_count": user.roster_count,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_buy failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/release/<int:roster_id>", methods=["POST"])
@csrf_exempt
def webapp_release(roster_id):
    """Release a single player from the user's roster.

    Returns coins received from sell value.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        from config import get_sell_value

        row = (db.query(UserRoster)
               .filter(UserRoster.id == roster_id,
                       UserRoster.user_id == user.id).first())
        if not row:
            return {"ok": False, "error": "not_found",
                    "message": "That player isn't in your roster."}, 404

        player = db.query(Player).get(row.player_id)
        if not player:
            return {"ok": False, "error": "player_missing"}, 404

        # Prevent releasing the captain
        if user.captain_roster_id == row.id:
            return {"ok": False, "error": "is_captain",
                    "message": "Can't release captain. Set a new captain first."}, 400

        sell_value = get_sell_value(player.rating)
        player_name = player.name
        player_rating = player.rating

        # Remove from roster
        db.delete(row)
        user.total_coins = (user.total_coins or 0) + sell_value
        user.roster_count = max(0, (user.roster_count or 0) - 1)

        # Activity log + undo
        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "release",
                         f"[MiniApp] Released {player_name} ({player_rating})",
                         coins_change=sell_value,
                         player_name=player_name, player_rating=player_rating)
        except Exception:
            pass
        try:
            from services.undo_service import record_release
            record_release(db, user.id, player.id, sell_value)
        except Exception:
            pass

        db.commit()
        return {
            "ok": True,
            "sell_value": sell_value,
            "player_name": player_name,
            "player_rating": player_rating,
            "balance": {
                "coins": user.total_coins or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_release failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/release_multi", methods=["POST"])
@csrf_exempt
def webapp_release_multi():
    """Release multiple players at once. Returns total coins gained.

    Request body: {roster_ids: [123, 456, ...]}
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        from config import get_sell_value

        data = request.get_json(silent=True) or {}
        ids = data.get("roster_ids") or []
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "no_ids",
                    "message": "No players selected."}, 400
        # Sanitize
        try:
            ids = [int(x) for x in ids][:25]  # cap at 25 to prevent abuse
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_ids"}, 400

        rows = (db.query(UserRoster)
                .filter(UserRoster.user_id == user.id,
                        UserRoster.id.in_(ids)).all())
        if not rows:
            return {"ok": False, "error": "not_found",
                    "message": "None of those players are in your roster."}, 404

        # Bail if captain is in the selection
        if user.captain_roster_id and any(r.id == user.captain_roster_id for r in rows):
            return {"ok": False, "error": "captain_selected",
                    "message": "Captain is in selection. Set a new captain first."}, 400

        # Look up players for sell values
        player_ids = [r.player_id for r in rows]
        players = {p.id: p for p in
                   db.query(Player).filter(Player.id.in_(player_ids)).all()}

        total_coins = 0
        released = []
        for r in rows:
            p = players.get(r.player_id)
            if not p: continue
            sv = get_sell_value(p.rating)
            total_coins += sv
            released.append({
                "roster_id": r.id,
                "name": p.name,
                "rating": p.rating,
                "sell_value": sv,
            })
            db.delete(r)
            try:
                from services.undo_service import record_release
                record_release(db, user.id, p.id, sv)
            except Exception:
                pass

        user.total_coins = (user.total_coins or 0) + total_coins
        user.roster_count = max(0, (user.roster_count or 0) - len(released))

        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "release_multi",
                         f"[MiniApp] Released {len(released)} players",
                         coins_change=total_coins)
        except Exception:
            pass

        db.commit()
        return {
            "ok": True,
            "released_count": len(released),
            "total_coins": total_coins,
            "released": released,
            "balance": {
                "coins": user.total_coins or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_release_multi failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/roster", methods=["POST"])
@csrf_exempt
def webapp_roster():
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        from config import get_sell_value
        rows = (db.query(UserRoster, Player)
                .join(Player, UserRoster.player_id == Player.id)
                .filter(UserRoster.user_id == user.id)
                .order_by(UserRoster.order_position.asc()).all())
        return {
            "ok": True,
            "roster": [{
                "roster_id": r.id,
                "position": r.order_position,
                "player_id": p.id,
                "name": p.name,
                "rating": p.rating,
                "category": p.category,
                "country": p.country,
                "version": p.version or "Base",
                "is_captain": bool(user.captain_roster_id == r.id),
                "sell_value": get_sell_value(p.rating),
            } for r, p in rows],
            "captain_roster_id": user.captain_roster_id,
            "count": len(rows),
            "max": 25,
        }
    except Exception as e:
        logger.exception("webapp_roster failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/spin", methods=["POST"])
@csrf_exempt
def webapp_spin():
    """Spin endpoint with quota system.

    Each user gets:
      - 1 FREE spin per 24h cycle (no ad needed)
      - 5 additional ad-gated spins per cycle

    Request body:
      {ad_token: <optional, only needed when using ad-gated slot>}

    If `ad_token` is missing/empty, we'll try to use the FREE slot. If
    free isn't available, returns 400 with hint to watch ad.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        import os as _os
        from datetime import datetime as _dt
        from models import UserStats
        from services import adsgram_service, quota_service

        data = request.get_json(silent=True) or {}
        ad_token = (data.get("ad_token") or "").strip()
        dev_mode = (_os.getenv("WEBAPP_DEV_MODE") == "1")
        ads_configured = adsgram_service.is_configured()

        # ── Load stats first to check quota ──
        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush()

        # ── Try to verify ad (so we know what slot is available) ──
        verified_via = None
        if adsgram_service.claim_postback(db, tg_id):
            verified_via = "server_postback"
        elif ad_token.startswith("CT-"):
            if adsgram_service.consume_client_token(ad_token, tg_id):
                verified_via = "client_token"
        elif ad_token.startswith("MOCK-"):
            if dev_mode or not ads_configured:
                verified_via = "mock"

        ad_provided = bool(verified_via)

        # Track ad_watched for the daily quest — ONLY for postback/mock.
        # client_token (CT-) ads were already tracked at /ad-completed.
        if verified_via in ("server_postback", "mock"):
            try:
                from services.quest_service import safe_track
                safe_track(db, user.id, "ad_watched", 1)
            except Exception:
                pass

        # ── Check quota ──
        allowed, slot_type, reason = quota_service.can_use(stats, "spin", ad_provided, session=db)
        if not allowed:
            status = quota_service.get_quota_status(stats, "spin")
            if reason == "ad_required":
                return {"ok": False, "error": "ad_required",
                        "message": "Free spin already used — watch an ad for next spin.",
                        "quota": status,
                        "ads_configured": ads_configured}, 400
            elif reason == "cycle_exhausted":
                h = status["cycle_reset_in"] // 3600
                m = (status["cycle_reset_in"] % 3600) // 60
                return {"ok": False, "error": "cycle_exhausted",
                        "message": f"All spins used. New spins in {h}h {m}m.",
                        "quota": status,
                        "cooldown_remaining": status["cycle_reset_in"]}, 429
            else:
                return {"ok": False, "error": reason or "blocked"}, 400

        # ── Reward ──
        from services.gspin_reward_service import pick_reward, apply_reward
        reward = pick_reward(db)
        if not reward:
            return {"ok": False, "error": "no_rewards_configured",
                    "message": "No spin rewards configured. Contact admin."}, 500

        result = apply_reward(db, user, reward)
        # Update legacy last_gspin (bot still references it) + consume quota slot
        stats.last_gspin = _dt.utcnow()
        quota_service.consume_slot(stats, "spin", slot_type)

        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "gspin",
                         f"[MiniApp/{slot_type}/{verified_via or 'free'}] "
                         f"Spin: {reward.label} → {result['type']}")
        except Exception:
            pass
        try:
            from services.quest_service import safe_track
            safe_track(db, user.id, "gspin", 1)
        except Exception:
            pass

        db.commit()
        return {
            "ok": True,
            "reward": result,
            "slot_type": slot_type,
            "verified_via": verified_via,
            "quota": quota_service.get_quota_status(stats, "spin", session=db),
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_spin failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/ad-completed", methods=["POST"])
@csrf_exempt
def webapp_ad_completed():
    """Called by client after Adsgram SDK's show() promise resolves.

    Returns a single-use token the client immediately passes to /api/webapp/spin.
    This is the client-side verification fallback used when server-side
    postback isn't available (small apps, dev mode, etc).
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import adsgram_service
        token = adsgram_service.issue_client_token(tg_id)
        # Track for the "watch N ads" daily quest. This endpoint fires once per
        # completed ad (the Adsgram SDK resolves only on full view).
        try:
            from services.quest_service import safe_track
            safe_track(db, user.id, "ad_watched", 1)
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": True, "ad_token": token}
    finally:
        db.close()


def _verify_ad_for_action(db, tg_id, ad_token, dev_mode, ads_configured):
    """Shared ad-verification helper for spin/daily. Returns verified_via str or None."""
    from services import adsgram_service
    if adsgram_service.claim_postback(db, tg_id):
        return "server_postback"
    if ad_token and ad_token.startswith("CT-"):
        if adsgram_service.consume_client_token(ad_token, tg_id):
            return "client_token"
    if ad_token and ad_token.startswith("MOCK-"):
        if dev_mode or not ads_configured:
            return "mock"
    return None


@app.route("/api/webapp/daily", methods=["POST"])
@csrf_exempt
def webapp_daily():
    """Daily-claim endpoint — ad-gated, shares cooldown with bot /daily.

    Uses services.daily_service.claim_daily so the reward logic stays
    consistent with the bot. UserStats.last_daily is shared, so claiming
    via Mini App also blocks the bot's /daily until cooldown elapses
    (and vice versa).
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        import os as _os
        from models import UserStats
        from services import adsgram_service, quota_service
        from services.daily_service import claim_daily

        data = request.get_json(silent=True) or {}
        ad_token = (data.get("ad_token") or "").strip()
        dev_mode = (_os.getenv("WEBAPP_DEV_MODE") == "1")
        ads_configured = adsgram_service.is_configured()

        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush()

        # ── Verify ad (so we know which slot is available) ──
        verified_via = _verify_ad_for_action(
            db, tg_id, ad_token, dev_mode, ads_configured)
        ad_provided = bool(verified_via)

        # Track ad_watched for daily quest — postback/mock only
        # (CT- tracked at /ad-completed).
        if verified_via in ("server_postback", "mock"):
            try:
                from services.quest_service import safe_track
                safe_track(db, user.id, "ad_watched", 1)
            except Exception:
                pass

        # ── Check quota ──
        allowed, slot_type, reason = quota_service.can_use(stats, "daily", ad_provided, session=db)
        if not allowed:
            status = quota_service.get_quota_status(stats, "daily")
            if reason == "ad_required":
                return {"ok": False, "error": "ad_required",
                        "message": "Free daily already used — watch an ad for next claim.",
                        "quota": status,
                        "ads_configured": ads_configured}, 400
            elif reason == "cycle_exhausted":
                h = status["cycle_reset_in"] // 3600
                m = (status["cycle_reset_in"] % 3600) // 60
                return {"ok": False, "error": "cycle_exhausted",
                        "message": f"All daily claims used. New claims in {h}h {m}m.",
                        "quota": status,
                        "cooldown_remaining": status["cycle_reset_in"]}, 429

        # ── Claim ── (skip the legacy 24h cooldown, quota_service handles limits)
        result = claim_daily(db, user, source_label=f"MiniApp/{slot_type}/{verified_via or 'free'}",
                             skip_cooldown=True)
        if not result["ok"]:
            return {"ok": False, "error": result.get("error", "internal"),
                    "message": "Daily claim failed unexpectedly."}, 500

        quota_service.consume_slot(stats, "daily", slot_type)
        db.commit()

        return {
            "ok": True,
            "slot_type": slot_type,
            "verified_via": verified_via,
            "quota": quota_service.get_quota_status(stats, "daily", session=db),
            "reward": {
                "coins": result["coins"],
                "gems": result["gems"],
                "streak": result["streak"],
                "streak_max": result["streak_max"],
                "milestone": result["milestone"],
                "milestone_player": result["milestone_player"],
                "players_added": result["players_added"],
                "players_skipped": result["players_skipped"],
                "lines": result["lines"],
            },
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_daily failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/players", methods=["POST"])
@csrf_exempt
def webapp_players():
    """Browse all players with filters: rating range, category, country, version.

    Different from /search in that it returns paginated results sorted
    by rating desc, suitable for an infinite-scroll "Browse" tab.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from config import get_buy_value
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or "").strip()
        min_r = int(data.get("min_rating") or 0)
        max_r = int(data.get("max_rating") or 100)
        # Exact rating filter: if set, overrides min/max
        exact_r = data.get("exact_rating")
        if exact_r not in (None, "", 0, "0"):
            try:
                er = int(exact_r)
                min_r = er
                max_r = er
            except (ValueError, TypeError):
                pass
        # Price filters
        min_price = data.get("min_price")
        max_price = data.get("max_price")
        try:
            min_price = int(min_price) if min_price not in (None, "", 0, "0") else 0
        except (ValueError, TypeError):
            min_price = 0
        try:
            max_price = int(max_price) if max_price not in (None, "") else 0
        except (ValueError, TypeError):
            max_price = 0
        category = (data.get("category") or "").strip()
        country = (data.get("country") or "").strip()
        page = max(1, int(data.get("page") or 1))
        per_page = min(50, max(1, int(data.get("per_page") or 20)))
        only_unowned = bool(data.get("only_unowned"))

        query = db.query(Player).filter(Player.is_active == True)
        # NB: include ALL versions (Base, Legend, Star, etc).
        # Version is shown as a badge in the row.

        if q:
            # Search matches name OR country (case-insensitive)
            like = f"%{q}%"
            query = query.filter(
                (Player.name.ilike(like)) | (Player.country.ilike(like))
            )
        if min_r > 0:
            query = query.filter(Player.rating >= min_r)
        if max_r < 100:
            query = query.filter(Player.rating <= max_r)

        # Price range filter: BUY_SELL is monotonic in rating, so convert
        # price thresholds to allowed rating list.
        if min_price > 0 or max_price > 0:
            from config import BUY_SELL
            allowed_ratings = []
            for r_v, (buy_p, _) in BUY_SELL.items():
                if min_price > 0 and buy_p < min_price: continue
                if max_price > 0 and buy_p > max_price: continue
                allowed_ratings.append(r_v)
            if allowed_ratings:
                query = query.filter(Player.rating.in_(allowed_ratings))
            else:
                query = query.filter(Player.rating < 0)  # empty result

        if category:
            query = query.filter(Player.category == category)
        if country:
            query = query.filter(Player.country == country)

        total = query.count()
        players = (query.order_by(Player.rating.desc(), Player.name.asc())
                   .offset((page - 1) * per_page).limit(per_page).all())

        from models import UserRoster
        owned_ids = {p[0] for p in (
            db.query(UserRoster.player_id)
              .filter(UserRoster.user_id == user.id).all())}

        results = []
        from config import get_buy_value
        for p in players:
            owned = p.id in owned_ids
            if only_unowned and owned:
                continue
            results.append({
                "id": p.id, "name": p.name, "rating": p.rating,
                "category": p.category, "country": p.country,
                "version": p.version or "Base",
                "bat_rating": p.bat_rating or 0,
                "bowl_rating": p.bowl_rating or 0,
                "price": get_buy_value(p.rating),
                "owned": owned,
                "blocked": bool(getattr(p, "restricted_from_buypl", False)),
            })

        return {
            "ok": True,
            "results": results,
            "page": page,
            "per_page": per_page,
            "total": total,
            "has_more": (page * per_page) < total,
        }
    except Exception as e:
        logger.exception("webapp_players failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/countries", methods=["POST"])
@csrf_exempt
def webapp_countries():
    """Distinct country list for filter dropdown."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        rows = (db.query(Player.country)
                .filter(Player.is_active == True,
                        Player.country.isnot(None),
                        Player.country != "")
                .distinct().order_by(Player.country.asc()).all())
        countries = [r[0] for r in rows if r[0]]
        return {"ok": True, "countries": countries}
    except Exception as e:
        logger.exception("webapp_countries failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/leaderboard", methods=["POST"])
@csrf_exempt
def webapp_leaderboard():
    """Top users by various metrics.

    Body: {metric: 'wins' | 'win_rate' | 'coins' | 'roster_rating', limit: int}
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        data = request.get_json(silent=True) or {}
        metric = (data.get("metric") or "wins").strip()
        limit = min(50, max(5, int(data.get("limit") or 20)))

        # Build the leaderboard query
        if metric == "wins":
            q = (db.query(User)
                 .filter(User.matches_played > 0)
                 .order_by(User.matches_won.desc().nullslast(),
                           User.matches_played.desc().nullslast()))
        elif metric == "win_rate":
            # Order by ratio — require at least 5 matches for stability
            from sqlalchemy import cast, Float, case
            ratio = case(
                (User.matches_played > 0,
                 cast(User.matches_won, Float) / cast(User.matches_played, Float)),
                else_=0.0,
            )
            q = (db.query(User)
                 .filter(User.matches_played >= 5)
                 .order_by(ratio.desc(), User.matches_played.desc()))
        elif metric == "coins":
            q = db.query(User).order_by(User.total_coins.desc().nullslast())
        elif metric == "roster_rating":
            # Average rating of player roster — requires join + aggregate
            from sqlalchemy import func as _sf
            from models import UserRoster
            sub = (db.query(UserRoster.user_id,
                            _sf.avg(Player.rating).label("avg_rating"),
                            _sf.count(UserRoster.id).label("roster_n"))
                   .join(Player, UserRoster.player_id == Player.id)
                   .group_by(UserRoster.user_id)
                   .having(_sf.count(UserRoster.id) >= 5)
                   .subquery())
            rows = (db.query(User, sub.c.avg_rating, sub.c.roster_n)
                    .join(sub, sub.c.user_id == User.id)
                    .order_by(sub.c.avg_rating.desc())
                    .limit(limit).all())
            users_list = []
            me_rank = None
            for i, (u, avg_r, n) in enumerate(rows, start=1):
                entry = {
                    "rank": i,
                    "telegram_id": u.telegram_id,
                    "username": u.username or "",
                    "first_name": u.first_name or "",
                    "team_name": u.team_name or "",
                    "value": round(float(avg_r or 0), 1),
                    "label": f"{round(float(avg_r or 0), 1)} avg ({n})",
                    "is_me": (u.telegram_id == tg_id),
                }
                if entry["is_me"]:
                    me_rank = i
                users_list.append(entry)
            return {
                "ok": True, "metric": metric,
                "rows": users_list, "my_rank": me_rank,
            }
        else:
            return {"ok": False, "error": "bad_metric"}, 400

        users = q.limit(limit).all()
        rows = []
        me_rank = None
        for i, u in enumerate(users, start=1):
            if metric == "wins":
                value = u.matches_won or 0
                label = f"{value} W / {u.matches_played or 0} M"
            elif metric == "win_rate":
                p = u.matches_played or 0
                w = u.matches_won or 0
                pct = round(w / p * 100, 1) if p else 0
                value = pct
                label = f"{pct}% ({w}/{p})"
            elif metric == "coins":
                value = u.total_coins or 0
                label = f"{value:,} 🪙"
            entry = {
                "rank": i,
                "telegram_id": u.telegram_id,
                "username": u.username or "",
                "first_name": u.first_name or "",
                "team_name": u.team_name or "",
                "value": value,
                "label": label,
                "is_me": (u.telegram_id == tg_id),
            }
            if entry["is_me"]:
                me_rank = i
            rows.append(entry)

        return {
            "ok": True, "metric": metric,
            "rows": rows, "my_rank": me_rank,
        }
    except Exception as e:
        logger.exception("webapp_leaderboard failed")
        return {"ok": False, "error": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quickmatch", methods=["POST"])
@csrf_exempt
def webapp_quickmatch():
    """DEPRECATED single-shot Quick Match endpoint.

    The Mini App now uses the phase-by-phase flow:
      /api/webapp/quickmatch/start
      /api/webapp/quickmatch/phase
      /api/webapp/quickmatch/abandon

    Returns a clear error so any older client knows to refresh.
    """
    return {
        "ok": False, "error": "endpoint_deprecated",
        "message": "Quick Match is now phase-by-phase. Please refresh the app to get the latest version.",
    }, 410


def _webapp_quickmatch_legacy_DISABLED():
    """Play a quick match (single endpoint — submit all choices at once).

    Request body:
      {
        toss_choice: 'bat' | 'bowl',
        innings_1: ['aggressive'|'balanced'|'defensive', ...x3],
        innings_2: ['aggressive'|'balanced'|'defensive', ...x3],
        difficulty: 'easy' | 'medium' | 'hard' (default 'medium')
      }

    Returns the full match simulation result.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quick_match_service import play_quick_match

        data = request.get_json(silent=True) or {}
        toss_choice = (data.get("toss_choice") or "bat").lower()
        if toss_choice not in ("bat", "bowl"):
            toss_choice = "bat"
        innings_1 = data.get("innings_1") or ["balanced", "balanced", "balanced"]
        innings_2 = data.get("innings_2") or ["balanced", "balanced", "balanced"]
        difficulty = (data.get("difficulty") or "medium").lower()
        if difficulty not in ("easy", "medium", "hard"):
            difficulty = "medium"

        # Validate choices
        valid_choices = ("aggressive", "balanced", "defensive")
        innings_1 = [c if c in valid_choices else "balanced" for c in innings_1[:3]]
        innings_2 = [c if c in valid_choices else "balanced" for c in innings_2[:3]]
        while len(innings_1) < 3: innings_1.append("balanced")
        while len(innings_2) < 3: innings_2.append("balanced")

        # Check roster size — and check for full XI
        if (user.roster_count or 0) < 5:
            return {"ok": False, "error": "roster_too_small",
                    "message": "You need at least 5 players in your roster to play."}, 400

        # Try XI-based simulation first (better experience)
        from services.quick_match_service import play_quick_match_xi, play_quick_match
        result = play_quick_match_xi(
            db, user,
            user_choices={
                "toss_choice": toss_choice,
                "innings_1": innings_1,
                "innings_2": innings_2,
            },
            opponent_difficulty=difficulty,
        )
        # If XI was incomplete, return the error explaining
        if not result.get("ok", True):
            err_code = result.get("error", "xi_incomplete")
            return {
                "ok": False, "error": err_code,
                "message": result.get("message",
                    "Your playing XI must have 11 players. Set up your XI first."),
            }, 400

        # XI sim handled coin reward + activity log internally — skip duplicating
        coin_reward = result.get("coin_reward", 0)
        # NOTE: total_coins was already updated in play_quick_match_xi

        # Bump match counters
        user.matches_played = (user.matches_played or 0) + 1
        if result["user_won"] is True:
            user.matches_won = (user.matches_won or 0) + 1
        elif result["user_won"] is False:
            user.matches_lost = (user.matches_lost or 0) + 1
        # Tie: no win/loss bump

        # (Activity log + quest tracking already handled in play_quick_match_xi)

        db.commit()
        return {
            "ok": True,
            "result": result,
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "matches_played": user.matches_played,
                "matches_won": user.matches_won,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quickmatch failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quickmatch/start", methods=["POST"])
@csrf_exempt
def webapp_quickmatch_start():
    """Start a phase-by-phase Quick Match (v3).

    Body: {toss_choice: 'bat'|'bowl', difficulty: 'easy'|'medium'|'hard'}

    Validates user's playing XI (3-5 bat / 3-5 bowl / 1-2 wk / 1-3 ar) AND
    daily Quick Match limit before starting. Quick Match stats are tracked
    SEPARATELY from global match stats and do not affect the leaderboard.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quick_match_service import (
            start_phase_match, drop_phase_match, get_phase_match,
        )
        data = request.get_json(silent=True) or {}
        toss_choice = (data.get("toss_choice") or "bat").lower()
        if toss_choice not in ("bat", "bowl"):
            toss_choice = "bat"
        difficulty = (data.get("difficulty") or "medium").lower()
        if difficulty not in ("easy", "medium", "hard"):
            difficulty = "medium"

        # Abandon any stale in-flight match (defensive cleanup)
        existing = get_phase_match(tg_id)
        if existing and not existing.get("complete"):
            drop_phase_match(tg_id)

        result = start_phase_match(db, user, tg_id, toss_choice, difficulty)
        # If start was blocked (limit/xi), we may have modified user fields
        # (date-reset in _reset_daily_counter_if_new_day) — persist either way
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quickmatch_start failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quickmatch/phase", methods=["POST"])
@csrf_exempt
def webapp_quickmatch_phase():
    """Advance the active Quick Match by one phase.
    Body: {choice: 'aggressive'|'balanced'|'defensive'}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quick_match_service import play_phase
        data = request.get_json(silent=True) or {}
        choice = (data.get("choice") or "balanced").lower()
        if choice not in ("aggressive", "balanced", "defensive"):
            choice = "balanced"

        result = play_phase(db, user, tg_id, choice)
        if result.get("match_complete"):
            db.commit()  # Persist QM counters + coins
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quickmatch_phase failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quickmatch/abandon", methods=["POST"])
@csrf_exempt
def webapp_quickmatch_abandon():
    """Cancel the active Quick Match (no rewards, no counter increment)."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quick_match_service import drop_phase_match
        drop_phase_match(tg_id)
        return {"ok": True}
    except Exception as e:
        logger.exception("webapp_quickmatch_abandon failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quickmatch/quota", methods=["POST"])
@csrf_exempt
def webapp_quickmatch_quota():
    """Return the user's current Quick Match daily quota."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quick_match_service import get_quick_match_quota
        used, limit, remaining = get_quick_match_quota(db, user)
        db.commit()  # Persist any date-reset
        return {
            "ok": True,
            "used": used, "limit": limit, "remaining": remaining,
            "stats": {
                "quick_matches_played": user.quick_matches_played or 0,
                "quick_matches_won": user.quick_matches_won or 0,
                "quick_matches_lost": user.quick_matches_lost or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quickmatch_quota failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/freepack/status", methods=["POST"])
@csrf_exempt
def webapp_freepack_status():
    """Return free pack cooldown + probability table for display."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import adsgram_service
        from services.free_pack_service import (
            check_free_pack_cooldown, get_band_percentages, get_cooldown_minutes,
        )
        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush(); db.commit()
        ready, remaining = check_free_pack_cooldown(db, stats)
        return {
            "ok": True,
            "ready": ready,
            "remaining_seconds": remaining,
            "cooldown_minutes": get_cooldown_minutes(db),
            "bands": get_band_percentages(db),
            "ads_configured": adsgram_service.is_configured(),
            "adsgram_block_id": adsgram_service.get_block_id(),
            "roster_count": user.roster_count or 0,
            "roster_full": (user.roster_count or 0) >= 25,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_freepack_status failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/freepack/open", methods=["POST"])
@csrf_exempt
def webapp_freepack_open():
    """Open a free pack. Requires a verified ad (postback / client token /
    mock in dev). Enforces the 1h cooldown server-side."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import adsgram_service
        from services.free_pack_service import open_free_pack, check_free_pack_cooldown
        data = request.get_json(silent=True) or {}
        ad_token = (data.get("ad_token") or "").strip()

        dev_mode = os.getenv("WEBAPP_DEV_MODE") == "1"
        ads_configured = adsgram_service.is_configured()

        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush()

        # Cooldown check first (don't waste their ad)
        ready, remaining = check_free_pack_cooldown(db, stats)
        if not ready:
            return {"ok": False, "error": "cooldown",
                    "message": "Free Pack is still on cooldown.",
                    "remaining_seconds": remaining}, 400

        # Verify ad
        verified_via = None
        if adsgram_service.claim_postback(db, tg_id):
            verified_via = "server_postback"
        elif ad_token.startswith("CT-"):
            if adsgram_service.consume_client_token(ad_token, tg_id):
                verified_via = "client_token"
        elif ad_token.startswith("MOCK-"):
            if dev_mode or not ads_configured:
                verified_via = "mock"

        if not verified_via:
            return {"ok": False, "error": "ad_required",
                    "message": "Watch an ad to claim your free pack.",
                    "ads_configured": ads_configured}, 400

        # Track ad_watched for the daily quest — ONLY postback/mock
        # (CT- already tracked at /ad-completed).
        if verified_via in ("server_postback", "mock"):
            try:
                from services.quest_service import safe_track
                safe_track(db, user.id, "ad_watched", 1)
            except Exception:
                pass

        result = open_free_pack(db, user)
        if not result.get("ok"):
            db.rollback()
            return result, 400

        db.commit()
        result["balance"] = {
            "coins": user.total_coins or 0,
            "gems": user.total_gems or 0,
            "roster_count": user.roster_count or 0,
        }
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_freepack_open failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/packs", methods=["POST"])
@csrf_exempt
def webapp_packs():
    """List buyable packs + the user's unopened inventory."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.pack_service import (
            list_packs, list_user_inventory, get_user_pack_purchases_today,
            count_main_pool, count_bonus_pool,
        )
        packs = list_packs(db, only_active=True)
        shop = []
        for p in packs:
            bought_today = get_user_pack_purchases_today(db, user.id, p.id)
            limit = p.daily_limit or 0
            shop.append({
                "id": p.id,
                "slot_number": p.slot_number,
                "name": p.name,
                "description": p.description or "",
                "emoji": p.emoji or "📦",
                "cost_coins": p.cost_coins or 0,
                "cost_gems": p.cost_gems or 0,
                "cost_quest_points": p.cost_quest_points or 0,
                "main_count": p.main_count or 1,
                "bonus_count": p.bonus_count or 0,
                "main_min_rating": p.main_min_rating,
                "main_max_rating": p.main_max_rating,
                "bonus_min_rating": p.bonus_min_rating,
                "bonus_max_rating": p.bonus_max_rating,
                "daily_limit": limit,
                "bought_today": bought_today,
                "sold_out_today": (limit > 0 and bought_today >= limit),
            })

        inv_rows = list_user_inventory(db, user.id)
        inventory = []
        for unopened, pack in inv_rows:
            inventory.append({
                "inventory_id": unopened.id,
                "pack_id": pack.id,
                "name": pack.name,
                "emoji": pack.emoji or "📦",
                "main_count": pack.main_count or 1,
                "bonus_count": pack.bonus_count or 0,
                "acquired_at": unopened.acquired_at.isoformat() if unopened.acquired_at else None,
            })

        return {
            "ok": True,
            "packs": shop,
            "inventory": inventory,
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_packs failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/packs/buy", methods=["POST"])
@csrf_exempt
def webapp_packs_buy():
    """Buy a pack → goes to inventory (does not auto-open).
    Body: {pack_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import Pack
        from services.pack_service import buy_pack, get_user_pack_purchases_today
        data = request.get_json(silent=True) or {}
        try:
            pack_id = int(data.get("pack_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        pack = db.query(Pack).filter(Pack.id == pack_id, Pack.is_active == True).first()
        if not pack:
            return {"ok": False, "error": "not_found", "message": "Pack not found."}, 404

        # Daily limit check
        if pack.daily_limit and pack.daily_limit > 0:
            bought = get_user_pack_purchases_today(db, user.id, pack.id)
            if bought >= pack.daily_limit:
                return {"ok": False, "error": "daily_limit",
                        "message": f"Daily limit reached for {pack.name} ({pack.daily_limit}/day)."}, 400

        result = buy_pack(db, user, pack)
        if not result.get("success"):
            db.rollback()
            return {"ok": False, "error": "buy_failed",
                    "message": result.get("message", "Could not buy pack.")}, 400

        db.commit()
        return {
            "ok": True,
            "inventory_id": result.get("inventory_id"),
            "cost_paid": result.get("cost_paid", 0),
            "currency": result.get("currency", "coins"),
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_packs_buy failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/packs/open", methods=["POST"])
@csrf_exempt
def webapp_packs_open():
    """Open an unopened pack from inventory → reveal players.
    Body: {inventory_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.pack_service import open_unopened_pack
        data = request.get_json(silent=True) or {}
        try:
            inventory_id = int(data.get("inventory_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400

        result = open_unopened_pack(db, user, inventory_id)
        if not result.get("success"):
            db.rollback()
            return {"ok": False, "error": "open_failed",
                    "message": result.get("message", "Could not open pack.")}, 400

        # Build player reveal list (main first, then bonus)
        try:
            from config import get_sell_value
        except Exception:
            get_sell_value = lambda r: 0

        players_out = []
        for player, slot_type in result.get("players", []):
            players_out.append({
                "id": player.id,
                "name": player.name,
                "rating": player.rating,
                "category": player.category,
                "country": player.country,
                "version": player.version or "Base",
                "slot": slot_type,  # 'main' or 'bonus'
                "sell_value": get_sell_value(player.rating),
            })

        # Players that couldn't fit (roster full)
        overflow = [{"id": p.id, "name": p.name, "rating": p.rating}
                    for p in result.get("players_to_claim", [])]

        db.commit()
        return {
            "ok": True,
            "pack_name": result["pack"].name if result.get("pack") else "Pack",
            "players": players_out,
            "overflow": overflow,
            "pity_triggered": result.get("pity_triggered", False),
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_packs_open failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/market", methods=["POST"])
@csrf_exempt
def webapp_market():
    """Return active market slots (player market only)."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import GlobalPlayerMarket, UserRoster
        from services.global_market import ensure_player_market_fresh
        try:
            ensure_player_market_fresh(db)
        except Exception:
            logger.exception("ensure_player_market_fresh failed (continuing)")

        slots = (db.query(GlobalPlayerMarket)
                 .filter(GlobalPlayerMarket.is_active == True)
                 .order_by(GlobalPlayerMarket.slot_index.asc()).all())
        # Pull all players in one query
        pids = [s.player_id for s in slots]
        players = {p.id: p for p in
                   db.query(Player).filter(Player.id.in_(pids)).all()} if pids else {}

        # Pull owned player ids
        owned = {pid for (pid,) in
                 db.query(UserRoster.player_id)
                   .filter(UserRoster.user_id == user.id).all()}

        results = []
        for s in slots:
            p = players.get(s.player_id)
            if not p: continue
            results.append({
                "slot_id": s.id,
                "slot_index": s.slot_index,
                "player_id": p.id,
                "name": p.name,
                "rating": p.rating,
                "category": p.category,
                "country": p.country,
                "version": p.version or "Base",
                "base_price": s.base_price,
                "final_price": s.final_price,
                "discount_pct": (int((1 - s.final_price / s.base_price) * 100)
                                 if s.base_price > 0 and s.final_price < s.base_price else 0),
                "quantity": s.quantity,
                "purchased_count": s.purchased_count,
                "sold_out": s.purchased_count >= s.quantity,
                "is_owned": p.id in owned,
            })
        return {"ok": True, "slots": results, "total": len(results),
                "balance": {"coins": user.total_coins or 0,
                            "roster_count": user.roster_count or 0,
                            "roster_max": 25}}
    except Exception as e:
        logger.exception("webapp_market failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/market/buy/<int:slot_id>", methods=["POST"])
@csrf_exempt
def webapp_market_buy(slot_id):
    """Buy from a market slot."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import GlobalPlayerMarket, UserRoster

        slot = db.query(GlobalPlayerMarket).filter(
            GlobalPlayerMarket.id == slot_id,
            GlobalPlayerMarket.is_active == True).first()
        if not slot:
            return {"ok": False, "error": "not_found",
                    "message": "Market slot not found."}, 404
        if slot.purchased_count >= slot.quantity:
            return {"ok": False, "error": "sold_out",
                    "message": "This item is sold out."}, 400

        player = db.query(Player).get(slot.player_id)
        if not player:
            return {"ok": False, "error": "player_missing"}, 404

        # Restrictions
        if getattr(player, "restricted_from_buypl", False):
            return {"ok": False, "error": "restricted",
                    "message": "This player isn't available for purchase."}, 400

        # Ownership: prevent double-owning the same version
        owned = (db.query(UserRoster)
                 .filter(UserRoster.user_id == user.id,
                         UserRoster.player_id == player.id).first())
        if owned:
            return {"ok": False, "error": "already_owned",
                    "message": "You already own this version."}, 400

        # Roster cap
        if (user.roster_count or 0) >= 25:
            return {"ok": False, "error": "roster_full",
                    "message": "Your roster is full (25/25)."}, 400

        # Balance
        if (user.total_coins or 0) < slot.final_price:
            return {"ok": False, "error": "insufficient_coins",
                    "message": f"Need {slot.final_price:,} 🪙 — you have {user.total_coins:,}."}, 400

        # Apply
        user.total_coins -= slot.final_price
        # Append at next free position (use roster_count+1 for predictability)
        max_pos = (db.query(UserRoster.order_position)
                   .filter(UserRoster.user_id == user.id)
                   .order_by(UserRoster.order_position.desc()).first())
        # If max position < 25, use max+1. Otherwise default to 99.
        if max_pos and max_pos[0] is not None and max_pos[0] < 25:
            next_pos = max_pos[0] + 1
        elif not max_pos:
            next_pos = 1
        else:
            next_pos = 99
        new_row = UserRoster(user_id=user.id, player_id=player.id,
                             order_position=next_pos,
                             acquired_date=datetime.utcnow())
        db.add(new_row)
        user.roster_count = (user.roster_count or 0) + 1
        slot.purchased_count += 1

        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "market_buy",
                         f"[MiniApp] Market: {player.name} ({player.rating})",
                         coins_change=-slot.final_price,
                         player_name=player.name, player_rating=player.rating)
        except Exception:
            pass
        try:
            from services.undo_service import record_buy
            record_buy(db, user.id, player.id, slot.final_price)
        except Exception:
            pass

        db.commit()
        return {
            "ok": True,
            "player_name": player.name,
            "rating": player.rating,
            "spent": slot.final_price,
            "balance": {
                "coins": user.total_coins or 0,
                "roster_count": user.roster_count or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_market_buy failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/xi", methods=["POST"])
@csrf_exempt
def webapp_xi():
    """Return the playing XI (positions 1-11) and bench (positions 12-25)."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        rows = (db.query(UserRoster, Player)
                .join(Player, UserRoster.player_id == Player.id)
                .filter(UserRoster.user_id == user.id)
                .order_by(UserRoster.order_position.asc()).all())
        xi = []
        bench = []
        for r, p in rows:
            entry = {
                "roster_id": r.id,
                "position": r.order_position,
                "player_id": p.id,
                "name": p.name,
                "rating": p.rating,
                "bat_rating": p.bat_rating or 0,
                "bowl_rating": p.bowl_rating or 0,
                "category": p.category,
                "country": p.country,
                "version": p.version or "Base",
                "is_captain": bool(user.captain_roster_id == r.id),
            }
            if r.order_position <= 11:
                xi.append(entry)
            else:
                bench.append(entry)
        return {"ok": True, "xi": xi, "bench": bench,
                "captain_roster_id": user.captain_roster_id,
                "roster_count": len(rows)}
    except Exception as e:
        logger.exception("webapp_xi failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/xi/autobuild", methods=["POST"])
@csrf_exempt
def webapp_xi_autobuild():
    """Pick the strongest valid Playing XI from the user's roster."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from handlers.lineup import _build_best_xi
        rows = (db.query(UserRoster, Player)
                .join(Player, UserRoster.player_id == Player.id)
                .filter(UserRoster.user_id == user.id)
                .order_by(UserRoster.order_position.asc()).all())
        if len(rows) < 11:
            return {"ok": False, "error": "not_enough_players",
                    "message": "You need at least 11 players to build an XI."}, 400
        xi, bench, build_error = _build_best_xi(rows)
        if build_error:
            return {"ok": False, "error": "invalid_roster",
                    "message": build_error}, 400
        for position, (entry, _) in enumerate(xi, start=1):
            entry.order_position = position
        for position, (entry, _) in enumerate(bench, start=12):
            entry.order_position = position
        db.commit()
        return {"ok": True, "message": "Your Playing XI is ready."}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_xi_autobuild failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/xi/reorder", methods=["POST"])
@csrf_exempt
def webapp_xi_reorder():
    """Reorder the playing XI based on a new ordered list of roster_ids.

    Request body: {ordered_roster_ids: [r1, r2, ..., r11]}
    Updates order_position 1..N for each. Bench positions are preserved.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        data = request.get_json(silent=True) or {}
        ids = data.get("ordered_roster_ids") or []
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "no_ids"}, 400
        try:
            ids = [int(x) for x in ids][:11]
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_ids"}, 400

        # Verify all belong to this user
        rows = (db.query(UserRoster)
                .filter(UserRoster.user_id == user.id,
                        UserRoster.id.in_(ids)).all())
        if len(rows) != len(ids):
            return {"ok": False, "error": "mismatch",
                    "message": "Some roster_ids not in your roster."}, 400

        by_id = {r.id: r for r in rows}
        for new_pos, rid in enumerate(ids, start=1):
            by_id[rid].order_position = new_pos

        db.commit()
        return {"ok": True, "reordered": len(ids)}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_xi_reorder failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/xi/swap_bench", methods=["POST"])
@csrf_exempt
def webapp_xi_swap_bench():
    """Swap a bench player into the XI.

    Request body: {bench_roster_id, xi_roster_id}
    The bench player takes the XI player's position; XI player goes to bench.
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import UserRoster
        data = request.get_json(silent=True) or {}
        bench_id = int(data.get("bench_roster_id") or 0)
        xi_id = int(data.get("xi_roster_id") or 0)
        if not bench_id or not xi_id:
            return {"ok": False, "error": "missing_ids"}, 400

        bench_row = (db.query(UserRoster)
                     .filter(UserRoster.id == bench_id,
                             UserRoster.user_id == user.id).first())
        xi_row = (db.query(UserRoster)
                  .filter(UserRoster.id == xi_id,
                          UserRoster.user_id == user.id).first())
        if not bench_row or not xi_row:
            return {"ok": False, "error": "not_found"}, 404
        if bench_row.order_position <= 11:
            return {"ok": False, "error": "not_bench"}, 400
        if xi_row.order_position > 11:
            return {"ok": False, "error": "not_xi"}, 400

        # Swap their positions
        bench_pos = bench_row.order_position
        xi_pos = xi_row.order_position
        bench_row.order_position = xi_pos
        xi_row.order_position = bench_pos
        db.commit()
        return {"ok": True}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_xi_swap_bench failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/invite", methods=["POST"])
@csrf_exempt
def webapp_invite():
    """Return the user's invite link, active competition info, and stats."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.referral_service import (
            get_active_competition, get_user_stats, get_invite_link,
            get_leaderboard, get_user_invitee_list,
            ensure_referral_code, get_branding, is_eligible_to_redeem,
        )

        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        link = get_invite_link(bot_username, tg_id) if bot_username else None
        comp = get_active_competition(db)
        stats = get_user_stats(db, user.id, comp.id if comp else None)

        # Ensure user has a referral code
        code = ensure_referral_code(db, user)
        # Eligibility to redeem (only new users)
        can_redeem, redeem_blocked_reason = is_eligible_to_redeem(db, user)
        db.commit()

        result = {
            "ok": True,
            "bot_configured": bool(bot_username),
            "invite_link": link,
            "user_referral_code": code,
            "can_redeem": can_redeem,
            "redeem_blocked_reason": redeem_blocked_reason,
            # Legacy field kept for backward compat
            "has_redeemed_code": (not can_redeem and redeem_blocked_reason == "already_redeemed"),
            "stats": stats,
            "active_competition": None,
            "leaderboard": [],
            "my_invitees": [],
            "branding": get_branding(db),
        }
        if comp:
            result["active_competition"] = {
                "id": comp.id,
                "name": comp.name,
                "start_date": comp.start_date.isoformat() if comp.start_date else None,
                "end_date": comp.end_date.isoformat() if comp.end_date else None,
                "prize_top1": comp.prize_top1,
                "prize_top2": comp.prize_top2,
                "prize_top3": comp.prize_top3,
                "prize_per_invite": comp.prize_per_invite,
                "prize_per_invite_gems": comp.prize_per_invite_gems,
                "winners_announced": comp.winners_announced_at is not None,
            }
            result["leaderboard"] = get_leaderboard(db, comp.id, limit=10)
            result["my_invitees"] = [
                {**iv,
                 "created_at": iv["created_at"].isoformat() if iv["created_at"] else None,
                 "completed_at": iv["completed_at"].isoformat() if iv["completed_at"] else None,
                 "reward_paid_at": iv["reward_paid_at"].isoformat() if iv["reward_paid_at"] else None}
                for iv in get_user_invitee_list(db, user.id, comp.id, limit=50)
            ]

        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_invite failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/redeem", methods=["POST"])
@csrf_exempt
def webapp_redeem():
    """Mini App referral code redemption."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.referral_service import redeem_code
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        if not code:
            return {"ok": False, "error": "missing_code",
                    "message": "Enter a code."}, 400
        # Validate format quickly
        import re
        if not re.match(r"^[A-HJ-NP-Z2-9]{6}$", code):
            return {"ok": False, "error": "invalid_format",
                    "message": "Codes are 6 characters (no spaces or special chars)."}, 400

        result = redeem_code(db, code, user.id)
        if result["ok"]:
            db.commit()
        else:
            db.rollback()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_redeem failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quests", methods=["POST"])
@csrf_exempt
def webapp_quests():
    """Return the user's active quests grouped by type (daily/weekly/monthly).

    For each quest: progress, target, percent, completed, claimed flags.
    Body: {type: 'daily'|'weekly'|'monthly'|'all'} default 'all'
    """
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quest_service import get_user_quests, consume_pending_auto_claims
        data = request.get_json(silent=True) or {}
        which = (data.get("type") or "all").lower()
        types = [which] if which in ("daily", "weekly", "monthly") else ["daily", "weekly", "monthly"]

        result = {"ok": True, "groups": {}, "auto_claimed_summary": []}

        for qtype in types:
            # Pick up any auto-claims from previous period (best-effort)
            try:
                ac = consume_pending_auto_claims(db, user.id, qtype)
                if ac:
                    result["auto_claimed_summary"].extend([{
                        "quest_type": qtype,
                        "title": entry.get("name", ""),
                        "coins": entry.get("coins", 0),
                        "gems": entry.get("gems", 0),
                        "points": entry.get("points", 0),
                    } for entry in ac])
            except Exception:
                logger.exception("auto-claim consume failed")

            items = get_user_quests(db, user.id, qtype)
            quests_out = []
            for it in items:
                q = it["quest"]
                quests_out.append({
                    "quest_id": q.id,
                    "uqp_id": it["uqp_id"],
                    "title": q.name,  # Quest model uses 'name', not 'title'
                    "description": q.description,
                    "emoji": getattr(q, "emoji", "🎯") or "🎯",
                    "event_key": getattr(q, "event_key", "") or "",
                    "progress": it["progress"],
                    "target": it["target"],
                    "percent": it["percent"],
                    "completed": it["completed"],
                    "claimed": it["claimed"],
                    "reward_coins": q.reward_coins or 0,
                    "reward_gems": q.reward_gems or 0,
                    "reward_points": q.reward_points or 0,
                })
            result["groups"][qtype] = quests_out

        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quests failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quests/claim", methods=["POST"])
@csrf_exempt
def webapp_quest_claim():
    """Claim a single quest reward. Body: {quest_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quest_service import claim_quest_reward
        data = request.get_json(silent=True) or {}
        try:
            qid = int(data.get("quest_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        if not qid:
            return {"ok": False, "error": "missing_id"}, 400

        ok, message, reward = claim_quest_reward(db, user.id, qid)
        if not ok:
            return {"ok": False, "error": "claim_failed", "message": message}, 400

        db.commit()
        return {
            "ok": True,
            "message": message,
            "reward": reward,
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quest_claim failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/quests/claim_all", methods=["POST"])
@csrf_exempt
def webapp_quest_claim_all():
    """Claim ALL completed-but-unclaimed quests of a given type.
    Body: {type: 'daily'|'weekly'|'monthly'}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.quest_service import claim_all_completed
        data = request.get_json(silent=True) or {}
        qtype = (data.get("type") or "").lower()
        if qtype not in ("daily", "weekly", "monthly"):
            return {"ok": False, "error": "bad_type"}, 400

        count, total = claim_all_completed(db, user.id, qtype)
        db.commit()
        return {
            "ok": True,
            "count_claimed": count,
            "total_reward": total,
            "balance": {
                "coins": user.total_coins or 0,
                "gems": user.total_gems or 0,
                "quest_points": user.quest_points or 0,
            },
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_quest_claim_all failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/login-streak", methods=["POST"])
@csrf_exempt
def webapp_login_streak():
    """Return the user's login streak ladder status."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.login_streak_service import touch_login_streak, get_login_status
        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush()
        status = touch_login_streak(db, stats)
        db.commit()
        return {"ok": True, **status}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_login_streak failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/login-streak/claim", methods=["POST"])
@csrf_exempt
def webapp_login_streak_claim():
    """Claim today's login ladder reward."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.login_streak_service import claim_login_reward, get_login_status
        stats = db.query(UserStats).filter(UserStats.user_id == user.id).first()
        if not stats:
            stats = UserStats(user_id=user.id)
            db.add(stats); db.flush()
        result = claim_login_reward(db, user, stats)
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        result["status"] = get_login_status(db, stats)
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_login_streak_claim failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/achievements", methods=["POST"])
@csrf_exempt
def webapp_achievements():
    """Return all achievements with unlock status + progress, grouped by category."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import achievement_service
        # Opportunistically check for newly-met achievements (auto-awards)
        try:
            achievement_service.safe_check_and_award(db, user.id)
            db.commit()
        except Exception:
            db.rollback()

        items = achievement_service.get_user_achievements_with_progress(db, user.id)
        # Group by category
        groups = {}
        unlocked_count = 0
        for a in items:
            cat = a.get("category", "Other")
            groups.setdefault(cat, [])
            if a["unlocked"]:
                unlocked_count += 1
            groups[cat].append({
                "key": a["key"],
                "emoji": a.get("emoji", "🏆"),
                "name": a.get("name", ""),
                "desc": a.get("desc", ""),
                "category": cat,
                "coins": a.get("coins", 0),
                "gems": a.get("gems", 0),
                "unlocked": a["unlocked"],
                "unlocked_at": a["unlocked_at"].isoformat() if a.get("unlocked_at") else None,
            })
        return {
            "ok": True,
            "groups": groups,
            "total": len(items),
            "unlocked": unlocked_count,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_achievements failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/season", methods=["POST"])
@csrf_exempt
def webapp_season():
    """Return the live season leaderboard + the user's standing + prizes."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.season_service import get_season_leaderboard, get_season_info
        info = get_season_info(db, user)
        season, board = get_season_leaderboard(db, limit=25)
        db.commit()
        # Mark which row is me
        for row in board:
            row["is_me"] = (row["user_id"] == user.id)
        return {
            "ok": True,
            "season": info,
            "leaderboard": board,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_season failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/upgrade/options", methods=["POST"])
@csrf_exempt
def webapp_upgrade_options():
    """Return the higher versions a roster entry can be upgraded to.
    Body: {roster_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.upgrade_service import get_upgrade_options
        data = request.get_json(silent=True) or {}
        try:
            roster_id = int(data.get("roster_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        result = get_upgrade_options(db, user.id, roster_id)
        if not result.get("ok"):
            return result, 400
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_upgrade_options failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/upgrade/apply", methods=["POST"])
@csrf_exempt
def webapp_upgrade_apply():
    """Upgrade a roster entry to a higher version.
    Body: {roster_id, target_player_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.upgrade_service import upgrade_player
        data = request.get_json(silent=True) or {}
        try:
            roster_id = int(data.get("roster_id") or 0)
            target_player_id = int(data.get("target_player_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        if not roster_id or not target_player_id:
            return {"ok": False, "error": "missing_params"}, 400
        result = upgrade_player(db, user, roster_id, target_player_id)
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_upgrade_apply failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/player-detail", methods=["POST"])
@csrf_exempt
def webapp_roster_player_detail():
    """Full detail for a roster entry: player info, aggregate stats, last-10
    matches, equipped traits, available inventory traits, and whether an
    upgrade (higher version) is available.
    Body: {roster_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import (UserRoster, Player, PlayerGameStats, PlayerMatchStats,
                             Match)
        from services.trait_service import get_player_traits
        from services.version_service import get_base_id, get_all_versions
        from config import (TRAIT_MAX_PER_PLAYER, TRAIT_REPLACE_COST,
                            TRAIT_UPGRADE_COSTS)

        data = request.get_json(silent=True) or {}
        try:
            roster_id = int(data.get("roster_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400

        entry = (db.query(UserRoster)
                 .filter(UserRoster.id == roster_id,
                         UserRoster.user_id == user.id).first())
        if not entry:
            return {"ok": False, "error": "not_found",
                    "message": "Player not in your roster."}, 404
        player = db.query(Player).get(entry.player_id)
        if not player:
            return {"ok": False, "error": "player_missing"}, 404

        try:
            from config import get_sell_value
            sell_value = get_sell_value(player.rating)
        except Exception:
            sell_value = 0

        # ── Aggregate stats (this user with this player) ──
        gs = (db.query(PlayerGameStats)
              .filter(PlayerGameStats.user_id == user.id,
                      PlayerGameStats.player_id == player.id).first())
        if gs:
            stats = {
                "has_stats": (gs.bat_inns or 0) + (gs.bowl_inns or 0) > 0,
                "potm": gs.potm or 0,
                "bat_inns": gs.bat_inns or 0,
                "runs": gs.runs or 0,
                "bat_avg": gs.bat_avg if hasattr(gs, "bat_avg") else 0,
                "fifties": gs.fifties or 0,
                "hundreds": gs.hundreds or 0,
                "fours": gs.fours or 0,
                "sixes": gs.sixes or 0,
                "balls_faced": gs.balls_faced or 0,
                "highest_score": gs.highest_score or 0,
                "highest_not_out": bool(gs.highest_score_not_out),
                "bowl_inns": gs.bowl_inns or 0,
                "wickets_taken": gs.wickets_taken or 0,
                "runs_conceded": gs.runs_conceded or 0,
                "overs_bowled": gs.overs_bowled or 0,
                "three_fers": gs.three_fers or 0,
                "five_fers": gs.five_fers or 0,
                "best_bowl": (f"{gs.best_bowl_wickets}/{gs.best_bowl_runs}"
                              if (gs.best_bowl_wickets or 0) > 0 else "—"),
                "strike_rate": (round(gs.runs * 100 / gs.balls_faced, 1)
                                if gs.balls_faced else 0),
            }
        else:
            stats = {"has_stats": False}

        # ── Last 10 matches (per-match snapshots) ──
        last10_rows = (db.query(PlayerMatchStats)
                       .filter(PlayerMatchStats.user_id == user.id,
                               PlayerMatchStats.player_id == player.id)
                       .order_by(PlayerMatchStats.created_at.desc())
                       .limit(10).all())
        last10 = []
        for m in last10_rows:
            bowl = (f"{m.bowl_wickets}/{m.bowl_runs}"
                    if (m.bowl_balls or 0) > 0 else None)
            bat = None
            if (m.bat_balls or 0) > 0 or (m.bat_runs or 0) > 0:
                bat = f"{m.bat_runs}{'' if m.bat_out else '*'} ({m.bat_balls})"
            last10.append({
                "bat": bat, "bowl": bowl,
                "bat_runs": m.bat_runs or 0, "bat_balls": m.bat_balls or 0,
                "bat_out": bool(m.bat_out),
                "fours": m.bat_fours or 0, "sixes": m.bat_sixes or 0,
                "bowl_wickets": m.bowl_wickets or 0,
                "bowl_runs": m.bowl_runs or 0, "bowl_balls": m.bowl_balls or 0,
                "when": m.created_at.isoformat() if m.created_at else None,
            })

        # ── Traits equipped on this roster entry ──
        equipped = get_player_traits(db, roster_id)
        traits = []
        for pt, t in equipped:
            up_cost = TRAIT_UPGRADE_COSTS.get(pt.level) if pt.level < 5 else None
            traits.append({
                "player_trait_id": pt.id,
                "trait_id": t.id,
                "name": t.name, "emoji": t.emoji, "category": t.category,
                "description": t.description,
                "level": pt.level, "max_level": 5,
                "upgrade_cost": up_cost,
                "can_upgrade": (pt.level < 5 and up_cost is not None),
            })

        # ── Inventory traits available to apply ──
        from models import TraitInventory, Trait
        inv_rows = (db.query(TraitInventory, Trait)
                    .join(Trait, TraitInventory.trait_id == Trait.id)
                    .filter(TraitInventory.user_id == user.id).all())
        equipped_trait_ids = {t.id for _pt, t in equipped}
        inventory = []
        for inv, t in inv_rows:
            inventory.append({
                "inventory_id": inv.id,
                "trait_id": t.id,
                "name": t.name, "emoji": t.emoji, "category": t.category,
                "description": t.description, "level": inv.level,
                "already_on_player": (t.id in equipped_trait_ids),
            })

        slots_full = len(equipped) >= TRAIT_MAX_PER_PLAYER

        # ── Upgrade availability (higher versions of same base player) ──
        base_id = get_base_id(player.id, db)
        versions = get_all_versions(db, base_id)
        higher = [v for v in versions if v.rating > player.rating]
        upgrade_available = len(higher) > 0

        return {
            "ok": True,
            "player": {
                "roster_id": entry.id,
                "id": player.id,
                "name": player.name,
                "rating": player.rating,
                "bat_rating": player.bat_rating,
                "bowl_rating": player.bowl_rating,
                "category": player.category,
                "country": player.country,
                "version": player.version or "Base",
                "bat_hand": player.bat_hand,
                "bowl_hand": player.bowl_hand,
                "bowl_style": player.bowl_style,
                "sell_value": sell_value,
                "is_captain": False,
            },
            "stats": stats,
            "last10": last10,
            "traits": traits,
            "inventory": inventory,
            "trait_slots": {"used": len(equipped), "max": TRAIT_MAX_PER_PLAYER,
                            "full": slots_full},
            "replace_cost": TRAIT_REPLACE_COST,
            "upgrade_available": upgrade_available,
            "gems": user.total_gems or 0,
            "coins": user.total_coins or 0,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_player_detail failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/trait/apply", methods=["POST"])
@csrf_exempt
def webapp_trait_apply():
    """Apply an inventory trait to a roster entry. Body: {roster_id, inventory_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.trait_service import apply_trait_to_player
        data = request.get_json(silent=True) or {}
        roster_id = int(data.get("roster_id") or 0)
        inventory_id = int(data.get("inventory_id") or 0)
        ok, msg = apply_trait_to_player(db, user, inventory_id, roster_id)
        if not ok:
            db.rollback()
            return {"ok": False, "message": msg}, 400
        db.commit()
        return {"ok": True, "message": msg}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_trait_apply failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/trait/remove", methods=["POST"])
@csrf_exempt
def webapp_trait_remove():
    """Remove (unequip) a trait from a player. Returns it to inventory.
    Body: {player_trait_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from models import PlayerTrait, TraitInventory
        data = request.get_json(silent=True) or {}
        ptid = int(data.get("player_trait_id") or 0)
        pt = (db.query(PlayerTrait)
              .filter(PlayerTrait.id == ptid,
                      PlayerTrait.user_id == user.id).first())
        if not pt:
            return {"ok": False, "message": "Trait not found."}, 404
        # Return to inventory at same level
        db.add(TraitInventory(user_id=user.id, trait_id=pt.trait_id, level=pt.level))
        db.delete(pt)
        db.commit()
        return {"ok": True, "message": "Trait removed and returned to inventory."}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_trait_remove failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/trait/upgrade", methods=["POST"])
@csrf_exempt
def webapp_trait_upgrade():
    """Upgrade an equipped trait's level (costs gems). Body: {player_trait_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.trait_service import upgrade_player_trait
        data = request.get_json(silent=True) or {}
        ptid = int(data.get("player_trait_id") or 0)
        ok, msg = upgrade_player_trait(db, user, ptid)
        if not ok:
            db.rollback()
            return {"ok": False, "message": msg}, 400
        db.commit()
        return {"ok": True, "message": msg, "gems": user.total_gems or 0}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_trait_upgrade failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/clubs", methods=["POST"])
@csrf_exempt
def webapp_clubs():
    """Return my club (if any) + club leaderboard + open clubs list."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import club_service
        my = club_service.get_my_club(db, user)
        db.commit()
        return {
            "ok": True,
            "my_club": my,
            "leaderboard": club_service.get_club_leaderboard(db, limit=25),
            "open_clubs": club_service.list_open_clubs(db, limit=30) if not my else [],
            "create_cost": club_service.CREATE_COST_COINS,
            "coins": user.total_coins or 0,
        }
    except Exception as e:
        db.rollback()
        logger.exception("webapp_clubs failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/clubs/create", methods=["POST"])
@csrf_exempt
def webapp_clubs_create():
    """Create a club. Body: {name, description?, emoji?}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import club_service
        data = request.get_json(silent=True) or {}
        result = club_service.create_club(
            db, user,
            name=data.get("name", ""),
            description=data.get("description", ""),
            emoji=data.get("emoji", "🛡️"))
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_clubs_create failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/clubs/join", methods=["POST"])
@csrf_exempt
def webapp_clubs_join():
    """Join a club. Body: {code} or {club_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import club_service
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip() or None
        club_id = data.get("club_id")
        try:
            club_id = int(club_id) if club_id else None
        except (ValueError, TypeError):
            club_id = None
        result = club_service.join_club(db, user, code=code, club_id=club_id)
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_clubs_join failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/clubs/leave", methods=["POST"])
@csrf_exempt
def webapp_clubs_leave():
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import club_service
        result = club_service.leave_club(db, user)
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_clubs_leave failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/clubs/kick", methods=["POST"])
@csrf_exempt
def webapp_clubs_kick():
    """Founder removes a member. Body: {user_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import club_service
        data = request.get_json(silent=True) or {}
        try:
            target = int(data.get("user_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        result = club_service.kick_member(db, user, target)
        if not result.get("ok"):
            db.rollback()
            return result, 400
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.exception("webapp_clubs_kick failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


# ══════════════════ Mini App live match (Phase 1) ════════════════════

@app.route("/api/webapp/match/state", methods=["POST"])
@csrf_exempt
def webapp_match_state():
    """Role-aware polling snapshot of a live match. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import build_snapshot
        data = request.get_json(silent=True) or {}
        try:
            match_id = int(data.get("match_id") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_id"}, 400
        snap = build_snapshot(db, match_id, user.id)
        if not snap:
            return {"ok": False, "error": "no_match",
                    "message": "No live match found."}, 404
        try:
            if snap.get("status") == "completed" or snap.get("next_action") == "COMPLETED":
                _finalize_and_broadcast_if_terminal(db, match_id)
        except Exception:
            logger.exception("polling completion self-heal failed")
        return snap
    except Exception as e:
        logger.exception("webapp_match_state failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()




# Public REST aliases for the premium live-match board. These endpoints keep the
# documented `/api/match/*` contract while reusing the authenticated Mini App
# service layer above. `userId` is the internal user id; callers may pass
# `matchId`/`match_id`, otherwise the latest playing match for that user is used.
def _match_rest_user_and_match(db, user_id, match_id=None):
    from models import User, Match
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return None, None, ({"ok": False, "error": "bad_user_id"}, 400)
    user = db.query(User).get(uid)
    if not user:
        # The Crickidex Arena frontend sends the raw Telegram id as userId;
        # fall back to a telegram_id lookup so those links resolve too.
        user = db.query(User).filter(User.telegram_id == uid).first()
    if not user:
        return None, None, ({"ok": False, "error": "no_user"}, 404)
    if match_id:
        try:
            mid = int(match_id)
        except (TypeError, ValueError):
            return user, None, ({"ok": False, "error": "bad_match_id"}, 400)
        match = db.query(Match).get(mid)
    else:
        match = (db.query(Match)
                 .filter(((Match.user1_id == user.id) | (Match.user2_id == user.id)),
                         Match.status == "playing")
                 .order_by(Match.id.desc()).first())
    if not match:
        return user, None, ({"ok": False, "error": "no_match"}, 404)
    if user.id not in (match.user1_id, match.user2_id):
        return user, match, ({"ok": False, "error": "not_participant"}, 403)
    return user, match, None


def _match_rest_full_state(db, match_id, user_id):
    import copy as _copy
    from services.match_webapp_service import build_snapshot, role_for
    from services.match_webapp_access import get_state, get_next_action, get_ball_seq
    state = get_state(match_id)
    if not state:
        return None
    participant_roles = {
        str(state.get("bat_team_id")): "batsman",
        str(state.get("bowl_team_id")): "bowler",
    }
    return {
        "ok": True,
        "match_id": match_id,
        "user_id": user_id,
        "role": role_for(state, user_id),
        "next_action": get_next_action(match_id),
        "ball_seq": get_ball_seq(match_id),
        "roles": participant_roles,
        "snapshot": build_snapshot(db, match_id, user_id),
        "state": _copy.deepcopy(state),
    }


@app.route("/api/match/state", methods=["GET"])
@csrf_exempt
def match_rest_state():
    """GET /api/match/state?userId=X[&matchId=Y]
    Returns the full serialized live match state plus a role-aware snapshot.
    """
    db = get_session()
    try:
        user, match, err = _match_rest_user_and_match(
            db, request.args.get("userId") or request.args.get("user_id"),
            request.args.get("matchId") or request.args.get("match_id"))
        if err:
            return err
        result = _match_rest_full_state(db, match.id, user.id)
        if not result:
            return {"ok": False, "error": "no_match"}, 404
        return result
    except Exception as e:
        logger.exception("match_rest_state failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/match/select-players", methods=["POST"])
@csrf_exempt
def match_rest_select_players():
    """POST /api/match/select-players
    Body: {userId, matchId?, striker_rid, non_striker_rid, bowler_rid}.
    Batting and bowling sides can submit independently; duplicate openers are
    rejected by the service layer and by the premium picker UI.
    """
    db = get_session()
    try:
        data = request.get_json(silent=True) or {}
        user, match, err = _match_rest_user_and_match(
            db, data.get("userId") or data.get("user_id"),
            data.get("matchId") or data.get("match_id"))
        if err:
            return err
        from services.match_webapp_service import (select_openers, select_bowler,
                                                    select_players, role_for)
        from services.match_webapp_access import get_state
        state = get_state(match.id)
        if not state:
            return {"ok": False, "error": "no_match"}, 404
        role = role_for(state, user.id)
        messages = []
        started = False

        # Crickidex Arena frontend submits XI *indices* (strikerIdx /
        # nonStrikerIdx / bowlerIdx). Detect that shape and route through the
        # index-based service; otherwise fall back to the roster-id contract.
        has_idx = any(data.get(k) is not None
                      for k in ("strikerIdx", "nonStrikerIdx", "bowlerIdx"))
        if has_idx:
            def _i(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
            ok, st, msg = select_players(
                match.id, user.id,
                striker_idx=_i(data.get("strikerIdx")),
                non_striker_idx=_i(data.get("nonStrikerIdx")),
                bowler_idx=_i(data.get("bowlerIdx")))
            if not ok:
                return {"ok": False, "error": msg, "message": msg}, 400
            if st:
                _broadcast_wpm_start_player_cards(match.id)
                try:
                    from services.match_webapp_service import auto_play_bot_turns, get_state_is_vsbot
                    if get_state_is_vsbot(match.id):
                        auto_play_bot_turns(db, match.id)
                except Exception:
                    logger.exception("auto-play after select-players failed")
            return {"ok": True, "success": True, "started": st, "message": msg}

        if role == "batsman":
            striker = int(data.get("striker_rid") or data.get("striker") or 0)
            non = int(data.get("non_striker_rid") or data.get("nonStriker") or 0)
            ok, st, msg = select_openers(match.id, user.id, striker, non)
            if not ok:
                return {"ok": False, "error": msg, "message": msg}, 400
            started = started or st; messages.append(msg)
        elif role == "bowler":
            rid = int(data.get("bowler_rid") or data.get("bowler") or 0)
            ok, st, msg = select_bowler(match.id, user.id, rid)
            if not ok:
                return {"ok": False, "error": msg, "message": msg}, 400
            started = started or st; messages.append(msg)
        else:
            return {"ok": False, "error": "spectator", "message": "Spectators cannot select players."}, 403
        if started:
            _broadcast_wpm_start_player_cards(match.id)
        return {"ok": True, "started": started, "message": " ".join(messages),
                "match": _match_rest_full_state(db, match.id, user.id)}
    except Exception as e:
        logger.exception("match_rest_select_players failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/match/action", methods=["POST"])
@csrf_exempt
def match_rest_action():
    """POST /api/match/action
    Body for bowler: {userId, matchId?, delivery_type/variation, length?, speed?}
    Body for batter: {userId, matchId?, shot_choice/shot_index}.
    """
    db = get_session()
    try:
        data = request.get_json(silent=True) or {}
        user, match, err = _match_rest_user_and_match(
            db, data.get("userId") or data.get("user_id"),
            data.get("matchId") or data.get("match_id"))
        if err:
            return err
        from services.match_webapp_service import set_delivery, play_shot, role_for
        from services.match_webapp_access import get_state, get_next_action, save_state
        state = get_state(match.id)
        if not state:
            return {"ok": False, "error": "no_match"}, 404
        if state.get("played_via") == "wsp":
            return {"ok": False, "error": "auto_simulated",
                    "message": "This match is auto-simulated — no manual actions allowed."}, 400
        role = role_for(state, user.id)

        def _should_refresh_live_scorecard(res, action_type):
            """Keep chat updates lightweight while preserving key moments.

            The Mini App already polls the full ball-by-ball state, so the
            Telegram chat card only needs refreshing for visible highlights:
            boundaries, wickets, over/innings transitions, and the result.
            """
            if action_type != "shot" or not isinstance(res, dict):
                return False
            if res.get("match_over") or res.get("innings_break") or res.get("eoo"):
                return True
            if res.get("need_new_bat") or res.get("type") == "wicket":
                return True
            try:
                return int(res.get("runs") or 0) >= 4
            except (TypeError, ValueError):
                return False

        def _finalize_and_respond(res, action_type=None):
            """Shared post-action finalize + throttled live-score refresh."""
            try:
                # vs-bot (Mini App): after the human's action, let the AI play its
                # consecutive turns so the bot side never stalls. This is the
                # envelope path the Mini App uses; the legacy /api/webapp/* path
                # already does this.
                try:
                    from services.match_webapp_service import (
                        get_state_is_vsbot, auto_play_bot_turns)
                    if get_state_is_vsbot(match.id):
                        auto_play_bot_turns(db, match.id)
                except Exception:
                    logger.exception("auto_play_bot_turns failed (non-fatal)")
                from services.match_state_store import A_COMPLETED as _DONE
                if get_next_action(match.id) == _DONE:
                    from services.match_webapp_service import finalize_webapp_match
                    fin = finalize_webapp_match(db, match.id)
                    _broadcast_match_result(match.id, (fin or {}).get("result") or {})
                else:
                    from services.match_state_store import A_PICK_DELIVERY as _DELIVERY
                    if (action_type == "shot" and isinstance(res, dict)
                            and res.get("need_new_bat")
                            and get_next_action(match.id) == _DELIVERY):
                        _broadcast_wpm_current_batter_card(match.id)
                    if _should_refresh_live_scorecard(res, action_type):
                        _broadcast_match_scorecard(match.id)
            except Exception:
                logger.exception("match_rest_action broadcast/finalize failed")
            # Return the updated Arena state with the action response so the
            # Mini App can show the outcome immediately instead of waiting for
            # its next polling request.
            match_state = None
            try:
                from services.crickidex_arena import serialize_match_state
                match_state = serialize_match_state(db, match, user)
            except Exception:
                logger.exception("could not serialize immediate post-action state")
            return {"ok": True, "success": True, "result": res,
                    "matchState": match_state}

        # ── Crickidex Arena envelope: {type, action:{...}} ──────────────
        # The frontend sends every gameplay action as {type, action}. Dispatch
        # those here (including the two post-event selections the legacy body
        # shape below doesn't cover).
        env_type = data.get("type")
        if env_type and isinstance(data.get("action"), dict):
            act = data.get("action")
            from services.match_webapp_service import (
                set_delivery_action, set_shot_action,
                select_wicket_batsman, select_new_bowler)
            from services.crickidex_arena import (
                xi_index_to_batting_order_index, xi_index_to_bowler_rid)
            if role not in ("batsman", "bowler"):
                return {"ok": False, "error": "Spectators cannot act."}, 403
            if env_type == "delivery":
                ok, res, _info = set_delivery_action(
                    match.id, user.id, (act.get("delivery") or "").strip(),
                    act.get("speed"))
            elif env_type == "shot":
                ok, res, _info = set_shot_action(match.id, user.id, act.get("shot"))
            elif env_type == "wicket_batsman":
                boi = xi_index_to_batting_order_index(state, act.get("index"))
                if boi is None:
                    return {"ok": False, "error": "Invalid batsman selection."}, 400
                ok, res, _info = select_wicket_batsman(match.id, user.id, boi)
            elif env_type == "over_bowler":
                rid = xi_index_to_bowler_rid(state, act.get("index"))
                if rid is None:
                    return {"ok": False, "error": "Invalid bowler selection."}, 400
                ok, res = select_new_bowler(match.id, user.id, rid)
            else:
                return {"ok": False, "error": f"Unknown action type '{env_type}'."}, 400
            if not ok:
                return {"ok": False, "error": res, "message": res}, 400
            if env_type == "wicket_batsman":
                _broadcast_wpm_current_batter_card(match.id)
            elif env_type == "over_bowler":
                _broadcast_wpm_current_bowler_card(match.id)
            return _finalize_and_respond(res, env_type)

        if role == "bowler":
            variation = (data.get("delivery_type") or data.get("variation") or data.get("delivery") or "").strip()
            length = (data.get("length") or "").strip() or None
            ok, res = set_delivery(match.id, user.id, variation, length)
            if ok and data.get("speed") is not None:
                try:
                    state = get_state(match.id) or state
                    state["last_speed"] = int(data.get("speed"))
                    save_state(match.id, state, next_action=get_next_action(match.id))
                except (TypeError, ValueError):
                    pass
        elif role == "batsman":
            if data.get("shot_index") is not None:
                shot_index = int(data.get("shot_index"))
            else:
                from services.bowling_service import AVAILABLE_SHOTS
                shot = (data.get("shot_choice") or data.get("shot") or "").strip()
                shot_index = AVAILABLE_SHOTS.index(shot) if shot in AVAILABLE_SHOTS else int(data.get("shot") or 0)
            ok, res = play_shot(match.id, user.id, shot_index)
        else:
            return {"ok": False, "error": "spectator", "message": "Spectators cannot act."}, 403
        if not ok:
            return {"ok": False, "message": res}, 400
        try:
            from services.match_state_store import A_COMPLETED as _DONE
            if get_next_action(match.id) == _DONE:
                from services.match_webapp_service import finalize_webapp_match
                fin = finalize_webapp_match(db, match.id)
                _broadcast_match_result(match.id, (fin or {}).get("result") or {})
            else:
                _broadcast_match_scorecard(match.id)
        except Exception:
            logger.exception("match_rest_action broadcast/finalize failed")
        return {"ok": True, "result": res,
                "match": _match_rest_full_state(db, match.id, user.id)}
    except Exception as e:
        logger.exception("match_rest_action failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/match/autoplay", methods=["POST"])
@csrf_exempt
def match_rest_autoplay():
    """POST /api/match/autoplay — hand the caller's side to the bot AI.

    Body: {userId, matchId}. Plays ALL of the requesting user's currently
    pending turns (and, in a vs-bot match, the bot's reply) with the same
    difficulty/phase-aware engine /vsbot uses. Powers the Mini App "autoplay"
    toggle. Returns the updated serialized match state so the client repaints
    immediately. Each call advances one human burst + one bot burst; the client
    keeps calling on its poll loop until the match completes.
    """
    db = get_session()
    try:
        data = request.get_json(silent=True) or {}
        user, match, err = _match_rest_user_and_match(
            db, data.get("userId") or data.get("user_id"),
            data.get("matchId") or data.get("match_id"))
        if err:
            return err
        from services.match_webapp_service import (
            auto_play_user_turns, auto_play_bot_turns, get_state_is_vsbot)
        from services.match_webapp_access import get_state, get_next_action
        state = get_state(match.id)
        if not state:
            return {"ok": False, "error": "no_match"}, 404
        # Spectators can't be autoplayed.
        from services.match_webapp_service import role_for
        if role_for(state, user.id) not in ("batsman", "bowler"):
            return {"ok": False, "error": "spectator",
                    "message": "Spectators cannot autoplay."}, 403

        # Diagnostic: Autoplay hands the caller's OWN side to the AI. With the
        # client toggle defaulting OFF per match, this should only fire when the
        # user has explicitly turned Autoplay on. Logged so a runtime test can
        # confirm the endpoint isn't being hit with the toggle off.
        logger.info("match_rest_autoplay tick: match=%s user=%s na=%s",
                    match.id, user.id, get_next_action(match.id))
        user_steps = auto_play_user_turns(db, match.id, user.id)
        bot_steps = []
        if get_state_is_vsbot(match.id):
            bot_steps = auto_play_bot_turns(db, match.id)

        try:
            from services.match_state_store import A_COMPLETED as _DONE
            if get_next_action(match.id) == _DONE:
                from services.match_webapp_service import finalize_webapp_match
                fin = finalize_webapp_match(db, match.id)
                _broadcast_match_result(match.id, (fin or {}).get("result") or {})
            else:
                _broadcast_match_scorecard(match.id)
        except Exception:
            logger.exception("match_rest_autoplay broadcast/finalize failed")

        match_state = None
        try:
            from services.crickidex_arena import serialize_match_state
            match_state = serialize_match_state(db, match, user)
        except Exception:
            logger.exception("could not serialize post-autoplay state")
        return {"ok": True, "matchState": match_state, "steps": user_steps + bot_steps}
    except Exception as e:
        logger.exception("match_rest_autoplay failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


# ── Crickidex Arena Mini App (the UnderCover-style live-match frontend) ──
# Served from static/cricket/. The "Play Match" button (services.match_broadcast)
# opens /cricket?match_id=<id>&chat_id=<chat>; the page reads the Telegram user
# from initData and polls GET /api/match.
_CRICKET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "cricket")


@app.route("/cricket")
def cricket_arena_index():
    from flask import send_from_directory
    resp = send_from_directory(_CRICKET_DIR, "index.html")
    # Never cache the entry HTML: Telegram's in-app WebView caches aggressively,
    # which otherwise pins an old index.html (and its old style.css/app.js),
    # making redeploys invisible to users. The HTML is tiny; refetch every time.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/cricket/<path:filename>")
def cricket_arena_asset(filename):
    from flask import send_from_directory
    resp = send_from_directory(_CRICKET_DIR, filename)
    # Assets are cache-busted via ?v= query strings. Telegram's in-app WebView
    # can still reuse a stored response without reliably revalidating it, so do
    # not let the live-match CSS or JavaScript be stored at all. This is small
    # enough to refetch and prevents an old app.js from hiding the batsman grid.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── Player photos (migrated from UnderCover assets/players/) ─────────────
_PLAYERS_IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "static", "players")


@app.route("/players/photo/<path:filename>")
def player_photo(filename):
    """Serve a player photo from static/players/. Used by mini-app shop."""
    from flask import send_from_directory
    return send_from_directory(_PLAYERS_IMG_DIR, filename)


# ── Lucky Spin standalone Mini App (UnderCover-style wheel) ──────────────
# Self-contained page wired to the existing POST /api/webapp/spin (+ ad-gating
# via /api/webapp/ad-completed). Open it with a Telegram Web App button so the
# page receives initData for auth.
_SPIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "spin")


@app.route("/spin")
def spin_wheel_index():
    from flask import send_from_directory
    return send_from_directory(_SPIN_DIR, "index.html")


@app.route("/api/event-media/<int:media_id>")
@csrf_exempt
def event_media_asset(media_id):
    """Serve/proxy an enabled event animation through a stable MiniApp URL."""
    db = get_session()
    try:
        from models import EventMedia
        from services.event_media_service import MEDIA_DIR
        media = db.query(EventMedia).get(media_id)
        if not media or (not media.enabled and not session.get("admin_user")):
            return {"error": "not_found"}, 404
        if media.source_type == "url":
            return redirect(media.source, code=302)
        if media.source_type == "file":
            path = media.source if os.path.isabs(media.source) else os.path.join(MEDIA_DIR, media.source)
            if not os.path.isfile(path):
                return {"error": "not_found"}, 404
            return send_file(path, conditional=True, max_age=86400)
        if media.source_type == "telegram":
            import requests as _requests
            token = os.getenv("BOT_TOKEN", "").strip()
            if not token:
                return {"error": "storage_unavailable"}, 503
            meta = _requests.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": media.source}, timeout=8).json()
            file_path = ((meta.get("result") or {}).get("file_path"))
            if not file_path:
                return {"error": "storage_unavailable"}, 503
            upstream = _requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=20)
            upstream.raise_for_status()
            return Response(upstream.content, content_type=upstream.headers.get("Content-Type", "image/gif"), headers={"Cache-Control": "public, max-age=86400"})
        return {"error": "not_found"}, 404
    except Exception:
        logger.exception("event media asset failed")
        return {"error": "storage_unavailable"}, 503
    finally:
        db.close()


@app.route("/api/match/autoplay-status", methods=["POST"])
@csrf_exempt
def match_rest_autoplay_status():
    """Persist a participant's Autoplay toggle so both players can see it."""
    db = get_session()
    try:
        data = request.get_json(silent=True) or {}
        user, match, err = _match_rest_user_and_match(
            db, data.get("userId") or data.get("user_id"),
            data.get("matchId") or data.get("match_id"))
        if err:
            return err
        from services.match_webapp_access import save_autoplay_users
        from services.crickidex_arena import serialize_match_state
        state = save_autoplay_users(match.id, user.id, bool(data.get("active")))
        if not state:
            return {"ok": False, "error": "no_match"}, 404
        return {
            "ok": True,
            "matchState": serialize_match_state(db, match, user),
        }
    except Exception as e:
        logger.exception("match_rest_autoplay_status failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/match", methods=["GET"])
@csrf_exempt
def match_rest_poll():
    """GET /api/match?userId=<telegram_id>[&matchId=<id>]
    Crickidex Arena polling endpoint. Returns the full UnderCover-style
    serialized match state (xi_selection / innings1 / innings2 / completed)."""
    db = get_session()
    try:
        from services.crickidex_arena import (resolve_viewer, find_active_match,
                                               serialize_match_state)
        uid = request.args.get("userId") or request.args.get("user_id")
        mid = request.args.get("matchId") or request.args.get("match_id")
        if not uid and not mid:
            return {"error": "userId or matchId is required"}, 400
        viewer = resolve_viewer(db, uid)
        match = find_active_match(db, viewer, mid)
        if not match:
            return {"error": "No active match found."}, 404
        # Finalize a terminal match AND queue its summary broadcast (+ deferred
        # state cleanup) here too, so completion first observed by a poll still
        # delivers the Match Summary. The broadcast guard dedupes against the
        # action endpoint, so the summary is sent exactly once.
        _finalize_and_broadcast_if_terminal(db, match.id)
        try:
            from services.match_webapp_service import auto_play_bot_turns, get_state_is_vsbot
            if get_state_is_vsbot(match.id):
                auto_play_bot_turns(db, match.id)
        except Exception:
            pass
        payload = serialize_match_state(db, match, viewer)
        if not payload:
            return {"error": "No active match found."}, 404
        from services.event_media_service import get_miniapp_event_gifs
        payload["eventGifs"] = get_miniapp_event_gifs(db)
        return payload
    except Exception as e:
        logger.exception("match_rest_poll failed")
        return {"error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/init", methods=["POST"])
@csrf_exempt
def webapp_match_init():
    """Initialize live state for a Mini-App match after the toss.
    Body: {match_id}. Idempotent."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import init_match_for_webapp, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        # Only participants can initialize
        from models import Match
        m = db.query(Match).get(match_id)
        if not m:
            return {"ok": False, "error": "no_match"}, 404
        if user.id not in (m.user1_id, m.user2_id):
            return {"ok": False, "error": "not_participant",
                    "message": "You're not in this match."}, 403
        ok, msg = init_match_for_webapp(db, match_id)
        if not ok:
            return {"ok": False, "message": msg}, 400
        snap = build_snapshot(db, match_id, user.id)
        return {"ok": True, "message": msg, "snapshot": snap}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_match_init failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/select-openers", methods=["POST"])
@csrf_exempt
def webapp_match_select_openers():
    """Batsman picks striker + non-striker.
    Body: {match_id, striker_rid, non_striker_rid}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import select_openers, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        striker = int(data.get("striker_rid") or 0)
        non_striker = int(data.get("non_striker_rid") or 0)
        ok, started, msg = select_openers(match_id, user.id, striker, non_striker)
        if not ok:
            return {"ok": False, "message": msg}, 400
        if started:
            _broadcast_wpm_start_player_cards(match_id)
        if started:
            try:
                from services.match_webapp_service import auto_play_bot_turns, get_state_is_vsbot
                if get_state_is_vsbot(match_id):
                    auto_play_bot_turns(db, match_id)
            except Exception:
                logger.exception("auto-play after openers failed")
        return {"ok": True, "message": msg, "started": started,
                "snapshot": build_snapshot(db, match_id, user.id)}
    except Exception as e:
        logger.exception("webapp_match_select_openers failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/select-bowler", methods=["POST"])
@csrf_exempt
def webapp_match_select_bowler():
    """Bowling side picks the bowler. Body: {match_id, bowler_rid}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import select_bowler, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        bowler_rid = int(data.get("bowler_rid") or 0)
        ok, started, msg = select_bowler(match_id, user.id, bowler_rid)
        if not ok:
            return {"ok": False, "message": msg}, 400
        if started:
            _broadcast_wpm_start_player_cards(match_id)
        if started:
            try:
                from services.match_webapp_service import auto_play_bot_turns, get_state_is_vsbot
                if get_state_is_vsbot(match_id):
                    auto_play_bot_turns(db, match_id)
            except Exception:
                logger.exception("auto-play after bowler failed")
        return {"ok": True, "message": msg, "started": started,
                "snapshot": build_snapshot(db, match_id, user.id)}
    except Exception as e:
        logger.exception("webapp_match_select_bowler failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/ready", methods=["POST"])
@csrf_exempt
def webapp_match_ready():
    """A side marks ready. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import mark_ready, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        ok, both, msg = mark_ready(match_id, user.id)
        if not ok:
            return {"ok": False, "message": msg}, 400
        if both:
            try:
                from services.match_webapp_service import auto_play_bot_turns
                auto_play_bot_turns(db, match_id)
            except Exception:
                logger.exception("auto_play_bot_turns (ready) failed")
        return {"ok": True, "both_ready": both, "message": msg,
                "snapshot": build_snapshot(db, match_id, user.id)}
    except Exception as e:
        logger.exception("webapp_match_ready failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/bowling-options", methods=["POST"])
@csrf_exempt
def webapp_match_bowling_options():
    """Bowler's delivery options. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import get_bowling_options
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        result = get_bowling_options(match_id, user.id)
        if not result.get("ok"):
            return result, 400
        return result
    except Exception as e:
        logger.exception("webapp_match_bowling_options failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/deliver", methods=["POST"])
@csrf_exempt
def webapp_match_deliver():
    """Bowler sets the delivery. Body: {match_id, variation, length?}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import set_delivery, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        variation = (data.get("variation") or "").strip()
        length = (data.get("length") or "").strip() or None
        if not variation:
            return {"ok": False, "message": "Pick a variation."}, 400
        ok, msg = set_delivery(match_id, user.id, variation, length)
        if not ok:
            return {"ok": False, "message": msg}, 400
        result_after = None
        try:
            from services.match_webapp_access import get_next_action as _gna
            from services.match_state_store import (A_COMPLETED as _DONE,
                                                     A_PICK_LENGTH as _LEN)
            na_now = _gna(match_id)
            # Pacer's first step (variation set, awaiting length): return fast,
            # nothing to broadcast or auto-play yet.
            if na_now == _LEN:
                return {"ok": True, "message": msg,
                        "snapshot": build_snapshot(db, match_id, user.id)}
            from services.match_webapp_service import (auto_play_bot_turns,
                                                       get_state_is_vsbot)
            if get_state_is_vsbot(match_id):
                # vs-bot: the AI plays its shot now, which CAN end the match.
                steps = auto_play_bot_turns(db, match_id)
                if steps:
                    result_after = steps[-1]
                if _gna(match_id) == _DONE:
                    _finalize_and_broadcast_if_terminal(db, match_id)
                else:
                    _broadcast_match_scorecard(match_id)  # non-blocking
            else:
                # PvP: a single delivery just hands the ball to the batsman — it
                # can never complete the match, so skip the extra completion
                # check and just refresh the chat scorecard (fully async).
                _broadcast_match_scorecard(match_id)
        except Exception:
            logger.exception("auto_play after deliver failed")
        return {"ok": True, "message": msg, "bot_step": result_after,
                "snapshot": build_snapshot(db, match_id, user.id)}
    except Exception as e:
        logger.exception("webapp_match_deliver failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


def _tg_send_async(payload):
    """Fire a Telegram sendMessage in a daemon thread so the request handler
    returns immediately (never blocks the Mini App on a slow Telegram call)."""
    import os as _os
    import threading
    token = _os.getenv("BOT_TOKEN", "").strip()
    if not token:
        return

    def _send():
        try:
            import requests as _rq
            _rq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json=payload, timeout=8)
        except Exception:
            logger.exception("async tg send failed")

    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        logger.exception("could not spawn tg send thread")



def _tg_send_photo_async(payload, photo_bytes, filename="match_summary.png"):
    """Fire a Telegram sendPhoto in a daemon thread so result cards do not
    block Mini App requests."""
    import os as _os
    import threading
    token = _os.getenv("BOT_TOKEN", "").strip()
    if not token or not photo_bytes:
        return

    def _send():
        try:
            import requests as _rq
            files = {"photo": (filename, photo_bytes, "image/png")}
            _rq.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                     data=payload, files=files, timeout=12)
        except Exception:
            logger.exception("async tg sendPhoto failed")

    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        logger.exception("could not spawn tg sendPhoto thread")



def _tg_api_post(method, payload, timeout=8):
    """Synchronous Telegram API helper for already-background workers."""
    import os as _os
    token = _os.getenv("BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        import requests as _rq
        resp = _rq.post(f"https://api.telegram.org/bot{token}/{method}",
                        json=payload, timeout=timeout)
        try:
            return resp.json()
        except Exception:
            return None
    except Exception:
        logger.exception("telegram %s failed", method)
        return None



def _player_stat_defaults(card_type):
    """Default career stat payloads used when a player has no saved stats."""
    if card_type == "bowling":
        return {
            "bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
            "balls_bowled": 0, "bowl_avg": 0, "bowl_sr": 0, "econ": 0,
            "hat_tricks": 0, "five_fers": 0, "three_fers": 0, "bbf_str": "-",
        }
    return {
        "bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
        "fours": 0, "sixes": 0, "bat_avg": 0, "bat_sr": 0,
        "ducks": 0, "hs_str": "-",
    }


def _build_player_stat_card_bytes(player, owner_user_id, card_type):
    """Render a batting/bowling career card for Mini-App match broadcasts.

    PlayerGameStats is shared by /wpm, /cm, /vsbot, and /playmatch, so querying
    this row gives a combined career card. Missing rows deliberately render the
    zero/default payload instead of failing the match request.
    """
    if not player or not owner_user_id:
        return None
    db = get_session()
    try:
        from models import PlayerGameStats
        if card_type == "bowling":
            from services.bowler_card import generate_bowler_card
            stats = _player_stat_defaults("bowling")
            gs = (db.query(PlayerGameStats)
                  .filter(PlayerGameStats.user_id == owner_user_id,
                          PlayerGameStats.player_id == player.get("player_id"))
                  .first())
            if gs:
                stats.update({
                    "bowl_inns": gs.bowl_inns or 0,
                    "wickets_taken": gs.wickets_taken or 0,
                    "runs_conceded": gs.runs_conceded or 0,
                    "balls_bowled": gs.balls_bowled or 0,
                    "bowl_avg": gs.bowl_avg or 0,
                    "bowl_sr": gs.bowl_sr or 0,
                    "econ": gs.bowl_economy or 0,
                    "hat_tricks": getattr(gs, "hat_tricks", 0) or 0,
                    "five_fers": gs.five_fers or 0,
                    "three_fers": gs.three_fers or 0,
                    "bbf_str": (f"{gs.best_bowl_wickets}/{gs.best_bowl_runs}"
                                if (gs.best_bowl_wickets or 0) > 0 else "-"),
                })
            return generate_bowler_card(
                player.get("name", "Player"), player.get("rating", 0),
                player.get("bowl_rating", 0), stats,
                bat_hand=player.get("bat_hand", "Right"),
                bowl_hand=player.get("bowl_hand", "Right"),
                bowl_style=player.get("bowl_style", "Medium Pacer"),
            )

        from services.batsman_card import generate_batsman_card
        stats = _player_stat_defaults("batting")
        gs = (db.query(PlayerGameStats)
              .filter(PlayerGameStats.user_id == owner_user_id,
                      PlayerGameStats.player_id == player.get("player_id"))
              .first())
        if gs:
            stats.update({
                "bat_inns": gs.bat_inns or 0,
                "runs": gs.runs or 0,
                "fifties": gs.fifties or 0,
                "hundreds": gs.hundreds or 0,
                "fours": gs.fours or 0,
                "sixes": gs.sixes or 0,
                "bat_avg": gs.bat_avg or 0,
                "bat_sr": gs.bat_sr or 0,
                "ducks": gs.ducks or 0,
                "hs_str": gs.hs_str or "-",
            })
        return generate_batsman_card(
            player.get("name", "Player"), player.get("rating", 0),
            player.get("bat_rating", 0), stats,
            bat_hand=player.get("bat_hand", "Right"),
            bowl_hand=player.get("bowl_hand", "Right"),
            bowl_style=player.get("bowl_style", "Medium Pacer"),
        )
    except Exception:
        logger.exception("failed to build %s stat card for %s", card_type, player.get("name"))
        return None
    finally:
        db.close()


def _match_origin_chat_id(match_id, state=None):
    """Return the Telegram chat where a Mini-App match was created."""
    state = state or {}
    chat_id = state.get("chat_id")
    if chat_id:
        return chat_id
    db = get_session()
    try:
        from models import Match
        match = db.query(Match).get(match_id)
        return match.chat_id if match else None
    except Exception:
        logger.exception("failed to resolve match origin chat")
        return None
    finally:
        db.close()


def _playmatch_spectate_markup(match_id, chat_id):
    """Inline keyboard for player-card broadcasts in /wpm and /cm chats."""
    try:
        from services.match_broadcast import play_match_url
        url = play_match_url(match_id, chat_id)
        if not url:
            return None
        return {
            "inline_keyboard": [[{
                "text": "👁 Spectate / Play Match",
                "url": url,
            }]]
        }
    except Exception:
        logger.exception("failed to build Playmatch spectate button")
        return None


def _broadcast_wpm_player_stat_card(match_id, player, owner_user_id, card_type):
    """Send one /wpm or /cm career stat card to the match's origin chat."""
    try:
        import json as _json
        from services.match_webapp_access import get_state
        state = get_state(match_id) or {}
        chat_id = _match_origin_chat_id(match_id, state)
        if not chat_id or not player:
            return
        photo = _build_player_stat_card_bytes(player, owner_user_id, card_type)
        if not photo:
            return
        if card_type == "bowling":
            caption = f"🎳 <b>{player.get('name', 'Player')}</b> starts bowling"
            filename = "wpm_bowler_card.png"
        else:
            caption = f"🏏 <b>{player.get('name', 'Player')}</b> comes to the crease"
            filename = "wpm_batter_card.png"
        payload = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        markup = _playmatch_spectate_markup(match_id, chat_id)
        if markup:
            # Telegram's multipart sendPhoto endpoint expects reply_markup as a
            # JSON-encoded form field, not a nested object.
            payload["reply_markup"] = _json.dumps(markup)
        _tg_send_photo_async(payload, photo, filename=filename)
    except Exception:
        logger.exception("failed to broadcast /wpm player stat card")


def _broadcast_wpm_start_player_cards(match_id):
    """Send opener and opening-bowler cards once a /wpm match actually starts."""
    try:
        from services.match_webapp_access import get_state
        state = get_state(match_id) or {}
        order = state.get("batting_order") or []
        for idx in (state.get("striker_idx"), state.get("non_striker_idx")):
            if isinstance(idx, int) and 0 <= idx < len(order):
                _broadcast_wpm_player_stat_card(match_id, order[idx], state.get("bat_team_id"), "batting")
        _broadcast_wpm_player_stat_card(match_id, state.get("current_bowler"),
                                        state.get("bowl_team_id"), "bowling")
    except Exception:
        logger.exception("failed to broadcast /wpm start player cards")


def _broadcast_wpm_current_batter_card(match_id):
    try:
        from services.match_webapp_access import get_state
        state = get_state(match_id) or {}
        order = state.get("batting_order") or []
        idx = state.get("striker_idx")
        if isinstance(idx, int) and 0 <= idx < len(order):
            _broadcast_wpm_player_stat_card(match_id, order[idx], state.get("bat_team_id"), "batting")
    except Exception:
        logger.exception("failed to broadcast /wpm batter card")


def _broadcast_wpm_current_bowler_card(match_id):
    try:
        from services.match_webapp_access import get_state
        state = get_state(match_id) or {}
        _broadcast_wpm_player_stat_card(match_id, state.get("current_bowler"),
                                        state.get("bowl_team_id"), "bowling")
    except Exception:
        logger.exception("failed to broadcast /wpm bowler card")

def _broadcast_match_scorecard(match_id):
    """Update the pinned live scorecard message in the match chat.

    The worker edits the same Telegram message whenever possible, sends it once
    if missing, and pins that live scorecard so the chat always has the latest
    score without spam. All Telegram/network work stays off the request path.
    """
    import threading

    def _work():
        try:
            from services.match_webapp_access import get_state, get_next_action, save_state
            from services.match_broadcast import build_live_scorecard_text, _launch_url
            from services.match_state_store import (A_PICK_DELIVERY, A_PICK_LENGTH,
                                                     A_PICK_NEW_BOWLER)

            state = get_state(match_id)
            if not state:
                return
            chat_id = state.get("chat_id")
            if not chat_id:
                return

            na = get_next_action(match_id)
            waiting = None
            if na in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
                uname = state.get("bowl_username")
                waiting = f"@{uname}" if uname else state.get("bowl_team_name", "the bowler")

            text = build_live_scorecard_text(state, waiting_for_mention=waiting)

            reply_markup = None
            url = _launch_url(match_id, chat_id)
            if url:
                reply_markup = {"inline_keyboard": [[
                    {"text": "▶️ Play Match", "url": url}]]}

            message_id = state.get("live_score_msg_id")
            edited = False
            if message_id:
                payload = {"chat_id": chat_id, "message_id": message_id,
                           "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                res = _tg_api_post("editMessageText", payload)
                edited = bool(res and (res.get("ok") or "not modified" in (res.get("description", "").lower())))

            if not edited:
                payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                res = _tg_api_post("sendMessage", payload)
                if res and res.get("ok"):
                    msg = (res.get("result") or {})
                    new_mid = msg.get("message_id")
                    if new_mid:
                        state["live_score_msg_id"] = new_mid
                        message_id = new_mid
                        save_state(match_id, state)

            if message_id and not state.get("live_score_pinned"):
                pin_res = _tg_api_post("pinChatMessage", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "disable_notification": True,
                })
                if pin_res and pin_res.get("ok"):
                    state["live_score_pinned"] = True
                    save_state(match_id, state)
        except Exception:
            logger.exception("scorecard broadcast worker failed")

    try:
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        logger.exception("could not spawn scorecard broadcast thread")


def _top_performers_for_summary(arena_state):
    """Build the performer payload expected by the shared summary renderer."""
    if not arena_state:
        return None, None, None

    top_scorer = None
    top_wicket = None

    def _batters(xi, stats, team_name):
        nonlocal top_scorer
        rows = []
        for player in xi or []:
            row = _arena_stat(stats, player.get("roster_id"))
            balls = int(row.get("balls", 0) or 0)
            if balls <= 0:
                continue
            batter = {
                "name": player.get("name", "—"),
                "rating": player.get("rating", "—"),
                "team": team_name,
                "runs": int(row.get("runs", 0) or 0),
                "balls": balls,
                "fours": int(row.get("fours", 0) or 0),
                "sixes": int(row.get("sixes", 0) or 0),
                "out": bool(row.get("out")),
            }
            rows.append(batter)
            if top_scorer is None or batter["runs"] > top_scorer["runs"]:
                top_scorer = batter
        return sorted(rows, key=lambda item: (-item["runs"], item["balls"]))[:4]

    def _bowlers(xi, stats, team_name):
        nonlocal top_wicket
        rows = []
        for player in xi or []:
            row = _arena_stat(stats, player.get("roster_id"))
            balls = int(row.get("balls", 0) or 0)
            if balls <= 0:
                continue
            runs = int(row.get("runs", 0) or 0)
            wickets = int(row.get("wickets", 0) or 0)
            bowler = {
                "name": player.get("name", "—"),
                "rating": player.get("rating", "—"),
                "team": team_name,
                "overs": f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6),
                "runs": runs,
                "wickets": wickets,
                "econ": runs / (balls / 6),
            }
            rows.append(bowler)
            if (top_wicket is None or wickets > top_wicket["wickets"] or
                    (wickets == top_wicket["wickets"] and runs < top_wicket["runs"])):
                top_wicket = bowler
        return sorted(rows, key=lambda item: (-item["wickets"], item["econ"]))[:4]

    inn1_team = arena_state.get("inn1_team") or "Team 1"
    inn2_team = arena_state.get("bat_team_name") or "Team 2"
    top_per_team = {
        "inn1": {
            "team": inn1_team,
            "bowl_team": inn2_team,
            "batters": _batters(arena_state.get("inn1_bat_xi"),
                                 arena_state.get("inn1_bat_stats"), inn1_team),
            "bowlers": _bowlers(arena_state.get("inn1_bowl_xi"),
                                 arena_state.get("inn1_bowl_stats"), inn2_team),
        },
        "inn2": {
            "team": inn2_team,
            "bowl_team": arena_state.get("bowl_team_name") or inn1_team,
            "batters": _batters(arena_state.get("bat_xi"),
                                 arena_state.get("bat_stats"), inn2_team),
            "bowlers": _bowlers(arena_state.get("bowl_xi"),
                                 arena_state.get("bowl_stats"), inn1_team),
        },
    }
    return top_scorer, top_wicket, top_per_team


def _build_match_summary_image(match, sc, result_text, arena, pom):
    """Render the same post-match summary card used by /playmatch when possible."""
    try:
        from datetime import datetime as _dt
        from services.match_summary_card import generate_match_summary
        from services.config_service import get_config
        innings = sc.get("innings") or []
        if len(innings) < 2:
            # Some completions previously persisted an incomplete scorecard (or
            # raced with cleanup) even though the final Arena state still has
            # both innings. Build the two score rows directly from that state so
            # the lobby always receives a non-empty Match Summary card.
            if not arena or int(arena.get("innings", 0) or 0) != 2:
                return None
            innings = [
                {
                    "bat_team": arena.get("inn1_team") or arena.get("bowl_team_name") or "Team 1",
                    "runs": int(arena.get("inn1_runs", 0) or 0),
                    "wickets": int(arena.get("inn1_wickets", 0) or 0),
                    "overs": arena.get("inn1_overs") or "0.0",
                },
                {
                    "bat_team": arena.get("bat_team_name") or "Team 2",
                    "runs": int(arena.get("total_runs", 0) or 0),
                    "wickets": int(arena.get("total_wickets", 0) or 0),
                    "overs": _arena_overs(arena),
                },
            ]
        inn1, inn2 = innings[0], innings[1]
        top_scorer, top_wicket, top_per_team = _top_performers_for_summary(arena)
        pom_name = pom.get("name") if isinstance(pom, dict) else None
        pom_team = pom.get("team") if isinstance(pom, dict) else None
        pom_rating = pom.get("rating") if isinstance(pom, dict) else None
        if pom_name and (not pom_team or pom_rating is None):
            player_groups = (
                (arena.get("inn1_bat_xi") or [], arena.get("inn1_team")),
                (arena.get("inn1_bowl_xi") or [], arena.get("bat_team_name")),
                (arena.get("bat_xi") or [], arena.get("bat_team_name")),
                (arena.get("bowl_xi") or [], arena.get("bowl_team_name")),
            )
            for players, team_name in player_groups:
                player = next((p for p in players if p.get("name") == pom_name), None)
                if player:
                    pom_team = pom_team or team_name
                    pom_rating = pom_rating if pom_rating is not None else player.get("rating")
                    break
        pom_stats = pom.get("stats") if isinstance(pom, dict) else None
        if not pom_stats and isinstance(pom, dict):
            pom_stats = f"{pom.get('runs', 0)} runs • {pom.get('wickets', 0)} wickets"
        return generate_match_summary(
            inn1_team=inn1.get("bat_team", "Team 1"),
            inn1_runs=inn1.get("runs", 0),
            inn1_wickets=inn1.get("wickets", 0),
            inn1_overs=inn1.get("overs", "0"),
            inn2_team=inn2.get("bat_team", "Team 2"),
            inn2_runs=inn2.get("runs", 0),
            inn2_wickets=inn2.get("wickets", 0),
            inn2_overs=inn2.get("overs", "0"),
            winner_name=(result_text.split(" won ", 1)[0]
                         if " won " in result_text else result_text),
            win_margin_text=(result_text.split(" won ", 1)[1]
                             if " won " in result_text else ""),
            overs_total=match.overs or arena.get("overs") or 0,
            potm_name=pom_name,
            potm_rating=pom_rating,
            potm_team=pom_team,
            potm_stats=pom_stats,
            potm_impact=pom.get("impact_points") if isinstance(pom, dict) else None,
            top_scorer=top_scorer,
            top_wicket=top_wicket,
            top_per_team=top_per_team,
            stadium=match.stadium or arena.get("stadium"),
            match_date=getattr(match, "completed_at", None) or _dt.utcnow(),
            is_spectator=bool(arena.get("is_spectator")),
            match_no=match.id,
            text_settings=get_config().get("scorecard_text_settings"),
        )
    except Exception:
        logger.exception("wpm match summary image render failed")
        return None


def _arena_stat(stats, roster_id):
    """Read an Arena stat row regardless of JSON/stringified roster keys."""
    stats = stats or {}
    return stats.get(roster_id) or stats.get(str(roster_id)) or {}


def _arena_overs(state):
    """Format the current (second) innings overs like the Telegram match flow."""
    return f"{max(0, int(state.get('current_over', 1) or 1) - 1)}.{int(state.get('current_ball', 0) or 0)}"


def _build_innings_cards(match, arena, innings_num):
    """Render the batting + bowling scorecard images for one innings of a
    completed Mini App match.

    Returns a dict ``{"bat": (bytes, caption, filename), "bowl": (...)}`` with
    only the entries that rendered successfully. Innings 1 is read from the
    ``inn1_*`` snapshots taken at the innings break; innings 2 from the live
    (final) state. Used by /wpm and /cm to post any admin-selected combination
    of Bat1 / Bowl1 / Bat2 / Bowl2 cards to the lobby chat.
    """
    if not arena or innings_num not in (1, 2):
        return {}
    # Both innings live in the final state only once the 2nd innings has begun.
    if int(arena.get("innings", 0) or 0) != 2:
        return {}
    try:
        from services.config_service import get_config
        from services.scorecard_card import generate_batting_scorecard, generate_bowling_scorecard

        config = get_config()
        if innings_num == 1:
            bat_team = arena.get("inn1_team") or arena.get("bowl_team_name", "Team")
            bowl_team = arena.get("bowl_team_name", "Opponent")
            total_runs = int(arena.get("inn1_runs", 0) or 0)
            total_wickets = int(arena.get("inn1_wickets", 0) or 0)
            overs_str = arena.get("inn1_overs") or "0.0"
            bat_xi = arena.get("inn1_bat_xi") or []
            bowl_xi = arena.get("inn1_bowl_xi") or []
            bat_stats = arena.get("inn1_bat_stats") or {}
            bowl_stats = arena.get("inn1_bowl_stats") or {}
            batting_order = arena.get("inn1_batting_order") or bat_xi
            fow = arena.get("inn1_fow") or []
            extras = {"wd": int(arena.get("inn1_wides", 0) or 0),
                      "nb": int(arena.get("inn1_noballs", 0) or 0), "b": 0,
                      "lb": int(arena.get("inn1_legbyes", 0) or 0)}
            target = None
            accent = config.get("scorecard_color_inn1")
        else:
            bat_team = arena.get("bat_team_name", "Team")
            bowl_team = arena.get("bowl_team_name", "Opponent")
            total_runs = int(arena.get("total_runs", 0) or 0)
            total_wickets = int(arena.get("total_wickets", 0) or 0)
            overs_str = _arena_overs(arena)
            bat_xi = arena.get("bat_xi") or []
            bowl_xi = arena.get("bowl_xi") or []
            bat_stats = arena.get("bat_stats") or {}
            bowl_stats = arena.get("bowl_stats") or {}
            batting_order = arena.get("batting_order") or bat_xi
            fow = arena.get("fow") or []
            extras = {"wd": int(arena.get("wides", 0) or 0),
                      "nb": int(arena.get("noballs", 0) or 0), "b": 0,
                      "lb": int(arena.get("legbyes", 0) or 0)}
            target = int(arena.get("target", 0) or 0) or None
            accent = config.get("scorecard_color_inn2")

        ordered_batters, seen = [], set()
        for player in list(batting_order) + list(bat_xi):
            rid = player.get("roster_id")
            if rid not in seen:
                seen.add(rid)
                ordered_batters.append(player)

        batsmen_rows = []
        for player in ordered_batters:
            stats = _arena_stat(bat_stats, player.get("roster_id"))
            balls = int(stats.get("balls", 0) or 0)
            is_out = bool(stats.get("out"))
            if balls == 0 and not is_out:
                status, dismissal = "dnb", "did not bat"
            elif is_out:
                status = "out"
                dismissal = stats.get("dismissal_text") or stats.get("how_out") or "out"
            else:
                status, dismissal = "not_out", "not out"
            runs = int(stats.get("runs", 0) or 0)
            batsmen_rows.append({
                "rating": player.get("rating", 0), "name": player.get("name", "?"),
                "dismissal": dismissal, "runs": runs, "balls": balls,
                "fours": stats.get("fours", 0), "sixes": stats.get("sixes", 0),
                "strike_rate": round(runs / balls * 100, 1) if balls else 0,
                "status": status,
            })

        bowlers_rows = []
        for player in bowl_xi:
            stats = _arena_stat(bowl_stats, player.get("roster_id"))
            balls = int(stats.get("balls", 0) or 0)
            runs = int(stats.get("runs", 0) or 0)
            wickets = int(stats.get("wickets", 0) or 0)
            if balls == 0 and runs == 0 and wickets == 0:
                continue
            overs = f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)
            bowlers_rows.append({
                "name": player.get("name", "?"), "overs": overs,
                "maidens": stats.get("maidens", 0), "runs_conceded": runs,
                "wickets": wickets,
                "economy": round(runs / balls * 6, 2) if balls else 0,
            })

        extras["total"] = sum(v for k, v in extras.items() if k != "total")
        chase_outcome = None
        if target:
            chase_outcome = ("won" if total_runs >= target else
                             "tied" if total_runs == target - 1 else "lost")

        common = {
            "is_first_innings": innings_num == 1, "match_title": "MATCH",
            "stadium": match.stadium or arena.get("stadium"), "match_no": match.id,
            "accent_hex": accent,
            "text_settings": config.get("scorecard_text_settings"),
        }
        batting = generate_batting_scorecard(
            bat_team, bowl_team, total_runs, total_wickets, overs_str,
            batsmen_rows, fow, extras, target=target,
            chase_outcome=chase_outcome, **common)
        bowling = generate_bowling_scorecard(
            bowl_team, bowlers_rows, fow, opponent_name=bat_team,
            opp_score=total_runs, opp_wickets=total_wickets,
            opp_overs=overs_str, **common)

        cards = {}
        if batting:
            cards["bat"] = (batting,
                            f"🏏 <b>{bat_team}</b> — Batting (Innings {innings_num})",
                            f"innings{innings_num}_batting.png")
        if bowling:
            cards["bowl"] = (bowling,
                             f"🎳 <b>{bowl_team}</b> — Bowling (Innings {innings_num})",
                             f"innings{innings_num}_bowling.png")
        return cards
    except Exception:
        logger.exception("/wpm or /cm innings %s scorecard render failed", innings_num)
        return {}


def _completed_team_name(arena, team_id, fallback):
    """Resolve a winning/losing Arena team id to its display name."""
    if team_id == arena.get("bat_team_id"):
        return arena.get("bat_team_name") or fallback
    if team_id == arena.get("bowl_team_id"):
        return arena.get("bowl_team_name") or fallback
    return fallback


def _send_completed_match_cards(chat_id, images, fallback_text, reply_markup):
    """Post a completed Mini-App match recap to the lobby chat synchronously.

    The caller already runs in a background broadcast worker, so doing the
    Telegram calls inline lets us keep live match_state until the result text is
    actually dispatched. If a selected album fails, each image is retried as an
    individual photo before the mandatory MATCH RESULT message is sent.
    """
    import json as _json
    import os as _os
    token = _os.getenv("BOT_TOKEN", "").strip()
    if not token:
        logger.warning("completed match Telegram flow skipped: BOT_TOKEN is empty")
        return False

    sent_any = False
    try:
        import requests as _rq
        base = f"https://api.telegram.org/bot{token}"
        cards = [c for c in (images or []) if c and c[0]]

        def _send_photo(card):
            img_bytes, caption, fname = card
            resp = _rq.post(
                f"{base}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption or "",
                      "parse_mode": "HTML"},
                files={"photo": (fname, img_bytes, "image/png")},
                timeout=30)
            if not resp.ok:
                logger.warning("completed match photo send failed: %s", resp.text)
                return False
            return True

        if len(cards) == 1:
            sent_any = _send_photo(cards[0]) or sent_any
        elif len(cards) >= 2:
            media, files = [], {}
            for i, (img_bytes, caption, fname) in enumerate(cards[:10]):
                key = f"photo{i}"
                files[key] = (fname, img_bytes, "image/png")
                item = {"type": "photo", "media": f"attach://{key}",
                        "parse_mode": "HTML"}
                if i == 0 and caption:
                    item["caption"] = caption
                media.append(item)
            resp = _rq.post(
                f"{base}/sendMediaGroup",
                data={"chat_id": chat_id, "media": _json.dumps(media)},
                files=files, timeout=40)
            if resp.ok:
                sent_any = True
            else:
                logger.warning("completed match album send failed: %s", resp.text)
                for card in cards[:10]:
                    sent_any = _send_photo(card) or sent_any

        payload = {"chat_id": chat_id, "text": fallback_text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = _json.dumps(reply_markup)
        resp = _rq.post(f"{base}/sendMessage", json=payload, timeout=12)
        if resp.ok:
            sent_any = True
        else:
            logger.warning("completed match result message send failed: %s", resp.text)
        return sent_any
    except Exception:
        logger.exception("completed match Telegram flow failed")
        return sent_any


def _send_completed_match_cards_async(chat_id, images, fallback_text, reply_markup):
    """Fire-and-forget compatibility wrapper for callers outside broadcast."""
    import threading
    try:
        threading.Thread(
            target=_send_completed_match_cards,
            args=(chat_id, images, fallback_text, reply_markup),
            daemon=True,
        ).start()
    except Exception:
        logger.exception("could not spawn completed match Telegram worker")


_COMPLETED_MATCH_BROADCASTS = set()
_COMPLETED_MATCH_BROADCASTS_LOCK = threading.Lock()


def _broadcast_match_result(match_id, result):
    """Queue /wpm and /cm's final lobby recap once, off the action request.

    Rendering the scorecard and match-summary images can be expensive. Keeping
    all of that work in a background thread lets the Mini App immediately show
    the winner and lock its controls before Telegram receives the final cards.
    The in-process guard also prevents two near-simultaneous completion actions
    from posting duplicate result flows.
    """
    with _COMPLETED_MATCH_BROADCASTS_LOCK:
        if match_id in _COMPLETED_MATCH_BROADCASTS:
            return
        # Keep the idempotency guard bounded across long-running workers.
        if len(_COMPLETED_MATCH_BROADCASTS) >= 4096:
            _COMPLETED_MATCH_BROADCASTS.clear()
        _COMPLETED_MATCH_BROADCASTS.add(match_id)

    def _work():
        import time
        try:
            # Give the Mini App a moment to render the just-decided result from
            # the still-live state before the lobby chat receives its summary.
            time.sleep(1.5)
            if not _build_and_send_match_result(match_id, result):
                raise RuntimeError("completed match result was not sent")
        except Exception:
            with _COMPLETED_MATCH_BROADCASTS_LOCK:
                _COMPLETED_MATCH_BROADCASTS.discard(match_id)
            logger.exception("completed match result build/send failed")
            return
        # The summary has been built (from the persisted scorecard) and its
        # Telegram send dispatched. The live match_state is no longer needed, so
        # remove it now — this is the deferred cleanup finalize_webapp_match no
        # longer performs inline.
        try:
            from services.match_state_store import cleanup_state
            from services.match_webapp_access import fresh_ctx
            cleanup_state(fresh_ctx(), match_id)
        except Exception:
            logger.exception("post-summary match state cleanup failed")

    try:
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        with _COMPLETED_MATCH_BROADCASTS_LOCK:
            _COMPLETED_MATCH_BROADCASTS.discard(match_id)
        logger.exception("could not queue completed match result")


def _build_and_send_match_result(match_id, result, override_chat_id=None):
    """Build and send /wpm or /cm's final cards to its lobby chat.

    ``override_chat_id`` is used by /testwpm so admins can verify the final
    summary flow in the chat where they run the diagnostic command without
    changing the match's persisted origin chat.
    """
    from html import escape
    from services.match_broadcast import _launch_url
    from models import Match

    db = get_session()
    try:
        match = db.query(Match).get(match_id)
        if not match:
            return False
        try:
            from services.match_webapp_service import load_final_scorecard
            scorecard = load_final_scorecard(db, match_id) or {}
        except Exception:
            scorecard = {}
        innings = scorecard.get("innings") or []
        arena = scorecard.get("arena_state") or {}
        # Fallback: if the persisted scorecard is missing or incomplete (e.g. it
        # was saved before both innings were populated), rebuild from the still
        # live match state. This broadcast runs BEFORE cleanup_state removes the
        # state, so it is still available here — this prevents an empty recap
        # (no batting/bowling/summary) in the lobby chat.
        if len(innings) < 2 or not arena:
            try:
                from services.match_webapp_service import build_scorecard
                from services.match_webapp_access import get_state as _live_state
                live = _live_state(match_id)
                if live:
                    if not arena:
                        arena = live
                        scorecard["arena_state"] = arena
                    rebuilt = build_scorecard(match_id, None)
                    if (rebuilt.get("ok") and rebuilt.get("innings")
                            and len(rebuilt["innings"]) > len(innings)):
                        innings = rebuilt["innings"]
                        scorecard["innings"] = innings
                        scorecard.setdefault("target", rebuilt.get("target"))
                    logger.warning(
                        "match %s recap used live-state fallback (persisted "
                        "scorecard had %s innings)", match_id,
                        len(scorecard.get("innings") or []))
            except Exception:
                logger.exception("scorecard live-state fallback failed")
        # Prefer the immutable lobby origin, then the persisted Match chat, and
        # finally the active Arena chat for older matches created before the
        # origin field existed.
        chat_id = (override_chat_id or arena.get("original_lobby_chat_id")
                   or match.chat_id or arena.get("chat_id"))
        if not chat_id:
            return False
        result = result or arena.get("match_result") or {}
        result_text = result.get("text") or scorecard.get("result_text") or "Match finished."
        rewards = arena.get("_completed_rewards") or {}
        pom = arena.get("_player_of_match") or result.get("player_of_match") or {}

        score_lines = []
        for color, innings_row in zip(("🔴", "🟢"), innings[:2]):
            score_lines.append(
                f"{color} {escape(str(innings_row.get('bat_team', 'Team')))}: "
                f"{innings_row.get('runs', 0)}/{innings_row.get('wickets', 0)} "
                f"({innings_row.get('overs', '0')})")
        score_block = "\n".join(score_lines) if score_lines else "Match completed."
        if result.get("margin_type") == "wickets":
            short_summary = (f"{arena.get('bat_team_name', 'The chasing team')} "
                             f"completed the chase with "
                             f"{result.get('margin_value', 0)} wickets remaining.")
        elif result.get("margin_type") == "runs":
            short_summary = (f"{arena.get('bowl_team_name', 'The defending team')} "
                             f"defended the target by {result.get('margin_value', 0)} runs.")
        else:
            short_summary = "Both teams finished level after the second innings."

        text = ("━━━━━━━━━━━━━━━━━━━\n🏆 <b>MATCH RESULT</b>\n\n"
                f"{score_block}\n\n🏆 <b>{escape(str(result_text))}</b>\n"
                f"<i>{escape(short_summary)}</i>\n\n"
                "━━━━━━━━━━━━━━━━━━━\n\n")
        if pom:
            text += ("⭐ <b>PLAYER OF THE MATCH</b>\n"
                     f"🌟 {escape(str(pom.get('name', 'Player')))}\n"
                     f"{pom.get('runs', 0)} runs • {pom.get('wickets', 0)} wickets\n"
                     f"💫 Impact Points: {pom.get('impact_points', 0)}\n\n"
                     "━━━━━━━━━━━━━━━━━━━\n\n")
        if arena.get("is_spectator"):
            text += ("🎬 <b>SPECTATOR MATCH</b>\n"
                     "<i>No rewards distributed — pure entertainment.</i>\n"
                     "━━━━━━━━━━━━━━━━━━━")
        elif rewards:
            winner_name = _completed_team_name(arena, result.get("winner_team_id"), "Winner")
            loser_name = _completed_team_name(arena, result.get("loser_team_id"), "Loser")
            text += ("🎁 <b>REWARDS</b>\n"
                     f"🏆 {escape(str(winner_name))}: +{rewards.get('winner_coins', 0):,} Coins 💰 +{rewards.get('winner_gems', 0)} Gems 💎\n"
                     f"📉 {escape(str(loser_name))}: +{rewards.get('loser_coins', 0):,} Coins 💰 +{rewards.get('loser_gems', 0)} Gems 💎\n"
                     "━━━━━━━━━━━━━━━━━━━")
        else:
            text += "Tap below to open the completed match."

        # Which cards to post is website-configurable (Scorecard Designer).
        # Default "summary" preserves the historical single-card behavior; an
        # admin can additionally enable Bat1 / Bowl1 / Bat2 / Bowl2. The MATCH
        # RESULT text above is always sent as the recap + spectate-button host.
        from services.config_service import get_wpm_result_cards
        selection = get_wpm_result_cards()
        inn_cards = {}
        if any(t in selection for t in ("bat1", "bowl1")):
            c1 = _build_innings_cards(match, arena, 1)
            inn_cards["bat1"] = c1.get("bat")
            inn_cards["bowl1"] = c1.get("bowl")
        if any(t in selection for t in ("bat2", "bowl2")):
            c2 = _build_innings_cards(match, arena, 2)
            inn_cards["bat2"] = c2.get("bat")
            inn_cards["bowl2"] = c2.get("bowl")
        summary_caption = f"🏆 <b>Match Summary</b> — {escape(str(result_text))}"
        if "summary" in selection:
            summary = _build_match_summary_image(match, scorecard, result_text, arena, pom)
            inn_cards["summary"] = ((summary, summary_caption, "match_summary.png")
                                    if summary else None)
        # Assemble in the configured (innings reading) order, dropping any card
        # that failed to render.
        images = [inn_cards.get(token) for token in selection]
        images = [c for c in images if c]
    finally:
        db.close()

    url = _launch_url(match_id, chat_id)
    reply_markup = None
    if url:
        reply_markup = {"inline_keyboard": [[{"text": "PlayMatch - Spectate", "url": url}]]}
    return _send_completed_match_cards(chat_id, images, text, reply_markup)




def send_testwpm_summary_to_chat(match_id, chat_id, result=None):
    """Queue a completed /wpm or /cm summary card to a specific chat.

    This intentionally bypasses the normal duplicate-broadcast guard because
    it is a website-toggleable diagnostic command: every /testwpm invocation
    should prove whether rendering and Telegram delivery still work.
    """
    import threading

    def _work():
        try:
            _build_and_send_match_result(match_id, result or {}, override_chat_id=chat_id)
        except Exception:
            logger.exception("/testwpm summary send failed")

    try:
        threading.Thread(target=_work, daemon=True).start()
        return True
    except Exception:
        logger.exception("could not queue /testwpm summary send")
        return False


def _finalize_and_broadcast_if_terminal(db, match_id):
    """Finalize a terminal Mini-App match and queue the summary once.

    Called from action endpoints and polling as a self-heal path, so /wpm and
    /cm cannot get stuck after a completed chase/second innings if the request
    that delivered the final ball returns before the broadcast is queued.
    """
    try:
        from services.match_webapp_service import ensure_webapp_match_completed
        fin = ensure_webapp_match_completed(db, match_id)
        if fin:
            _broadcast_match_result(match_id, (fin or {}).get("result") or {})
        return fin
    except Exception:
        logger.exception("terminal match finalize/broadcast self-heal failed")
        return None

@app.route("/api/webapp/match/play-shot", methods=["POST"])
@csrf_exempt
def webapp_match_play_shot():
    """Batsman plays a shot, resolving the ball. Body: {match_id, shot_index}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import play_shot, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        shot_index = int(data.get("shot_index"))
        ok, res = play_shot(match_id, user.id, shot_index)
        if not ok:
            return {"ok": False, "message": res}, 400
        # vsbot: advance any consecutive AI turns after the human's shot
        try:
            from services.match_webapp_service import auto_play_bot_turns, get_state_is_vsbot
            if get_state_is_vsbot(match_id):
                auto_play_bot_turns(db, match_id)
        except Exception:
            logger.exception("auto_play_bot_turns failed (non-fatal)")
        # Completion can be triggered by the human's shot OR the bot's auto-play.
        from services.match_webapp_access import get_next_action as _gna
        from services.match_state_store import A_COMPLETED as _DONE
        ended = (isinstance(res, dict) and res.get("match_over")) or (_gna(match_id) == _DONE)
        if ended:
            try:
                fin = _finalize_and_broadcast_if_terminal(db, match_id)
                result = (fin or {}).get("result") or (res.get("result") if isinstance(res, dict) else {}) or {}
            except Exception:
                logger.exception("match finalize/broadcast failed")
        else:
            try:
                from services.match_state_store import A_PICK_DELIVERY as _DELIVERY
                if (isinstance(res, dict) and res.get("need_new_bat")
                        and _gna(match_id) == _DELIVERY):
                    _broadcast_wpm_current_batter_card(match_id)
                _broadcast_match_scorecard(match_id)
            except Exception:
                logger.exception("scorecard/player-card broadcast failed (non-fatal)")
        snap = build_snapshot(db, match_id, user.id)
        return {"ok": True, "result": res, "snapshot": snap}
    except Exception as e:
        logger.exception("webapp_match_play_shot failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/new-bowler-options", methods=["POST"])
@csrf_exempt
def webapp_match_new_bowler_options():
    """Eligible bowlers for the next over. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import get_new_bowler_options
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        return get_new_bowler_options(match_id, user.id)
    except Exception as e:
        logger.exception("webapp_match_new_bowler_options failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/new-bowler", methods=["POST"])
@csrf_exempt
def webapp_match_new_bowler():
    """Bowling side picks next over's bowler. Body: {match_id, bowler_rid}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import select_new_bowler, build_snapshot
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        bowler_rid = int(data.get("bowler_rid") or 0)
        ok, msg = select_new_bowler(match_id, user.id, bowler_rid)
        if not ok:
            return {"ok": False, "message": msg}, 400
        _broadcast_wpm_current_bowler_card(match_id)
        try:
            from services.match_webapp_service import auto_play_bot_turns
            auto_play_bot_turns(db, match_id)
        except Exception:
            logger.exception("auto_play after new-bowler failed")
        return {"ok": True, "message": msg,
                "snapshot": build_snapshot(db, match_id, user.id)}
    except Exception as e:
        logger.exception("webapp_match_new_bowler failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/abandon", methods=["POST"])
@csrf_exempt
def webapp_match_abandon():
    """A participant forfeits the live match. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import abandon_match
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        ok, msg = abandon_match(db, match_id, user.id)
        if not ok:
            return {"ok": False, "message": msg}, 400
        try:
            _broadcast_match_result(match_id, {"text": "Match ended by forfeit."})
        except Exception:
            pass
        return {"ok": True, "message": msg}
    except Exception as e:
        db.rollback()
        logger.exception("webapp_match_abandon failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/webapp/match/scorecard", methods=["POST"])
@csrf_exempt
def webapp_match_scorecard():
    """Full tabbed scorecard for both innings. Body: {match_id}"""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services.match_webapp_service import get_scorecard_any
        data = request.get_json(silent=True) or {}
        match_id = int(data.get("match_id") or 0)
        return get_scorecard_any(db, match_id, user.id)
    except Exception as e:
        logger.exception("webapp_match_scorecard failed")
        return {"ok": False, "error": "internal", "message": str(e)}, 500
    finally:
        db.close()


@app.route("/api/adsgram/reward", methods=["GET"])
@csrf_exempt
def adsgram_postback():
    """Adsgram server-to-server postback endpoint.

    Adsgram fires GET requests here with ?userid=<telegram_id> after a
    user finishes watching a rewarded ad. We log it; the spin endpoint
    claims it later.

    Configure this URL in your Adsgram block as:
      https://your-app.onrender.com/api/adsgram/reward?userid=[userId]

    Adsgram substitutes [userId] with the actual telegram_id before
    making the request. No auth needed — Adsgram's docs don't include
    signed requests; the security model is "obscurity of the URL +
    cooldown limits".
    """
    try:
        userid_raw = request.args.get("userid", "")
        if not userid_raw:
            return {"ok": False, "error": "no_userid"}, 400
        try:
            telegram_id = int(userid_raw)
        except ValueError:
            return {"ok": False, "error": "bad_userid"}, 400

        # Optional: only accept postbacks for users that actually exist
        db = get_session()
        try:
            user = db.query(User).filter(User.telegram_id == telegram_id).first()
            if not user:
                # Still 200 OK so Adsgram doesn't retry, but don't insert a row
                logger.warning(f"Adsgram postback for unknown user {telegram_id}")
                return {"ok": True, "note": "user_not_found"}, 200

            from services import adsgram_service
            adsgram_service.record_postback(
                db, telegram_id,
                source_ip=request.remote_addr,
                query_string=request.query_string.decode("utf-8", errors="ignore"),
            )
            logger.info(f"Adsgram postback recorded for {telegram_id}")
            return {"ok": True}
        finally:
            db.close()
    except Exception as e:
        logger.exception("adsgram_postback failed")
        return {"ok": False, "error": "internal"}, 500


@app.route("/status")
@login_required
def status():
    import json
    checks = {}

    # DB check
    try:
        db = get_session()
        player_count = db.query(func.count(Player.id)).scalar()
        db.close()
        checks["database"] = {"ok": True, "detail": f"{player_count:,} players"}
    except Exception as e:
        checks["database"] = {"ok": False, "detail": str(e)}

    # Data file check
    data_path = os.path.join(os.path.dirname(__file__), "data", "players.json")
    if os.path.exists(data_path):
        size = os.path.getsize(data_path)
        try:
            with open(data_path) as f:
                data = json.load(f)
            checks["data_file"] = {"ok": True, "detail": f"{len(data):,} entries, {size:,} bytes"}
        except Exception as e:
            checks["data_file"] = {"ok": False, "detail": str(e)}
    else:
        checks["data_file"] = {"ok": False, "detail": f"File not found at {data_path}"}

    # Bot token check
    bot_token = os.getenv("BOT_TOKEN", "")
    if bot_token:
        masked = bot_token[:8] + "..." + bot_token[-4:]
        checks["bot_token"] = {"ok": True, "detail": masked}
    else:
        checks["bot_token"] = {"ok": False, "detail": "BOT_TOKEN env var not set"}

    # ENV vars
    checks["database_url"] = {"ok": True, "detail": os.getenv("DATABASE_URL", "sqlite:///cricket_bot.db")}
    checks["admin_password"] = {"ok": bool(os.getenv("ADMIN_PASSWORD")), "detail": "Set" if os.getenv("ADMIN_PASSWORD") else "Using default"}
    checks["port"] = {"ok": True, "detail": os.getenv("PORT", os.getenv("ADMIN_PORT", "5000"))}

    return render_template("status.html", checks=checks)


# ═══════════════════════════════════════════════════════════════════════
# BOT TEAMS ADMIN (for /vsbot)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/bot-teams")
@login_required
def admin_bot_teams_list():
    db = get_session()
    try:
        from services.bot_team_service import team_summary
        teams = db.query(BotTeam).order_by(BotTeam.name).all()
        team_data = [(t, team_summary(db, t.id)) for t in teams]
        return render_template("admin_bot_teams.html", team_data=team_data)
    finally:
        db.close()


@app.route("/bot-teams/new", methods=["GET", "POST"])
@login_required
def admin_bot_team_new():
    db = get_session()
    try:
        if request.method == "POST":
            from services.bot_team_service import create_team
            team, err = create_team(
                db,
                request.form.get("name", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("difficulty", "Medium"),
            )
            if err:
                flash(err, "error")
                return redirect(url_for("admin_bot_team_new"))
            db.commit()
            log_admin(db, "bot_team_create", "bot_team", team.id, team.name,
                      f"Created team {team.name}")
            db.commit()
            flash(f"Team '{team.name}' created.", "info")
            return redirect(url_for("admin_bot_team_edit", team_id=team.id))
        return render_template("admin_bot_team_form.html", team=None)
    finally:
        db.close()


@app.route("/bot-teams/<int:team_id>/edit", methods=["GET", "POST"])
@login_required
def admin_bot_team_edit(team_id):
    db = get_session()
    try:
        from services.bot_team_service import (
            get_team_with_players, update_team, team_summary
        )
        team, rows = get_team_with_players(db, team_id)
        if not team:
            flash("Team not found.", "error")
            return redirect(url_for("admin_bot_teams_list"))

        if request.method == "POST":
            t, err = update_team(
                db, team_id,
                name=request.form.get("name", team.name).strip(),
                description=request.form.get("description", team.description or "").strip(),
                difficulty=request.form.get("difficulty", team.difficulty),
                is_active=bool(request.form.get("is_active")),
            )
            if err:
                flash(err, "error")
            else:
                db.commit()
                log_admin(db, "bot_team_edit", "bot_team", team_id, t.name,
                          f"Edited team {t.name}")
                db.commit()
                flash(f"Team '{t.name}' updated.", "info")
            return redirect(url_for("admin_bot_team_edit", team_id=team_id))

        summary = team_summary(db, team_id)
        all_players = (db.query(Player)
                       .filter(Player.is_active == True)
                       .order_by(Player.name).all())
        return render_template("admin_bot_team_form.html",
                               team=team, rows=rows, summary=summary,
                               all_players=all_players)
    finally:
        db.close()


@app.route("/bot-teams/<int:team_id>/delete", methods=["POST"])
@login_required
def admin_bot_team_delete(team_id):
    db = get_session()
    try:
        from services.bot_team_service import delete_team
        team = db.query(BotTeam).get(team_id)
        if team:
            name = team.name
            ok, err = delete_team(db, team_id)
            if ok:
                db.commit()
                log_admin(db, "bot_team_delete", "bot_team", team_id, name,
                          f"Deleted team {name}")
                db.commit()
                flash(f"Team '{name}' deleted.", "info")
            else:
                flash(err, "error")
    finally:
        db.close()
    return redirect(url_for("admin_bot_teams_list"))


@app.route("/bot-teams/<int:team_id>/add-player", methods=["POST"])
@login_required
def admin_bot_team_add_player(team_id):
    db = get_session()
    try:
        from services.bot_team_service import add_player_to_team
        btp, err = add_player_to_team(db, team_id, int(request.form.get("player_id", 0)))
        if err: flash(err, "error")
        else: db.commit(); flash("Player added.", "info")
    except Exception as e:
        db.rollback(); flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_bot_team_edit", team_id=team_id))


@app.route("/bot-teams/<int:team_id>/remove-player/<int:player_id>", methods=["POST"])
@login_required
def admin_bot_team_remove_player(team_id, player_id):
    db = get_session()
    try:
        from services.bot_team_service import remove_player_from_team
        ok, err = remove_player_from_team(db, team_id, player_id)
        if err: flash(err, "error")
        else: db.commit(); flash("Player removed.", "info")
    finally:
        db.close()
    return redirect(url_for("admin_bot_team_edit", team_id=team_id))


@app.route("/bot-teams/<int:team_id>/move-player", methods=["POST"])
@login_required
def admin_bot_team_move_player(team_id):
    db = get_session()
    try:
        from services.bot_team_service import reorder_player
        ok, err = reorder_player(
            db, team_id,
            int(request.form.get("player_id", 0)),
            int(request.form.get("new_position", 1)),
        )
        if err: flash(err, "error")
        else: db.commit(); flash("Moved.", "info")
    except (ValueError, TypeError):
        flash("Invalid position.", "error")
    finally:
        db.close()
    return redirect(url_for("admin_bot_team_edit", team_id=team_id))


@app.route("/bot-teams/<int:team_id>/set-captain", methods=["POST"])
@login_required
def admin_bot_team_set_captain(team_id):
    db = get_session()
    try:
        from services.bot_team_service import set_captain
        ok, err = set_captain(db, team_id, int(request.form.get("player_id", 0)))
        if err: flash(err, "error")
        else: db.commit(); flash("Captain updated.", "info")
    except (ValueError, TypeError):
        flash("Invalid.", "error")
    finally:
        db.close()
    return redirect(url_for("admin_bot_team_edit", team_id=team_id))


@app.route("/bot-teams/<int:team_id>/bulk-add", methods=["POST"])
@login_required
def admin_bot_team_bulk_add(team_id):
    db = get_session()
    try:
        from services.bot_team_service import bulk_add_players
        names = [l.strip() for l in request.form.get("bulk_text", "").splitlines() if l.strip()]
        if not names:
            flash("No names provided.", "error")
            return redirect(url_for("admin_bot_team_edit", team_id=team_id))
        added, skipped = bulk_add_players(db, team_id, names)
        db.commit()
        msg = f"Added {added} player{'s' if added != 1 else ''}."
        if skipped:
            msg += f" Skipped: {', '.join(skipped[:5])}"
            if len(skipped) > 5: msg += f" (+{len(skipped) - 5} more)"
        flash(msg, "info")
    except Exception as e:
        db.rollback(); flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_bot_team_edit", team_id=team_id))


# ═══════════════════════════════════════════════════════════════════════
# QUESTS ADMIN
# ═══════════════════════════════════════════════════════════════════════

EVENT_KEYS = [
    ("claim", "Claim a player (/claim)"),
    ("gspin", "Spin GSpin (/gspin)"),
    ("daily", "Collect daily (/daily)"),
    ("match_played", "Play a match (any result)"),
    ("match_won", "Win a match"),
    ("runs_scored", "Total runs scored across matches (cumulative)"),
    ("wickets_taken", "Total wickets taken across matches (cumulative)"),
    ("fifty", "Score 50+ in a match"),
    ("hundred", "Score 100+ in a match"),
    ("sixes_hit", "Sixes hit (cumulative across matches)"),
    ("sixes_in_match", "Sixes in a single match (uses MAX, not sum)"),
    ("boundaries_hit", "Boundaries 4s+6s (cumulative)"),
    ("boundaries_in_match", "Boundaries in a single match (uses MAX)"),
    ("wickets_in_match", "Wickets in a single match (uses MAX)"),
    ("runs_in_innings", "Runs in a single innings (uses MAX)"),
    ("hattrick", "Take a hat-trick"),
    ("maiden_over", "Bowl a maiden over (cumulative)"),
    ("not_out_innings", "Stay not out in an innings (cumulative)"),
    ("allrounder_match", "30+ runs AND 2+ wickets in same match"),
    ("chase_won", "Win a match while batting second"),
    ("clean_spell", "4-over spell with 1+ maiden and 3+ wickets"),
    ("economy_under_4_5", "4+ over spell with economy < 4.5"),
    ("economy_under_5", "4+ over spell with economy < 5.0"),
    ("economy_under_6", "4+ over spell with economy < 6.0"),
    ("economy_under_7", "4+ over spell with economy < 7.0"),
    ("trait_apply", "Apply a trait (/traitapply)"),
    ("trait_buy", "Buy a trait (/traitshop)"),
    ("market_buy", "Buy from /playermarket"),
    ("vsbot_played", "Play a /vsbot match"),
    ("vsbot_won", "Win a /vsbot match"),
    ("manual", "Manual — admin bumps progress directly"),
]


@app.route("/quests/convert_manual", methods=["POST"])
@login_required
def admin_quests_convert_manual():
    """Bulk convert all manual-only quests to auto-tracked or delete them."""
    db = get_session()
    try:
        from convert_manual_quests import convert_manual_quests
        result = convert_manual_quests(db)
        log_admin(db, "quest_convert_manual", "quest", 0, "bulk_convert",
                  f"Converted {result['converted']}, deleted {result['deleted']}, "
                  f"kept manual {result['kept_manual']}")
        db.commit()
        flash(
            f"✅ Manual quest cleanup: "
            f"{result['converted']} converted to auto-tracked, "
            f"{result['deleted']} deleted, "
            f"{result['kept_manual']} still manual.",
            "info",
        )
    except Exception as e:
        db.rollback()
        flash(f"Conversion failed: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_quests_list"))


@app.route("/quests/import", methods=["POST"])
@login_required
def admin_quests_import():
    """Bulk-import quests from data/quests_table.md."""
    import os
    db = get_session()
    try:
        from seed_quests_v2 import import_quests
        md_path = os.path.join(os.path.dirname(__file__), "data", "quests_table.md")
        if not os.path.exists(md_path):
            flash(f"Markdown file not found at {md_path}.", "error")
            return redirect(url_for("admin_quests_list"))
        result = import_quests(db, md_path)
        log_admin(db, "quest_import", "quest", 0, "bulk_import",
                  f"Imported {result['inserted']} quests "
                  f"({result['auto_tracked']} auto-tracked, {result['manual']} manual)")
        db.commit()
        flash(
            f"✅ Imported {result['inserted']} quests "
            f"({result['auto_tracked']} auto-tracked, {result['manual']} manual). "
            f"Skipped {result['skipped_existing']} existing.",
            "info",
        )
    except Exception as e:
        db.rollback()
        flash(f"Import failed: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_quests_list"))


# ══════════════════════════════════════════════════════════════════════
# Pack admin (CRUD)
# ══════════════════════════════════════════════════════════════════════

@app.route("/packs")
@login_required
def admin_packs_list():
    db = get_session()
    try:
        from models import Pack, PackPurchase
        from services.pack_service import count_main_pool, count_bonus_pool
        packs = db.query(Pack).order_by(Pack.slot_number).all()
        rows = []
        for p in packs:
            rows.append({
                "pack": p,
                "main_pool": count_main_pool(db, p),
                "bonus_pool": count_bonus_pool(db, p),
            })
        recent_purchases = (db.query(PackPurchase)
                            .order_by(PackPurchase.purchased_at.desc())
                            .limit(20).all())
        return render_template("admin_packs.html",
                               rows=rows, recent_purchases=recent_purchases)
    finally:
        db.close()


@app.route("/packs/seed_defaults", methods=["POST"])
@login_required
def admin_packs_seed_defaults():
    db = get_session()
    try:
        from services.pack_service import seed_default_packs
        n = seed_default_packs(db)
        db.commit()
        if n:
            log_admin(db, "pack_seed_defaults", "pack", 0, "defaults",
                      f"Seeded {n} default packs")
            db.commit()
            flash(f"✅ Seeded {n} default packs.", "info")
        else:
            flash("ℹ️ All default packs already exist.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_packs_list"))


@app.route("/packs/new", methods=["GET", "POST"])
@login_required
def admin_pack_new():
    db = get_session()
    try:
        from models import Pack
        if request.method == "POST":
            try:
                p = _save_pack_from_form(db, Pack(), is_new=True)
                db.commit()
                log_admin(db, "pack_new", "pack", p.id, p.name,
                          f"Created pack slot={p.slot_number}")
                db.commit()
                flash(f"✅ Created '{p.name}'.", "info")
                return redirect(url_for("admin_packs_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        return render_template("admin_pack_form.html", pack=None)
    finally:
        db.close()


@app.route("/packs/<int:pack_id>/edit", methods=["GET", "POST"])
@login_required
def admin_pack_edit(pack_id):
    db = get_session()
    try:
        from models import Pack
        from services.pack_service import count_main_pool, count_bonus_pool
        p = db.query(Pack).get(pack_id)
        if not p:
            flash("Pack not found.", "error")
            return redirect(url_for("admin_packs_list"))
        if request.method == "POST":
            try:
                _save_pack_from_form(db, p, is_new=False)
                db.commit()
                log_admin(db, "pack_edit", "pack", p.id, p.name,
                          "Updated pack")
                db.commit()
                flash(f"✅ Updated '{p.name}'.", "info")
                return redirect(url_for("admin_packs_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        # GET — show form with pool counts
        return render_template("admin_pack_form.html", pack=p,
                               main_pool=count_main_pool(db, p),
                               bonus_pool=count_bonus_pool(db, p))
    finally:
        db.close()


@app.route("/packs/<int:pack_id>/delete", methods=["POST"])
@login_required
def admin_pack_delete(pack_id):
    db = get_session()
    try:
        from models import Pack
        p = db.query(Pack).get(pack_id)
        if not p:
            flash("Pack not found.", "error")
            return redirect(url_for("admin_packs_list"))
        name = p.name
        db.delete(p)
        db.commit()
        log_admin(db, "pack_delete", "pack", pack_id, name, "Deleted pack")
        db.commit()
        flash(f"🗑️ Deleted '{name}'.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_packs_list"))


def _save_pack_from_form(db, pack, *, is_new=False):
    """Helper: populate pack fields from request.form. Returns the pack."""
    import json as _j
    f = request.form

    pack.slot_number = int(f.get("slot_number") or 1)
    pack.name = f.get("name", "").strip() or "Untitled Pack"
    pack.description = f.get("description", "").strip() or None
    pack.emoji = f.get("emoji", "📦").strip() or "📦"

    pack.cost_coins = int(f.get("cost_coins") or 0)
    pack.cost_quest_points = int(f.get("cost_quest_points") or 0)
    pack.cost_gems = int(f.get("cost_gems") or 0)

    mode = f.get("main_filter_mode", "rating").strip().lower()
    if mode not in ("rating", "version", "both"):
        mode = "rating"
    pack.main_filter_mode = mode

    pack.main_min_rating = max(50, min(100, int(f.get("main_min_rating") or 70)))
    pack.main_max_rating = max(pack.main_min_rating, min(100, int(f.get("main_max_rating") or 99)))
    pack.main_count = max(1, min(10, int(f.get("main_count") or 1)))

    # Weights — comma-separated list of integers
    weights_raw = (f.get("main_weights") or "").strip()
    if weights_raw:
        try:
            weights = [int(x.strip()) for x in weights_raw.split(",") if x.strip()]
            expected = pack.main_max_rating - pack.main_min_rating + 1
            if len(weights) == expected and all(w >= 0 for w in weights):
                pack.main_weights_json = _j.dumps(weights)
            else:
                # Mismatched length — store as null and warn
                pack.main_weights_json = None
                flash(f"⚠️ Weights count ({len(weights)}) doesn't match rating range "
                      f"({expected}). Using uniform.", "error")
        except ValueError:
            pack.main_weights_json = None
            flash("⚠️ Weights must be comma-separated integers. Using uniform.", "error")
    else:
        pack.main_weights_json = None

    # Versions — comma-separated names
    versions_raw = (f.get("main_versions") or "").strip()
    if versions_raw:
        versions = [v.strip() for v in versions_raw.split(",") if v.strip()]
        pack.main_versions_json = _j.dumps(versions) if versions else None
    else:
        pack.main_versions_json = None

    pack.bonus_min_rating = max(50, min(100, int(f.get("bonus_min_rating") or 70)))
    pack.bonus_max_rating = max(pack.bonus_min_rating, min(100, int(f.get("bonus_max_rating") or 80)))
    pack.bonus_count = max(0, min(10, int(f.get("bonus_count") or 0)))

    pack.daily_limit = max(0, int(f.get("daily_limit") or 0))
    pack.is_active = (f.get("is_active") == "on")

    if is_new:
        db.add(pack)
    db.flush()
    return pack


# ══════════════════════════════════════════════════════════════════════
# Tours admin (read + force-expire + delete)
# ══════════════════════════════════════════════════════════════════════

@app.route("/tours")
@login_required
def admin_tours_list():
    db = get_session()
    try:
        from models import Tour, TourMatch
        status_filter = request.args.get("status", "all")
        page = int(request.args.get("page", 1))
        per = 25

        q = db.query(Tour)
        if status_filter != "all":
            q = q.filter(Tour.status == status_filter)
        q = q.order_by(Tour.created_at.desc())

        total = q.count()
        tours = q.offset((page - 1) * per).limit(per).all()

        # Pre-load user labels + match progress for the table
        rows = []
        from models import User
        for t in tours:
            u1 = db.query(User).get(t.user1_id)
            u2 = db.query(User).get(t.user2_id)
            done_count = (db.query(func.count(TourMatch.id))
                          .filter(TourMatch.tour_id == t.id,
                                  TourMatch.status == "done").scalar()) or 0
            rows.append({
                "tour": t,
                "u1_label": f"@{u1.username}" if u1 and u1.username else (
                    u1.first_name if u1 else "?"),
                "u2_label": f"@{u2.username}" if u2 and u2.username else (
                    u2.first_name if u2 else "?"),
                "u1_id": u1.id if u1 else None,
                "u2_id": u2.id if u2 else None,
                "matches_done": done_count,
            })

        # Stat summary (status counts)
        stats = {}
        for st in ("pending", "active", "completed", "expired", "declined"):
            stats[st] = (db.query(func.count(Tour.id))
                         .filter(Tour.status == st).scalar()) or 0
        stats["total"] = sum(stats.values())

        total_pages = max(1, (total + per - 1) // per)
        return render_template("admin_tours.html",
                               rows=rows, stats=stats,
                               status_filter=status_filter,
                               page=page, total_pages=total_pages)
    finally:
        db.close()


@app.route("/tours/<int:tour_id>")
@login_required
def admin_tour_detail(tour_id):
    db = get_session()
    try:
        from models import Tour, TourMatch, User, Match
        from services.tour_service import get_tour_stats, get_tour_matches

        tour = db.query(Tour).get(tour_id)
        if not tour:
            flash("Tour not found.", "error")
            return redirect(url_for("admin_tours_list"))

        u1 = db.query(User).get(tour.user1_id)
        u2 = db.query(User).get(tour.user2_id)

        # Match list with linked Match info (winner, scores)
        tour_matches = get_tour_matches(db, tour_id)
        match_rows = []
        for tm in tour_matches:
            entry = {
                "tm": tm,
                "winner_label": None,
                "match": None,
            }
            if tm.match_id:
                m = db.query(Match).get(tm.match_id)
                entry["match"] = m
                if m and m.winner_id:
                    w = db.query(User).get(m.winner_id)
                    entry["winner_label"] = (f"@{w.username}" if w and w.username
                                              else (w.first_name if w else "?"))
            match_rows.append(entry)

        # Tour leaderboard
        stats = get_tour_stats(db, tour_id, top_n=5)

        # Labels
        def _ulabel(u):
            if not u: return "?"
            return f"@{u.username}" if u.username else (u.first_name or "?")

        return render_template("admin_tour_detail.html",
                               tour=tour,
                               u1=u1, u2=u2,
                               u1_label=_ulabel(u1), u2_label=_ulabel(u2),
                               match_rows=match_rows,
                               stats=stats)
    finally:
        db.close()


@app.route("/tours/<int:tour_id>/expire", methods=["POST"])
@login_required
def admin_tour_force_expire(tour_id):
    """Force a tour to 'expired' (or 'completed' if it had matches played)."""
    db = get_session()
    try:
        from models import Tour, TourMatch
        from services.tour_service import _decide_winner
        tour = db.query(Tour).get(tour_id)
        if not tour:
            flash("Tour not found.", "error")
            return redirect(url_for("admin_tours_list"))
        if tour.status not in ("pending", "active"):
            flash(f"Tour is already {tour.status}.", "error")
            return redirect(url_for("admin_tour_detail", tour_id=tour_id))

        # If at least one match played → completed (winner decided)
        # Otherwise → expired
        if tour.user1_wins or tour.user2_wins:
            tour.status = "completed"
            tour.winner_id = _decide_winner(tour)
        else:
            tour.status = "expired"
        tour.completed_at = datetime.utcnow()
        # Mark unplayed TourMatches as expired
        (db.query(TourMatch)
         .filter(TourMatch.tour_id == tour.id,
                 TourMatch.status.in_(["pending", "playing"]))
         .update({"status": "expired"}, synchronize_session=False))
        db.commit()
        log_admin(db, "tour_expire", "tour", tour_id, f"#{tour_id}",
                  f"Force-expired tour (final score {tour.user1_wins}-{tour.user2_wins})")
        db.commit()
        flash(f"✅ Tour #{tour_id} marked {tour.status}.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_tour_detail", tour_id=tour_id))


@app.route("/tours/<int:tour_id>/delete", methods=["POST"])
@login_required
def admin_tour_delete(tour_id):
    """Permanently delete a tour and its TourMatch rows.

    NOTE: Does NOT delete the underlying Match records or per-match stats —
    those represent real cricket history and should be preserved.
    """
    db = get_session()
    try:
        from models import Tour, TourMatch
        tour = db.query(Tour).get(tour_id)
        if not tour:
            flash("Tour not found.", "error")
            return redirect(url_for("admin_tours_list"))

        # Delete TourMatch rows first (FK)
        db.query(TourMatch).filter(TourMatch.tour_id == tour_id).delete(
            synchronize_session=False)
        db.delete(tour)
        db.commit()
        log_admin(db, "tour_delete", "tour", tour_id, f"#{tour_id}",
                  "Hard-deleted tour")
        db.commit()
        flash(f"🗑️ Tour #{tour_id} deleted.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_tours_list"))


@app.route("/quests")
@login_required
def admin_quests_list():
    db = get_session()
    try:
        quests = db.query(Quest).order_by(Quest.quest_type, Quest.sort_order, Quest.id).all()
        # Group by type
        daily = [q for q in quests if q.quest_type == "daily"]
        monthly = [q for q in quests if q.quest_type == "monthly"]
        return render_template("admin_quests.html",
                               daily_quests=daily, monthly_quests=monthly)
    finally:
        db.close()


@app.route("/quests/new", methods=["GET", "POST"])
@login_required
def admin_quest_new():
    db = get_session()
    try:
        if request.method == "POST":
            try:
                q = Quest(
                    name=request.form.get("name", "").strip(),
                    description=request.form.get("description", "").strip(),
                    quest_type=request.form.get("quest_type", "daily"),
                    event_key=request.form.get("event_key", "claim"),
                    target_count=max(1, int(request.form.get("target_count", "1") or 1)),
                    reward_points=int(request.form.get("reward_points", "5") or 5),
                    reward_coins=int(request.form.get("reward_coins", "0") or 0),
                    reward_gems=int(request.form.get("reward_gems", "0") or 0),
                    is_active=bool(request.form.get("is_active")),
                    emoji=request.form.get("emoji", "🎯") or "🎯",
                    sort_order=int(request.form.get("sort_order", "0") or 0),
                )
                if not q.name:
                    flash("Name is required.", "error")
                    return redirect(url_for("admin_quest_new"))
                db.add(q)
                db.commit()
                log_admin(db, "quest_create", "quest", q.id, q.name,
                          f"Created {q.quest_type} quest: {q.name}")
                db.commit()
                flash(f"Quest '{q.name}' created.", "info")
                return redirect(url_for("admin_quests_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
                return redirect(url_for("admin_quest_new"))
        return render_template("admin_quest_form.html", quest=None, event_keys=EVENT_KEYS)
    finally:
        db.close()


@app.route("/quests/<int:quest_id>/edit", methods=["GET", "POST"])
@login_required
def admin_quest_edit(quest_id):
    db = get_session()
    try:
        q = db.query(Quest).get(quest_id)
        if not q:
            flash("Quest not found.", "error")
            return redirect(url_for("admin_quests_list"))

        if request.method == "POST":
            try:
                q.name = request.form.get("name", q.name).strip()
                q.description = request.form.get("description", q.description).strip()
                q.quest_type = request.form.get("quest_type", q.quest_type)
                q.event_key = request.form.get("event_key", q.event_key)
                q.target_count = max(1, int(request.form.get("target_count") or 1))
                q.reward_points = int(request.form.get("reward_points") or 0)
                q.reward_coins = int(request.form.get("reward_coins") or 0)
                q.reward_gems = int(request.form.get("reward_gems") or 0)
                q.is_active = bool(request.form.get("is_active"))
                q.emoji = request.form.get("emoji", q.emoji) or "🎯"
                q.sort_order = int(request.form.get("sort_order") or 0)
                db.commit()
                log_admin(db, "quest_edit", "quest", quest_id, q.name,
                          f"Edited quest {q.name}")
                db.commit()
                flash(f"Quest '{q.name}' updated.", "info")
                return redirect(url_for("admin_quests_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        return render_template("admin_quest_form.html", quest=q, event_keys=EVENT_KEYS)
    finally:
        db.close()


@app.route("/quests/<int:quest_id>/delete", methods=["POST"])
@login_required
def admin_quest_delete(quest_id):
    db = get_session()
    try:
        q = db.query(Quest).get(quest_id)
        if q:
            name = q.name
            # Also delete any user progress on this quest
            db.query(UserQuestProgress).filter(UserQuestProgress.quest_id == quest_id).delete()
            db.delete(q)
            db.commit()
            log_admin(db, "quest_delete", "quest", quest_id, name, f"Deleted {name}")
            db.commit()
            flash(f"Quest '{name}' deleted.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_quests_list"))


@app.route("/quests/<int:quest_id>/toggle", methods=["POST"])
@login_required
def admin_quest_toggle(quest_id):
    db = get_session()
    try:
        q = db.query(Quest).get(quest_id)
        if q:
            q.is_active = not q.is_active
            db.commit()
            flash(f"'{q.name}' → {'active' if q.is_active else 'inactive'}.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_quests_list"))


# ═══════════════════════════════════════════════════════════════════════
# COMMENTARY ADMIN
# ═══════════════════════════════════════════════════════════════════════

@app.route("/commentary")
@login_required
def admin_commentary_list():
    db = get_session()
    try:
        from services.commentary_service import list_event_keys, get_stats
        # Filter
        event_filter = request.args.get("event", "all")
        q = db.query(CommentaryEntry).order_by(CommentaryEntry.event_key, CommentaryEntry.id)
        if event_filter and event_filter != "all":
            q = q.filter(CommentaryEntry.event_key == event_filter)
        entries = q.all()
        stats = get_stats(db)
        keys = list_event_keys()
        return render_template("admin_commentary.html",
                               entries=entries, stats=stats, keys=keys,
                               event_filter=event_filter)
    finally:
        db.close()


@app.route("/commentary/new", methods=["GET", "POST"])
@login_required
def admin_commentary_new():
    db = get_session()
    try:
        if request.method == "POST":
            event_key = request.form.get("event_key", "").strip()
            text = request.form.get("text", "").strip()
            weight = int(request.form.get("weight", "1") or 1)
            is_active = bool(request.form.get("is_active"))
            if not event_key or not text:
                flash("Event key and text are required.", "error")
                return redirect(url_for("admin_commentary_new"))
            entry = CommentaryEntry(event_key=event_key, text=text,
                                    weight=weight, is_active=is_active)
            db.add(entry); db.commit()
            log_admin(db, "commentary_create", "commentary", entry.id, event_key,
                      f"Added commentary line for {event_key}")
            db.commit()
            flash("Commentary line added.", "info")
            return redirect(url_for("admin_commentary_list", event=event_key))
        from services.commentary_service import list_event_keys
        return render_template("admin_commentary_form.html",
                               entry=None, keys=list_event_keys())
    finally:
        db.close()


@app.route("/commentary/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def admin_commentary_edit(entry_id):
    db = get_session()
    try:
        entry = db.query(CommentaryEntry).get(entry_id)
        if not entry:
            flash("Entry not found.", "error")
            return redirect(url_for("admin_commentary_list"))
        if request.method == "POST":
            entry.event_key = request.form.get("event_key", entry.event_key).strip()
            entry.text = request.form.get("text", entry.text).strip()
            entry.weight = int(request.form.get("weight", entry.weight) or 1)
            entry.is_active = bool(request.form.get("is_active"))
            db.commit()
            log_admin(db, "commentary_edit", "commentary", entry_id, entry.event_key,
                      f"Edited commentary {entry_id}")
            db.commit()
            flash("Saved.", "info")
            return redirect(url_for("admin_commentary_list", event=entry.event_key))
        from services.commentary_service import list_event_keys
        return render_template("admin_commentary_form.html",
                               entry=entry, keys=list_event_keys())
    finally:
        db.close()


@app.route("/commentary/<int:entry_id>/delete", methods=["POST"])
@login_required
def admin_commentary_delete(entry_id):
    db = get_session()
    try:
        entry = db.query(CommentaryEntry).get(entry_id)
        if entry:
            event_key = entry.event_key
            db.delete(entry); db.commit()
            log_admin(db, "commentary_delete", "commentary", entry_id, event_key,
                      f"Deleted commentary {entry_id}")
            db.commit()
            flash("Deleted.", "info")
            return redirect(url_for("admin_commentary_list", event=event_key))
    finally:
        db.close()
    return redirect(url_for("admin_commentary_list"))


@app.route("/commentary/import", methods=["GET", "POST"])
@login_required
def admin_commentary_import():
    db = get_session()
    try:
        if request.method == "POST":
            from services.commentary_service import parse_commentary_py, bulk_import
            replace = bool(request.form.get("replace"))
            file = request.files.get("file")
            text_content = request.form.get("text_content", "").strip()
            content = ""
            if file and file.filename:
                content = file.read().decode("utf-8", errors="replace")
            elif text_content:
                content = text_content
            else:
                flash("Provide a file or paste content.", "error")
                return redirect(url_for("admin_commentary_import"))
            try:
                parsed = parse_commentary_py(content)
            except ValueError as e:
                flash(f"Parse error: {e}", "error")
                return redirect(url_for("admin_commentary_import"))
            added, skipped = bulk_import(db, parsed, replace=replace)
            db.commit()
            log_admin(db, "commentary_import", "commentary", 0, "bulk",
                      f"Bulk import: +{added} added, {skipped} skipped, replace={replace}")
            db.commit()
            flash(f"✅ Imported {added} lines ({skipped} skipped). Replace mode: {replace}", "info")
            return redirect(url_for("admin_commentary_list"))
        return render_template("admin_commentary_import.html")
    finally:
        db.close()


@app.route("/commentary/export.py")
@login_required
def admin_commentary_export():
    db = get_session()
    try:
        from services.commentary_service import export_as_py
        content = export_as_py(db)
        from flask import Response
        return Response(content, mimetype="text/x-python",
                        headers={"Content-Disposition": "attachment; filename=cricket_commentary.py"})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# NOTIFICATIONS ADMIN
# ═══════════════════════════════════════════════════════════════════════

# Cross-thread bot reference — set by bot.py at startup so Flask can send
_BOT_REF = {"bot": None, "loop": None}


def set_bot_for_admin(bot, loop):
    """Called by bot.py at startup with the bot instance + asyncio loop.
    Lets the Flask 'Send Now' button schedule sends on the bot's event loop.
    """
    _BOT_REF["bot"] = bot
    _BOT_REF["loop"] = loop


@app.route("/notifications")
@login_required
def admin_notifications_list():
    db = get_session()
    try:
        schedules = (db.query(NotificationSchedule)
                     .order_by(NotificationSchedule.is_active.desc(),
                               NotificationSchedule.fire_hour, NotificationSchedule.id).all())
        # Recent log
        recent = (db.query(NotificationLog)
                  .order_by(NotificationLog.sent_at.desc()).limit(20).all())
        # Map schedule_id → schedule.name
        sched_names = {s.id: s.name for s in db.query(NotificationSchedule).all()}
        return render_template("admin_notifications.html",
                               schedules=schedules, recent_logs=recent,
                               sched_names=sched_names)
    finally:
        db.close()


@app.route("/notifications/new", methods=["GET", "POST"])
@login_required
def admin_notification_new():
    db = get_session()
    try:
        if request.method == "POST":
            try:
                ns = NotificationSchedule(
                    name=request.form.get("name", "").strip() or "Untitled",
                    message=request.form.get("message", "").strip(),
                    schedule_type=request.form.get("schedule_type", "daily"),
                    fire_hour=int(request.form.get("fire_hour", 18) or 18),
                    fire_minute=int(request.form.get("fire_minute", 0) or 0),
                    interval_hours=int(request.form.get("interval_hours", 24) or 24),
                    window_start_hour=int(request.form.get("window_start_hour", 10) or 10),
                    window_end_hour=int(request.form.get("window_end_hour", 22) or 22),
                    target_filter=request.form.get("target_filter", "all"),
                    is_active=bool(request.form.get("is_active")),
                )
                if not ns.message:
                    flash("Message required.", "error")
                    return redirect(url_for("admin_notification_new"))
                db.add(ns); db.commit()
                log_admin(db, "notif_create", "notification", ns.id, ns.name,
                          f"Created notification: {ns.name}")
                db.commit()
                flash(f"Notification '{ns.name}' created.", "info")
                return redirect(url_for("admin_notifications_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
                return redirect(url_for("admin_notification_new"))
        return render_template("admin_notification_form.html", schedule=None)
    finally:
        db.close()


@app.route("/notifications/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def admin_notification_edit(sid):
    db = get_session()
    try:
        ns = db.query(NotificationSchedule).get(sid)
        if not ns:
            flash("Not found.", "error")
            return redirect(url_for("admin_notifications_list"))
        if request.method == "POST":
            try:
                ns.name = request.form.get("name", ns.name).strip()
                ns.message = request.form.get("message", ns.message).strip()
                ns.schedule_type = request.form.get("schedule_type", ns.schedule_type)
                ns.fire_hour = int(request.form.get("fire_hour", ns.fire_hour) or 0)
                ns.fire_minute = int(request.form.get("fire_minute", ns.fire_minute) or 0)
                ns.interval_hours = int(request.form.get("interval_hours", ns.interval_hours) or 24)
                ns.window_start_hour = int(request.form.get("window_start_hour", ns.window_start_hour) or 0)
                ns.window_end_hour = int(request.form.get("window_end_hour", ns.window_end_hour) or 0)
                ns.target_filter = request.form.get("target_filter", ns.target_filter)
                ns.is_active = bool(request.form.get("is_active"))
                db.commit()
                log_admin(db, "notif_edit", "notification", sid, ns.name,
                          f"Edited notification {sid}")
                db.commit()
                flash("Saved.", "info")
                return redirect(url_for("admin_notifications_list"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        return render_template("admin_notification_form.html", schedule=ns)
    finally:
        db.close()


@app.route("/notifications/<int:sid>/delete", methods=["POST"])
@login_required
def admin_notification_delete(sid):
    db = get_session()
    try:
        ns = db.query(NotificationSchedule).get(sid)
        if ns:
            name = ns.name
            db.delete(ns); db.commit()
            log_admin(db, "notif_delete", "notification", sid, name, f"Deleted {name}")
            db.commit()
            flash(f"Deleted '{name}'.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_notifications_list"))


@app.route("/notifications/<int:sid>/toggle", methods=["POST"])
@login_required
def admin_notification_toggle(sid):
    db = get_session()
    try:
        ns = db.query(NotificationSchedule).get(sid)
        if ns:
            ns.is_active = not ns.is_active
            db.commit()
            flash(f"'{ns.name}' is now {'active' if ns.is_active else 'inactive'}.", "info")
    finally:
        db.close()
    return redirect(url_for("admin_notifications_list"))


@app.route("/notifications/<int:sid>/send_now", methods=["POST"])
@login_required
def admin_notification_send_now(sid):
    """Manually fire a schedule immediately (ignores time window)."""
    bot = _BOT_REF.get("bot")
    loop = _BOT_REF.get("loop")
    if not bot or not loop:
        flash("Bot not running — cannot send.", "error")
        return redirect(url_for("admin_notifications_list"))

    # Run the async send on the bot's event loop, blocking until done
    import asyncio as _asyncio
    db = get_session()
    try:
        ns = db.query(NotificationSchedule).get(sid)
        if not ns:
            flash("Not found.", "error")
            return redirect(url_for("admin_notifications_list"))

        async def _do():
            from services.notification_service import fire_one_off
            from database import get_session as _gs
            ses = _gs()
            try:
                sent, failed = await fire_one_off(bot, ses, sid)
                ses.commit()
                return sent, failed
            finally:
                ses.close()

        future = _asyncio.run_coroutine_threadsafe(_do(), loop)
        try:
            sent, failed = future.result(timeout=120)
            log_admin(db, "notif_send_now", "notification", sid, ns.name,
                      f"Manual send: {sent} delivered, {failed} failed")
            db.commit()
            flash(f"✅ Sent to {sent} users ({failed} failed).", "info")
        except Exception as e:
            flash(f"Send error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_notifications_list"))


@app.route("/notifications/seed_starters", methods=["POST"])
@login_required
def admin_notification_seed_starters():
    """Quick-add the 6 built-in FOMO templates."""
    db = get_session()
    try:
        from services.notification_service import STARTER_TEMPLATES
        added = 0
        existing_names = {s.name for s in db.query(NotificationSchedule).all()}
        for t in STARTER_TEMPLATES:
            if t["name"] in existing_names:
                continue
            ns = NotificationSchedule(
                name=t["name"], message=t["message"],
                schedule_type="daily",
                fire_hour=t["fire_hour"], fire_minute=t["fire_minute"],
                window_start_hour=t["window_start_hour"],
                window_end_hour=t["window_end_hour"],
                target_filter=t["target_filter"],
                is_active=False,  # admin manually activates after review
            )
            db.add(ns); added += 1
        db.commit()
        log_admin(db, "notif_seed", "notification", 0, "starter_pack",
                  f"Seeded {added} starter notifications")
        db.commit()
        flash(f"✅ Added {added} starter templates (inactive — review and activate them).", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_notifications_list"))


# ═══════════════════════════════════════════════════════════════════════
# CLAIM RARITY TIER ADMIN — control chance per rating band
# ═══════════════════════════════════════════════════════════════════════

# Default starter set if admin clicks "Reset to defaults"
DEFAULT_RARITY_TIERS = [
    {"label": "Bronze",    "rating_min": 50, "rating_max": 59, "probability": 26.0,  "emoji": "🟫", "sort_order": 1},
    {"label": "Silver",    "rating_min": 60, "rating_max": 69, "probability": 25.0,  "emoji": "⚪", "sort_order": 2},
    {"label": "Super",     "rating_min": 70, "rating_max": 79, "probability": 38.0,  "emoji": "🟦", "sort_order": 3},
    {"label": "Rare",      "rating_min": 80, "rating_max": 84, "probability": 6.0,   "emoji": "🟩", "sort_order": 4},
    {"label": "Epic",      "rating_min": 85, "rating_max": 89, "probability": 3.5,   "emoji": "🟪", "sort_order": 5},
    {"label": "Legendary", "rating_min": 90, "rating_max": 94, "probability": 1.45,  "emoji": "🟨", "sort_order": 6},
    {"label": "Ultimate",  "rating_min": 95, "rating_max": 100, "probability": 0.05, "emoji": "⭐", "sort_order": 7},
]


@app.route("/rarity")
@login_required
def admin_rarity_list():
    db = get_session()
    try:
        tiers = (db.query(ClaimRarityTier)
                 .order_by(ClaimRarityTier.sort_order, ClaimRarityTier.id).all())
        # Compute total + normalized %
        total_active = sum(t.probability for t in tiers if t.is_active)
        # For each tier, count actual players in range so admin sees pool size
        from models import Player
        pool = {}
        for t in tiers:
            count = (db.query(Player)
                     .filter(Player.rating >= t.rating_min,
                             Player.rating <= t.rating_max,
                             Player.is_active == True).count())
            pool[t.id] = count
        return render_template("admin_rarity.html",
                               tiers=tiers, total_active=total_active, pool=pool)
    finally:
        db.close()


@app.route("/rarity/save", methods=["POST"])
@login_required
def admin_rarity_save():
    """Bulk-save all tiers in one form submit."""
    db = get_session()
    try:
        tier_ids = request.form.getlist("tier_id")
        for tid_str in tier_ids:
            tid = int(tid_str)
            t = db.query(ClaimRarityTier).get(tid)
            if not t:
                continue
            try:
                t.label = request.form.get(f"label_{tid}", t.label).strip() or t.label
                t.rating_min = int(request.form.get(f"rating_min_{tid}", t.rating_min) or 50)
                t.rating_max = int(request.form.get(f"rating_max_{tid}", t.rating_max) or 100)
                t.probability = float(request.form.get(f"probability_{tid}", t.probability) or 0)
                t.sort_order = int(request.form.get(f"sort_order_{tid}", t.sort_order) or 0)
                t.emoji = request.form.get(f"emoji_{tid}", t.emoji)[:10] or "🃏"
                t.is_active = bool(request.form.get(f"is_active_{tid}"))
                # Sanity-clamp
                if t.rating_min < 50: t.rating_min = 50
                if t.rating_max > 100: t.rating_max = 100
                if t.rating_min > t.rating_max:
                    t.rating_min, t.rating_max = t.rating_max, t.rating_min
                if t.probability < 0: t.probability = 0
            except Exception as e:
                flash(f"Tier {tid} skipped (bad input): {e}", "error")
        db.commit()
        log_admin(db, "rarity_save", "rarity", 0, "bulk", "Saved rarity tiers")
        db.commit()
        flash("✅ Rarity tiers saved.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_rarity_list"))


@app.route("/rarity/new", methods=["POST"])
@login_required
def admin_rarity_new():
    db = get_session()
    try:
        max_so = (db.query(func.coalesce(func.max(ClaimRarityTier.sort_order), 0)).scalar() or 0)
        t = ClaimRarityTier(
            label="New Tier",
            rating_min=50, rating_max=59,
            probability=10.0,
            sort_order=max_so + 1,
            is_active=False,  # admin must review + activate
            emoji="🃏",
        )
        db.add(t); db.commit()
        flash("New tier added (inactive — review and enable).", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_rarity_list"))


@app.route("/rarity/<int:tid>/delete", methods=["POST"])
@login_required
def admin_rarity_delete(tid):
    db = get_session()
    try:
        t = db.query(ClaimRarityTier).get(tid)
        if t:
            label = t.label
            db.delete(t); db.commit()
            log_admin(db, "rarity_delete", "rarity", tid, label, f"Deleted {label}")
            db.commit()
            flash(f"Deleted '{label}'.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_rarity_list"))


@app.route("/rarity/reset_defaults", methods=["POST"])
@login_required
def admin_rarity_reset_defaults():
    """Wipe + reinsert the default 7 tiers. Useful after experimenting."""
    db = get_session()
    try:
        db.query(ClaimRarityTier).delete()
        for t in DEFAULT_RARITY_TIERS:
            db.add(ClaimRarityTier(**t, is_active=True))
        db.commit()
        log_admin(db, "rarity_reset", "rarity", 0, "defaults", "Reset to defaults")
        db.commit()
        flash("✅ Reset to default 7 tiers.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_rarity_list"))


@app.route("/rarity/simulate", methods=["GET"])
@login_required
def admin_rarity_simulate():
    """Simulate 10,000 pulls and show actual distribution.
    Lets the admin visualize the impact of their config before committing."""
    db = get_session()
    try:
        from services.player_service import get_random_player_by_rarity
        N = 10000
        bands = {}  # rating_band → count
        # Pre-compute band labels from active tiers
        tiers = (db.query(ClaimRarityTier)
                 .filter(ClaimRarityTier.is_active == True)
                 .order_by(ClaimRarityTier.sort_order, ClaimRarityTier.id).all())
        if not tiers:
            flash("No active tiers — set some up first.", "error")
            return redirect(url_for("admin_rarity_list"))

        for _ in range(N):
            p = get_random_player_by_rarity(db)
            if not p:
                continue
            for t in tiers:
                if t.rating_min <= p.rating <= t.rating_max:
                    bands[t.label] = bands.get(t.label, 0) + 1
                    break
            else:
                bands["(out of range)"] = bands.get("(out of range)", 0) + 1

        return render_template("admin_rarity_simulate.html",
                               bands=bands, total=N, tiers=tiers)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# ECONOMY ADMIN — tunable rewards (coins/gems per match, daily, gspin, debut)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/economy", methods=["GET", "POST"])
@login_required
def admin_economy():
    db = get_session()
    try:
        from services.config_service import get_config, save_config, DEFAULTS
        if request.method == "POST":
            try:
                updates = {
                    "match_win_coins_per_over": int(request.form.get("match_win_coins_per_over", 300)),
                    "match_win_gems_per_over": float(request.form.get("match_win_gems_per_over", 1.0)),
                    "match_loss_coins_per_over": int(request.form.get("match_loss_coins_per_over", 150)),
                    "match_loss_gems_per_over": float(request.form.get("match_loss_gems_per_over", 0.5)),
                    "gspin_gem_min": int(request.form.get("gspin_gem_min", 5)),
                    "gspin_gem_max": int(request.form.get("gspin_gem_max", 50)),
                    "daily_coins": int(request.form.get("daily_coins", 1000)),
                    "daily_gems": int(request.form.get("daily_gems", 0)),
                    "daily_streak_bonus_coins": int(request.form.get("daily_streak_bonus_coins", 200)),
                    "daily_streak_bonus_gems": int(request.form.get("daily_streak_bonus_gems", 0)),
                    "spin_ad_quota": max(0, min(99, int(request.form.get("spin_ad_quota", 5)))),
                    "daily_ad_quota": max(0, min(99, int(request.form.get("daily_ad_quota", 5)))),
                    "debut_coins": int(request.form.get("debut_coins", 100000)),
                    "debut_gems": int(request.form.get("debut_gems", 20)),
                }
                save_config(db, updates, updated_by=session.get("admin_user", "admin"))
                db.commit()
                log_admin(db, "economy_save", "config", 0, "economy", "Updated economy config")
                db.commit()
                flash("✅ Economy config saved.", "info")
                return redirect(url_for("admin_economy"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        cfg = get_config(db)
        return render_template("admin_economy.html", cfg=cfg, defaults=DEFAULTS)
    finally:
        db.close()


@app.route("/economy/reset", methods=["POST"])
@login_required
def admin_economy_reset():
    db = get_session()
    try:
        from services.config_service import reset_to_defaults
        reset_to_defaults(db, updated_by=session.get("admin_user", "admin"))
        db.commit()
        log_admin(db, "economy_reset", "config", 0, "economy", "Reset to defaults")
        db.commit()
        flash("✅ Reset to defaults.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_economy"))


# ═══════════════════════════════════════════════════════════════════════
# SCORECARD APPEARANCE — color customization for innings cards
# ═══════════════════════════════════════════════════════════════════════

# Curated color presets — chosen for good contrast on the dark scorecard bg
SCORECARD_COLOR_PRESETS = [
    ("Lava Red",     "#c41e3a"),   # default innings 1
    ("Teal",         "#00c9a7"),   # default innings 2
    ("Trophy Gold",  "#fbbf24"),
    ("Royal Purple", "#8b5cf6"),
    ("Ocean Blue",   "#3b82f6"),
    ("Forest Green", "#22c55e"),
    ("Sunset Orange", "#f97316"),
    ("Hot Pink",     "#ec4899"),
    ("Cyan",         "#06b6d4"),
    ("Lime",         "#84cc16"),
    ("Crimson",      "#dc2626"),
    ("Indigo",       "#6366f1"),
]


@app.route("/settings/matches", methods=["GET", "POST"])
@login_required
def admin_match_settings():
    """Choose one global gameplay style for newly started matches."""
    db = get_session()
    try:
        from services.config_service import MATCH_STYLES, get_config, save_config
        if request.method == "POST":
            match_style = (request.form.get("match_style") or "telegram").lower()
            if match_style not in MATCH_STYLES:
                flash("Invalid match style.", "error")
                return redirect(url_for("admin_match_settings"))
            try:
                challenge_max_overs = max(1, min(20, int(request.form.get("challenge_max_overs", 2))))
            except ValueError:
                flash("Challenge max overs must be a whole number from 1 to 20.", "error")
                return redirect(url_for("admin_match_settings"))
            save_config(db, {"match_style": match_style, "challenge_max_overs": challenge_max_overs},
                        updated_by=session.get("admin_user", "admin"))
            db.commit()
            log_admin(db, "match_style_save", "config", 0, "matches",
                      f"match_style={match_style} challenge_max_overs={challenge_max_overs}")
            db.commit()
            flash("✅ Match gameplay style saved for all new matches.", "info")
            return redirect(url_for("admin_match_settings"))
        return render_template("admin_match_settings.html", cfg=get_config(db))
    except Exception as e:
        db.rollback()
        logger.exception("match settings save failed")
        flash(f"Error: {e}", "error")
        return redirect(url_for("admin_match_settings"))
    finally:
        db.close()


@app.route("/settings/scorecard", methods=["GET", "POST"])
@login_required
def admin_scorecard_settings():
    """Per-innings accent color customization for scorecard graphics."""
    db = get_session()
    try:
        from services.config_service import get_config, save_config
        from services.scorecard_card import SCORECARD_TEXT_FIELDS, normalize_scorecard_text_settings
        import re as _re
        import json as _json

        def _validate_hex(s, fallback):
            """Accept #rrggbb (7 chars) only."""
            if not s:
                return fallback
            s = s.strip()
            if _re.fullmatch(r"#[0-9a-fA-F]{6}", s):
                return s.lower()
            return fallback

        if request.method == "POST":
            try:
                c1 = _validate_hex(
                    request.form.get("scorecard_color_inn1"), "#c41e3a")
                c2 = _validate_hex(
                    request.form.get("scorecard_color_inn2"), "#00c9a7")
                raw_text_settings = {}
                for card_type in ("batting", "bowling", "summary"):
                    raw_text_settings[card_type] = {}
                    prefix = f"{card_type}_"
                    for key in request.form.getlist(f"{card_type}_field_keys"):
                        raw_text_settings[card_type][key] = {
                            "text": request.form.get(f"{prefix}{key}_text"),
                            "font": request.form.get(f"{prefix}{key}_font"),
                            "size": request.form.get(f"{prefix}{key}_size"),
                            "x": request.form.get(f"{prefix}{key}_x"),
                            "y": request.form.get(f"{prefix}{key}_y"),
                        }
                text_settings = normalize_scorecard_text_settings(raw_text_settings)
                from services.config_service import WPM_RESULT_CARD_TOKENS
                chosen_cards = [t for t in request.form.getlist("wpm_result_cards")
                                if t in WPM_RESULT_CARD_TOKENS]
                wpm_result_cards = ",".join(chosen_cards) if chosen_cards else "summary"
                save_config(db, {
                    "scorecard_color_inn1": c1,
                    "scorecard_color_inn2": c2,
                    "scorecard_text_settings": _json.dumps(text_settings, separators=(",", ":")),
                    "wpm_result_cards": wpm_result_cards,
                }, updated_by=session.get("admin_user", "admin"))
                db.commit()
                log_admin(db, "scorecard_settings_save", "config", 0,
                          "scorecard", f"inn1={c1} inn2={c2} cards={wpm_result_cards}")
                db.commit()
                flash("✅ Scorecard settings saved.", "info")
                return redirect(url_for("admin_scorecard_settings"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")

        cfg = get_config(db)
        from services.config_service import get_wpm_result_cards
        return render_template("admin_scorecard_settings.html",
                               cfg=cfg, presets=SCORECARD_COLOR_PRESETS,
                               text_settings=normalize_scorecard_text_settings(cfg.get("scorecard_text_settings")),
                               text_fields=SCORECARD_TEXT_FIELDS,
                               wpm_result_cards=get_wpm_result_cards(db))
    finally:
        db.close()


@app.route("/settings/scorecard/preview")
@login_required
def admin_scorecard_preview():
    """Render a sample scorecard PNG with current colors for live preview.

    Returns a small sample card so admins can see the effect of color changes
    without playing a real match.
    """
    db = get_session()
    try:
        from services.config_service import get_config
        from services.scorecard_card import (
            SCORECARD_TEXT_FIELDS,
            generate_batting_scorecard, generate_bowling_scorecard,
            normalize_scorecard_text_settings,
        )
        import re as _re
        cfg = get_config(db)
        innings = request.args.get("innings", "1")
        is_first = (innings == "1")
        card_type = (request.args.get("card_type") or "batting").lower()
        raw_text_settings = {}
        has_live_text_controls = False
        for ct in ("batting", "bowling", "summary"):
            raw_text_settings[ct] = {}
            for field in SCORECARD_TEXT_FIELDS.get(ct, []):
                key = field[0]
                prefix = f"{ct}_{key}_"
                if any((prefix + suffix) in request.args for suffix in ("text", "font", "size", "x", "y")):
                    has_live_text_controls = True
                    raw_text_settings[ct][key] = {
                        "text": request.args.get(prefix + "text"),
                        "font": request.args.get(prefix + "font"),
                        "size": request.args.get(prefix + "size"),
                        "x": request.args.get(prefix + "x"),
                        "y": request.args.get(prefix + "y"),
                    }
        text_settings = (normalize_scorecard_text_settings(raw_text_settings)
                         if has_live_text_controls else cfg.get("scorecard_text_settings"))

        # Sample data — 5 batsmen with each status type
        sample_rows = [
            {"rating": 92, "name": "Captain Star", "dismissal": "not out",
             "runs": 78, "balls": 54, "fours": 7, "sixes": 3, "strike_rate": 144.4,
             "status": "not_out"},
            {"rating": 88, "name": "Top Order Bat", "dismissal": "c slip b spinner",
             "runs": 42, "balls": 31, "fours": 4, "sixes": 1, "strike_rate": 135.5,
             "status": "out"},
            {"rating": 85, "name": "Middle Order", "dismissal": "b pacer",
             "runs": 21, "balls": 18, "fours": 2, "sixes": 0, "strike_rate": 116.7,
             "status": "out"},
            {"rating": 80, "name": "All Rounder", "dismissal": "not out",
             "runs": 15, "balls": 9, "fours": 1, "sixes": 1, "strike_rate": 166.7,
             "status": "not_out"},
            {"rating": 76, "name": "Tail Ender", "dismissal": "did not bat",
             "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "strike_rate": 0.0,
             "status": "dnb"},
        ]
        requested_color = request.args.get("color")
        saved_color = (cfg.get("scorecard_color_inn1") if is_first
                       else cfg.get("scorecard_color_inn2"))
        preview_color = requested_color if _re.fullmatch(r"#[0-9a-fA-F]{6}", requested_color or "") else saved_color
        if card_type == "summary":
            from services.match_summary_card import generate_match_summary
            png = generate_match_summary(
                inn1_team="Very Touchables", inn1_runs=184, inn1_wickets=8, inn1_overs="20.0",
                inn2_team="DaddyAlec", inn2_runs=185, inn2_wickets=4, inn2_overs="16.5",
                winner_name="DaddyAlec", win_margin_text="by 6 wickets",
                overs_total=20,
                potm_name="Balla Steyn", potm_stats="4/25 (4 OVERS)",
                top_per_team={
                    "inn1": {"team": "Very Touchables", "batters": [
                        {"name": "Kusal Mendis", "runs": 68, "balls": 38, "out": True},
                        {"name": "Gaby Lewis", "runs": 27, "balls": 18, "out": False},
                        {"name": "Harmanpreet Kaur", "runs": 19, "balls": 15, "out": True},
                        {"name": "Shubman Gill", "runs": 18, "balls": 7, "out": True},
                    ], "bowlers": [
                        {"name": "Balla Steyn", "wickets": 4, "runs": 25, "overs": "4"},
                        {"name": "Josh Hazlewood", "wickets": 2, "runs": 30, "overs": "4"},
                        {"name": "Mitchell Starc", "wickets": 1, "runs": 42, "overs": "4"},
                        {"name": "Amelia Kerr", "wickets": 1, "runs": 44, "overs": "4"},
                    ]},
                    "inn2": {"team": "DaddyAlec", "batters": [
                        {"name": "Virat Kohli", "runs": 79, "balls": 41, "out": True},
                        {"name": "Shreyas Iyer", "runs": 27, "balls": 15, "out": True},
                        {"name": "Sai Sudharsan", "runs": 25, "balls": 21, "out": True},
                        {"name": "Sachin Tendulkar", "runs": 24, "balls": 10, "out": False},
                    ], "bowlers": [
                        {"name": "Shane Warne", "wickets": 1, "runs": 31, "overs": "4"},
                        {"name": "Glenn McGrath", "wickets": 1, "runs": 34, "overs": "2.5"},
                        {"name": "Amelia Kerr", "wickets": 1, "runs": 38, "overs": "3"},
                        {"name": "Jay Dijkstra", "wickets": 1, "runs": 42, "overs": "4"},
                    ]},
                },
                stadium="Wanderers Stadium", match_no=118722,
                text_settings=text_settings,
            )
        elif card_type == "bowling":
            png = generate_bowling_scorecard(
                "Sample Team A",
                [
                    {"name": "Glenn McGrath", "overs": "2.5", "dots": 1, "runs_conceded": 34, "wickets": 1, "economy": 12.0},
                    {"name": "Mitchell Starc", "overs": "3.0", "dots": 5, "runs_conceded": 37, "wickets": 0, "economy": 12.33},
                    {"name": "Jay Dijkstra", "overs": "4.0", "dots": 5, "runs_conceded": 42, "wickets": 1, "economy": 10.5},
                    {"name": "Amelia Kerr", "overs": "3.0", "dots": 6, "runs_conceded": 38, "wickets": 1, "economy": 12.67},
                    {"name": "Shane Warne", "overs": "4.0", "dots": 5, "runs_conceded": 31, "wickets": 1, "economy": 7.75},
                ],
                [(1, 9, "1.3"), (2, 85, "9.1"), (3, 125, "13.5"), (4, 137, "14.2")],
                is_first_innings=is_first,
                match_title="WANDERERS STADIUM",
                opponent_name="Sample Team B",
                opp_score=156, opp_wickets=3, opp_overs="15.2",
                match_no=42,
                accent_hex=preview_color,
                text_settings=text_settings,
            )
        else:
            png = generate_batting_scorecard(
                "Sample Team A", "Sample Team B", 156, 3, "15.2",
                sample_rows, [(1, 12, "1.3"), (2, 78, "9.1"), (3, 134, "13.5")],
                {"wd": 4, "nb": 1, "b": 0, "lb": 2, "total": 7},
                is_first_innings=is_first,
                match_title="WANDERERS STADIUM",
                match_no=42,
                accent_hex=preview_color,
                text_settings=text_settings,
            )
        if not png:
            return "Preview render failed", 500
        from flask import Response
        return Response(png, mimetype="image/png")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# EVENT MEDIA — GIFs sent for in-match events (six, four, wicket, etc.)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/media")
@login_required
def admin_media_list():
    """List all event keys with their media counts."""
    db = get_session()
    try:
        from models import EventMedia
        from services.event_media_service import EVENT_KEYS
        # Count enabled / total per event_key
        all_rows = db.query(EventMedia).all()
        counts = {}
        for row in all_rows:
            d = counts.setdefault(row.event_key, {"total": 0, "enabled": 0})
            d["total"] += 1
            if row.enabled:
                d["enabled"] += 1
        return render_template("admin_media_list.html",
                               event_keys=EVENT_KEYS, counts=counts)
    finally:
        db.close()


@app.route("/media/<event_key>", methods=["GET", "POST"])
@login_required
def admin_media_detail(event_key):
    """List/upload/delete media for a single event_key."""
    db = get_session()
    try:
        from models import EventMedia
        from services.event_media_service import EVENT_KEYS, MEDIA_DIR, clamp_mobile_width, normalize_size_mode
        import os as _os
        import uuid as _uuid
        from werkzeug.utils import secure_filename

        # Validate event_key
        valid_keys = {k for k, _, _ in EVENT_KEYS}
        if event_key not in valid_keys:
            flash(f"Unknown event_key: {event_key}", "error")
            return redirect(url_for("admin_media_list"))

        event_meta = next((m for m in EVENT_KEYS if m[0] == event_key), None)

        if request.method == "POST":
            action = request.form.get("action", "add")
            try:
                if action == "add":
                    url_input = (request.form.get("url") or "").strip()
                    label = (request.form.get("label") or "").strip()[:120]
                    weight_str = (request.form.get("weight") or "1").strip()
                    try:
                        weight = max(1, min(100, int(weight_str)))
                    except ValueError:
                        weight = 1
                    try:
                        duration_ms = max(500, min(15000, int(float(request.form.get("duration_seconds", 3)) * 1000)))
                    except (TypeError, ValueError):
                        duration_ms = 3000
                    size_mode = normalize_size_mode(request.form.get("size_mode"))
                    max_mobile_width = clamp_mobile_width(request.form.get("max_mobile_width"))

                    em = None
                    uploaded = request.files.get("upload")
                    if uploaded and uploaded.filename:
                        ext = _os.path.splitext(uploaded.filename)[1].lower()
                        if ext not in (".gif", ".mp4", ".webp"):
                            flash("File must be .gif, .mp4, or .webp", "error")
                            return redirect(url_for("admin_media_detail",
                                                    event_key=event_key))

                        # Read file once into memory so we can both forward
                        # to Telegram AND save to disk if the channel isn't
                        # configured.
                        file_bytes = uploaded.read()
                        media_width = None
                        media_height = None
                        media_type = "video" if ext == ".mp4" else "image"
                        if file_bytes:
                            from services.event_media_service import detect_media_dimensions, optimize_event_media_upload
                            file_bytes = optimize_event_media_upload(file_bytes, uploaded.filename)
                            media_width, media_height, media_type = detect_media_dimensions(file_bytes, uploaded.filename)
                        if not file_bytes:
                            flash("Uploaded file is empty.", "error")
                            return redirect(url_for("admin_media_detail",
                                                    event_key=event_key))

                        # 50MB Telegram limit for bot uploads
                        if len(file_bytes) > 50 * 1024 * 1024:
                            flash(f"File too large ({len(file_bytes)/1024/1024:.1f}MB). "
                                  f"Telegram limit is 50MB.", "error")
                            return redirect(url_for("admin_media_detail",
                                                    event_key=event_key))

                        # Prefer Telegram storage if env var set
                        from config import MEDIA_STORAGE_CHAT_ID
                        em = None
                        if MEDIA_STORAGE_CHAT_ID:
                            from services.event_media_service import upload_to_storage_channel
                            up = upload_to_storage_channel(
                                file_bytes, uploaded.filename,
                                original_label=label or None,
                            )
                            if up["success"]:
                                em = EventMedia(
                                    event_key=event_key,
                                    source_type="telegram",
                                    source=up["file_id"],
                                    label=label or uploaded.filename,
                                    weight=weight, enabled=True, duration_ms=duration_ms,
                                    size_mode=size_mode, original_width=media_width,
                                    original_height=media_height, max_mobile_width=max_mobile_width,
                                    media_type=media_type,
                                    uploaded_by=session.get("admin_user", "admin"),
                                )
                                db.add(em); db.commit()
                                log_admin(db, "media_add_telegram",
                                          "event_media", em.id, event_key,
                                          f"file_id={up['file_id'][:20]}…")
                                db.commit()
                                flash(f"✅ Uploaded to Telegram channel "
                                      f"(persistent, never resets). "
                                      f"file_id stored.", "info")
                            else:
                                # Telegram upload failed — fall back to disk
                                # save but tell the admin what went wrong
                                flash(f"⚠️ Telegram channel upload failed: "
                                      f"{up['error']}. Saved to disk instead "
                                      f"(may not persist on deploy).", "error")

                        if em is None:
                            # Disk fallback (also when no channel configured)
                            safe_stem = secure_filename(_os.path.splitext(uploaded.filename)[0]) or "media"
                            unique = f"{safe_stem}_{_uuid.uuid4().hex[:8]}{ext}"
                            event_dir = _os.path.join(MEDIA_DIR, event_key)
                            _os.makedirs(event_dir, exist_ok=True)
                            full_path = _os.path.join(event_dir, unique)
                            with open(full_path, "wb") as f:
                                f.write(file_bytes)
                            em = EventMedia(
                                event_key=event_key,
                                source_type="file",
                                source=_os.path.join(event_key, unique),
                                label=label or uploaded.filename,
                                weight=weight, enabled=True, duration_ms=duration_ms,
                                size_mode=size_mode, original_width=media_width,
                                original_height=media_height, max_mobile_width=max_mobile_width,
                                media_type=media_type,
                                uploaded_by=session.get("admin_user", "admin"),
                            )
                            db.add(em); db.commit()
                            log_admin(db, "media_add_file", "event_media",
                                      em.id, event_key, f"file={unique}")
                            db.commit()
                            if not MEDIA_STORAGE_CHAT_ID:
                                flash(f"✅ Uploaded {uploaded.filename}. "
                                      f"<i>For persistence across deploys, "
                                      f"set MEDIA_STORAGE_CHAT_ID env var to "
                                      f"a private channel id.</i>", "info")
                    elif url_input:
                        # URL path
                        if not (url_input.startswith("http://")
                                or url_input.startswith("https://")):
                            flash("URL must start with http:// or https://", "error")
                            return redirect(url_for("admin_media_detail",
                                                    event_key=event_key))
                        try:
                            url_width = int(request.form.get("original_width") or 0) or None
                            url_height = int(request.form.get("original_height") or 0) or None
                        except (TypeError, ValueError):
                            url_width = url_height = None
                        em = EventMedia(
                            event_key=event_key, source_type="url",
                            source=url_input, label=label or None,
                            weight=weight, enabled=True, duration_ms=duration_ms,
                            size_mode=size_mode, original_width=url_width,
                            original_height=url_height, max_mobile_width=max_mobile_width,
                            media_type=("video" if url_input.lower().split("?")[0].endswith(".mp4") else "image"),
                            uploaded_by=session.get("admin_user", "admin"),
                        )
                        db.add(em); db.commit()
                        log_admin(db, "media_add_url", "event_media", em.id,
                                  event_key, f"url={url_input[:60]}")
                        db.commit()
                        flash("✅ Added URL", "info")
                    else:
                        flash("Provide either a URL or a file upload.", "error")

                    replace_id = request.form.get("replace_media_id")
                    if em is not None and replace_id:
                        old_media = db.query(EventMedia).get(int(replace_id))
                        if old_media and old_media.event_key == event_key and old_media.id != em.id:
                            if old_media.source_type == "file":
                                old_path = _os.path.join(MEDIA_DIR, old_media.source)
                                if _os.path.exists(old_path):
                                    try: _os.remove(old_path)
                                    except OSError: pass
                            db.delete(old_media)
                            db.commit()
                            flash("Replaced the previous GIF.", "info")

                elif action == "delete":
                    mid = int(request.form.get("media_id", 0))
                    em = db.query(EventMedia).get(mid)
                    if em and em.event_key == event_key:
                        # If file-backed, try to remove from disk too
                        if em.source_type == "file":
                            full_path = _os.path.join(MEDIA_DIR, em.source)
                            if _os.path.exists(full_path):
                                try: _os.remove(full_path)
                                except OSError: pass
                        db.delete(em); db.commit()
                        log_admin(db, "media_delete", "event_media", mid,
                                  event_key, "deleted")
                        db.commit()
                        flash("Deleted.", "info")

                elif action == "toggle":
                    mid = int(request.form.get("media_id", 0))
                    em = db.query(EventMedia).get(mid)
                    if em and em.event_key == event_key:
                        em.enabled = not em.enabled
                        db.commit()
                        flash(f"{'Enabled' if em.enabled else 'Disabled'}.", "info")

                elif action == "update":
                    mid = int(request.form.get("media_id", 0))
                    em = db.query(EventMedia).get(mid)
                    if em and em.event_key == event_key:
                        em.label = (request.form.get("label") or "").strip()[:120] or None
                        try:
                            em.weight = max(1, min(100, int(request.form.get("weight", 1))))
                        except ValueError:
                            pass
                        try:
                            em.duration_ms = max(500, min(15000, int(float(request.form.get("duration_seconds", 3)) * 1000)))
                        except (TypeError, ValueError):
                            pass
                        em.size_mode = normalize_size_mode(request.form.get("size_mode"))
                        em.max_mobile_width = clamp_mobile_width(request.form.get("max_mobile_width"))
                        try:
                            em.original_width = int(request.form.get("original_width") or 0) or None
                            em.original_height = int(request.form.get("original_height") or 0) or None
                        except (TypeError, ValueError):
                            pass
                        db.commit()
                        flash("Saved and published.", "info")
            except Exception as e:
                db.rollback()
                logger.exception("media admin error")
                flash(f"Error: {e}", "error")
            try:
                from services.event_media_service import invalidate_miniapp_event_gifs
                invalidate_miniapp_event_gifs()
            except Exception:
                pass
            return redirect(url_for("admin_media_detail", event_key=event_key))

        rows = (db.query(EventMedia)
                .filter(EventMedia.event_key == event_key)
                .order_by(EventMedia.enabled.desc(),
                          EventMedia.uploaded_at.desc())
                .all())
        return render_template("admin_media_detail.html",
                               event_key=event_key, event_meta=event_meta,
                               rows=rows)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# LIVE MATCHES — operations view
# ═══════════════════════════════════════════════════════════════════════

@app.route("/live-matches")
@login_required
def admin_live_matches():
    """Shows currently-active matches with state snapshot pulled from MatchState."""
    db = get_session()
    try:
        from models import Match, MatchState
        import json as _json
        matches = (db.query(Match)
                    .filter(Match.status.in_(("active", "pending")))
                    .order_by(Match.created_at.desc()).all())
        live = []
        for m in matches:
            ms = db.query(MatchState).filter(MatchState.match_id == m.id).first()
            u1 = db.query(User).get(m.user1_id) if m.user1_id else None
            u2 = db.query(User).get(m.user2_id) if m.user2_id else None
            snap = {
                "id": m.id, "status": m.status,
                "u1": ((u1.username or u1.first_name) if u1 else "?"),
                "u2": (("Bot" if (u2 and u2.telegram_id == -1) or m.user2_id == -1
                        else (u2.username or u2.first_name)) if u2 else "?"),
                "u1_id": m.user1_id, "u2_id": m.user2_id,
                "chat_id": m.chat_id, "overs": m.overs,
                "stadium": m.stadium or "—", "pitch": m.pitch_type or "—",
                "created_at": m.created_at,
                "last_modified": ms.last_modified if ms else None,
                "next_action": ms.next_action if ms else None,
                "ball_seq": ms.ball_seq if ms else 0,
            }
            if ms and ms.state_json:
                try:
                    st = _json.loads(ms.state_json)
                    snap["innings"] = st.get("innings")
                    snap["score"] = f"{st.get('total_runs',0)}/{st.get('total_wickets',0)}"
                    co = st.get("current_over", 1) - 1
                    cb = st.get("current_ball", 0)
                    snap["overs_played"] = f"{co}.{cb}"
                    snap["target"] = st.get("target")
                    snap["bat_team"] = st.get("bat_team_name", "?")
                    snap["bowl_team"] = st.get("bowl_team_name", "?")
                except Exception: pass
            live.append(snap)
        return render_template("admin_live_matches.html", live=live, total=len(live))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# GSPIN REWARDS — admin CRUD for wheel outcomes
# ═══════════════════════════════════════════════════════════════════════

@app.route("/gspin-rewards", methods=["GET", "POST"])
@login_required
def admin_gspin_rewards():
    db = get_session()
    try:
        from models import GSpinReward, Pack
        if request.method == "POST":
            action = request.form.get("action", "")
            try:
                if action == "add":
                    r = GSpinReward(
                        label=(request.form.get("label", "") or "Reward").strip()[:60],
                        emoji=(request.form.get("emoji", "") or "🎁").strip()[:10],
                        color=(request.form.get("color", "888888") or "888888").lstrip("#")[:7],
                        weight=max(1, int(request.form.get("weight", 10))),
                        sort_order=int(request.form.get("sort_order", 100)),
                        enabled=request.form.get("enabled") == "on",
                        reward_type=(request.form.get("reward_type") or "coins"),
                        amount_min=max(0, int(request.form.get("amount_min", 0) or 0)),
                        amount_max=max(0, int(request.form.get("amount_max", 0) or 0)),
                        player_rating_min=max(0, int(request.form.get("player_rating_min", 0) or 0)),
                        player_rating_max=max(0, int(request.form.get("player_rating_max", 0) or 0)),
                        pack_id=int(request.form.get("pack_id")) if request.form.get("pack_id") else None,
                    )
                    db.add(r); db.commit()
                    log_admin(db, "gspin_reward_add", target_type="gspin_reward",
                              target_id=r.id, target_name=r.label,
                              detail=f"type={r.reward_type} weight={r.weight}")
                    db.commit()
                    flash(f"✅ Added '{r.label}'", "success")

                elif action == "edit":
                    rid = int(request.form.get("id", 0))
                    r = db.query(GSpinReward).get(rid)
                    if r:
                        r.label = (request.form.get("label", "") or r.label).strip()[:60]
                        r.emoji = (request.form.get("emoji", "") or r.emoji or "🎁").strip()[:10]
                        r.color = (request.form.get("color", "") or r.color or "888888").lstrip("#")[:7]
                        r.weight = max(1, int(request.form.get("weight", r.weight)))
                        r.sort_order = int(request.form.get("sort_order", r.sort_order))
                        r.enabled = request.form.get("enabled") == "on"
                        r.reward_type = request.form.get("reward_type") or r.reward_type
                        r.amount_min = max(0, int(request.form.get("amount_min", 0) or 0))
                        r.amount_max = max(0, int(request.form.get("amount_max", 0) or 0))
                        r.player_rating_min = max(0, int(request.form.get("player_rating_min", 0) or 0))
                        r.player_rating_max = max(0, int(request.form.get("player_rating_max", 0) or 0))
                        pid = request.form.get("pack_id", "")
                        r.pack_id = int(pid) if pid else None
                        db.commit()
                        log_admin(db, "gspin_reward_edit", target_type="gspin_reward",
                                  target_id=r.id, target_name=r.label,
                                  detail=f"type={r.reward_type} weight={r.weight}")
                        db.commit()
                        flash(f"✅ Updated '{r.label}'", "success")

                elif action == "toggle":
                    rid = int(request.form.get("id", 0))
                    r = db.query(GSpinReward).get(rid)
                    if r:
                        r.enabled = not r.enabled
                        db.commit()
                        log_admin(db, "gspin_reward_toggle", target_type="gspin_reward",
                                  target_id=r.id, target_name=r.label,
                                  detail=f"enabled={r.enabled}")
                        db.commit()
                        flash(f"{'Enabled' if r.enabled else 'Disabled'} '{r.label}'", "info")

                elif action == "delete":
                    rid = int(request.form.get("id", 0))
                    r = db.query(GSpinReward).get(rid)
                    if r:
                        name = r.label
                        db.delete(r); db.commit()
                        log_admin(db, "gspin_reward_delete", target_type="gspin_reward",
                                  target_id=rid, target_name=name, detail="deleted")
                        db.commit()
                        flash(f"Deleted '{name}'", "info")
            except Exception as e:
                db.rollback()
                logger.exception("gspin_rewards mutation failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_gspin_rewards"))

        rewards = (db.query(GSpinReward)
                    .order_by(GSpinReward.sort_order, GSpinReward.id).all())
        enabled_rewards = [r for r in rewards if r.enabled]
        total_weight = sum(max(1, r.weight) for r in enabled_rewards) or 1
        for r in rewards:
            r._pct = round(max(1, r.weight) / total_weight * 100, 1) if r.enabled else 0.0
        packs = db.query(Pack).filter(Pack.is_active == True).order_by(Pack.slot_number).all()
        return render_template("admin_gspin_rewards.html",
                               rewards=rewards, packs=packs,
                               total_weight=total_weight,
                               enabled_count=len(enabled_rewards))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# COMMANDS — admin CRUD for command metadata + simple rewards
# ═══════════════════════════════════════════════════════════════════════

@app.route("/commands", methods=["GET", "POST"])
@login_required
def admin_commands():
    """List + edit BotCommand metadata. For commands with simple rewards
    (claim, daily, debut), also edit CommandReward values inline.

    Note: command names and aliases stay in code — this page edits behavior
    (enabled, cooldown, reward amounts), not the slash command name itself.
    """
    db = get_session()
    try:
        from models import BotCommand, CommandReward
        if request.method == "POST":
            action = request.form.get("action", "")
            try:
                if action == "save_command":
                    key = request.form.get("command_key", "").strip()
                    cmd = (db.query(BotCommand)
                             .filter(BotCommand.command_key == key).first())
                    if cmd:
                        cmd.description = request.form.get("description", "").strip()[:500] or cmd.description
                        cmd.category = request.form.get("category", cmd.category).strip()[:30]
                        cmd.enabled = request.form.get("enabled") == "on"
                        try:
                            cmd.cooldown_seconds = max(0, int(request.form.get("cooldown_seconds", 0) or 0))
                        except ValueError: pass
                        try:
                            cmd.sort_order = int(request.form.get("sort_order", cmd.sort_order))
                        except ValueError: pass
                        db.commit()
                        log_admin(db, "command_edit", target_type="bot_command",
                                  target_id=cmd.id, target_name=key,
                                  detail=f"enabled={cmd.enabled} cooldown={cmd.cooldown_seconds}")
                        db.commit()
                        flash(f"✅ Updated /{key}", "success")

                elif action == "save_reward":
                    key = request.form.get("command_key", "").strip()
                    r = (db.query(CommandReward)
                           .filter(CommandReward.command_key == key).first())
                    if not r:
                        r = CommandReward(command_key=key)
                        db.add(r)

                    def _safe_int(field, default=0):
                        try: return max(0, int(request.form.get(field, default) or 0))
                        except (ValueError, TypeError): return default

                    r.coin_amount = _safe_int("coin_amount")
                    r.gem_amount = _safe_int("gem_amount")
                    r.quest_points = _safe_int("quest_points")
                    r.player_count = _safe_int("player_count")
                    r.player_rating_min = _safe_int("player_rating_min")
                    r.player_rating_max = _safe_int("player_rating_max")
                    r.milestone_bonus_coins = _safe_int("milestone_bonus_coins")
                    r.milestone_bonus_gems = _safe_int("milestone_bonus_gems")
                    r.milestone_bonus_player_min = _safe_int("milestone_bonus_player_min")
                    r.milestone_bonus_player_max = _safe_int("milestone_bonus_player_max")
                    r.milestone_every_n = _safe_int("milestone_every_n")
                    r.notes = (request.form.get("notes", "") or "").strip()[:500]

                    db.commit()
                    log_admin(db, "command_reward_edit", target_type="command_reward",
                              target_id=r.id, target_name=key,
                              detail=f"coins={r.coin_amount} gems={r.gem_amount}")
                    db.commit()
                    flash(f"✅ Saved rewards for /{key}", "success")

                elif action == "toggle":
                    key = request.form.get("command_key", "").strip()
                    cmd = (db.query(BotCommand)
                             .filter(BotCommand.command_key == key).first())
                    if cmd:
                        cmd.enabled = not cmd.enabled
                        db.commit()
                        log_admin(db, "command_toggle", target_type="bot_command",
                                  target_id=cmd.id, target_name=key,
                                  detail=f"enabled={cmd.enabled}")
                        db.commit()
                        flash(f"{'Enabled' if cmd.enabled else 'Disabled'} /{key}", "info")
            except Exception as e:
                db.rollback()
                logger.exception("commands mutation failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_commands"))

        # GET: build the page model
        cmds = (db.query(BotCommand)
                  .order_by(BotCommand.sort_order, BotCommand.command_key).all())
        rewards = {r.command_key: r
                    for r in db.query(CommandReward).all()}
        # Attach reward (or None) to each cmd
        for c in cmds:
            c._reward = rewards.get(c.command_key)
            c._cooldown_display = _format_cooldown(c.cooldown_seconds)
        return render_template("admin_commands.html", cmds=cmds,
                               total=len(cmds),
                               enabled_count=sum(1 for c in cmds if c.enabled))
    finally:
        db.close()


def _format_cooldown(seconds):
    """Human label for a cooldown in seconds."""
    if not seconds: return "code default"
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    if seconds < 86400: return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ═══════════════════════════════════════════════════════════════════════
# MAINTENANCE MODE — turn the bot on/off
# ═══════════════════════════════════════════════════════════════════════

@app.route("/maintenance", methods=["GET", "POST"])
@login_required
def admin_maintenance():
    """Toggle maintenance mode, edit the user-facing message, set an ETA,
    and manage bypass user IDs.

    Matches already in progress continue to work even when maintenance is ON
    — only NEW commands are blocked. Callback queries (in-progress match
    button taps) are allowed through.
    """
    db = get_session()
    try:
        from models import GameConfig
        from services.config_service import _refresh as _refresh_cfg
        row = db.query(GameConfig).first()
        if not row:
            row = GameConfig()
            db.add(row); db.flush()

        if request.method == "POST":
            action = request.form.get("action", "")
            try:
                if action == "enable":
                    msg = (request.form.get("message", "") or "").strip()[:2000]
                    until_str = (request.form.get("until", "") or "").strip()
                    bypass_str = (request.form.get("bypass_ids", "") or "").strip()[:500]

                    row.is_maintenance = True
                    row.maintenance_message = msg or None
                    row.maintenance_started_at = datetime.utcnow()
                    row.maintenance_bypass_ids = bypass_str or None

                    # Parse the datetime-local input as IST → convert to UTC for storage
                    if until_str:
                        try:
                            from datetime import timedelta as _td
                            # datetime-local format: 2025-12-31T18:30
                            d = datetime.strptime(until_str, "%Y-%m-%dT%H:%M")
                            # Treat input as IST, convert to UTC
                            row.maintenance_until = d - _td(hours=5, minutes=30)
                        except ValueError:
                            row.maintenance_until = None
                    else:
                        row.maintenance_until = None

                    row.updated_at = datetime.utcnow()
                    row.updated_by = session.get("admin", "admin")
                    db.commit()
                    _refresh_cfg(db)  # invalidate cache so middleware sees the change

                    log_admin(db, "maintenance_enable", target_type="config",
                              target_name="maintenance",
                              detail=f"until={until_str or 'no ETA'}, msg={'custom' if msg else 'default'}")
                    db.commit()
                    flash("🛠️ Maintenance mode <b>ENABLED</b>. New commands now blocked.", "info")

                elif action == "disable":
                    row.is_maintenance = False
                    # Keep the message + until for reference, but clear start time
                    row.maintenance_started_at = None
                    row.updated_at = datetime.utcnow()
                    db.commit()
                    _refresh_cfg(db)
                    log_admin(db, "maintenance_disable", target_type="config",
                              target_name="maintenance", detail="disabled")
                    db.commit()
                    flash("✅ Maintenance mode <b>DISABLED</b>. Bot fully operational.", "success")

                elif action == "save_message":
                    # Update message + bypass without toggling state
                    msg = (request.form.get("message", "") or "").strip()[:2000]
                    bypass_str = (request.form.get("bypass_ids", "") or "").strip()[:500]
                    until_str = (request.form.get("until", "") or "").strip()
                    row.maintenance_message = msg or None
                    row.maintenance_bypass_ids = bypass_str or None
                    if until_str:
                        try:
                            from datetime import timedelta as _td
                            d = datetime.strptime(until_str, "%Y-%m-%dT%H:%M")
                            row.maintenance_until = d - _td(hours=5, minutes=30)
                        except ValueError: pass
                    row.updated_at = datetime.utcnow()
                    db.commit()
                    _refresh_cfg(db)
                    log_admin(db, "maintenance_edit", target_type="config",
                              target_name="maintenance",
                              detail=f"msg={'custom' if msg else 'default'}")
                    db.commit()
                    flash("✅ Maintenance settings saved", "success")
            except Exception as e:
                db.rollback()
                logger.exception("maintenance toggle failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_maintenance"))

        # GET — render the page
        from services.maintenance_service import is_maintenance_active
        from datetime import timedelta as _td

        cfg = {
            "is_maintenance": row.is_maintenance or False,
            "maintenance_message": row.maintenance_message,
            "maintenance_until": row.maintenance_until,
            "maintenance_started_at": row.maintenance_started_at,
            "maintenance_bypass_ids": row.maintenance_bypass_ids or "",
        }
        # Convert UTC → IST for the datetime-local input value
        until_ist_str = ""
        if row.maintenance_until:
            ist = row.maintenance_until + _td(hours=5, minutes=30)
            until_ist_str = ist.strftime("%Y-%m-%dT%H:%M")

        # Render preview message
        from services.maintenance_service import get_maintenance_message
        preview = get_maintenance_message(cfg=cfg) if cfg["is_maintenance"] or cfg["maintenance_message"] else None

        active = is_maintenance_active(cfg)

        return render_template("admin_maintenance.html",
                               cfg=cfg, active=active,
                               until_ist_str=until_ist_str,
                               preview=preview)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# USER REPORTS — inbox for /report submissions
# ═══════════════════════════════════════════════════════════════════════

@app.route("/reports", methods=["GET", "POST"])
@login_required
def admin_reports():
    db = get_session()
    try:
        from models import UserReport
        if request.method == "POST":
            action = request.form.get("action", "")
            try:
                if action == "mark_read":
                    rid = int(request.form.get("report_id"))
                    r = db.query(UserReport).get(rid)
                    if r:
                        r.is_read = True; db.commit()
                        flash("Marked as read", "info")
                elif action == "resolve":
                    rid = int(request.form.get("report_id"))
                    r = db.query(UserReport).get(rid)
                    if r:
                        r.is_resolved = True; r.is_read = True
                        db.commit()
                        log_admin(db, "report_resolve", target_type="report",
                                  target_id=r.id, target_name=f"#{r.id}",
                                  detail="Marked resolved")
                        db.commit()
                        flash(f"✅ Report #{r.id} resolved", "success")
                elif action == "reply":
                    rid = int(request.form.get("report_id"))
                    reply = (request.form.get("reply") or "").strip()[:2000]
                    if not reply:
                        flash("Reply cannot be empty", "error")
                    else:
                        r = db.query(UserReport).get(rid)
                        if r:
                            user = db.query(User).get(r.user_id)
                            if user:
                                try:
                                    from bot import _send_admin_reply_blocking
                                    sent = _send_admin_reply_blocking(
                                        user.telegram_id, reply)
                                    if not sent:
                                        flash("Saved reply but bot send failed", "error")
                                except Exception as e:
                                    logger.exception("Reply send failed")
                                    flash(f"Saved reply but send failed: {e}", "error")
                                r.admin_response = reply
                                r.replied_at = datetime.utcnow()
                                r.replied_by = session.get("admin", "admin")
                                r.is_read = True
                                db.commit()
                                log_admin(db, "report_reply",
                                          target_type="report",
                                          target_id=r.id, target_name=f"#{r.id}",
                                          detail=f"Replied ({len(reply)} chars)")
                                db.commit()
                                flash(f"✅ Replied to report #{r.id}", "success")
            except Exception as e:
                db.rollback()
                logger.exception("Report action failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_reports",
                                     status=request.args.get("status", "")))

        status_filter = (request.args.get("status") or "").strip()
        query = db.query(UserReport)
        if status_filter == "unread":
            query = query.filter(UserReport.is_read == False)
        elif status_filter == "open":
            query = query.filter(UserReport.is_resolved == False)
        elif status_filter == "resolved":
            query = query.filter(UserReport.is_resolved == True)
        reports = query.order_by(UserReport.created_at.desc()).limit(200).all()

        user_ids = list({r.user_id for r in reports})
        users_by_id = {u.id: u for u in db.query(User).filter(
            User.id.in_(user_ids)).all()} if user_ids else {}

        counts = {
            "unread": db.query(UserReport).filter(UserReport.is_read == False).count(),
            "open": db.query(UserReport).filter(UserReport.is_resolved == False).count(),
            "resolved": db.query(UserReport).filter(UserReport.is_resolved == True).count(),
            "total": db.query(UserReport).count(),
        }
        return render_template("admin_reports.html",
                               reports=reports, users_by_id=users_by_id,
                               counts=counts, status_filter=status_filter)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# SHOT PROBABILITIES — per-shot outcome modifiers + simulator
# ═══════════════════════════════════════════════════════════════════════

@app.route("/shot-probabilities", methods=["GET", "POST"])
@login_required
def admin_shot_probs():
    db = get_session()
    try:
        from models import ShotProbability
        from services.probability_engine import invalidate_shot_mods_cache

        if request.method == "POST":
            action = request.form.get("action", "save")
            try:
                if action == "save":
                    rows = db.query(ShotProbability).all()
                    for r in rows:
                        prefix = f"shot_{r.id}_"
                        def _f(key, default=0.0):
                            v = request.form.get(prefix + key)
                            if v is None or v == "":
                                return default
                            try:
                                return float(v)
                            except ValueError:
                                return default
                        r.mod_dot = _f("dot")
                        r.mod_1 = _f("1")
                        r.mod_2 = _f("2")
                        r.mod_3 = _f("3")
                        r.mod_4 = _f("4")
                        r.mod_6 = _f("6")
                        r.mod_wicket = _f("wicket")
                        r.mod_extras = _f("extras")
                        r.enabled = (request.form.get(prefix + "enabled") == "on")
                    db.commit()
                    invalidate_shot_mods_cache()
                    log_admin(db, "shot_probs_save", target_type="config",
                              target_name="shot_probabilities",
                              detail=f"Saved {len(rows)} shots")
                    db.commit()
                    flash(f"✅ Saved {len(rows)} shots. Changes apply to next match ball.",
                          "success")
                elif action == "reset":
                    from services.probability_engine import SHOT_MODS as _SM
                    for r in db.query(ShotProbability).all():
                        mods = _SM.get(r.shot_name, {})
                        r.mod_dot = float(mods.get("dot", 0))
                        r.mod_1 = float(mods.get("1", 0))
                        r.mod_2 = float(mods.get("2", 0))
                        r.mod_3 = float(mods.get("3", 0))
                        r.mod_4 = float(mods.get("4", 0))
                        r.mod_6 = float(mods.get("6", 0))
                        r.mod_wicket = float(mods.get("W", 0))
                        r.mod_extras = float(mods.get("extras", 0))
                        r.enabled = True
                    db.commit()
                    invalidate_shot_mods_cache()
                    flash("✅ All shots reset to code defaults", "info")
            except Exception as e:
                db.rollback()
                logger.exception("Shot probs save failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_shot_probs"))

        shots = (db.query(ShotProbability)
                   .order_by(ShotProbability.shot_name).all())
        return render_template("admin_shot_probs.html", shots=shots)
    finally:
        db.close()


@app.route("/shot-probabilities/simulate", methods=["POST"])
@login_required
def admin_shot_simulate():
    try:
        from collections import Counter
        from services.probability_engine import calculate_outcome

        shot = (request.form.get("shot") or "Drive").strip()
        bowl_style = (request.form.get("bowl_style") or "Right-arm fast").strip()
        bowl_hand = (request.form.get("bowl_hand") or "R").strip()
        variation = (request.form.get("variation") or "Stock").strip()
        length = (request.form.get("length") or "Good").strip()
        pitch_type = (request.form.get("pitch_type") or "Flat").strip()
        bat_rating = int(request.form.get("bat_rating") or 75)
        bowl_rating = int(request.form.get("bowl_rating") or 75)
        over = int(request.form.get("over") or 10)
        total_overs = int(request.form.get("total_overs") or 20)
        n_balls = min(20000, max(100, int(request.form.get("n_balls") or 5000)))

        c = Counter()
        runs_total = 0
        for _ in range(n_balls):
            out = calculate_outcome(
                bowl_style=bowl_style, bowl_hand=bowl_hand,
                variation=variation, length=length,
                pitch_type=pitch_type, over=over, total_overs=total_overs,
                shot=shot, bat_rating=bat_rating, bowl_rating=bowl_rating,
            )
            t = out.get("type")
            r = out.get("runs", 0)
            if t == "wicket":
                c["W"] += 1
            elif t == "runs":
                runs_total += r
                if r == 0: c["dot"] += 1
                elif r == 1: c["1"] += 1
                elif r == 2: c["2"] += 1
                elif r == 3: c["3"] += 1
                elif r == 4: c["4"] += 1
                elif r == 6: c["6"] += 1
                else: c[f"{r}r"] += 1
            elif t in ("wide", "noball", "legbye"):
                runs_total += r
                c["extras"] += 1
            else:
                c[str(t)] += 1

        return {
            "ok": True,
            "shot": shot,
            "n": n_balls,
            "distribution": {k: c.get(k, 0) for k in
                ["dot", "1", "2", "3", "4", "6", "W", "extras"]},
            "percentages": {k: round(c.get(k, 0) / n_balls * 100, 2) for k in
                ["dot", "1", "2", "3", "4", "6", "W", "extras"]},
            "avg_runs": round(runs_total / n_balls, 3),
            "expected_RR": round(runs_total / n_balls * 6, 2),
        }
    except Exception as e:
        logger.exception("Shot simulate failed")
        return {"ok": False, "error": str(e)}, 500


# ═══════════════════════════════════════════════════════════════════════
# BROADCAST — send a message to all groups
# ═══════════════════════════════════════════════════════════════════════

@app.route("/broadcast", methods=["GET", "POST"])
@login_required
def admin_broadcast():
    db = get_session()
    try:
        from models import BotChat, Broadcast
        if request.method == "POST":
            try:
                from werkzeug.utils import secure_filename

                message = (request.form.get("message") or "").strip()
                target = (request.form.get("target") or "all").strip()
                images = [
                    file for file in request.files.getlist("image_files")
                    if file and file.filename
                ]
                document = request.files.get("document_file")
                document = document if document and document.filename else None

                if images and document:
                    flash("Choose either image album uploads or one file, not both.", "error")
                    return redirect(url_for("admin_broadcast"))
                if len(images) > 10:
                    flash("Telegram albums support up to 10 images per broadcast.", "error")
                    return redirect(url_for("admin_broadcast"))
                if not message and not images and not document:
                    flash("Add a message, image album, or file before sending.", "error")
                    return redirect(url_for("admin_broadcast"))
                if len(message) > 4000:
                    flash("Message too long (max 4000 chars)", "error")
                    return redirect(url_for("admin_broadcast"))

                attachment = None
                if images:
                    items = []
                    for image in images:
                        if not (image.mimetype or "").startswith("image/"):
                            flash("Every image upload must be an image file.", "error")
                            return redirect(url_for("admin_broadcast"))
                        file_bytes = image.read()
                        if not file_bytes:
                            flash("Uploaded image is empty.", "error")
                            return redirect(url_for("admin_broadcast"))
                        if len(file_bytes) > 50 * 1024 * 1024:
                            flash(f"Image too large ({len(file_bytes) / 1024 / 1024:.1f}MB). "
                                  "Telegram limit is 50MB.", "error")
                            return redirect(url_for("admin_broadcast"))
                        filename = secure_filename(image.filename) or "broadcast-image"
                        items.append({"name": filename[:255], "bytes": file_bytes})
                    if len(items) == 1:
                        attachment = {
                            "type": "image",
                            "name": items[0]["name"],
                            "bytes": items[0]["bytes"],
                        }
                    else:
                        attachment = {
                            "type": "album",
                            "name": f"{len(items)} images",
                            "items": items,
                        }
                elif document:
                    file_bytes = document.read()
                    if not file_bytes:
                        flash("Uploaded attachment is empty.", "error")
                        return redirect(url_for("admin_broadcast"))
                    if len(file_bytes) > 50 * 1024 * 1024:
                        flash(f"Attachment too large ({len(file_bytes) / 1024 / 1024:.1f}MB). "
                              "Telegram limit is 50MB.", "error")
                        return redirect(url_for("admin_broadcast"))
                    filename = secure_filename(document.filename) or "broadcast-file"
                    attachment = {
                        "type": "document",
                        "name": filename[:255],
                        "bytes": file_bytes,
                    }

                # Optional "Join Fantasy" button — opens the squad picker Mini App.
                fantasy_btn = None
                if request.form.get("add_fantasy_button") == "on":
                    from services import fantasy_service
                    league = fantasy_service.get_active_league(db)
                    link = fantasy_service.fantasy_deep_link(league.id if league else None)
                    if not link:
                        flash("Set BOT_USERNAME (and MINIAPP_NAME) to use the "
                              "Join Fantasy button.", "error")
                        return redirect(url_for("admin_broadcast"))
                    fantasy_btn = {"text": "🏏 Join Fantasy", "url": link}

                query = db.query(BotChat).filter(BotChat.is_active == True)
                if target == "groups":
                    query = query.filter(BotChat.chat_type.in_(["group", "supergroup"]))
                elif target == "private":
                    query = query.filter(BotChat.chat_type == "private")
                chats = query.all()
                chat_ids = [c.chat_id for c in chats]

                if not chat_ids:
                    flash(f"No active chats matching '{target}'", "info")
                    return redirect(url_for("admin_broadcast"))

                bc = Broadcast(
                    message=message, target_type=target,
                    sent_by=session.get("admin", "admin"),
                    status="pending",
                    attachment_type=attachment["type"] if attachment else None,
                    attachment_name=attachment["name"] if attachment else None,
                )
                db.add(bc); db.commit()
                bc_id = bc.id

                log_admin(db, "broadcast_create", target_type="broadcast",
                          target_id=bc.id, target_name=target,
                          detail=f"To {len(chat_ids)} chats"
                                 + (f" with {attachment['type']} {attachment['name']}"
                                    if attachment else ""))
                db.commit()

                import threading
                t = threading.Thread(
                    target=_run_broadcast_worker,
                    args=(bc_id, list(chat_ids), message, attachment, fantasy_btn),
                    daemon=True,
                )
                t.start()
                flash(f"📢 Broadcast #{bc_id} queued for {len(chat_ids)} chats. "
                      f"Refresh to see progress.", "success")
            except Exception as e:
                db.rollback()
                logger.exception("Broadcast create failed")
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_broadcast"))

        broadcasts = (db.query(Broadcast)
                       .order_by(Broadcast.sent_at.desc())
                       .limit(20).all())
        chat_counts = {
            "total": db.query(BotChat).filter(BotChat.is_active == True).count(),
            "groups": db.query(BotChat).filter(
                BotChat.is_active == True,
                BotChat.chat_type.in_(["group", "supergroup"])).count(),
            "private": db.query(BotChat).filter(
                BotChat.is_active == True,
                BotChat.chat_type == "private").count(),
            "inactive": db.query(BotChat).filter(BotChat.is_active == False).count(),
        }
        recent_chats = (db.query(BotChat)
                         .filter(BotChat.is_active == True)
                         .order_by(BotChat.last_seen_at.desc())
                         .limit(20).all())
        return render_template("admin_broadcast.html",
                               broadcasts=broadcasts,
                               chat_counts=chat_counts,
                               recent_chats=recent_chats)
    finally:
        db.close()


def _run_broadcast_worker(bc_id, chat_ids, message, attachment=None, button=None, pin_messages=False):
    """Background worker — sends a text, image, or file broadcast at ~20 chats/sec.

    ``button`` (optional) is a {"text": str, "url": str} dict rendered as an
    inline keyboard button beneath the broadcast (e.g. "🏏 Join Fantasy").
    ``pin_messages`` pins the delivered broadcast message when Telegram allows it.
    """
    import time
    import logging as _lg
    log = _lg.getLogger("broadcast")
    sent = 0
    failed = 0

    from telegram import (Bot, InputFile, InputMediaPhoto, InlineKeyboardButton,
                          InlineKeyboardMarkup)
    from config import BOT_TOKEN
    bot_instance = Bot(token=BOT_TOKEN)

    reply_markup = None
    if button and button.get("url") and button.get("text"):
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(button["text"], url=button["url"])
        ]])

    import asyncio
    loop = asyncio.new_event_loop()

    if bc_id is not None:
        try:
            from database import get_session as _gs
            from models import Broadcast as _BC
            s = _gs()
            bc = s.query(_BC).get(bc_id)
            if bc:
                bc.status = "running"
                s.commit()
            s.close()
        except Exception:
            pass

    DELAY = 0.05

    PHOTO_CAPTION_LIMIT = 1024

    async def _send_text(cid, text):
        return await bot_instance.send_message(
            chat_id=cid, text=text, parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    async def _send_album(cid):
        items = (attachment.get("items") or [])[:10]
        media = []
        # Telegram albums cannot carry inline keyboards. If a button is present,
        # keep the caption on the follow-up text so the button is still visible.
        use_caption = bool(message) and len(message) <= PHOTO_CAPTION_LIMIT and not reply_markup
        for index, item in enumerate(items):
            upload = InputFile(io.BytesIO(item["bytes"]), filename=item["name"])
            kwargs = {}
            if index == 0 and use_caption:
                kwargs.update({"caption": message, "parse_mode": "HTML"})
            media.append(InputMediaPhoto(media=upload, **kwargs))
        pin_message_id = None
        if media:
            sent_media = await bot_instance.send_media_group(chat_id=cid, media=media)
            if sent_media:
                pin_message_id = sent_media[0].message_id
        if message and not use_caption:
            sent_text = await _send_text(cid, message)
            pin_message_id = sent_text.message_id
        elif reply_markup and not message:
            sent_text = await _send_text(cid, button.get("text") or "Open")
            pin_message_id = sent_text.message_id
        return pin_message_id

    async def _send_single_attachment(cid, attachment_type):
        upload = InputFile(io.BytesIO(attachment["bytes"]),
                           filename=attachment["name"])
        if attachment_type == "image":
            kwargs = {}
            # Prefer a photo caption for image broadcasts. It keeps the image,
            # text, and optional button in one Telegram API call and avoids the
            # previous failure mode where a photo error prevented the text from
            # being attempted at all.
            if message and len(message) <= PHOTO_CAPTION_LIMIT:
                kwargs.update({"caption": message, "parse_mode": "HTML"})
            if reply_markup and (not message or len(message) <= PHOTO_CAPTION_LIMIT):
                kwargs["reply_markup"] = reply_markup
            sent_photo = await bot_instance.send_photo(chat_id=cid, photo=upload, **kwargs)
            return bool(message and len(message) <= PHOTO_CAPTION_LIMIT), sent_photo.message_id

        kwargs = {}
        if reply_markup and not message:
            kwargs["reply_markup"] = reply_markup
        sent_document = await bot_instance.send_document(chat_id=cid, document=upload, **kwargs)
        return False, sent_document.message_id

    async def _send_one(cid):
        text_delivered = False
        attachment_delivered = False
        pin_message_id = None
        try:
            if attachment:
                attachment_type = attachment.get("type")
                try:
                    if attachment_type == "album":
                        pin_message_id = await _send_album(cid)
                        attachment_delivered = True
                        text_delivered = bool(message)
                    else:
                        text_delivered, pin_message_id = await _send_single_attachment(cid, attachment_type)
                        attachment_delivered = True
                except Exception as e:
                    # Do not let a bad/oversized image upload suppress the actual
                    # broadcast message. Send the text fallback before marking the
                    # chat as failed.
                    log.warning(f"Broadcast attachment send to {cid} failed: {e}")

                if message and not text_delivered:
                    try:
                        sent_text = await _send_text(cid, message)
                        pin_message_id = sent_text.message_id
                        text_delivered = True
                    except Exception as e:
                        log.warning(f"Broadcast text send to {cid} failed: {e}")
                elif reply_markup and not message and not attachment_delivered:
                    try:
                        sent_text = await _send_text(cid, button.get("text") or "Open")
                        pin_message_id = sent_text.message_id
                        text_delivered = True
                    except Exception as e:
                        log.warning(f"Broadcast fallback button send to {cid} failed: {e}")
            elif message:
                sent_text = await _send_text(cid, message)
                pin_message_id = sent_text.message_id
                text_delivered = True

            delivered = attachment_delivered or text_delivered
            if delivered and pin_messages and pin_message_id:
                try:
                    await bot_instance.pin_chat_message(
                        chat_id=cid,
                        message_id=pin_message_id,
                        disable_notification=True,
                    )
                except Exception as e:
                    log.warning(f"Broadcast pin in {cid} failed: {e}")
            return delivered
        except Exception as e:
            log.warning(f"Broadcast send to {cid} failed: {e}")
            return False

    for cid in chat_ids:
        try:
            ok = loop.run_until_complete(_send_one(cid))
            if ok: sent += 1
            else: failed += 1
        except Exception as e:
            log.warning(f"Broadcast {bc_id} send to {cid} crashed: {e}")
            failed += 1
        time.sleep(DELAY)

        if bc_id is not None and (sent + failed) % 50 == 0:
            try:
                s = _gs()
                bc = s.query(_BC).get(bc_id)
                if bc:
                    bc.sent_count = sent
                    bc.failed_count = failed
                    s.commit()
                s.close()
            except Exception:
                pass

    if bc_id is not None:
        try:
            s = _gs()
            bc = s.query(_BC).get(bc_id)
            if bc:
                bc.sent_count = sent
                bc.failed_count = failed
                bc.status = "done"
                s.commit()
            s.close()
        except Exception:
            pass

    try:
        loop.close()
    except Exception:
        pass

    log.info(f"Broadcast {bc_id} complete: {sent} sent, {failed} failed")


# ═══════════════════════════════════════════════════════════════════════
# AUDIT LOG — global admin actions feed
# ═══════════════════════════════════════════════════════════════════════

@app.route("/audit-log")
@login_required
def admin_audit_log():
    """Global feed of every admin action recorded via log_admin()."""
    db = get_session()
    try:
        from models import AdminLog
        action_f = request.args.get("action", "").strip()
        target_f = request.args.get("target_type", "").strip()
        since = request.args.get("since", "").strip()
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(20, min(200, int(request.args.get("per_page", 50))))

        query = db.query(AdminLog)
        if action_f: query = query.filter(AdminLog.action == action_f)
        if target_f: query = query.filter(AdminLog.target_type == target_f)
        if since:
            try:
                d = datetime.strptime(since, "%Y-%m-%d")
                query = query.filter(AdminLog.timestamp >= d)
            except ValueError: pass

        total = query.count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        logs = (query.order_by(AdminLog.timestamp.desc())
                     .offset((page - 1) * per_page).limit(per_page).all())

        actions = [r[0] for r in db.query(AdminLog.action)
                    .distinct().order_by(AdminLog.action).all() if r[0]]
        targets = [r[0] for r in db.query(AdminLog.target_type)
                    .distinct().order_by(AdminLog.target_type).all() if r[0]]

        return render_template("admin_audit_log.html",
                               logs=logs, total=total, page=page,
                               total_pages=total_pages, per_page=per_page,
                               action_f=action_f, target_f=target_f, since=since,
                               actions=actions, targets=targets)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# DIAGNOSTICS — cache stats, egress hints, system info
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE — admin tunable probability adjustments
# ═══════════════════════════════════════════════════════════════════════

@app.route("/simulation", methods=["GET", "POST"])
@login_required
def admin_simulation():
    db = get_session()
    try:
        from services.config_service import get_config, save_config, DEFAULTS
        if request.method == "POST":
            try:
                updates = {
                    "sim_dot_adjust": float(request.form.get("sim_dot_adjust", 0)),
                    "sim_one_adjust": float(request.form.get("sim_one_adjust", 0)),
                    "sim_two_adjust": float(request.form.get("sim_two_adjust", 0)),
                    "sim_four_adjust": float(request.form.get("sim_four_adjust", 0)),
                    "sim_six_adjust": float(request.form.get("sim_six_adjust", 0)),
                    "sim_wicket_adjust": float(request.form.get("sim_wicket_adjust", 0)),
                    "sim_extras_adjust": float(request.form.get("sim_extras_adjust", 0)),
                }
                save_config(db, updates, updated_by=session.get("admin_user", "admin"))
                db.commit()
                log_admin(db, "sim_save", "config", 0, "simulation",
                          f"Updated simulation tuning: dot={updates['sim_dot_adjust']}, 1={updates['sim_one_adjust']}")
                db.commit()
                flash("✅ Simulation tuning saved. Applies to next ball.", "info")
                return redirect(url_for("admin_simulation"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        cfg = get_config(db)
        return render_template("admin_simulation.html", cfg=cfg, defaults=DEFAULTS)
    finally:
        db.close()


@app.route("/simulation/run_test", methods=["POST"])
@login_required
def admin_simulation_test():
    """Run N balls through the engine with current settings, show outcome distribution."""
    db = get_session()
    try:
        N = int(request.form.get("n", 1000))
        N = max(100, min(20000, N))

        from services.probability_engine import calculate_outcome
        # Simulate a typical mid-rated matchup
        counts = {"dot": 0, "1": 0, "2": 0, "3": 0, "4": 0, "6": 0,
                  "wicket": 0, "wide": 0, "noball": 0, "legbye": 0}
        runs_total = 0
        for i in range(N):
            # Spread balls across phases for a realistic mix
            over = (i % 20) + 1
            oc = calculate_outcome(
                bowl_style="Medium Pacer", bowl_hand="Right",
                variation="Seam Up", length="Good",
                pitch_type="Flat", over=over, total_overs=20,
                shot="Drive",
                bat_rating=80, bowl_rating=80,
            )
            t = oc.get("type")
            if t == "runs":
                r = oc.get("runs", 0)
                key = str(r) if r in (0, 1, 2, 3, 4, 6) else "1"
                if key == "0": key = "dot"
                counts[key] += 1
                runs_total += r
            else:
                counts[t] = counts.get(t, 0) + 1
                runs_total += oc.get("runs", 0)
                if t in ("wide", "noball"):
                    runs_total += 1  # extras add 1
        rpo = runs_total / (N / 6) if N else 0

        # Build percentages
        pct = {k: (v / N * 100) for k, v in counts.items()}
        return render_template("admin_simulation_result.html",
                               counts=counts, pct=pct, total=N,
                               rpo=rpo, runs_total=runs_total)
    except Exception as e:
        flash(f"Error: {e}", "error")
        return redirect(url_for("admin_simulation"))
    finally:
        db.close()


@app.route("/diagnostics")
@login_required
def admin_diagnostics():
    db = get_session()
    try:
        # Cache stats
        try:
            from services.player_cache import stats as pc_stats
            cache = pc_stats()
        except Exception as e:
            cache = {"error": str(e)}

        # Image cache stats
        try:
            from services.player_image_service import _IMG_CACHE
            img_cache_count = len(_IMG_CACHE)
        except Exception:
            img_cache_count = 0

        # Generated card cache stats
        try:
            from services.card_generator import _CARD_CACHE
            gen_card_count = len(_CARD_CACHE)
        except Exception:
            gen_card_count = 0

        # Table sizes (cheap counts)
        from sqlalchemy import func
        sizes = {
            "users": db.query(func.count(User.id)).scalar(),
            "players": db.query(func.count(Player.id)).scalar(),
            "user_roster": db.query(func.count(UserRoster.id)).scalar(),
            "matches": db.query(func.count(Match.id)).scalar(),
            "activity_log": db.query(func.count(ActivityLog.id)).scalar(),
            "trades": db.query(func.count(Trade.id)).scalar(),
            "notifications": db.query(func.count(NotificationSchedule.id)).scalar(),
            "commentary": db.query(func.count(CommentaryEntry.id)).scalar(),
            "achievements_unlocked": db.query(func.count(UserAchievement.id)).scalar(),
        }

        return render_template("admin_diagnostics.html",
                               cache=cache,
                               img_cache_count=img_cache_count,
                               gen_card_count=gen_card_count,
                               sizes=sizes)
    finally:
        db.close()


@app.route("/diagnostics/refresh_cache", methods=["POST"])
@login_required
def admin_diagnostics_refresh_cache():
    try:
        from services.player_cache import invalidate as _inv_pc
        from services.card_generator import invalidate_card_cache
        from services.player_image_service import _invalidate_image_cache
        _inv_pc()
        invalidate_card_cache()
        _invalidate_image_cache()
        flash("✅ All caches cleared. Will rebuild on next access.", "info")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_diagnostics"))


# ═══════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES — admin-editable bot strings
# ═══════════════════════════════════════════════════════════════════════

@app.route("/messages")
@login_required
def admin_messages_list():
    db = get_session()
    try:
        from services.message_service import all_with_metadata
        templates = all_with_metadata(db)
        # Group by category
        by_cat = {}
        for t in templates:
            by_cat.setdefault(t["category"], []).append(t)
        return render_template("admin_messages.html",
                               by_category=by_cat, total=len(templates))
    finally:
        db.close()


@app.route("/messages/<key>/edit", methods=["GET", "POST"])
@login_required
def admin_message_edit(key):
    db = get_session()
    try:
        from services.message_service import REGISTRY, save_template
        if key not in REGISTRY:
            flash(f"Unknown template key: {key}", "error")
            return redirect(url_for("admin_messages_list"))
        meta = REGISTRY[key]

        if request.method == "POST":
            body = request.form.get("body", "").strip()
            if not body:
                flash("Body cannot be empty.", "error")
                return redirect(url_for("admin_message_edit", key=key))
            ok, msg = save_template(db, key, body,
                                    updated_by=session.get("admin_user", "admin"))
            if ok:
                db.commit()
                log_admin(db, "msg_edit", "message", 0, key, f"Edited template {key}")
                db.commit()
                flash(f"✅ Saved '{meta['label']}'.", "info")
                return redirect(url_for("admin_messages_list"))
            else:
                flash(f"Error: {msg}", "error")

        # Load current
        row = db.query(MessageTemplate).filter(MessageTemplate.key == key).first()
        current_body = row.body if row else meta["default"]
        is_overridden = row is not None
        return render_template("admin_message_form.html",
                               key=key, meta=meta,
                               current_body=current_body,
                               is_overridden=is_overridden,
                               default_body=meta["default"])
    finally:
        db.close()


@app.route("/messages/<key>/reset", methods=["POST"])
@login_required
def admin_message_reset(key):
    db = get_session()
    try:
        from services.message_service import reset_to_default, REGISTRY
        if key not in REGISTRY:
            flash("Unknown key", "error")
            return redirect(url_for("admin_messages_list"))
        reset_to_default(db, key)
        db.commit()
        log_admin(db, "msg_reset", "message", 0, key, f"Reset to default")
        db.commit()
        flash(f"✅ '{REGISTRY[key]['label']}' reset to default.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_messages_list"))


# ═══════════════════════════════════════════════════════════════════════
# GLOBAL MARKETS — admin manages shared player + trait market
# ═══════════════════════════════════════════════════════════════════════

@app.route("/markets")
@login_required
def admin_markets_overview():
    db = get_session()
    try:
        from services.global_market import (
            list_player_market, list_trait_market, get_next_refresh_at,
            get_next_trait_refresh_at,
        )
        from services.config_service import get_config
        cfg = get_config(db)
        p_slots = list_player_market(db)
        t_slots = list_trait_market(db)
        p_data = []
        for s in p_slots:
            player = db.query(Player).get(s.player_id)
            p_data.append({"row": s, "player": player})
        t_data = []
        for s in t_slots:
            trait = db.query(Trait).get(s.trait_id)
            t_data.append({"row": s, "trait": trait})
        recent = (db.query(MarketPurchase)
                  .order_by(MarketPurchase.purchased_at.desc()).limit(20).all())

        # Build dropdown options: every active player with version label visible
        all_players = (db.query(Player)
                       .filter(Player.is_active == True)
                       .order_by(Player.rating.desc(), Player.name).all())
        dropdown_options = []
        for p in all_players:
            label_parts = [p.name]
            # The seed uses version="Base card" for original players; treat that
            # (and NULL) as "Base", everything else is a true variant label.
            v = (p.version or "").strip()
            if v and v.lower() not in ("", "base card", "base"):
                label_parts.append(f"[{v}]")
            else:
                label_parts.append("[Base]")
            label_parts.append(f"({p.rating} OVR)")
            if p.country:
                label_parts.append(f"· {p.country}")
            dropdown_options.append({
                "id": p.id,
                "label": " ".join(label_parts),
                "rating": p.rating,
                "is_variant": bool(p.parent_player_id),
            })

        # Trait list for trait dropdowns
        all_traits = (db.query(Trait)
                      .filter(Trait.is_active == True)
                      .order_by(Trait.name).all())

        next_refresh = get_next_refresh_at(db)
        next_trait_refresh = get_next_trait_refresh_at(db)

        return render_template("admin_markets.html",
                               p_data=p_data, t_data=t_data, recent=recent,
                               cfg=cfg, dropdown_options=dropdown_options,
                               all_traits=all_traits,
                               next_refresh=next_refresh,
                               next_trait_refresh=next_trait_refresh)
    finally:
        db.close()


@app.route("/markets/settings/save", methods=["POST"])
@login_required
def admin_market_settings_save():
    db = get_session()
    try:
        from services.config_service import save_config
        updates = {
            "market_min_rating": int(request.form.get("market_min_rating", 87)),
            "market_default_slots": int(request.form.get("market_default_slots", 6)),
            "market_refresh_hour_ist": int(request.form.get("market_refresh_hour_ist", 0)),
            "trait_market_default_slots": int(request.form.get("trait_market_default_slots", 5)),
        }
        # Clamp
        updates["market_min_rating"] = max(50, min(100, updates["market_min_rating"]))
        updates["market_default_slots"] = max(1, min(20, updates["market_default_slots"]))
        updates["market_refresh_hour_ist"] = max(0, min(23, updates["market_refresh_hour_ist"]))
        updates["trait_market_default_slots"] = max(1, min(15, updates["trait_market_default_slots"]))
        save_config(db, updates,
                    updated_by=session.get("admin_user", "admin"))
        db.commit()
        log_admin(db, "market_settings", "config", 0, "market",
                  f"min_rating={updates['market_min_rating']}, slots={updates['market_default_slots']}, "
                  f"refresh@{updates['market_refresh_hour_ist']}:00 IST, "
                  f"trait_slots={updates['trait_market_default_slots']}")
        db.commit()
        flash("✅ Market settings saved. Applied on next refresh.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/players/add", methods=["POST"])
@login_required
def admin_market_player_add():
    db = get_session()
    try:
        player_id = int(request.form.get("player_id", 0))
        if not player_id:
            flash("Pick a player from the dropdown.", "error")
            return redirect(url_for("admin_markets_overview"))

        # Optional custom price
        custom_price = request.form.get("price", "").strip()
        custom_price_int = int(custom_price) if custom_price else None

        from services.global_market import add_player_to_market
        ok, result = add_player_to_market(db, player_id, custom_price=custom_price_int)
        if ok:
            db.commit()
            player = db.query(Player).get(player_id)
            label = player.name + (f" [{player.version}]" if player.version else "")
            log_admin(db, "market_add_player", "market", result, label,
                      f"Added to slot {result}")
            db.commit()
            flash(f"✅ Added {label} to slot #{result}.", "info")
        else:
            flash(f"⚠️ {result}", "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/players/reroll", methods=["POST"])
@login_required
def admin_market_player_reroll():
    db = get_session()
    try:
        n = int(request.form.get("num_slots", 8))
        n = max(1, min(20, n))
        from services.global_market import reroll_player_market
        count = reroll_player_market(db, num_slots=n)
        db.commit()
        log_admin(db, "market_reroll", "market", 0, "player_market",
                  f"Rerolled with {count} slots")
        db.commit()
        flash(f"✅ Player market rerolled — {count} new slots.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/traits/reroll", methods=["POST"])
@login_required
def admin_market_trait_reroll():
    db = get_session()
    try:
        n = int(request.form.get("num_slots", 5))
        n = max(1, min(20, n))
        from services.global_market import reroll_trait_market
        count = reroll_trait_market(db, num_slots=n)
        db.commit()
        log_admin(db, "market_reroll", "market", 0, "trait_market",
                  f"Rerolled with {count} slots")
        db.commit()
        flash(f"✅ Trait market rerolled — {count} new slots.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/traits/add", methods=["POST"])
@login_required
def admin_market_trait_add():
    db = get_session()
    try:
        trait_id = int(request.form.get("trait_id", 0))
        if not trait_id:
            flash("Pick a trait from the dropdown.", "error")
            return redirect(url_for("admin_markets_overview"))

        custom_price = request.form.get("price", "").strip()
        custom_price_int = int(custom_price) if custom_price else None
        quantity = request.form.get("quantity", "10").strip()
        quantity_int = int(quantity) if quantity else 10

        from services.global_market import add_trait_to_market
        ok, result = add_trait_to_market(db, trait_id,
                                         custom_price=custom_price_int,
                                         quantity=quantity_int)
        if ok:
            db.commit()
            trait = db.query(Trait).get(trait_id)
            log_admin(db, "market_add_trait", "market", result, trait.name,
                      f"Added trait to slot {result}, qty={quantity_int}")
            db.commit()
            flash(f"✅ Added {trait.name} to slot #{result} (qty {quantity_int}).", "info")
        else:
            flash(f"⚠️ {result}", "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/players/<int:slot_id>/edit", methods=["POST"])
@login_required
def admin_market_player_edit(slot_id):
    db = get_session()
    try:
        from services.global_market import update_player_slot
        # Build update dict from form
        data = {}
        for k in ("base_price", "final_price", "quantity", "purchased_count", "player_id"):
            v = request.form.get(k)
            if v is not None and v != "":
                data[k] = v
        data["is_active"] = bool(request.form.get("is_active"))
        ok, msg = update_player_slot(db, slot_id, **data)
        if ok:
            db.commit()
            log_admin(db, "market_edit", "market", slot_id, "player_slot",
                      f"Edited slot {slot_id}: {data}")
            db.commit()
            flash("✅ Slot updated.", "info")
        else:
            flash(f"Error: {msg}", "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/traits/<int:slot_id>/edit", methods=["POST"])
@login_required
def admin_market_trait_edit(slot_id):
    db = get_session()
    try:
        from services.global_market import update_trait_slot
        data = {}
        for k in ("base_price", "final_price", "discount_pct", "quantity", "purchased_count", "trait_id"):
            v = request.form.get(k)
            if v is not None and v != "":
                data[k] = v
        data["is_active"] = bool(request.form.get("is_active"))
        ok, msg = update_trait_slot(db, slot_id, **data)
        if ok:
            db.commit()
            log_admin(db, "market_edit", "market", slot_id, "trait_slot",
                      f"Edited slot {slot_id}: {data}")
            db.commit()
            flash("✅ Slot updated.", "info")
        else:
            flash(f"Error: {msg}", "error")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/players/<int:slot_id>/delete", methods=["POST"])
@login_required
def admin_market_player_delete(slot_id):
    db = get_session()
    try:
        s = db.query(GlobalPlayerMarket).get(slot_id)
        if s:
            db.delete(s); db.commit()
            flash("✅ Slot removed.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


@app.route("/markets/traits/<int:slot_id>/delete", methods=["POST"])
@login_required
def admin_market_trait_delete(slot_id):
    db = get_session()
    try:
        s = db.query(GlobalTraitMarket).get(slot_id)
        if s:
            db.delete(s); db.commit()
            flash("✅ Slot removed.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_markets_overview"))


# ─── Adsgram (Mini App ad analytics) ────────────────────────────────────

@app.route("/adsgram")
@login_required
def admin_adsgram():
    """Recent Adsgram postbacks + Mini App spin history."""
    db = get_session()
    try:
        from models import AdsgramReward, ActivityLog
        from services import adsgram_service
        from sqlalchemy import func as _sqlfn

        # Last 100 postbacks
        recent = (db.query(AdsgramReward)
                  .order_by(AdsgramReward.received_at.desc())
                  .limit(100).all())

        # Summary
        total = db.query(_sqlfn.count(AdsgramReward.id)).scalar() or 0
        consumed = (db.query(_sqlfn.count(AdsgramReward.id))
                    .filter(AdsgramReward.consumed_at.isnot(None))
                    .scalar() or 0)

        # Today (last 24h)
        from datetime import timedelta
        day_ago = datetime.utcnow() - timedelta(hours=24)
        today = (db.query(_sqlfn.count(AdsgramReward.id))
                 .filter(AdsgramReward.received_at >= day_ago)
                 .scalar() or 0)

        # Recent Mini App spins (from activity log)
        spins = (db.query(ActivityLog)
                 .filter(ActivityLog.action == "gspin")
                 .filter(ActivityLog.detail.like("[MiniApp/%"))
                 .order_by(ActivityLog.created_at.desc())
                 .limit(50).all())

        # Build username map
        from models import User
        if spins:
            uids = list({s.user_id for s in spins})
            users = {u.id: (u.username or u.first_name or f"#{u.id}")
                     for u in db.query(User).filter(User.id.in_(uids)).all()}
        else:
            users = {}

        return render_template(
            "admin_adsgram.html",
            recent=recent,
            spins=spins,
            users=users,
            total=total, consumed=consumed, today=today,
            configured=adsgram_service.is_configured(),
            block_id=adsgram_service.get_block_id() or "",
        )
    finally:
        db.close()


# ─── Storage management (Telegram channel archives) ────────────────────

@app.route("/storage")
@login_required
def admin_storage():
    """Admin page for Telegram-channel storage status and activity archive."""
    db = get_session()
    try:
        from models import ActivityLog, ActivityLogArchive, PlayerImage
        from services import tg_storage_service, activity_archive_service
        from sqlalchemy import func as _sf

        active_logs = db.query(_sf.count(ActivityLog.id)).scalar() or 0
        archives = (db.query(ActivityLogArchive)
                    .order_by(ActivityLogArchive.archived_at.desc())
                    .limit(50).all())
        total_archived = sum(a.row_count for a in archives) if archives else 0
        images_with_fileid = (db.query(_sf.count(PlayerImage.id))
                              .filter(PlayerImage.tg_file_id.isnot(None))
                              .scalar() or 0)
        images_total = db.query(_sf.count(PlayerImage.id)).scalar() or 0

        est_30 = activity_archive_service.estimate_archivable(db, 30)
        est_7 = activity_archive_service.estimate_archivable(db, 7)

        return render_template(
            "admin_storage.html",
            configured=tg_storage_service.is_configured(),
            chat_id=os.getenv("STORAGE_CHAT_ID", "") or "(not set)",
            active_logs=active_logs,
            archives=archives,
            total_archived=total_archived,
            images_with_fileid=images_with_fileid,
            images_total=images_total,
            est_30=est_30,
            est_7=est_7,
        )
    finally:
        db.close()


@app.route("/storage/archive", methods=["POST"])
@login_required
def admin_storage_archive():
    """Trigger an archive run. Posts older_than_days from form."""
    db = get_session()
    try:
        from services import activity_archive_service
        try:
            days = int(request.form.get("older_than_days", 30))
        except ValueError:
            days = 30
        days = max(1, min(365, days))
        result = activity_archive_service.archive_old_logs(
            db, older_than_days=days,
            archived_by=session.get("admin_user", "admin"),
        )
        if result["ok"]:
            flash(f"✅ {result['message']}", "info")
            try:
                log_admin(db, "archive_logs", "activity_log",
                          result.get("archive_id") or 0, "system",
                          f"Archived {result['archived']} rows older than {days}d")
                db.commit()
            except Exception:
                pass
        else:
            flash(f"❌ {result['message']}", "error")
        return redirect(url_for("admin_storage"))
    finally:
        db.close()


# ─── Referral competitions ─────────────────────────────────────────────

@app.route("/competitions")
@login_required
def admin_competitions():
    """List all referral competitions + create form."""
    db = get_session()
    try:
        from models import ReferralCompetition, CompetitionTemplate
        comps = (db.query(ReferralCompetition)
                 .order_by(ReferralCompetition.start_date.desc()).all())
        templates = (db.query(CompetitionTemplate)
                     .order_by(CompetitionTemplate.sort_order.asc(),
                               CompetitionTemplate.name.asc()).all())
        from services.referral_service import get_active_competition
        active = get_active_competition(db)
        return render_template("admin_competitions.html",
                               competitions=comps,
                               templates=templates,
                               active_competition_id=active.id if active else None)
    finally:
        db.close()


@app.route("/competitions/create", methods=["POST"])
@login_required
def admin_competition_create():
    db = get_session()
    try:
        from models import ReferralCompetition
        from datetime import datetime as _dt
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("admin_competitions"))
        try:
            start = _dt.fromisoformat(request.form.get("start_date") or "")
            end = _dt.fromisoformat(request.form.get("end_date") or "")
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DDTHH:MM.", "error")
            return redirect(url_for("admin_competitions"))
        if end <= start:
            flash("End date must be after start date.", "error")
            return redirect(url_for("admin_competitions"))

        comp = ReferralCompetition(
            name=name[:120],
            start_date=start, end_date=end,
            is_active=True,
            prize_top1=int(request.form.get("prize_top1") or 0),
            prize_top2=int(request.form.get("prize_top2") or 0),
            prize_top3=int(request.form.get("prize_top3") or 0),
            prize_per_invite=int(request.form.get("prize_per_invite") or 0),
            prize_per_invite_gems=int(request.form.get("prize_per_invite_gems") or 0),
            notes=(request.form.get("notes") or "").strip()[:500] or None,
            created_by=session.get("admin_user", "admin"),
        )
        db.add(comp); db.commit()
        flash(f"✅ Created competition '{name}'", "info")
        try:
            log_admin(db, "create_competition", "referral_competition",
                      comp.id, "system", f"name={name}")
            db.commit()
        except Exception:
            pass
        return redirect(url_for("admin_competition_detail", comp_id=comp.id))
    except Exception as e:
        db.rollback()
        flash(f"❌ Error: {e}", "error")
        return redirect(url_for("admin_competitions"))
    finally:
        db.close()


@app.route("/competitions/<int:comp_id>")
@login_required
def admin_competition_detail(comp_id):
    """Per-competition leaderboard + edit form."""
    db = get_session()
    try:
        from models import ReferralCompetition, Referral
        from services.referral_service import get_leaderboard
        comp = db.query(ReferralCompetition).get(comp_id)
        if not comp:
            flash("Competition not found.", "error")
            return redirect(url_for("admin_competitions"))
        lb = get_leaderboard(db, comp_id, limit=100)
        total_invites = (db.query(Referral)
                         .filter(Referral.competition_id == comp_id).count())
        completed_invites = (db.query(Referral)
                             .filter(Referral.competition_id == comp_id,
                                     Referral.completed_at.isnot(None),
                                     Referral.is_valid == True).count())
        return render_template("admin_competition_detail.html",
                               comp=comp,
                               leaderboard=lb,
                               total_invites=total_invites,
                               completed_invites=completed_invites)
    finally:
        db.close()


@app.route("/competitions/<int:comp_id>/update", methods=["POST"])
@login_required
def admin_competition_update(comp_id):
    db = get_session()
    try:
        from models import ReferralCompetition
        from datetime import datetime as _dt
        comp = db.query(ReferralCompetition).get(comp_id)
        if not comp:
            flash("Not found.", "error")
            return redirect(url_for("admin_competitions"))
        # Fields: name, start_date, end_date, prizes, notes, is_active
        comp.name = (request.form.get("name") or comp.name).strip()[:120]
        try:
            sd = request.form.get("start_date")
            ed = request.form.get("end_date")
            if sd: comp.start_date = _dt.fromisoformat(sd)
            if ed: comp.end_date = _dt.fromisoformat(ed)
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("admin_competition_detail", comp_id=comp_id))
        comp.prize_top1 = int(request.form.get("prize_top1") or 0)
        comp.prize_top2 = int(request.form.get("prize_top2") or 0)
        comp.prize_top3 = int(request.form.get("prize_top3") or 0)
        comp.prize_per_invite = int(request.form.get("prize_per_invite") or 0)
        comp.prize_per_invite_gems = int(request.form.get("prize_per_invite_gems") or 0)
        comp.is_active = bool(request.form.get("is_active"))
        comp.notes = (request.form.get("notes") or "").strip()[:500] or None
        db.commit()
        flash("✅ Updated.", "info")
        return redirect(url_for("admin_competition_detail", comp_id=comp_id))
    except Exception as e:
        db.rollback()
        flash(f"❌ Error: {e}", "error")
        return redirect(url_for("admin_competition_detail", comp_id=comp_id))
    finally:
        db.close()


@app.route("/competitions/<int:comp_id>/declare", methods=["POST"])
@login_required
def admin_competition_declare(comp_id):
    """Declare winners and pay top-N prizes."""
    db = get_session()
    try:
        from services.referral_service import declare_winners
        result = declare_winners(db, comp_id,
                                 declared_by=session.get("admin_user", "admin"))
        if result["ok"]:
            db.commit()
            winners_str = ", ".join(
                f"#{w['rank']} @{w['username'] or w['telegram_id']} ({w['prize_coins']:,}🪙)"
                for w in result["winners"])
            flash(f"✅ {result['message']} {winners_str}", "info")
            try:
                log_admin(db, "declare_winners", "referral_competition",
                          comp_id, "system", winners_str)
                db.commit()
            except Exception:
                pass
        else:
            flash(f"❌ {result['message']}", "error")
        return redirect(url_for("admin_competition_detail", comp_id=comp_id))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(url_for("admin_competition_detail", comp_id=comp_id))
    finally:
        db.close()


@app.route("/competitions/<int:comp_id>/user/<int:user_id>")
@login_required
def admin_competition_user(comp_id, user_id):
    """View a specific user's invite list for a competition."""
    db = get_session()
    try:
        from models import ReferralCompetition
        from services.referral_service import (
            get_user_invitee_list, get_user_stats,
        )
        comp = db.query(ReferralCompetition).get(comp_id)
        target_user = db.query(User).get(user_id)
        if not comp or not target_user:
            flash("Not found.", "error")
            return redirect(url_for("admin_competitions"))
        invitees = get_user_invitee_list(db, user_id, comp_id, limit=200)
        stats = get_user_stats(db, user_id, comp_id)
        return render_template("admin_competition_user.html",
                               comp=comp, target_user=target_user,
                               invitees=invitees, stats=stats)
    finally:
        db.close()


@app.route("/competitions/referral/<int:ref_id>/toggle_valid", methods=["POST"])
@login_required
def admin_referral_toggle(ref_id):
    """Mark a referral as invalid (suspected abuse) or valid again."""
    db = get_session()
    try:
        from models import Referral
        ref = db.query(Referral).get(ref_id)
        if not ref:
            flash("Referral not found.", "error")
            return redirect(request.referrer or url_for("admin_competitions"))
        ref.is_valid = not ref.is_valid
        db.commit()
        flash(f"Referral marked {'valid' if ref.is_valid else 'INVALID'}.", "info")
        try:
            log_admin(db, "toggle_referral", "referral", ref_id, "system",
                      f"is_valid={ref.is_valid}")
            db.commit()
        except Exception:
            pass
        return redirect(request.referrer or url_for("admin_competitions"))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(request.referrer or url_for("admin_competitions"))
    finally:
        db.close()


@app.route("/competitions/templates")
@login_required
def admin_competition_templates():
    """Manage competition templates (preset prize structures)."""
    db = get_session()
    try:
        from models import CompetitionTemplate
        templates = (db.query(CompetitionTemplate)
                     .order_by(CompetitionTemplate.sort_order.asc(),
                               CompetitionTemplate.name.asc()).all())
        return render_template("admin_competition_templates.html",
                               templates=templates)
    finally:
        db.close()


@app.route("/competitions/templates/create", methods=["POST"])
@login_required
def admin_competition_template_create():
    db = get_session()
    try:
        from models import CompetitionTemplate
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("admin_competition_templates"))
        # Uniqueness check
        existing = (db.query(CompetitionTemplate)
                    .filter(CompetitionTemplate.name == name).first())
        if existing:
            flash(f"Template '{name}' already exists.", "error")
            return redirect(url_for("admin_competition_templates"))

        # Optional duration
        duration_raw = (request.form.get("duration_days") or "").strip()
        try:
            duration = int(duration_raw) if duration_raw else None
            if duration is not None and duration < 1:
                duration = None
        except ValueError:
            duration = None

        tpl = CompetitionTemplate(
            name=name[:120],
            description=(request.form.get("description") or "").strip()[:300] or None,
            duration_days=duration,
            prize_top1=max(0, int(request.form.get("prize_top1") or 0)),
            prize_top2=max(0, int(request.form.get("prize_top2") or 0)),
            prize_top3=max(0, int(request.form.get("prize_top3") or 0)),
            prize_per_invite=max(0, int(request.form.get("prize_per_invite") or 0)),
            prize_per_invite_gems=max(0, int(request.form.get("prize_per_invite_gems") or 0)),
            sort_order=int(request.form.get("sort_order") or 100),
            created_by=session.get("admin_user", "admin"),
        )
        db.add(tpl); db.commit()
        flash(f"✅ Template '{name}' created", "info")
        try:
            log_admin(db, "create_template", "competition_template",
                      tpl.id, "system", f"name={name}")
            db.commit()
        except Exception:
            pass
        return redirect(url_for("admin_competition_templates"))
    except Exception as e:
        db.rollback()
        flash(f"❌ Error: {e}", "error")
        return redirect(url_for("admin_competition_templates"))
    finally:
        db.close()


@app.route("/competitions/templates/<int:tpl_id>/update", methods=["POST"])
@login_required
def admin_competition_template_update(tpl_id):
    db = get_session()
    try:
        from models import CompetitionTemplate
        tpl = db.query(CompetitionTemplate).get(tpl_id)
        if not tpl:
            flash("Template not found.", "error")
            return redirect(url_for("admin_competition_templates"))
        new_name = (request.form.get("name") or tpl.name).strip()[:120]
        # If renaming, ensure no collision
        if new_name != tpl.name:
            collision = (db.query(CompetitionTemplate)
                         .filter(CompetitionTemplate.name == new_name,
                                 CompetitionTemplate.id != tpl_id).first())
            if collision:
                flash(f"Name '{new_name}' is already used.", "error")
                return redirect(url_for("admin_competition_templates"))
        tpl.name = new_name
        tpl.description = (request.form.get("description") or "").strip()[:300] or None
        duration_raw = (request.form.get("duration_days") or "").strip()
        try:
            d = int(duration_raw) if duration_raw else None
            tpl.duration_days = d if (d and d >= 1) else None
        except ValueError:
            tpl.duration_days = None
        tpl.prize_top1 = max(0, int(request.form.get("prize_top1") or 0))
        tpl.prize_top2 = max(0, int(request.form.get("prize_top2") or 0))
        tpl.prize_top3 = max(0, int(request.form.get("prize_top3") or 0))
        tpl.prize_per_invite = max(0, int(request.form.get("prize_per_invite") or 0))
        tpl.prize_per_invite_gems = max(0, int(request.form.get("prize_per_invite_gems") or 0))
        tpl.sort_order = int(request.form.get("sort_order") or 100)
        db.commit()
        flash("✅ Template updated", "info")
        return redirect(url_for("admin_competition_templates"))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(url_for("admin_competition_templates"))
    finally:
        db.close()


@app.route("/competitions/templates/<int:tpl_id>/delete", methods=["POST"])
@login_required
def admin_competition_template_delete(tpl_id):
    db = get_session()
    try:
        from models import CompetitionTemplate
        tpl = db.query(CompetitionTemplate).get(tpl_id)
        if not tpl:
            flash("Template not found.", "error")
            return redirect(url_for("admin_competition_templates"))
        name = tpl.name
        db.delete(tpl)
        db.commit()
        flash(f"🗑️ Deleted template '{name}'", "info")
        try:
            log_admin(db, "delete_template", "competition_template",
                      tpl_id, "system", f"name={name}")
            db.commit()
        except Exception:
            pass
        return redirect(url_for("admin_competition_templates"))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(url_for("admin_competition_templates"))
    finally:
        db.close()


# ─── Branding (channel + group links) ────────────────────────────────

@app.route("/branding")
@login_required
def admin_branding():
    """Edit the global channel/group branding shown in bot messages and Mini App."""
    db = get_session()
    try:
        from models import GameConfig
        cfg = db.query(GameConfig).first()
        if not cfg:
            cfg = GameConfig()
            db.add(cfg); db.commit()
        return render_template("admin_branding.html", cfg=cfg)
    finally:
        db.close()


@app.route("/branding/save", methods=["POST"])
@login_required
def admin_branding_save():
    db = get_session()
    try:
        from models import GameConfig
        cfg = db.query(GameConfig).first()
        if not cfg:
            cfg = GameConfig()
            db.add(cfg); db.flush()

        def _clean(s, max_len):
            return (s or "").strip().lstrip("@")[:max_len] or None

        cfg.branding_channel_username = _clean(request.form.get("channel_username"), 64)
        cfg.branding_channel_label = (request.form.get("channel_label") or "").strip()[:80] or None
        cfg.branding_group_username = _clean(request.form.get("group_username"), 64)
        cfg.branding_group_label = (request.form.get("group_label") or "").strip()[:80] or None
        cfg.branding_tagline = (request.form.get("tagline") or "").strip()[:200] or None

        # Official group for buying restriction
        ogid = (request.form.get("official_group_id") or "").strip()
        if ogid:
            try:
                cfg.official_group_id = int(ogid)
            except (ValueError, TypeError):
                flash("⚠️ Official group ID must be a number (e.g. -1001234567890). Ignored.", "error")
        else:
            cfg.official_group_id = None
        cfg.official_group_link = (request.form.get("official_group_link") or "").strip()[:200] or None
        # Editable welcome message (supports @User mention)
        wm = (request.form.get("welcome_message") or "").strip()
        cfg.welcome_message = wm[:2000] or None

        # Refresh in-memory config cache (used by config_service)
        try:
            from services import config_service as _cs
            if hasattr(_cs, "_refresh_cfg"):
                _cs._refresh_cfg(db)
        except Exception:
            pass

        db.commit()
        flash("✅ Branding updated. Will appear immediately everywhere.", "info")
        try:
            log_admin(db, "update_branding", "game_config", cfg.id, "system",
                      f"channel=@{cfg.branding_channel_username or '-'} group=@{cfg.branding_group_username or '-'}")
            db.commit()
        except Exception:
            pass
        return redirect(url_for("admin_branding"))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(url_for("admin_branding"))
    finally:
        db.close()


# ─── Card Generator (website-managed v7.1 templates + layout) ─────────

@app.route("/card-template", methods=["GET"])
@login_required
def admin_card_template():
    """Manage v7.1 Base, Star, and Legend templates, font, flags, layout, and preview."""
    db = get_session()
    try:
        from models import GameConfig
        cfg = db.query(GameConfig).first()
        if not cfg:
            cfg = GameConfig()
            db.add(cfg); db.commit()
        # A small player list for the live-preview dropdown (highest rated first).
        players = (db.query(Player)
                   .filter(Player.parent_player_id.is_(None))
                   .order_by(Player.rating.desc(), Player.name.asc())
                   .limit(200).all())
        from services.card_template_service import (list_template_variants,
                                                    normalise_template_settings)
        from services.cmu_stats_card_service import normalise_stats_card_settings
        from services.country_flag_service import list_country_flags
        from services.player_portrait_service import has_global_player_portrait
        from services.card_template_storage_service import get_state
        state = get_state() or {}
        cfg.card_style = state.get("card_style") or cfg.card_style
        cfg.card_template_show_portrait = state.get(
            "show_portrait", cfg.card_template_show_portrait)
        settings = normalise_template_settings(
            state.get("settings", cfg.card_template_settings))
        stats_settings = normalise_stats_card_settings(state.get("stats_settings"))
        template_variants = list_template_variants(db)
        has_template = template_variants["base"]["uploaded"]
        from services.card_template_service import TEMPLATES_ROOT, ALLOWED_FONT_EXT
        has_font = any(os.path.isfile(os.path.join(TEMPLATES_ROOT, f"font.{ext}"))
                       for ext in ALLOWED_FONT_EXT) or bool(cfg.card_template_font_path)
        return render_template("admin_card_template.html", cfg=cfg, players=players,
                               settings=settings, has_template=has_template,
                               template_variants=template_variants, has_font=has_font, country_flags=list_country_flags(),
                               has_global_portrait=has_global_player_portrait(),
                               stats_settings=stats_settings)
    finally:
        db.close()


@app.route("/card-template/save", methods=["POST"])
@login_required
def admin_card_template_save():
    db = get_session()
    try:
        # Optional blank-template uploads. All rarity templates are stored as
        # deterministic files under data/card_templates and mirrored to the
        # Telegram storage channel; card-template values are no longer saved in
        # GameConfig.
        from services.card_template_service import save_template_image
        for variant in ("base", "star", "legend"):
            template_file = request.files.get(f"template_file_{variant}")
            if template_file and template_file.filename:
                ok, msg, path = save_template_image(
                    template_file.read(), template_file.filename, variant=variant)
                if not ok:
                    db.rollback()
                    flash(f"❌ {variant.title()} template: {msg}", "error")
                    return redirect(url_for("admin_card_template"))

        # Optional font upload. This is the font used for every generated card.
        font_file = request.files.get("font_file")
        if font_file and font_file.filename:
            from services.card_template_service import save_template_font
            ok, msg, path = save_template_font(font_file.read(), font_file.filename)
            if not ok:
                db.rollback()
                flash(f"❌ {msg}", "error")
                return redirect(url_for("admin_card_template"))

        # Style and concrete v7.1 HTML generator controls.
        from services.card_template_service import normalise_template_settings
        from services.cmu_stats_card_service import normalise_stats_card_settings
        style = (request.form.get("card_style") or "tier").strip().lower()
        card_style = style if style in ("tier", "template") else "tier"
        show_portrait = bool(request.form.get("show_portrait"))
        raw_settings = {
            key: request.form.get(key) for key in (
                "player_x", "player_y", "player_w", "player_h",
                "player_scale", "player_opacity", "flag_scale",
                "flag_y_offset", "name_x", "name_y", "name_font_size",
                "name_letter_gap", "name_max_width", "name_line_gap",
                "ovr_x", "ovr_y", "ovr_font_size", "ovr_letter_gap", "ovr_max_width",
                "bat_x", "bat_y", "bat_font_size", "bat_letter_gap", "bat_max_width",
                "bowl_x", "bowl_y", "bowl_font_size", "bowl_letter_gap", "bowl_max_width",
                "cat_x", "cat_y", "cat_font_size", "cat_letter_gap", "cat_max_width",
                "country_x", "country_y", "country_font_size", "country_letter_gap", "country_max_width",
                "bat_style_x", "bat_style_y", "bat_style_font_size",
                "bat_style_letter_gap", "bat_style_max_width",
                "bowl_style_x", "bowl_style_y", "bowl_style_font_size",
                "bowl_style_letter_gap", "bowl_style_max_width",
            )
        }
        raw_settings["trim_transparent"] = bool(request.form.get("trim_transparent"))
        raw_settings["protect_bottom_box"] = bool(request.form.get("protect_bottom_box"))
        settings = normalise_template_settings(raw_settings)
        stats_label_keys = {
            "batting_labels": ("inns", "runs", "hs", "avg", "sr", "hundreds", "fifties", "fours", "sixes", "ducks"),
            "bowling_labels": ("inns", "wickets", "bbf", "avg", "econ", "sr", "hat_tricks", "five_fers", "three_fers"),
        }
        raw_stats_settings = {
            key: request.form.get(f"stats_{key}") for key in (
                "title_batting", "title_bowling", "badge_batting", "badge_bowling",
                "ovr_label", "bat_rating_label", "bowl_rating_label", "bat_meta_suffix",
                "name_font_size", "rating_label_font_size", "rating_num_font_size",
                "badge_font_size", "meta_font_size", "title_font_size",
                "stat_label_font_size", "stat_value_font_size", "stat_value_second_font_size",
            )
        }
        for group, keys in stats_label_keys.items():
            raw_stats_settings[group] = {
                key: request.form.get(f"stats_{group}_{key}") for key in keys
            }
        raw_stats_settings["stats_enabled"] = True
        stats_settings = normalise_stats_card_settings(raw_stats_settings)
        from services.card_template_storage_service import (
            save_local_state, pin_state_sync, upload_assets_sync,
            is_configured as template_storage_configured,
        )
        uploaded_assets = upload_assets_sync() if template_storage_configured() else {}
        state = save_local_state({
            "card_style": card_style,
            "show_portrait": show_portrait,
            "settings": settings,
            "stats_settings": stats_settings,
            "assets": uploaded_assets,
        })
        storage_saved = pin_state_sync(state, audit_text=f"style={card_style}")

        # Invalidate caches so the change shows immediately everywhere.
        try:
            from services import config_service as _cs
            _cs._CACHE["data"] = None
        except Exception:
            pass
        try:
            from services.card_generator import (invalidate_card_cache,
                                                  invalidate_template_card_cache)
            invalidate_card_cache()
            invalidate_template_card_cache()
        except Exception:
            pass

        if template_storage_configured() and storage_saved:
            flash("✅ Card template updated and pinned to Telegram storage. Will appear immediately everywhere.", "info")
        elif template_storage_configured():
            flash("⚠️ Card template updated locally, but Telegram storage pin failed. Check bot channel permissions.", "error")
        else:
            flash("✅ Card template updated locally. Set CARD_TEMPLATE_STORAGE_CHAT_ID or STORAGE_CHAT_ID to mirror it to Telegram storage.", "info")
        return redirect(url_for("admin_card_template"))
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
        return redirect(url_for("admin_card_template"))
    finally:
        db.close()


@app.route("/card-template/template/remove", methods=["POST"])
@login_required
def admin_card_template_remove_image():
    """Remove one rarity's blank template without affecting other variants."""
    variant = (request.form.get("variant") or "base").strip().lower()
    from services.card_template_service import (CARD_TEMPLATE_VARIANTS,
                                                 remove_template_image)
    if variant not in CARD_TEMPLATE_VARIANTS:
        flash("❌ Unknown card-template variant.", "error")
        return redirect(url_for("admin_card_template"))
    if not remove_template_image(variant):
        flash(f"❌ No {variant.title()} Card template is uploaded.", "error")
        return redirect(url_for("admin_card_template"))
    if variant == "base":
        db = get_session()
        try:
            from models import GameConfig
            cfg = db.query(GameConfig).first()
            if cfg:
                cfg.card_template_image_path = None
                db.commit()
        finally:
            db.close()
    try:
        from services import config_service as _cs
        _cs._CACHE["data"] = None
        from services.card_generator import (invalidate_card_cache,
                                              invalidate_template_card_cache)
        invalidate_card_cache()
        invalidate_template_card_cache()
    except Exception:
        pass
    flash(f"✅ Removed the {variant.title()} Card template.", "info")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/font/remove", methods=["POST"])
@login_required
def admin_card_template_remove_font():
    """Remove the shared font file and restore the condensed fallback font."""
    from services.card_template_service import remove_template_font
    if not remove_template_font():
        flash("❌ No shared font file is uploaded.", "error")
        return redirect(url_for("admin_card_template"))
    db = get_session()
    try:
        from models import GameConfig
        cfg = db.query(GameConfig).first()
        if cfg:
            cfg.card_template_font_path = None
            db.commit()
    finally:
        db.close()
    try:
        from services import config_service as _cs
        _cs._CACHE["data"] = None
        from services.card_generator import (invalidate_card_cache,
                                              invalidate_template_card_cache)
        invalidate_card_cache()
        invalidate_template_card_cache()
    except Exception:
        pass
    flash("✅ Removed the shared font file. Cards now use the fallback font.", "info")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/global-player/upload", methods=["POST"])
@login_required
def admin_card_template_global_player_upload():
    """Upload or replace the PNG used when a player has no individual cutout."""
    file = request.files.get("global_player_file")
    if not file or not file.filename:
        flash("❌ Choose a global player PNG to upload.", "error")
        return redirect(url_for("admin_card_template"))
    from services.player_portrait_service import save_global_player_portrait
    ok, msg = save_global_player_portrait(file.read(), file.filename)
    if not ok:
        flash(f"❌ {msg}", "error")
        return redirect(url_for("admin_card_template"))
    try:
        from services.card_generator import invalidate_card_cache
        invalidate_card_cache()
    except Exception:
        pass
    flash(f"✅ {msg}", "info")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/global-player/remove", methods=["POST"])
@login_required
def admin_card_template_global_player_remove():
    """Remove the global player cutout and restore procedural fallback cards."""
    from services.player_portrait_service import remove_global_player_portrait
    if not remove_global_player_portrait():
        flash("❌ No global player fallback PNG is uploaded.", "error")
        return redirect(url_for("admin_card_template"))
    try:
        from services.card_generator import invalidate_card_cache
        invalidate_card_cache()
    except Exception:
        pass
    flash("✅ Removed the global player fallback PNG.", "info")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/flags/upload", methods=["POST"])
@login_required
def admin_card_template_flag_upload():
    """Upload or replace the PNG used for one country in website template cards."""
    country = (request.form.get("flag_country") or "").strip()
    file = request.files.get("flag_file")
    if not file or not file.filename:
        flash("❌ Choose a country flag PNG to upload.", "error")
        return redirect(url_for("admin_card_template"))
    from services.country_flag_service import save_country_flag
    ok, msg = save_country_flag(country, file.read(), file.filename)
    if not ok:
        flash(f"❌ {msg}", "error")
        return redirect(url_for("admin_card_template"))
    try:
        from services.card_generator import invalidate_template_card_cache
        invalidate_template_card_cache()
    except Exception:
        pass
    flash(f"✅ {msg}", "info")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/flags/remove", methods=["POST"])
@login_required
def admin_card_template_flag_remove():
    """Remove an uploaded country flag and restore the renderer fallback."""
    country = (request.form.get("flag_country") or "").strip()
    from services.country_flag_service import remove_country_flag
    if remove_country_flag(country):
        try:
            from services.card_generator import invalidate_template_card_cache
            invalidate_template_card_cache()
        except Exception:
            pass
        flash(f"✅ Removed the uploaded flag for {country}.", "info")
    else:
        flash(f"❌ No uploaded flag found for {country or 'that country'}.", "error")
    return redirect(url_for("admin_card_template"))


@app.route("/card-template/preview/<int:player_id>")
@login_required
def admin_card_template_preview(player_id):
    """Force-render the template card for a player (regardless of active style)
    so the admin can preview before activating it."""
    db = get_session()
    try:
        player = db.query(Player).get(player_id)
        if not player:
            return "Player not found", 404
        from services.card_generator import (generate_template_card,
                                              invalidate_template_card_cache)
        # Always render fresh so edits show without waiting on the cache.
        invalidate_template_card_cache(player_id)
        from services.card_template_service import DEFAULT_TEMPLATE_SETTINGS
        preview_settings = {
            key: request.args.get(key) for key in DEFAULT_TEMPLATE_SETTINGS
            if key in request.args
        }
        for key in ("trim_transparent", "protect_bottom_box"):
            if key in preview_settings:
                preview_settings[key] = preview_settings[key] == "1"
        show_portrait = request.args.get("show_portrait")
        image_bytes = generate_template_card(
            player, force_global_portrait=request.args.get("global_player") == "1",
            template_variant=request.args.get("variant"),
            preview_settings=preview_settings or None,
            preview_show_portrait=None if show_portrait is None else show_portrait == "1")
        if not image_bytes:
            return ("No template configured. Upload a template image and save "
                    "first."), 400
        return send_file(io.BytesIO(image_bytes), mimetype="image/png",
                         download_name=f"template-card-{player_id}.png")
    finally:
        db.close()


# ─── Events (time-limited) ───────────────────────────────────────────

@app.route("/events")
@login_required
def admin_events():
    db = get_session()
    try:
        from models import Event
        events = db.query(Event).order_by(Event.start_at.desc()).limit(100).all()
        from services.event_service import get_active_events
        active_ids = {e.id for e in get_active_events(db)}
        return render_template("admin_events.html", events=events, active_ids=active_ids)
    finally:
        db.close()


@app.route("/events/create", methods=["POST"])
@login_required
def admin_events_create():
    db = get_session()
    try:
        from models import Event
        from datetime import datetime as _dt
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("❌ Name required", "error")
            return redirect(url_for("admin_events"))
        ev = Event(
            name=name[:120],
            description=(request.form.get("description") or "").strip()[:500] or None,
            emoji=(request.form.get("emoji") or "🎉").strip()[:10],
            event_type=request.form.get("event_type") or "coin_multiplier",
            multiplier=float(request.form.get("multiplier") or 2.0),
            start_at=_dt.fromisoformat(request.form.get("start_at")) if request.form.get("start_at") else _dt.utcnow(),
            end_at=_dt.fromisoformat(request.form.get("end_at")),
            is_active=True,
            created_by="admin",
        )
        db.add(ev)
        db.commit()
        flash(f"✅ Event '{name}' created", "info")
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_events"))


@app.route("/events/<int:event_id>/toggle", methods=["POST"])
@login_required
def admin_events_toggle(event_id):
    db = get_session()
    try:
        from models import Event
        ev = db.query(Event).get(event_id)
        if ev:
            ev.is_active = not ev.is_active
            db.commit()
            flash(f"✅ '{ev.name}' {'activated' if ev.is_active else 'deactivated'}", "info")
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_events"))


@app.route("/events/<int:event_id>/delete", methods=["POST"])
@login_required
def admin_events_delete(event_id):
    db = get_session()
    try:
        from models import Event
        ev = db.query(Event).get(event_id)
        if ev:
            db.delete(ev)
            db.commit()
            flash("✅ Event deleted", "info")
    except Exception as e:
        db.rollback()
        flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_events"))


# ─── Seasons ─────────────────────────────────────────────────────────

@app.route("/seasons")
@login_required
def admin_seasons():
    db = get_session()
    try:
        from models import Season, SeasonStanding, User
        from services.season_service import ensure_current_season, get_season_leaderboard
        live = ensure_current_season(db)
        db.commit()
        seasons = db.query(Season).order_by(Season.season_key.desc()).limit(24).all()
        _, board = get_season_leaderboard(db, limit=25)
        # Resolve names for the live board
        return render_template("admin_seasons.html",
                               live=live, seasons=seasons, board=board)
    finally:
        db.close()


# ─── Clubs (admin) ───────────────────────────────────────────────────

@app.route("/clubs")
@login_required
def admin_clubs():
    db = get_session()
    try:
        from services.club_service import admin_list_clubs, admin_get_club, CREATE_COST_COINS, MAX_MEMBERS
        detail = None
        view_id = request.args.get("view", type=int)
        if view_id:
            detail = admin_get_club(db, view_id)
        clubs = admin_list_clubs(db)
        return render_template("admin_clubs.html", clubs=clubs, detail=detail,
                               create_cost=CREATE_COST_COINS, default_max=MAX_MEMBERS)
    finally:
        db.close()


@app.route("/clubs/<int:club_id>/edit", methods=["POST"])
@login_required
def admin_clubs_edit(club_id):
    db = get_session()
    try:
        from services.club_service import admin_rename_club
        r = admin_rename_club(db, club_id,
                              name=request.form.get("name"),
                              description=request.form.get("description"),
                              emoji=request.form.get("emoji"),
                              max_members=request.form.get("max_members"))
        if r.get("ok"):
            db.commit(); flash("✅ Club updated", "info")
        else:
            flash(f"❌ {r.get('error')}", "error")
    except Exception as e:
        db.rollback(); flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_clubs", view=club_id))


@app.route("/clubs/<int:club_id>/transfer", methods=["POST"])
@login_required
def admin_clubs_transfer(club_id):
    db = get_session()
    try:
        from services.club_service import admin_transfer_founder
        uid = request.form.get("user_id", type=int)
        r = admin_transfer_founder(db, club_id, uid)
        if r.get("ok"):
            db.commit(); flash("✅ Ownership transferred", "info")
        else:
            flash(f"❌ {r.get('message', r.get('error'))}", "error")
    except Exception as e:
        db.rollback(); flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_clubs", view=club_id))


@app.route("/clubs/<int:club_id>/kick", methods=["POST"])
@login_required
def admin_clubs_kick(club_id):
    db = get_session()
    try:
        from services.club_service import admin_kick
        uid = request.form.get("user_id", type=int)
        r = admin_kick(db, club_id, uid)
        if r.get("ok"):
            db.commit(); flash("✅ Member removed", "info")
        else:
            flash(f"❌ {r.get('error')}", "error")
    except Exception as e:
        db.rollback(); flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_clubs", view=club_id))


@app.route("/clubs/<int:club_id>/disband", methods=["POST"])
@login_required
def admin_clubs_disband(club_id):
    db = get_session()
    try:
        from services.club_service import admin_disband_club
        r = admin_disband_club(db, club_id)
        if r.get("ok"):
            db.commit(); flash(f"✅ Club disbanded ({r.get('removed_members',0)} members freed)", "info")
        else:
            flash(f"❌ {r.get('error')}", "error")
    except Exception as e:
        db.rollback(); flash(f"❌ {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_clubs"))


# ══════════════════════════════════════════════════════════════════════
# FANTASY LEAGUE — admin management routes
# ══════════════════════════════════════════════════════════════════════

@app.route("/fantasy")
@login_required
def admin_fantasy_list():
    db = get_session()
    try:
        from models import FantasyLeague, FantasyEntry, FantasyMatch
        from sqlalchemy import func
        leagues = (db.query(FantasyLeague)
                   .order_by(FantasyLeague.id.desc()).all())
        stats = {}
        for lg in leagues:
            entries = db.query(func.count(FantasyEntry.id)).filter_by(league_id=lg.id).scalar() or 0
            matches = db.query(func.count(FantasyMatch.id)).filter_by(league_id=lg.id).scalar() or 0
            stats[lg.id] = {"entries": entries, "matches": matches}
        return render_template("fantasy_list.html", leagues=leagues, stats=stats)
    finally:
        db.close()


@app.route("/fantasy/new", methods=["GET", "POST"])
@login_required
def admin_fantasy_new():
    db = get_session()
    try:
        if request.method == "POST":
            try:
                from models import FantasyLeague
                from datetime import datetime as _dt
                name = request.form.get("name", "").strip()
                description = request.form.get("description", "").strip()
                broadcast_message = request.form.get("broadcast_message", "").strip()
                if not name:
                    flash("League name is required.", "error")
                    return redirect(url_for("admin_fantasy_new"))
                week = int(request.form.get("week_number", 1) or 1)
                year = int(request.form.get("year", _dt.utcnow().year) or _dt.utcnow().year)
                start_raw = request.form.get("start_date", "").strip()
                end_raw = request.form.get("end_date", "").strip()
                start_date = _dt.fromisoformat(start_raw) if start_raw else None
                end_date = _dt.fromisoformat(end_raw) if end_raw else None
                # Auto-lock time is entered in IST → store as UTC.
                lock_raw = request.form.get("lock_at", "").strip()
                lock_at = (_dt.fromisoformat(lock_raw) - _timedelta(hours=5, minutes=30)) if lock_raw else None
                league = FantasyLeague(
                    name=name, week_number=week, year=year,
                    start_date=start_date, end_date=end_date, status="open",
                    lock_at=lock_at, description=description or None,
                    broadcast_message=broadcast_message or None,
                )
                db.add(league)
                db.commit()
                flash(f"✅ Fantasy league '{name}' created.", "success")
                return redirect(url_for("admin_fantasy_detail", league_id=league.id))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        return render_template("fantasy_list.html",
                               leagues=[], stats={}, show_new_form=True)
    finally:
        db.close()


@app.route("/fantasy/<int:league_id>")
@login_required
def admin_fantasy_detail(league_id):
    db = get_session()
    try:
        from models import FantasyLeague, FantasyEntry, FantasyMatch, User, Player
        from services import fantasy_service
        league = db.query(FantasyLeague).get(league_id)
        if not league:
            flash("League not found.", "error")
            return redirect(url_for("admin_fantasy_list"))
        matches = (db.query(FantasyMatch).filter_by(league_id=league_id)
                   .order_by(FantasyMatch.match_date).all())
        entries, total = fantasy_service.get_leaderboard(db, league_id, page=1, per_page=50)
        ownership = fantasy_service.get_player_ownership(db, league_id)
        top_scorers = fantasy_service.get_top_scorers(db, league_id)
        selected_player_ids = set(fantasy_service.get_selected_player_ids(db, league_id))
        country_rules = fantasy_service.get_country_rules(db, league_id)
        role_rules = fantasy_service.get_role_rules(db, league_id)
        role_rule_options = fantasy_service.ROLE_RULES
        players = (db.query(Player).filter(Player.is_active == True)
                   .order_by(Player.country, Player.rating.desc(), Player.name).all())
        countries = [c for (c,) in (db.query(Player.country)
                     .filter(Player.is_active == True)
                     .group_by(Player.country).order_by(Player.country).all())]
        return render_template("fantasy_detail.html",
                               league=league, matches=matches,
                               entries=entries, total_entries=total,
                               ownership=ownership, top_scorers=top_scorers,
                               players=players,
                               selected_player_ids=selected_player_ids,
                               country_rules=country_rules,
                               role_rules=role_rules,
                               role_rule_options=role_rule_options,
                               countries=countries)
    finally:
        db.close()


@app.route("/fantasy/<int:league_id>/broadcast", methods=["POST"])
@login_required
def admin_fantasy_broadcast(league_id):
    """Broadcast fantasy league invite with an image, without changing lock status."""
    db = get_session()
    try:
        from models import FantasyLeague
        from services import fantasy_service
        league = db.query(FantasyLeague).get(league_id)
        if not league:
            flash("League not found.", "error")
            return redirect(url_for("admin_fantasy_list"))
        target = (request.form.get("target") or "groups").strip()
        custom_message = (request.form.get("message") or "").strip()
        include_description = request.form.get("include_description") == "on"
        league.broadcast_message = custom_message or None
        chat_ids = fantasy_service.get_fantasy_chat_ids(db, target)
        msg = fantasy_service.build_broadcast_message(
            league, custom_message=custom_message,
            include_description=include_description,
        )
        attachment = {
            "type": "image",
            "name": f"fantasy_{league_id}.png",
            "bytes": fantasy_service.generate_broadcast_image(league),
        }
        button = {"text": "🏏 Open Fantasy", "url": fantasy_service.fantasy_deep_link(league_id)}
        if chat_ids:
            import threading
            threading.Thread(
                target=_run_broadcast_worker,
                args=(None, list(chat_ids), msg, attachment, button, True),
                daemon=True,
            ).start()
            flash(f"📣 Fantasy broadcast queued for {len(chat_ids)} {target} chat(s).", "success")
        else:
            flash(f"No active chats matching '{target}' to notify.", "info")
        log_admin(db, "fantasy_broadcast", target_type="fantasy_league", target_id=league_id)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/lock", methods=["POST"])
@login_required
def admin_fantasy_lock(league_id):
    db = get_session()
    try:
        from services import fantasy_service
        chat_ids, msg = fantasy_service.lock_league(db, league_id)
        db.commit()
        if chat_ids:
            import threading
            threading.Thread(
                target=_run_broadcast_worker,
                args=(None, list(chat_ids), msg, None),
                daemon=True,
            ).start()
            flash(f"🔒 League locked. Broadcast sent to {len(chat_ids)} groups.", "success")
        else:
            flash("🔒 League locked (no active groups to notify).", "success")
        log_admin(db, "fantasy_lock", target_type="fantasy_league", target_id=league_id)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/settings", methods=["POST"])
@login_required
def admin_fantasy_settings(league_id):
    """Update the auto-lock time (entered in IST) for a league."""
    db = get_session()
    try:
        from models import FantasyLeague
        from datetime import datetime as _dt
        league = db.query(FantasyLeague).get(league_id)
        if not league:
            flash("League not found.", "error")
            return redirect(url_for("admin_fantasy_list"))
        lock_raw = request.form.get("lock_at", "").strip()
        if lock_raw:
            # datetime-local input is treated as IST → store UTC.
            league.lock_at = _dt.fromisoformat(lock_raw) - _timedelta(hours=5, minutes=30)
            flash("🔒 Auto-lock time updated.", "success")
        else:
            league.lock_at = None
            flash("Auto-lock time cleared.", "info")
        db.commit()
        log_admin(db, "fantasy_settings", target_type="fantasy_league", target_id=league_id)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/players", methods=["POST"])
@login_required
def admin_fantasy_players(league_id):
    """Save eligible fantasy players and per-country squad rules."""
    db = get_session()
    try:
        from models import FantasyLeague, Player
        from services import fantasy_service
        league = db.query(FantasyLeague).get(league_id)
        if not league:
            flash("League not found.", "error")
            return redirect(url_for("admin_fantasy_list"))

        league.description = (request.form.get("description") or "").strip() or None
        selected_ids = request.form.getlist("player_ids")
        countries = [c for (c,) in (db.query(Player.country)
                     .filter(Player.is_active == True)
                     .group_by(Player.country).all())]
        rules = {}
        for country in countries:
            key = country.replace(" ", "_")
            min_raw = (request.form.get(f"country_min_{key}") or "").strip()
            max_raw = (request.form.get(f"country_max_{key}") or "").strip()
            if min_raw or max_raw:
                rules[country] = {"min": min_raw or 0, "max": max_raw}
        role_rules = {}
        for role_key in fantasy_service.ROLE_RULES.keys():
            min_raw = (request.form.get(f"role_min_{role_key}") or "").strip()
            max_raw = (request.form.get(f"role_max_{role_key}") or "").strip()
            if min_raw or max_raw:
                role_rules[role_key] = {"min": min_raw or 0, "max": max_raw}
        ok, msg = fantasy_service.replace_player_pool(db, league_id, selected_ids, rules, role_rules)
        if ok:
            db.commit()
            flash(f"✅ {msg}", "success")
            log_admin(db, "fantasy_players", target_type="fantasy_league", target_id=league_id)
            db.commit()
        else:
            db.rollback()
            flash(msg, "error")
    except Exception as e:
        db.rollback()
        logger.exception("Fantasy player settings failed")
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/score", methods=["POST"])
@login_required
def admin_fantasy_score(league_id):
    db = get_session()
    try:
        from services import fantasy_service
        ok, msg = fantasy_service.score_league(db, league_id)
        db.commit()
        flash(f"✅ {msg}", "success")
        log_admin(db, "fantasy_score", target_type="fantasy_league", target_id=league_id)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/delete", methods=["POST"])
@login_required
def admin_fantasy_delete(league_id):
    db = get_session()
    try:
        from models import FantasyLeague, FantasyMatch, FantasyEntry
        db.query(FantasyMatch).filter_by(league_id=league_id).delete()
        db.query(FantasyEntry).filter_by(league_id=league_id).delete()
        lg = db.query(FantasyLeague).get(league_id)
        if lg:
            db.delete(lg)
        db.commit()
        flash("League deleted.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_list"))


@app.route("/fantasy/<int:league_id>/match/new", methods=["GET", "POST"])
@login_required
def admin_fantasy_match_new(league_id):
    db = get_session()
    try:
        from models import FantasyLeague, FantasyMatch
        league = db.query(FantasyLeague).get(league_id)
        if not league:
            flash("League not found.", "error")
            return redirect(url_for("admin_fantasy_list"))
        if request.method == "POST":
            try:
                from datetime import datetime as _dt
                name = request.form.get("match_name", "").strip()
                if not name:
                    flash("Match name required.", "error")
                    return redirect(url_for("admin_fantasy_match_new", league_id=league_id))
                date_raw = request.form.get("match_date", "").strip()
                match_date = _dt.fromisoformat(date_raw) if date_raw else None
                fm = FantasyMatch(league_id=league_id, match_name=name, match_date=match_date)
                db.add(fm)
                db.commit()
                flash(f"✅ Match '{name}' added.", "success")
                return redirect(url_for("admin_fantasy_match_scores", league_id=league_id, match_id=fm.id))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
        return render_template("fantasy_match_new.html", league=league)
    finally:
        db.close()


@app.route("/fantasy/<int:league_id>/match/<int:match_id>/scores", methods=["GET", "POST"])
@login_required
def admin_fantasy_match_scores(league_id, match_id):
    db = get_session()
    try:
        from models import FantasyLeague, FantasyMatch, FantasyPlayerScore, Player
        league = db.query(FantasyLeague).get(league_id)
        fmatch = db.query(FantasyMatch).get(match_id)
        if not league or not fmatch or fmatch.league_id != league_id:
            flash("Not found.", "error")
            return redirect(url_for("admin_fantasy_list"))

        if request.method == "POST":
            try:
                from services import fantasy_service
                scores = []
                for key, val in request.form.items():
                    if key.startswith("pts_"):
                        pid = int(key[4:])
                        pts = float(val or 0)
                        notes = request.form.get(f"notes_{pid}", "")
                        if pts != 0 or notes:
                            scores.append({"player_id": pid, "points": pts, "notes": notes})
                ok, msg = fantasy_service.save_player_scores(db, match_id, scores)
                db.commit()
                flash(f"✅ {msg}", "success")
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_fantasy_match_scores",
                                    league_id=league_id, match_id=match_id))

        # Load existing scores
        existing = {s.player_id: s for s in
                    db.query(FantasyPlayerScore).filter_by(fantasy_match_id=match_id).all()}
        # Load players who were picked in this league (union with existing scored)
        from models import FantasyPick, FantasyEntry
        from sqlalchemy import func
        picked_ids = (db.query(FantasyPick.player_id)
                      .join(FantasyEntry, FantasyPick.entry_id == FantasyEntry.id)
                      .filter(FantasyEntry.league_id == league_id)
                      .distinct().all())
        picked_ids = {r[0] for r in picked_ids} | set(existing.keys())
        players = (db.query(Player).filter(Player.id.in_(picked_ids))
                   .order_by(Player.name).all() if picked_ids else [])
        return render_template("fantasy_match_scores.html",
                               league=league, fmatch=fmatch,
                               players=players, existing=existing)
    finally:
        db.close()


@app.route("/fantasy/<int:league_id>/match/<int:match_id>/delete", methods=["POST"])
@login_required
def admin_fantasy_match_delete(league_id, match_id):
    db = get_session()
    try:
        from models import FantasyMatch, FantasyPlayerScore
        db.query(FantasyPlayerScore).filter_by(fantasy_match_id=match_id).delete()
        fm = db.query(FantasyMatch).get(match_id)
        if fm and fm.league_id == league_id:
            db.delete(fm)
        db.commit()
        flash("Match deleted.", "info")
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
    finally:
        db.close()
    return redirect(url_for("admin_fantasy_detail", league_id=league_id))


@app.route("/fantasy/<int:league_id>/entry/<int:entry_id>", methods=["GET", "POST"])
@login_required
def admin_fantasy_entry_edit(league_id, entry_id):
    db = get_session()
    try:
        from models import FantasyLeague, FantasyEntry, FantasyPick, Player, User
        from services import fantasy_service
        league = db.query(FantasyLeague).get(league_id)
        entry = db.query(FantasyEntry).get(entry_id)
        if not league or not entry or entry.league_id != league_id:
            flash("Not found.", "error")
            return redirect(url_for("admin_fantasy_list"))
        user = db.query(User).get(entry.user_id)

        if request.method == "POST":
            try:
                import json
                picks_json = request.form.get("picks_json", "[]")
                picks = json.loads(picks_json)
                ok, msg = fantasy_service.set_picks(db, entry_id, picks, bypass_lock=True)
                if ok:
                    db.commit()
                    flash(f"✅ {msg}", "success")
                else:
                    flash(f"❌ {msg}", "error")
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")
            return redirect(url_for("admin_fantasy_entry_edit",
                                    league_id=league_id, entry_id=entry_id))

        picks = (db.query(FantasyPick, Player)
                 .join(Player, FantasyPick.player_id == Player.id)
                 .filter(FantasyPick.entry_id == entry_id).all())
        picked_player_ids = [player.id for _, player in picks]
        player_filter = Player.is_active == True
        if picked_player_ids:
            player_filter = or_(player_filter, Player.id.in_(picked_player_ids))
        all_players = db.query(Player).filter(player_filter).order_by(Player.name).all()
        role_rules = fantasy_service.get_role_rules(db, league_id)
        return render_template("fantasy_entry_edit.html",
                               league=league, entry=entry, user=user,
                               picks=picks, all_players=all_players,
                               role_rules=role_rules)
    finally:
        db.close()


# ── Fantasy Mini App ─────────────────────────────────────────────────

@app.route("/fantasy-picker")
def fantasy_picker():
    """Serve the fantasy squad selection Mini App HTML."""
    return render_template("fantasy_picker.html")


@app.route("/api/fantasy/players")
@csrf_exempt
def api_fantasy_players():
    """Paginated player list for fantasy picker Mini App."""
    db = get_session()
    try:
        from models import Player
        from services import fantasy_service
        q_str = request.args.get("q", "").strip()
        role = request.args.get("role", "").strip().lower()
        country = request.args.get("country", "").strip()
        league_id = request.args.get("league_id", type=int)
        page = max(1, int(request.args.get("page", 1) or 1))
        per_page = 30

        q = db.query(Player).filter(Player.is_active == True)
        if league_id:
            eligible_ids = fantasy_service.get_selected_player_ids(db, league_id)
            if eligible_ids:
                q = q.filter(Player.id.in_(eligible_ids))
        country_q = q
        if q_str:
            q = q.filter(Player.name.ilike(f"%{q_str}%"))
        if country:
            q = q.filter(Player.country == country)
        if role and role != "all":
            from sqlalchemy import or_
            role_terms = {
                "bat": ["BAT", "BATSMAN", "BATTER"],
                "bowl": ["BOWL", "BOWLER"],
                "wk": ["WK", "WICKET"],
                "ar": ["AR", "ALL"],
                "all-rounder": ["AR", "ALL"],
            }
            terms = role_terms.get(role, [role.upper()])
            q = q.filter(or_(*[Player.category.ilike(f"%{term}%") for term in terms]))
        total = q.count()
        players = q.order_by(Player.rating.desc(), Player.name).offset((page - 1) * per_page).limit(per_page).all()
        return {
            "ok": True,
            "total": total,
            "page": page,
            "countries": [c for (c,) in country_q.with_entities(Player.country)
                          .group_by(Player.country).order_by(Player.country).all()],
            "country_rules": fantasy_service.get_country_rules(db, league_id) if league_id else {},
            "role_rules": fantasy_service.get_role_rules(db, league_id) if league_id else {},
            "players": [
                {"id": p.id, "name": p.name, "country": p.country,
                 "category": p.category, "rating": p.rating,
                 "version": p.version or "Base"}
                for p in players
            ],
        }
    except Exception:
        logger.exception("api_fantasy_players failed")
        return {"ok": False, "error": "server_error"}, 500
    finally:
        db.close()


@app.route("/api/fantasy/entry")
@csrf_exempt
def api_fantasy_entry():
    """Return current picks for a user in the active league (for pre-loading Mini App)."""
    db = get_session()
    try:
        league_id = request.args.get("league_id")
        user_id = request.args.get("user_id")
        if not league_id or not user_id:
            return {"ok": True, "squad": []}
        from services import fantasy_service
        info = fantasy_service.get_user_team_info(db, int(user_id), int(league_id))
        if not info:
            return {"ok": True, "squad": []}
        return {"ok": True, "entry_id": info["entry_id"],
                "locked": info["locked"], "squad": info["squad"]}
    except Exception:
        logger.exception("api_fantasy_entry failed")
        return {"ok": False, "error": "server_error"}, 500
    finally:
        db.close()


@app.route("/api/fantasy/save", methods=["POST"])
@csrf_exempt
def api_fantasy_save():
    """Save a user's fantasy squad (called by Mini App via Telegram initData)."""
    auth, tg_id, err = _webapp_auth()
    if err:
        return err
    db, user, tg_id = auth
    try:
        import json
        data = request.get_json(force=True) or {}
        league_id = data.get("league_id")
        picks = data.get("picks", [])
        if not league_id:
            return {"ok": False, "error": "missing league_id"}, 400

        from services import fantasy_service
        league = fantasy_service.get_active_league(db)
        if not league or league.id != int(league_id):
            return {"ok": False, "error": "league_not_active"}, 400
        if fantasy_service.is_locked(league):
            return {"ok": False, "error": "league_locked"}, 403

        entry = fantasy_service.get_or_create_entry(db, user.id, league.id)
        ok, msg = fantasy_service.set_picks(db, entry.id, picks)
        if ok:
            db.commit()
            return {"ok": True, "message": msg}
        return {"ok": False, "error": msg}, 400
    except Exception:
        logger.exception("api_fantasy_save failed")
        db.rollback()
        return {"ok": False, "error": "server_error"}, 500
    finally:
        db.close()


@app.route("/api/fantasy/league")
@csrf_exempt
def api_fantasy_league():
    """Return current active league info for the Mini App."""
    auth, tg_id, err = _webapp_auth(allow_not_debuted=True)
    if err:
        return err
    db, user, tg_id = auth
    try:
        from services import fantasy_service
        league = fantasy_service.get_active_league(db)
        if not league:
            return {"ok": True, "league": None}
        team_info = None
        if user:
            team_info = fantasy_service.get_user_team_info(db, user.id, league.id)
        return {
            "ok": True,
            "league": {
                "id": league.id,
                "name": league.name,
                "description": league.description or "",
                "status": league.status,
                "locked": fantasy_service.is_locked(league),
                "week_number": league.week_number,
                "year": league.year,
                "country_rules": fantasy_service.get_country_rules(db, league.id),
                "role_rules": fantasy_service.get_role_rules(db, league.id),
            },
            "team": team_info,
        }
    except Exception:
        logger.exception("api_fantasy_league failed")
        return {"ok": False, "error": "server_error"}, 500
    finally:
        db.close()


# ── Run ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ADMIN_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
