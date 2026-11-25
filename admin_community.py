"""Community Engagement admin section + Mini App Community Arena API.

Registered onto the main admin Flask app by :func:`register_community_admin`
(called from ``admin.py`` once ``login_required`` / ``_webapp_auth`` exist).
Kept in its own module so the 1 MB ``admin.py`` doesn't grow another feature
inline.

Admin HTML routes are ``login_required``; the Mini App JSON routes under
``/api/webapp/community/`` authenticate with Telegram initData via the shared
``_webapp_auth`` helper and are CSRF-exempt (they are token-authenticated APIs).
"""

from __future__ import annotations

import logging

from flask import (render_template, request, redirect, url_for, flash,
                   jsonify, session as flask_session)

logger = logging.getLogger(__name__)


def register_community_admin(app, *, login_required, get_session, webapp_auth,
                             csrf_exempt=None, admin_telegram_id=None):
    """Attach all Community Arena admin + Mini App routes to ``app``."""
    from services import community_config_service as C
    from services import community_admin_service as A
    from services import community_game_service as G
    from services import community_xp_service as X

    def _exempt(view):
        return csrf_exempt(view) if csrf_exempt else view

    def _admin_tg_id():
        if admin_telegram_id:
            try:
                return admin_telegram_id()
            except Exception:
                return None
        return None

    # ── Dashboard ────────────────────────────────────────────────────
    @app.route("/community-arena")
    @login_required
    def admin_community():
        period = request.args.get("period", "today")
        if period not in ("today", "week", "month", "all"):
            period = "today"
        session = get_session()
        try:
            cfg = C.get_config(session, fresh=True)
            stats = A.analytics(session, period)
            active = A.active_games(session)
            recent = A.recent_games(session, limit=15)
            from services import community_level_service as L
            ladder = X._ladder(session)
            rewards = L.milestone_overview(session, -1)
            official = _official_group_id()
            return render_template(
                "admin_community.html", cfg=cfg, stats=stats, period=period,
                active_games=active, recent_games=recent, ladder=ladder,
                rewards=rewards, game_types=G.GAME_TYPES,
                game_labels=A.GAME_LABELS, official_group=official,
                bridge_available=_bridge_available())
        finally:
            session.close()

    @app.route("/community-arena/save", methods=["POST"])
    @login_required
    def admin_community_save():
        session = get_session()
        try:
            updates = A.parse_settings_form(request.form)
            C.save(session,
                   enabled=bool(request.form.get("enabled")),
                   auto_enabled=bool(request.form.get("auto_enabled")),
                   settings_updates=updates, updated_by=_admin_tg_id())
            flash("Community Arena settings saved.", "success")
        except Exception:
            logger.exception("community settings save failed")
            flash("Could not save settings.", "error")
        finally:
            session.close()
        return redirect(url_for("admin_community"))

    @app.route("/community-arena/launch", methods=["POST"])
    @login_required
    def admin_community_launch():
        game_type = request.form.get("game_type", "")
        difficulty = request.form.get("difficulty") or None
        if game_type not in G.GAME_TYPES:
            flash("Unknown game type.", "error")
            return redirect(url_for("admin_community"))
        official = _official_group_id()
        if not official:
            flash("Configure the Official Group first (Branding).", "error")
            return redirect(url_for("admin_community"))
        ok, msg = _launch_via_bridge(game_type, difficulty, _admin_tg_id())
        flash(msg, "success" if ok else "error")
        return redirect(url_for("admin_community"))

    @app.route("/community-arena/preview", methods=["POST"])
    @login_required
    def admin_community_preview():
        """Generate (but do not launch) a question to preview."""
        game_type = request.form.get("game_type", "")
        session = get_session()
        try:
            cfg = C.get_config(session, fresh=True)
            payload = G.generate_payload(session, game_type, cfg,
                                         difficulty=request.form.get("difficulty"))
            if payload is None:
                return jsonify({"ok": False, "error": "could not build"})
            # Strip the answer from the preview-safe copy.
            safe = {"game_type": payload["game_type"],
                    "question": payload.get("question"),
                    "answer_name": payload.get("answer", {}).get("name"),
                    "reward": payload.get("reward"),
                    "duration": payload.get("duration")}
            return jsonify({"ok": True, "preview": safe})
        finally:
            session.close()

    @app.route("/community-arena/cancel", methods=["POST"])
    @login_required
    def admin_community_cancel():
        gid = request.form.get("game_id")
        session = get_session()
        try:
            from models import CommunityGame
            game = (session.query(CommunityGame)
                    .filter(CommunityGame.id == int(gid),
                            CommunityGame.status == "active")
                    .with_for_update().first())
            if game:
                G.cancel_game(session, game)
                session.commit()
                flash("Game cancelled.", "success")
            else:
                flash("No such active game.", "error")
        except Exception:
            session.rollback()
            flash("Could not cancel.", "error")
        finally:
            session.close()
        return redirect(url_for("admin_community"))

    # ── User management ──────────────────────────────────────────────
    @app.route("/community-arena/users")
    @login_required
    def admin_community_users():
        query = request.args.get("q", "")
        session = get_session()
        try:
            rows = A.find_users(session, query)
            users = []
            for prof, user in rows:
                users.append({
                    "user_id": user.id, "telegram_id": user.telegram_id,
                    "name": (f"@{user.username}" if user.username else user.first_name),
                    "level": prof.level, "lifetime_xp": prof.lifetime_xp,
                    "streak": prof.current_streak,
                    "suspended": prof.rewards_suspended})
            detail = None
            uid = request.args.get("user_id")
            if uid:
                detail = _user_detail(session, int(uid))
            return render_template("admin_community_users.html",
                                   users=users, query=query, detail=detail)
        finally:
            session.close()

    @app.route("/community-arena/users/adjust", methods=["POST"])
    @login_required
    def admin_community_user_adjust():
        uid = int(request.form.get("user_id"))
        delta = int(request.form.get("delta", 0))
        reason = (request.form.get("reason") or "").strip()
        session = get_session()
        try:
            if not reason:
                flash("A reason is required for CXP adjustments.", "error")
            else:
                A.adjust_user_xp(session, uid, delta, reason, _admin_tg_id())
                flash(f"Adjusted CXP by {delta:+d}.", "success")
        except Exception:
            session.rollback()
            flash("Adjustment failed.", "error")
        finally:
            session.close()
        return redirect(url_for("admin_community_users", user_id=uid))

    @app.route("/community-arena/users/suspend", methods=["POST"])
    @login_required
    def admin_community_user_suspend():
        uid = int(request.form.get("user_id"))
        suspend = request.form.get("action") == "suspend"
        reason = (request.form.get("reason") or "").strip()
        session = get_session()
        try:
            A.set_user_suspension(session, uid, suspend, reason, _admin_tg_id())
            flash("Reward eligibility updated.", "success")
        except Exception:
            session.rollback()
            flash("Could not update suspension.", "error")
        finally:
            session.close()
        return redirect(url_for("admin_community_users", user_id=uid))

    @app.route("/community-arena/users/reset-games", methods=["POST"])
    @login_required
    def admin_community_user_reset_games():
        uid = int(request.form.get("user_id"))
        if request.form.get("confirm") != "RESET":
            flash("Type RESET to confirm.", "error")
            return redirect(url_for("admin_community_users", user_id=uid))
        session = get_session()
        try:
            A.reset_user_game_stats(session, uid, _admin_tg_id(),
                                    reason=request.form.get("reason", ""))
            flash("Game stats reset.", "success")
        except Exception:
            session.rollback()
            flash("Reset failed.", "error")
        finally:
            session.close()
        return redirect(url_for("admin_community_users", user_id=uid))

    def _user_detail(session, user_id):
        from models import User
        from services import community_profile_service as P
        user = session.get(User, user_id)
        if user is None:
            return None
        summary = P.profile_summary(session, user_id, user=user)
        ledger = A.user_ledger(session, user_id, limit=40)
        prof = X.get_profile(session, user_id)
        return {"summary": summary, "ledger": ledger,
                "suspended": prof.rewards_suspended if prof else False,
                "suspended_reason": prof.suspended_reason if prof else None}

    # ── Mini App API (Telegram initData auth) ────────────────────────
    @app.route("/api/webapp/community/overview")
    @_exempt
    def api_community_overview():
        auth, tg_id, err = webapp_auth()
        if err:
            return err
        db, user, tg_id = auth
        try:
            from services import community_profile_service as P
            from services import community_level_service as L
            summary = P.profile_summary(db, user.id, user=user)
            prof = X.get_profile(db, user.id)
            today = X.ist_today_str()
            active = _active_game_public(db)
            cfg = C.get_config(db)
            return jsonify({
                "ok": True,
                "enabled": bool(cfg.get("enabled")),
                "group": _group_branding(),
                "profile": summary,
                "checkin": {
                    "done_today": (prof.last_checkin_date == today) if prof else False,
                    "streak": (prof.current_streak if prof else 0),
                    "longest": (prof.longest_streak if prof else 0),
                    "total": (prof.total_checkins if prof else 0),
                },
                "active_game": active,
                "rewards": L.milestone_overview(db, user.id),
            })
        finally:
            db.close()

    @app.route("/api/webapp/community/leaderboard")
    @_exempt
    def api_community_leaderboard():
        auth, tg_id, err = webapp_auth()
        if err:
            return err
        db, user, tg_id = auth
        try:
            from services import community_leaderboard_service as LB
            category = request.args.get("category", "cxp")
            period = request.args.get("period", "all")
            page = max(0, int(request.args.get("page", 0) or 0))
            lb = LB.leaderboard(db, category, period, limit=10,
                                offset=page * 10, user_id=user.id)
            return jsonify({"ok": True, "leaderboard": lb})
        finally:
            db.close()

    @app.route("/api/webapp/community/games")
    @_exempt
    def api_community_games():
        auth, tg_id, err = webapp_auth()
        if err:
            return err
        db, user, tg_id = auth
        try:
            from services import community_profile_service as P
            from services import community_achievement_service as ACH
            codes = sorted(ACH.unlocked_codes(db, user.id))
            return jsonify({
                "ok": True,
                "games": P.game_summary(db, user.id),
                "achievements": {
                    "unlocked": [{"code": c, "name": ACH.CATALOG[c][0]} for c in codes],
                    "all": [{"code": c, "name": n, "req": r}
                            for c, (n, r) in ACH.CATALOG.items()],
                },
            })
        finally:
            db.close()

    # ── Mini App page shell ──────────────────────────────────────────
    @app.route("/webapp/community")
    def webapp_community():
        return render_template("webapp_community.html")

    def _active_game_public(db):
        official = _official_group_id()
        if not official:
            return None
        game = G.active_game_for_chat(db, official)
        if game is None:
            return None
        import json
        try:
            q = json.loads(game.question_json or "{}")
        except (ValueError, TypeError):
            q = {}
        remaining = max(0, int((game.deadline - _utcnow()).total_seconds()))
        return {"game_type": game.game_type,
                "label": G.GAME_LABEL.get(game.game_type, game.game_type),
                "reward": game.reward_xp, "remaining_seconds": remaining,
                "question": q}

    logger.info("Community Arena admin + Mini App routes registered")


