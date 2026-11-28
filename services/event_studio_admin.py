"""Flask integration for Events Studio and the signed Events Hub API."""

from datetime import datetime
import json

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from database import get_session
from models import ChallengeLeague
from models_events import StudioAudit, StudioEnrollment, StudioEntitlement, StudioEvent, StudioGame, StudioRevision
from services import event_studio as studio, event_studio_import as imports, event_studio_play as play
from services.event_studio_rules import liability
from services.event_studio_schedule import utc_instant, utc_text

MAX_CONFIG = 300_000


def _decode(value):
    if not value or len(value) > MAX_CONFIG:
        raise ValueError("The event configuration is missing or too large.")
    return studio.normalize(json.loads(value))


def _draft_form():
    raw = _decode(request.form["config"])
    for key in ("kind", "format", "timezone", "opens_at", "closes_at", "description"):
        if key in request.form:
            raw[key] = request.form[key].strip()
    return studio.normalize(raw)


def _actor():
    return str(session.get("admin_username") or "admin")[:100]


def _screen(db, event=None, config=None, preview=None, error=None):
    events = db.scalars(select(StudioEvent).order_by(StudioEvent.id.desc()).limit(100)).all()
    rev = studio.revision(db, event) if event else None
    current = config if config is not None else rev.config if rev else studio.defaults()
    events_activity = None
    history = []
    audit = []
    if event:
        history = db.scalars(select(StudioRevision).filter_by(event_id=event.id)
                             .order_by(StudioRevision.number.desc()).limit(20)).all()
        if event.status != "draft":
            audit = db.scalars(select(StudioAudit).filter_by(event_id=event.id)
                              .order_by(StudioAudit.id.desc()).limit(20)).all()
            entries = select(StudioEnrollment.id).where(StudioEnrollment.event_id == event.id)
            events_activity = {
                "enrolled": db.scalar(select(func.count()).select_from(entries.subquery())),
                "results": db.execute(select(StudioGame.status, func.count()).where(
                    StudioGame.enrollment_id.in_(entries)).group_by(StudioGame.status)).all(),
                "recent_fixtures": db.execute(select(StudioGame.id, StudioGame.fixture_key, StudioGame.status,
                                                     StudioGame.deadline, StudioGame.failure_type)
                                             .where(StudioGame.enrollment_id.in_(entries))
                                             .order_by(StudioGame.id.desc()).limit(25)).all(),
                "claims": db.execute(select(StudioEntitlement.claimed_at.is_not(None), func.count()).where(
                    StudioEntitlement.enrollment_id.in_(entries)).group_by(
                        StudioEntitlement.claimed_at.is_not(None))).all(),
            }
    clean_config = {key: value for key, value in current.items() if key in studio.defaults()}
    from services.pitch_report import WEATHERS
    return render_template("admin_event_studio.html", events=events, event=event, config=current,
                           config_json=json.dumps(clean_config, indent=2, ensure_ascii=False),
                           leagues=db.scalars(select(ChallengeLeague).order_by(ChallengeLeague.name).limit(200)).all(),
                           history=history, stale=studio.stale(current), preview=preview, error=error,
                           activity=events_activity, audit=audit, maximum=liability(current),
                           pitches=[pitch for pitch in studio.PITCHES if pitch not in studio.LEGACY_PITCHES],
                           weathers=WEATHERS, stadium_countries=_stadium_countries(),
                           publish_enabled=studio.allowed(current["kind"], current["format"]))


def _stadium_countries():
    """The countries Stadium Data has a ground in, for the venue-country help."""
    from services.tournament_stadiums import all_rows
    return sorted({str(row.get("country")).strip() for row in all_rows() if row.get("country")})


def _change(db, event, expected, action):
    if event.status != "draft" or expected != event.draft_number:
        raise studio.Conflict("The draft changed. Reload it before making changes.")
    if action == "publish":
        studio.publish(db, event, expected, _actor(), datetime.utcnow())
        return None
    if action == "restore":
        old = db.get(StudioRevision, (event.id, int(request.form["restore_number"])))
        if not old:
            raise ValueError("Draft revision was not found.")
        studio.save(db, event, expected, old.config, _actor(), "restore")
        return None
    config = _draft_form()
    g.studio_config = config
    if action == "copy":
        league_id = (request.form.get("league_id") or "").strip()
        if not league_id.isdigit():
            raise ValueError("Choose a Challenge League before copying teams and squads.")
        copied = studio.copied_teams(db, int(league_id))
        config.update(copied)
        studio.save(db, event, expected, config, _actor(), "copy_squads")
        g.studio_copied = copied["teams"]
    elif action in ("import_teams", "import_squads"):
        text = request.form.get("import_data", "")
        if not text or len(text) > 100_000:
            raise ValueError("Import between 1 and 100,000 characters.")
        config["teams"] = (imports.teams_import(text, config["teams"]) if action == "import_teams"
                           else imports.squad_import(db, text, config["teams"]))
        config = studio.normalize(config)
        return config, {"key": "import", "old_fixtures": len(config["fixtures"]),
                        "old_max": liability(config)}
    elif action in (*studio.ACTIONS, "all"):
        regenerated = studio.generate(config, action, overwrite=request.form.get("overwrite") == "yes")
        return regenerated, {"key": action, "old_fixtures": len(config["fixtures"]),
                             "old_max": liability(config)}
    elif action == "validate":
        frozen, opens, closes, maximum = studio.validate_publish(db, config, datetime.utcnow())
        editions = studio.rotation_editions(frozen, opens, closes, maximum)
        return config, {"key": "validate", "maximum": maximum,
                        "editions": [(utc_text(start), utc_text(end)) for _, start, end in editions]}
    elif action == "save" or action == "apply":
        studio.save(db, event, expected, config, _actor(), action)
    else:
        raise ValueError("Unknown draft action.")
    return None


