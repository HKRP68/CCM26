"""Admin panel for Cricket Bot — manage players (CRUD).
Shares the same database as the bot. Any changes here reflect in the bot instantly.
"""

import os
import io
import csv
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, send_file
from sqlalchemy import func, or_, desc, asc
from dotenv import load_dotenv

load_dotenv()

# ── Import shared DB and models ─────────────────────────────────────
from database import get_session, init_db
from models import (Player, User, Trade, UserStats, UserRoster, ActivityLog,
                    PlayerGameStats, AdminLog, Match, UserAchievement,
                    Trait, PlayerTrait, TraitInventory, TraitMarket, TraitDaily,
                    BotTeam, BotTeamPlayer,
                    Quest, UserQuestProgress,
                    CommentaryEntry,
                    NotificationSchedule, NotificationLog,
                    ClaimRarityTier, GameConfig,
                    MessageTemplate,
                    GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase)

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_SECRET", os.urandom(24).hex())

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
    from flask_wtf.csrf import CSRFProtect, generate_csrf
    csrf = CSRFProtect(app)

    @app.context_processor
    def _inject_csrf():
        # Makes csrf_token() available in every template
        return {"csrf_token": generate_csrf}
except ImportError:
    # flask-wtf not available — log a warning, fall back to no-op
    import logging
    logging.getLogger("admin").warning(
        "flask-wtf not installed; CSRF protection DISABLED. "
        "Add 'flask-wtf' to requirements.txt to enable.")
    csrf = None

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
        total_players = db.query(func.count(Player.id)).scalar()
        active_players = db.query(func.count(Player.id)).filter(Player.is_active == True).scalar()
        total_users = db.query(func.count(User.id)).scalar()
        total_trades = db.query(func.count(Trade.id)).scalar()

        stats = {
            "total_players": total_players,
            "active_players": active_players,
            "total_users": total_users,
            "total_trades": total_trades,
        }

        # Rating distribution
        tier_defs = [
            ("95-100", "legendary", "#e6ac00", 95, 100),
            ("90-94", "epic", "#9b59b6", 90, 94),
            ("85-89", "rare", "#2980b9", 85, 89),
            ("80-84", "uncommon", "#27ae60", 80, 84),
            ("75-79", "common", "#7f8c8d", 75, 79),
            ("70-74", "common", "#7f8c8d", 70, 74),
            ("65-69", "basic", "#95a5a6", 65, 69),
            ("60-64", "basic", "#95a5a6", 60, 64),
            ("55-59", "basic", "#95a5a6", 55, 59),
            ("50-54", "basic", "#bdc3c7", 50, 54),
        ]
        max_count = 1
        tiers = []
        for label, css, color, lo, hi in tier_defs:
            count = db.query(func.count(Player.id)).filter(
                Player.rating >= lo, Player.rating <= hi
            ).scalar()
            max_count = max(max_count, count)
            tiers.append({"label": label, "css": css, "color": color, "count": count, "pct": 0})
        for t in tiers:
            t["pct"] = round(t["count"] / max_count * 100) if max_count else 0

        # Top countries
        countries = (
            db.query(Player.country, func.count(Player.id).label("count"))
            .group_by(Player.country)
            .order_by(func.count(Player.id).desc())
            .limit(10)
            .all()
        )
        countries = [{"country": c, "count": n} for c, n in countries]

        return render_template("dashboard.html", stats=stats, tiers=tiers, countries=countries)
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
        if bat_hand:
            query = query.filter(Player.bat_hand == bat_hand)
        if bowl_hand:
            query = query.filter(Player.bowl_hand == bowl_hand)
        if is_active == "1":
            query = query.filter(Player.is_active == True)
        elif is_active == "0":
            query = query.filter(Player.is_active == False)

        # Sorting
        sort_map = {
            "name_asc": (Player.name.asc(),),
            "name_desc": (Player.name.desc(),),
            "rating_desc": (Player.rating.desc(), Player.name.asc()),
            "rating_asc": (Player.rating.asc(), Player.name.asc()),
            "category_asc": (Player.category.asc(), Player.rating.desc()),
            "country_asc": (Player.country.asc(), Player.rating.desc()),
            "bat_rating_desc": (Player.bat_rating.desc(), Player.name.asc()),
            "bowl_rating_desc": (Player.bowl_rating.desc(), Player.name.asc()),
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

        # Set of player IDs with active custom images (for 🎨 indicator)
        from services.player_image_service import list_players_with_custom_images
        custom_image_ids = list_players_with_custom_images(db)

        return render_template(
            "players.html",
            players=players, total=total, page=page, total_pages=total_pages,
            q=q, category=category, country_filter=country_filter,
            rating_range=rating_range,
            rating_ranges=list(RANGE_MAP.keys()),
            bat_hand=bat_hand, bowl_hand=bowl_hand, is_active=is_active,
            sort=sort,
            categories=categories, countries=countries,
            custom_image_ids=custom_image_ids,
        )
    finally:
        db.close()


# ── Download all players as CSV ──────────────────────────────────────

@app.route("/players/download")
@login_required
def players_download():
    db = get_session()
    try:
        # Apply same filters as the current view (if any)
        q = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        country_filter = request.args.get("country", "").strip()
        rating_range = request.args.get("rating_range", "").strip()

        RANGE_MAP = {
            "95-100": (95, 100), "90-94": (90, 94), "85-89": (85, 89),
            "80-84": (80, 84), "75-79": (75, 79), "70-74": (70, 74),
            "65-69": (65, 69), "60-64": (60, 64), "55-59": (55, 59), "50-54": (50, 54),
        }

        query = db.query(Player)
        if q: query = query.filter(Player.name.ilike(f"%{q}%"))
        if category: query = query.filter(Player.category == category)
        if country_filter: query = query.filter(Player.country == country_filter)
        if rating_range and rating_range in RANGE_MAP:
            r_min, r_max = RANGE_MAP[rating_range]
            query = query.filter(Player.rating >= r_min, Player.rating <= r_max)

        players = query.order_by(Player.rating.desc(), Player.name.asc()).all()

        log_admin(db, "players_download", detail=f"Downloaded {len(players)} players as CSV")
        db.commit()

        # Build CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Name", "Rating", "Category", "Country", "Bat Hand",
                         "Bowl Hand", "Bowl Style", "Bat Rating", "Bowl Rating",
                         "Version", "Is Active"])
        for p in players:
            writer.writerow([
                p.name, p.rating, p.category, p.country, p.bat_hand,
                p.bowl_hand, p.bowl_style, p.bat_rating, p.bowl_rating,
                p.version or "Base", "1" if p.is_active else "0",
            ])

        csv_data = output.getvalue()
        output.close()

        filename = f"players_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
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
                    # Format: Name, Rating, Category, Country, Bat Hand, Bowl Hand, Bowl Style, Bat Rating, Bowl Rating
                    # Minimum required: Name, Rating, Category
                    cols = [c.strip() for c in row]
                    while len(cols) < 9:
                        cols.append("")

                    name = cols[0]
                    if not name:
                        continue

                    # Check duplicate
                    existing = db.query(Player).filter(Player.name == name).first()
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

                    p = Player(
                        name=name, rating=rating, category=category, country=country,
                        bat_hand=bat_hand, bowl_hand=bowl_hand, bowl_style=bowl_style,
                        bat_rating=bat_rating, bowl_rating=bowl_rating,
                        version="Base", is_active=True,
                    )
                    db.add(p)
                    added += 1
                except Exception as e:
                    errors.append(f"Row {i}: {str(e)[:80]}")

            db.commit()

            log_admin(db, "bulk_upload", detail=f"Added {added}, skipped {skipped} duplicates, {len(errors)} errors")
            db.commit()

            msg = f"✅ Added {added} players"
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
            )
            db.add(player)
            db.flush()
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
        return render_template("player_form.html", player=player,
                               image_meta=image_meta, versions=versions, base_id=base_id)
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
        return redirect(url_for("players_list"))
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


