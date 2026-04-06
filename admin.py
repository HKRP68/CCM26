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
from models import Player, User, Trade

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


# ── Run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ADMIN_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