# ── Module helpers ──────────────────────────────────────────────────────

def _utcnow():
    from datetime import datetime
    return datetime.utcnow()


def _official_group_id():
    try:
        from services.gc_gate import official_group_id
        return official_group_id()
    except Exception:
        return None


def _group_branding():
    """{'label': '@cmugames', 'url': 't.me/...'} from admin Branding config."""
    try:
        from services import gc_gate
        from services.config_service import get_config
        cfg = get_config()
        return {"label": gc_gate._group_label(cfg), "url": gc_gate.join_url(cfg)}
    except Exception:
        return {"label": None, "url": None}


def _bridge_available():
    try:
        from services import bot_bridge
        return bot_bridge.available()
    except Exception:
        return False


def _launch_via_bridge(game_type, difficulty, admin_id):
    """Launch a game on the bot's event loop from the Flask thread."""
    try:
        from services import bot_bridge
    except Exception:
        return False, "Bot bridge unavailable."
    if not bot_bridge.available():
        return False, "The bot isn't reachable right now — use /gcevent in the group."
    official = _official_group_id()

    async def factory(ctx, accept):
        from handlers.community_games import launch_and_announce
        await accept()
        return await launch_and_announce(
            ctx, official, game_type, source="manual",
            launched_by=admin_id, difficulty=difficulty)

    try:
        result = bot_bridge.submit_and_wait_for_accept(factory, timeout=15.0)
        if isinstance(result, tuple) and result and result[0]:
            return True, f"Launched {game_type} in the Official Group."
        return False, "Launch was not accepted (another game may be active)."
    except Exception:
        logger.exception("bridge launch failed")
        return False, "Launch failed."
