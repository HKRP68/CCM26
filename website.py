"""Standalone franchise website sharing the bot's auction services and DB.

Importing this module never initializes or migrates the database. Run
``python website.py init-db`` explicitly when provisioning, then deploy
``gunicorn website:app`` alongside the existing bot/scheduler worker.
"""

import argparse
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import threading
import time
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, session

from services.website_auth import (SESSION_MAX_AGE_SECONDS, positive_integer,
                                   valid_bot_token, verify_widget_login)

load_dotenv()
logger = logging.getLogger(__name__)


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        WEBSITE_SECRET=os.getenv("WEBSITE_SECRET", "").strip(),
        BOT_TOKEN=os.getenv("BOT_TOKEN", "").strip(),
        BOT_USERNAME=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
        SESSION_COOKIE_NAME="ccm26_franchise_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.getenv("WEBSITE_HTTPS", "true").lower() != "false",
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_MAX_AGE_SECONDS),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=2_000_000,
    )
    if test_config:
        app.config.update(test_config)
    # Missing persistent configuration can serve the setup page, but cannot
    # establish browser identities. Never reuse the password-admin secret.
    app.secret_key = app.config["WEBSITE_SECRET"] or secrets.token_hex(32)

    def configured():
        username = app.config["BOT_USERNAME"]
        secret = app.config["WEBSITE_SECRET"]
        return (valid_bot_token(app.config["BOT_TOKEN"])
                and isinstance(secret, str) and len(secret) >= 32
                and isinstance(username, str)
                and bool(re.fullmatch(r"[A-Za-z0-9_]{5,32}", username))
                and username.lower().endswith("bot"))

    def csrf_token():
        token = session.get("website_csrf")
        if not isinstance(token, str) or len(token) < 32:
            token = secrets.token_urlsafe(32)
            session["website_csrf"] = token
        return token

    def check_csrf():
        expected = session.get("website_csrf")
        supplied = request.headers.get("X-CSRF-Token")
        return (isinstance(expected, str) and isinstance(supplied, str)
                and bool(expected) and supplied.isascii() and expected.isascii()
                and hmac.compare_digest(expected, supplied))

    def browser_user():
        if not configured():
            return None
        profile = session.get("website_user")
        issued = session.get("website_authenticated_at")
        if (not isinstance(profile, dict) or positive_integer(profile.get("id")) is None
                or isinstance(issued, bool) or not isinstance(issued, (int, float))
                or not 0 <= time.time() - issued <= SESSION_MAX_AGE_SECONDS):
            session.pop("website_user", None)
            session.pop("website_authenticated_at", None)
            return None
        return profile

    def session_payload():
        user = browser_user()
        return dict(ok=True, user=user, csrf_token=csrf_token(),
                    bot_username=app.config["BOT_USERNAME"], login_configured=bool(configured()),
                    expires_at=int(session["website_authenticated_at"] + SESSION_MAX_AGE_SECONDS)
                    if user else None)

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith("/api/") or request.path in ("/", "/franchise"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return dict(ok=False, error="request_too_large", message="Requests must be under 2 MB."), 413

    @app.route("/")
    @app.route("/franchise")
    def franchise():
        return render_template("franchise_site.html", bot_username=app.config["BOT_USERNAME"],
                               login_configured=bool(configured()), csrf_token=csrf_token())

    @app.get("/api/franchise/auth/session")
    def auth_session():
        return session_payload()

    @app.post("/api/franchise/auth/login")
    def auth_login():
        if not check_csrf():
            return dict(ok=False, error="csrf", message="Refresh the page before signing in."), 403
        if not configured():
            return dict(ok=False, error="login_unavailable",
                        message="Telegram website login has not been configured."), 503
        profile = verify_widget_login(request.get_json(silent=True), app.config["BOT_TOKEN"])
        if profile is None:
            return dict(ok=False, error="bad_telegram_login",
                        message="Telegram login is invalid or expired. Sign in again."), 401
        session.clear()
        session["website_user"] = profile
        session["website_authenticated_at"] = time.time()
        session.permanent = True
        return session_payload()

    @app.post("/api/franchise/auth/logout")
    def auth_logout():
        if not check_csrf():
            return dict(ok=False, error="csrf", message="Refresh the page before signing out."), 403
        session.clear()
        return session_payload()

    def authenticate(allow_not_debuted=True, *, require_csrf=True):
        """Match the shared auction callback contract; never trust client roles."""
        token = app.config["BOT_TOKEN"]
        if not valid_bot_token(token):
            return None, None, (dict(ok=False, error="login_unavailable",
                                    message="Telegram website login has not been configured."), 503)
        if "Authorization" in request.headers:
            # An explicit but bad header cannot borrow a logged-in browser's
            # identity. Development identities are never supported here.
            raw = request.headers.get("Authorization", "").removeprefix("tma ")
            from services.webapp_auth import get_user_id, verify_init_data
            verified = verify_init_data(raw, token)
            tg_id = positive_integer(get_user_id(verified))
            try:
                auth_date = int((verified or {}).get("auth_date", 0))
            except (ValueError, TypeError):
                auth_date = 0
            if tg_id is None or not 0 <= time.time() - auth_date <= SESSION_MAX_AGE_SECONDS:
                return None, None, (dict(ok=False, error="bad_init_data",
                                        message="A verified Telegram login is required."), 401)
        else:
            profile = browser_user()
            if profile is None:
                return None, None, (dict(ok=False, error="login_required",
                                        message="Sign in with Telegram to open the auction."), 401)
            if require_csrf and not check_csrf():
                return None, None, (dict(ok=False, error="csrf",
                                        message="Your page session changed. Refresh before continuing."), 403)
            tg_id = profile["id"]

        from database import get_session
        from models import User
        from services.config_service import get_config
        from services import maintenance_service as maintenance, rookie_gate
        db = get_session()
        try:
            user = db.query(User).filter(User.telegram_id == tg_id).first()
            if user is not None and user.is_banned:
                db.close()
                return None, tg_id, (dict(ok=False, error="banned",
                                          message="This account is suspended."), 403)
            cfg = get_config(db)
            if maintenance.should_block_miniapp_path("/api/webapp/auction", tg_id, cfg):
                db.close()
                return None, tg_id, (dict(ok=False, error="maintenance",
                                          message=maintenance.get_maintenance_message(cfg)), 503)
            if rookie_gate.is_rookie_mode_active(cfg) and rookie_gate.should_block_miniapp_path("/api/webapp/auction",
                    has_membership=rookie_gate.has_access(user), telegram_id=tg_id, cfg=cfg):
                db.close()
                return None, tg_id, (dict(ok=False, error="rookie_required",
                                          message=rookie_gate.rookie_required_message(cfg)), 403)
            return (db, user, tg_id), tg_id, None
        except Exception:
            db.close()
            logger.exception("Standalone website account check failed")
            return None, tg_id, (dict(ok=False, error="unavailable",
                                      message="The website is temporarily unavailable. Try again shortly."), 503)

    def member_rpc(method, json_body=None, params=None, timeout=4):
        # Read-only Telegram membership lookup; HTTP actions never send bot
        # messages. Keep token-bearing request URLs out of exception logs.
        import requests
        if method != "getChatMember" or not valid_bot_token(app.config["BOT_TOKEN"]):
            return None
        try:
            result = requests.get("https://api.telegram.org/bot" + app.config["BOT_TOKEN"] + "/" + method,
                                  params=params, timeout=timeout)
            return result.json() if result.ok else None
        except (requests.RequestException, ValueError):
            logger.warning("Telegram auction membership lookup unavailable")
            return None

    from services.auction_webapp_routes import register
    register(app, authenticate, lambda view: view, member_rpc)

    render_slots = threading.BoundedSemaphore(2)

    @app.get("/webapp/player-card/<int:player_id>")
    def player_card(player_id):
        auth, _tg_id, error = authenticate(require_csrf=False)
        if error:
            return error
        db, _user, _tg_id = auth
        if not render_slots.acquire(blocking=False):
            db.close()
            return Response("Card renderer busy", status=503, headers={"Retry-After": "2"})
        try:
            from models import Player
            player = db.get(Player, player_id)
            if player is None or not player.is_active:
                return Response("Player not found", status=404)
            data = None
            if request.args.get("portrait") == "1":
                from services.player_portrait_service import get_player_portrait
                portrait = get_player_portrait(player, include_global=False)
                if portrait is not None:
                    portrait.thumbnail((480, 600))
                    buffer = io.BytesIO()
                    portrait.save(buffer, format="PNG")
                    data = buffer.getvalue()
            if data is None:
                from services.card_generator import generate_card
                data = generate_card(player)
            if not data:
                return Response("Card unavailable", status=503)
            mime = "image/png"
            if data.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif data.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "image/webp"
            response = Response(data, mimetype=mime)
            response.set_etag(hashlib.sha256(data).hexdigest())
            response.headers["Cache-Control"] = "private, max-age=300"
            return response.make_conditional(request)
        except Exception:
            logger.exception("Standalone website card rendering failed")
            return Response("Card unavailable", status=503)
        finally:
            db.close()
            render_slots.release()

    return app


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "init-db"), nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "init-db":
        from database import init_db
        init_db()
    else:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", os.getenv("WEBSITE_PORT", "5001"))),
                debug=False, use_reloader=False)
