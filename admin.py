"""Admin panel for Cricket Bot — manage players (CRUD).
Shares the same database as the bot. Any changes here reflect in the bot instantly.
"""

import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session
from sqlalchemy import func, or_
from dotenv import load_dotenv

load_dotenv()

# ── Import shared DB and models ─────────────────────────────────────
from database import get_session, init_db
from models import Player, User, Trade, UserStats, UserRoster, ActivityLog

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_SECRET", os.urandom(24).hex())

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PER_PAGE = 30


# ── Helpers ──────────────────────────────────────────────────────────

def tier_css(rating: int) -> str:
    if rating >= 95:   return "legendary"
    elif rating >= 90: return "epic"
    elif rating >= 85: return "rare"
    elif rating >= 80: return "uncommon"
    elif rating >= 70: return "common"
    else:              return "basic"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
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
        rating_min = request.args.get("rating_min", "").strip()
        rating_max = request.args.get("rating_max", "").strip()
        page = max(1, int(request.args.get("page", 1)))

        query = db.query(Player)

        if q:
            query = query.filter(Player.name.ilike(f"%{q}%"))
        if category:
            query = query.filter(Player.category == category)
        if country_filter:
            query = query.filter(Player.country == country_filter)
        if rating_min:
            query = query.filter(Player.rating >= int(rating_min))
        if rating_max:
            query = query.filter(Player.rating <= int(rating_max))

        total = query.count()
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)

        players = (
            query.order_by(Player.rating.desc(), Player.name)
            .offset((page - 1) * PER_PAGE)
            .limit(PER_PAGE)
            .all()
        )

        # Add tier CSS class to each player for template
        for p in players:
            p._tier_css = tier_css(p.rating)

        # Get unique categories and countries for filters
        categories = [r[0] for r in db.query(Player.category).distinct().order_by(Player.category).all()]
        countries = [r[0] for r in db.query(Player.country).distinct().order_by(Player.country).all()]

        return render_template(
            "players.html",
            players=players, total=total, page=page, total_pages=total_pages,
            q=q, category=category, country_filter=country_filter,
            rating_min=rating_min, rating_max=rating_max,
            categories=categories, countries=countries,
        )
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

            db.commit()
            flash(f"Player '{player.name}' updated", "success")
            return redirect(url_for("players_list"))

        return render_template("player_form.html", player=player)
    except Exception as e:
        db.rollback()
        flash(f"Error: {e}", "error")
        return redirect(url_for("players_list"))
    finally:
        db.close()


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
        db.delete(player)
        db.commit()
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
            db.commit()
            status = "activated" if player.is_active else "deactivated"
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

        return render_template("user_detail.html", user=user, stats=stats,
                               roster=roster, activities=activities)
    finally:
        db.close()


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

            scale = max(0.2, (rating - 50) / 50)
            is_bat = category in ("Batsman", "Wicket Keeper")
            is_bowl = category == "Bowler"

            if is_bat:
                bat_avg = round(random.uniform(20, 32) + scale * random.uniform(10, 25), 1)
                sr = round(random.uniform(55, 75) + scale * random.uniform(10, 50), 1)
                runs = int(random.uniform(500, 3000) + scale * random.uniform(2000, 12000))
                centuries = int(scale * random.uniform(1, 45))
                bowl_avg = round(random.uniform(30, 80), 1) if bowl_rating > 20 else 0.0
                economy = round(random.uniform(4.0, 8.0), 1) if bowl_rating > 20 else 0.0
                wickets = int(random.uniform(0, 20) * scale) if bowl_rating > 20 else 0
            elif is_bowl:
                bat_avg = round(random.uniform(5, 18) + scale * random.uniform(2, 12), 1)
                sr = round(random.uniform(30, 60) + scale * random.uniform(5, 30), 1)
                runs = int(random.uniform(50, 500) + scale * random.uniform(100, 2000))
                centuries = 0
                bowl_avg = max(12.0, round(random.uniform(18, 35) - scale * random.uniform(0, 8), 1))
                economy = max(2.5, round(random.uniform(3.0, 6.5) - scale * random.uniform(0, 1.5), 1))
                wickets = int(random.uniform(30, 100) + scale * random.uniform(50, 400))
            else:
                bat_avg = round(random.uniform(18, 28) + scale * random.uniform(5, 20), 1)
                sr = round(random.uniform(55, 75) + scale * random.uniform(5, 35), 1)
                runs = int(random.uniform(500, 2000) + scale * random.uniform(1000, 6000))
                centuries = int(scale * random.uniform(0, 20))
                bowl_avg = max(15.0, round(random.uniform(22, 40) - scale * random.uniform(0, 8), 1))
                economy = max(3.0, round(random.uniform(3.5, 6.5) - scale * random.uniform(0, 1.0), 1))
                wickets = int(random.uniform(20, 80) + scale * random.uniform(30, 250))

            player = Player(
                name=name, version=version, rating=rating, category=category,
                country=country, bat_hand=bat_hand, bowl_hand=bowl_hand,
                bowl_style=bowl_style, bat_rating=bat_rating, bowl_rating=bowl_rating,
                bat_avg=bat_avg, strike_rate=sr, runs=runs, centuries=centuries,
                bowl_avg=bowl_avg, economy=economy, wickets=wickets, is_active=True,
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


# ── Run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ADMIN_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