# ── User management ──────────────────────────────────────────────────

@app.route("/users")
@login_required
def users_list():
    db = get_session()
    try:
        q = request.args.get("q", "").strip()
        page = max(1, int(request.args.get("page", 1)))
        per_page = 20

        query = db.query(User)
        if q:
            query = query.filter(
                (User.username.ilike(f"%{q}%")) | (User.first_name.ilike(f"%{q}%"))
            )

        total = query.count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        users = query.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

        # Attach streak count
        for u in users:
            st = db.query(UserStats).filter(UserStats.user_id == u.id).first()
            u._streak = f"{st.streak_count}/14" if st else "0/14"

        return render_template("users.html", users=users, total=total, page=page,
                               total_pages=total_pages, q=q)
    finally:
        db.close()


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

        return render_template("user_detail.html", user=user, stats=stats,
                               roster=roster, activities=activities,
                               active_matches=match_meta,
                               recent_matches=recent_matches,
                               pending_trades=pending_trades,
                               quest_progress=quest_progress,
                               today_key=today_key, month_key=month_key)
    finally:
        db.close()


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
        player = db.query(Player).filter(Player.name.ilike(f"%{player_name}%")).first()
        if not player:
            flash(f"Player '{player_name}' not found", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        user = db.query(User).get(user_id)
        if not user:
            flash("User not found", "error")
            return redirect(url_for("users_list"))

        from datetime import datetime
        entry = UserRoster(user_id=user.id, player_id=player.id, acquired_date=datetime.utcnow())
        db.add(entry)
        user.roster_count += 1
        from services.activity_service import log_activity
        log_activity(db, user.id, "admin_add", f"Admin added {player.name} ({player.rating} OVR)",
                     player_name=player.name, player_rating=player.rating)
        db.commit()
        flash(f"Added {player.name} ({player.rating} OVR) to roster", "success")
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
            flash(f"Seeded {added:,} players from uploaded file!", "success")
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
            flash(f"Seeded {added:,} players from data/players.json!", "success")
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
    """Seed players from parsed JSON list. Returns count added."""
    import random
    db = get_session()
    added = 0
    try:
        existing_names = {n[0] for n in db.query(Player.name).all()}

        for entry in raw_data:
            name = entry.get("Player Name", "").strip()
            if not name or name in existing_names:
                continue
            try:
                rating = int(entry.get("overall all", 0))
            except (ValueError, TypeError):
                continue
            if rating < 50:
                continue
            if rating > 100:
                rating = 100

            category = _normalise_category(entry.get("Category", "Batsman"))
            bat_hand = "Left" if "left" in entry.get("Batting Style", "").lower() else "Right"
            bowl_raw = entry.get("Bowling Style", "Right arm medium fast")
            bowl_hand = "Left" if "left" in bowl_raw.lower() else "Right"
            bowl_style = _parse_bowl_style(bowl_raw)
            country = entry.get("Country", "Unknown").strip()
            version = entry.get("Version ", "Base card").strip() or "Base card"

            try:
                bat_rating = int(entry.get("Batting Rating", 0))
            except (ValueError, TypeError):
                bat_rating = 0
            try:
                bowl_rating = int(entry.get("Bowling Rating", 0))
            except (ValueError, TypeError):
                bowl_rating = 0

            player = Player(
                name=name, version=version, rating=rating, category=category,
                country=country, bat_hand=bat_hand, bowl_hand=bowl_hand,
                bowl_style=bowl_style, bat_rating=bat_rating, bowl_rating=bowl_rating,
                bat_avg=0, strike_rate=0, runs=0, centuries=0,
                bowl_avg=0, economy=0, wickets=0, is_active=True,
            )
            db.add(player)
            existing_names.add(name)
            added += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return added


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


@app.route("/settings/scorecard", methods=["GET", "POST"])
@login_required
def admin_scorecard_settings():
    """Per-innings accent color customization for scorecard graphics."""
    db = get_session()
    try:
        from services.config_service import get_config, save_config
        import re as _re

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
                save_config(db, {
                    "scorecard_color_inn1": c1,
                    "scorecard_color_inn2": c2,
                }, updated_by=session.get("admin_user", "admin"))
                db.commit()
                log_admin(db, "scorecard_colors_save", "config", 0,
                          "scorecard", f"inn1={c1} inn2={c2}")
                db.commit()
                flash("✅ Scorecard colors saved.", "info")
                return redirect(url_for("admin_scorecard_settings"))
            except Exception as e:
                db.rollback()
                flash(f"Error: {e}", "error")

        cfg = get_config(db)
        return render_template("admin_scorecard_settings.html",
                               cfg=cfg, presets=SCORECARD_COLOR_PRESETS)
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
        from services.scorecard_card import generate_batting_scorecard
        cfg = get_config(db)
        innings = request.args.get("innings", "1")
        is_first = (innings == "1")

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
        png = generate_batting_scorecard(
            "Sample Team A", "Sample Team B", 156, 3, "15.2",
            sample_rows, [(1, 12, "1.3"), (2, 78, "9.1"), (3, 134, "13.5")],
            {"wd": 4, "nb": 1, "b": 0, "lb": 2, "total": 7},
            is_first_innings=is_first,
            match_title="PREVIEW",
            match_no=42,
            accent_hex=(cfg.get("scorecard_color_inn1") if is_first
                        else cfg.get("scorecard_color_inn2")),
        )
        if not png:
            return "Preview render failed", 500
        from flask import Response
        return Response(png, mimetype="image/png")
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


# ── Run ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ADMIN_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