def install(app, login_required, csrf_exempt, webapp_auth):
    @app.route("/events/studio", methods=["GET", "POST"])
    @login_required
    def admin_event_studio():
        with get_session() as db:
            if request.method == "GET":
                return _screen(db)
            if request.content_length is not None and request.content_length > MAX_CONFIG * 4:
                abort(413)
            try:
                initial = None
                copy = request.form.get("copy_event")
                imported = request.form.get("import_config")
                if copy:
                    source = db.get(StudioEvent, int(copy))
                    if source is None:
                        raise ValueError("Source event was not found.")
                    raw = studio.revision(db, source).config
                    initial = {key: value for key, value in raw.items() if key in studio.defaults()}
                elif imported:
                    initial = _decode(imported)
                event = studio.create(db, request.form.get("name"), request.form.get("slug", ""), _actor(), initial)
                db.commit()
                return redirect(url_for("admin_event_studio_detail", event_id=event.id))
            except (ValueError, KeyError, TypeError, AttributeError, IntegrityError) as exc:
                db.rollback()
                message = str(exc) if isinstance(exc, ValueError) else "Check the event fields and ensure the slug is unique."
                return _screen(db, error=message), 400

    @app.route("/events/studio/<int:event_id>", methods=["GET", "POST"])
    @login_required
    def admin_event_studio_detail(event_id):
        with get_session() as db:
            event = db.get(StudioEvent, event_id)
            if event is None:
                abort(404)
            if request.method == "GET":
                return _screen(db, event)
            if request.content_length is not None and request.content_length > MAX_CONFIG * 4:
                abort(413)
            try:
                event = studio.lock(db, StudioEvent, event_id)
                action = request.form.get("action")
                if action == "extend":
                    config = studio.revision(db, event, published=True)
                    if config is None:
                        raise ValueError("Publish the draft before extending an official fixture.")
                    deadline = utc_instant(request.form["deadline"], config.config["timezone"])
                    play.extend_deadline(db, event, int(request.form["game_id"]), deadline, _actor(),
                                         request.form.get("reason", ""), datetime.utcnow())
                    preview = None
                elif action == "resolve_failure":
                    play.resolve_technical_failure(db, event, int(request.form["game_id"]), _actor(),
                                                   request.form.get("reason", ""), datetime.utcnow())
                    preview = None
                elif action in ("pause", "resume", "archive", "retry", "cancel"):
                    if action == "cancel" and request.form.get("confirm") != "yes":
                        raise ValueError("Confirm event cancellation.")
                    play.operate(db, event, action, _actor(), request.form.get("reason", ""), datetime.utcnow())
                    preview = None
                else:
                    preview = _change(db, event, int(request.form["revision"]), action)
                if preview:
                    config, report = preview
                    db.rollback()
                    return _screen(db, event, config, report)
                db.commit()
                copied = getattr(g, "studio_copied", None)
                if copied is not None:
                    cards = sum(len(team["players"]) for team in copied)
                    flash(f"Copied {len(copied)} team{'' if len(copied) == 1 else 's'} and "
                          f"{cards} card{'' if cards == 1 else 's'} into Team details and Squads.", "info")
                else:
                    flash("Event Studio changes saved.", "info")
                return redirect(url_for("admin_event_studio_detail", event_id=event_id))
            except studio.Conflict as exc:
                db.rollback()
                return _screen(db, event, error=str(exc)), 409
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                db.rollback()
                message = str(exc) if isinstance(exc, ValueError) else "The submitted configuration has invalid or missing fields."
                return _screen(db, event, config=getattr(g, "studio_config", None), error=message), 400

    @app.get("/events/studio/<int:event_id>/export")
    @login_required
    def admin_event_studio_export(event_id):
        with get_session() as db:
            event = db.get(StudioEvent, event_id)
            if not event:
                abort(404)
            raw = studio.revision(db, event).config
            export = {key: value for key, value in raw.items() if key in studio.defaults()}
            return export, 200, {"Content-Disposition": f'attachment; filename="{event.slug}.json"'}

    def api(handler, *, write=False):
        if request.content_length is not None and request.content_length > 8192:
            return {"ok": False, "message": "The event request is too large."}, 413
        auth, _, error = webapp_auth()
        if error:
            return error
        db, user, _ = auth
        with db:
            try:
                now = datetime.utcnow()
                payload = request.get_json(silent=True)
                if payload is None:
                    payload = {}
                if not isinstance(payload, dict):
                    return {"ok": False, "message": "Enter a valid request."}, 400
                result = handler(db, user, payload, now)
                if write:
                    db.commit()
                return {"ok": True, "server_time": utc_text(now), **result}
            except studio.ExecutionUnavailable as exc:
                db.commit()
                return {"ok": False, "error": "engine_unavailable", "message": str(exc)}, 503
            except studio.Conflict as exc:
                db.rollback()
                return {"ok": False, "error": "conflict", "message": str(exc)}, 409
            except (ValueError, KeyError, TypeError) as exc:
                db.rollback()
                return {"ok": False, "message": str(exc) if isinstance(exc, ValueError)
                        else "Invalid event request."}, 400

    @app.post("/api/webapp/events-hub")
    @csrf_exempt
    def webapp_event_hub():
        return api(lambda db, user, body, now: {"events": play.list_events(db, user, now)})

    @app.post("/api/webapp/events-hub/<int:event_id>")
    @csrf_exempt
    def webapp_event_detail(event_id):
        return api(lambda db, user, body, now: {"event": play.detail(db, user, event_id, now)})

    @app.post("/api/webapp/events-hub/progression/<int:event_id>")
    @csrf_exempt
    def webapp_event_progression(event_id):
        return api(lambda db, user, body, now: play.progression(db, user, event_id))

    @app.post("/api/webapp/events-hub/scorecard/<int:game_id>")
    @csrf_exempt
    def webapp_event_scorecard(game_id):
        return api(lambda db, user, body, now: {"scorecard": play.scorecard(db, user, game_id)})

    @app.post("/api/webapp/events-hub/arena/<int:game_id>")
    @csrf_exempt
    def webapp_event_arena(game_id):
        def dispatch(db, user, data, now):
            action = data.get("type")
            if action:
                version = data.get("version")
                if type(version) is not int:
                    raise ValueError("Refresh this match before making your selection.")
                if action in ("pause", "resume", "switch"):
                    play.set_play_state(db, user, game_id, version, action, "app")
                elif action in ("retire_batsman", "live_batting_order"):
                    args = data.get("action")
                    if not isinstance(args, dict):
                        raise ValueError("Invalid batting selection.")
                    play.change_batting(db, user, game_id, version, action, args, source="app")
                elif action == "impact":
                    args = data.get("action")
                    if not isinstance(args, dict):
                        raise ValueError("Invalid Impact Player selection.")
                    play.use_impact(db, user, game_id, version, args.get("inRosterId"),
                                    args.get("outRosterId"), args.get("batPosition"), source="app")
                elif action == "abandon" and data.get("confirmed") is True:
                    play.abandon(db, user, game_id, version)
                else:
                    from services.match_webapp_service import APPROACH_ACTION_TYPES, approach_pick_value
                    kind = "continue" if action == "continue" else APPROACH_ACTION_TYPES.get(action)
                    if kind in (None, "concussion"):
                        raise ValueError("Invalid selection.")
                    value = 0 if kind == "continue" else approach_pick_value(kind, data.get("action"))
                    play.submit_pick(db, user, game_id, version, kind, value, source="app")
                db.flush()
            return play.arena(db, user, game_id, now)
        return api(dispatch, write=True)

    @app.post("/api/webapp/events-hub/action")
    @csrf_exempt
    def webapp_event_action():
        def dispatch(db, user, body, now):
            def number(key):
                value = body.get(key)
                if type(value) is not int or not 0 <= value <= 2**31 - 1:
                    raise ValueError(f"Invalid {key}.")
                return value

            action = body.get("action")
            if action == "join":
                play.join(db, user, number("event_id"), body.get("team"), body.get("mode"), now)
            elif action == "setup":
                return {"setup": play.setup_fixture(
                    db, user, number("event_id"), number("game_id"), number("previous_attempt"),
                    now, call=body.get("call"), decision=body.get("decision"),
                    xi=body.get("xi"), edit=body.get("edit") is True)}
            elif action == "start":
                game = play.start_fixture(db, user, number("game_id"), None, source="app",
                                          require_toss=True, require_confirmed=True)
                return {"game_id": game.id}
            elif action == "attempt":
                game = play.start_attempt(db, user, number("event_id"), number("previous_attempt"), source="app",
                                          require_toss=True, require_confirmed=True)
                return {"game_id": game.id}
            elif action == "abandon" and body.get("confirmed") is True:
                play.abandon(db, user, number("game_id"), number("version"))
            elif action == "claim":
                reward = number("reward_id") if body.get("reward_id") is not None else None
                play.claim(db, user, number("event_id"), reward, now)
            else:
                raise ValueError("Unknown event action or confirmation missing.")
            return {}
        return api(dispatch, write=True)
