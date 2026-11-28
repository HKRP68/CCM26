"""Flask integration for Events Studio and the signed Events Hub API."""

from datetime import datetime
import json

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from database import get_session
from models import ChallengeLeague, User
from models_events import (
    StudioAudit, StudioAwardMedia, StudioCompetition, StudioEnrollment, StudioEntitlement, StudioEvent, StudioFixture,
    StudioGame, StudioGroupNotice, StudioParticipant, StudioRevision, StudioStarClaim,
)
from services import event_studio as studio, event_studio_import as imports, event_studio_play as play
from services import event_studio_competition as competitions, event_studio_objectives as goals
from services.economy_transactions import reserve_miniapp_action
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
            if goals.tournament(current):
                events_activity["competitions"] = db.execute(
                    select(StudioCompetition, func.count(StudioParticipant.id),
                           func.sum(case((StudioParticipant.user_id.is_not(None), 1), else_=0)),
                           func.coalesce(User.first_name, User.username, "Manager"))
                    .join(User, User.id == StudioCompetition.host_id)
                    .outerjoin(StudioParticipant, StudioParticipant.competition_id == StudioCompetition.id)
                    .where(StudioCompetition.event_id == event.id)
                    .group_by(StudioCompetition.id, User.first_name, User.username)
                    .order_by(StudioCompetition.id.desc()).limit(50)).all()
                events_activity["award_art_ready"] = set(db.scalars(
                    select(StudioAwardMedia.competition_id).join(StudioCompetition)
                    .where(StudioCompetition.event_id == event.id, StudioAwardMedia.generated_at.is_not(None))))
                events_activity["canonical"] = db.scalars(
                    select(StudioFixture).join(StudioCompetition, StudioCompetition.id == StudioFixture.competition_id)
                    .where(StudioCompetition.event_id == event.id)
                    .order_by(StudioFixture.failure_at.is_(None), StudioFixture.id.desc()).limit(50)).all()
                events_activity["delivery"] = db.scalars(select(StudioGroupNotice).join(StudioCompetition)
                    .where(StudioCompetition.event_id == event.id)
                    .order_by(StudioGroupNotice.id.desc()).limit(30)).all()
                events_activity["star_claims"] = db.execute(
                    select(StudioStarClaim, StudioParticipant.competition_id, StudioParticipant.team_key)
                    .join(StudioParticipant, StudioParticipant.id == StudioStarClaim.participant_id)
                    .where(StudioParticipant.event_id == event.id).order_by(StudioStarClaim.id.desc()).limit(80)).all()
                members = db.scalars(select(StudioParticipant).where(
                    StudioParticipant.event_id == event.id, StudioParticipant.user_id.is_not(None))
                    .order_by(StudioParticipant.id.desc()).limit(40)).all()
                events_activity["objective_progress"] = [(member, competitions._progress(db, member, current, datetime.utcnow()))
                                                          for member in members]
    clean_config = {key: value for key, value in current.items() if key in studio.defaults()}
    from services.event_studio_items import liability as inventory_liability
    from services.pitch_report import WEATHERS
    return render_template("admin_event_studio.html", events=events, event=event, config=current,
                           config_json=json.dumps(clean_config, indent=2, ensure_ascii=False),
                           leagues=db.scalars(select(ChallengeLeague).order_by(ChallengeLeague.name).limit(200)).all(),
                           history=history, stale=studio.stale(current), preview=preview, error=error,
                           inventory_maximum=inventory_liability(current),
                           activity=events_activity, audit=audit, maximum=liability(current),
                           pitches=[pitch for pitch in studio.PITCHES if pitch not in studio.LEGACY_PITCHES],
                           weathers=WEATHERS, stadium_countries=_stadium_countries(),
                           publish_enabled=studio.allowed(current["kind"], current["format"]),
                           news_preview=_news_preview(db, event, current),
                           objective_metrics=goals.METRICS, objective_max=goals.max_stars(current),
                           objective_warnings=goals.warnings(current),
                           multi_enabled=competitions.mode_enabled(current, "multi"))


def _news_preview(db, event, config):
    if not event or not event.published_number or not config.get("competition", {}).get("news"):
        return []
    from services import event_studio_insights as newsroom
    rooms = db.scalars(select(StudioCompetition).filter_by(event_id=event.id)
                       .where(StudioCompetition.started_at.is_not(None))
                       .order_by(StudioCompetition.id.desc()).limit(4))
    result = []
    for room in rooms:
        frozen = competitions._table_config(competitions._config(db, room))
        result.extend(newsroom.match_news(frozen, competitions._rows(db, room),
                      newsroom.performances(db, room.id), limit=5)["items"])
    return sorted(result, key=lambda item: item["recorded_at"], reverse=True)


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
                if action in ("close_lobby", "retry_fixture", "void_fixture", "extend_fixture"):
                    competitions.administer(db, event_id, _actor(), "extend" if action == "extend_fixture" else action,
                                            int(request.form["target_id"]), request.form.get("reason", ""),
                                            datetime.utcnow(), deadline=request.form.get("deadline"))
                    preview = None
                elif action == "extend":
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
                    if request.is_json and request.content_length:
                        return {"ok": False, "message": "Enter valid event request JSON."}, 400
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
                if not isinstance(exc, ValueError):
                    from flask import current_app
                    current_app.logger.exception("Invalid Events Hub execution")
                db.rollback()
                return {"ok": False, "message": str(exc) if isinstance(exc, ValueError)
                        else "Invalid event request."}, 400

    def limit_action(db, user):
        delay = reserve_miniapp_action(db, user)
        db.commit()
        if delay:
            raise ValueError(f"Try again in {delay}s.")

    from services import competition_sources as universe_sources

    app.jinja_env.globals["competition_universe_enabled"] = universe_sources.enabled
    app.jinja_env.globals["competition_universe_feature"] = universe_sources.flag

    @app.post("/api/webapp/competition-universe")
    @csrf_exempt
    def webapp_competition_universe():
        def dispatch(db, user, body, now):
            from services import competition_analytics, competition_collection, competition_rankings, competition_universe

            if not universe_sources.enabled() or user.is_banned:
                raise ValueError("Competition Universe is unavailable.")
            action = body.get("action", "directory")
            index = body.get("page", 0)
            if action == "directory":
                return competition_universe.directory(db, user, now, source=body.get("source", "all"),
                    status=body.get("status", "all"), fmt=body.get("format"), page=index)
            if action == "rankings":
                return competition_rankings.page(db, user, scope=body.get("scope", "overall"),
                    fmt=body.get("format", "all"), season=body.get("season", 0), index=index)
            if action == "collection":
                return competition_collection.page(db, user, index, source=body.get("source"),
                    mode=body.get("mode"), fmt=body.get("format"), season=body.get("season"))
            if action in ("analytics", "csv"):
                key = body.get("instance")
                if type(key) is not int or key < 1:
                    raise ValueError("Choose a competition instance.")
                result = competition_analytics.build(db, user, body.get("source"), key, body)
                return {"csv": competition_analytics.export(result)} if action == "csv" else result
            raise ValueError("Choose a valid Competition Universe section.")
        return api(dispatch)

    @app.route("/competitions/operations", methods=["GET", "POST"])
    @login_required
    def admin_competition_operations():
        from models import AdminLog, ChallengeLeague, Tournament
        from models_universe import CompetitionCatalogue, CompetitionCatalogueLink
        from services import competition_collection, competition_operations, competition_rankings

        if not universe_sources.enabled():
            abort(404)
        with get_session() as db:
            now = datetime.utcnow()
            report = error = None
            if request.method == "POST":
                try:
                    action = request.form.get("action")
                    if action in ("dry_run", "refresh"):
                        report = {}
                        dry = action == "dry_run"
                        if universe_sources.flag("RANKINGS"):
                            report["Rankings"] = competition_rankings.rebuild(db, now, dry_run=dry, force=True)
                        if universe_sources.flag("COLLECTION"):
                            report["Collection"] = competition_collection.sync(db, now, dry_run=dry)
                        if not dry:
                            db.add(AdminLog(action="competition_projection", detail=f"Refreshed by {_actor()}"))
                    elif action in ("rotation_preview", "rotation_approve"):
                        template_id, weeks, count = (int(request.form.get(key, "")) for key in ("template_id", "weeks", "count"))
                        opening = request.form.get("first", "")
                        if action == "rotation_preview":
                            event, rev, plan = competition_operations.preview(db, template_id, opening, weeks, count, now)
                            report = {"Rotation preview": [{**row, "opens": utc_text(row["opens"]), "close": utc_text(row["close"])}
                                                            for row in plan],
                                      "approval": {"template_id": event.id, "revision": rev.number,
                                                   "first": opening, "weeks": weeks, "count": count}}
                        else:
                            template = studio.lock(db, StudioEvent, template_id)
                            if template is None or template.published_number != int(request.form.get("revision", "0")):
                                raise ValueError("The template changed. Preview the rotation again.")
                            count = competition_operations.queue(db, template_id, opening, weeks, count, _actor(),
                                                                  request.form.get("reason"), now)
                            report = {"Queued or already scheduled": count}
                    elif action == "rotation_retry":
                        reason = (request.form.get("reason") or "").strip()
                        if not 1 <= len(reason) <= 500:
                            raise ValueError("Enter a short retry reason.")
                        job = competition_operations.retry(db, int(request.form.get("job_id", "")), now)
                        db.add(AdminLog(action="competition_rotation_retry", target_type="competition_rotation",
                                        target_id=job.id, detail=f"{_actor()} · {reason}"))
                        report = {"Rotation retry queued": job.id}
                    elif action == "catalogue":
                        if not universe_sources.flag("COLLECTION"):
                            raise ValueError("Collection Book is disabled.")
                        source = request.form.get("source")
                        if not universe_sources.enabled(source) or source not in universe_sources.SCOPES:
                            raise ValueError("Choose an enabled competition source.")
                        family = int(request.form.get("family", ""))
                        model = StudioEvent if source == universe_sources.LIVE else ChallengeLeague
                        parent = studio.lock(db, model, family)
                        if parent is None or source == universe_sources.LIVE and not parent.published_number:
                            raise ValueError("Choose an existing catalogue family.")
                        label = request.form.get("label", "").strip()
                        category = request.form.get("category", "")
                        if not 1 <= len(label) <= 100 or category not in ("domestic", "international", "challenge"):
                            raise ValueError("Enter a label and choose a collection category.")
                        row = db.get(CompetitionCatalogue, (source, family))
                        if not row:
                            row = CompetitionCatalogue(source=source, family_id=family, version=1)
                            db.add(row)
                        else:
                            row.version += 1
                        row.label, row.category = label, category
                        row.retired_at = now if request.form.get("retired") else None
                        db.add(AdminLog(action="competition_catalogue", target_type="competition",
                                        target_id=family, detail=f"{_actor()} · {source} · {label}"))
                    elif action == "catalogue_link":
                        source = request.form.get("source")
                        if (not universe_sources.flag("COLLECTION") or source not in universe_sources.SCOPES
                                or not universe_sources.enabled(source)):
                            raise ValueError("Choose an enabled Collection Book source.")
                        edition, family = (int(request.form.get(key, "")) for key in ("edition", "family"))
                        model = StudioEvent if source == universe_sources.LIVE else Tournament
                        parent = studio.lock(db, model, edition)
                        slot = db.get(CompetitionCatalogue, (source, family))
                        if (slot is None or parent is None
                                or source == universe_sources.LIVE and not parent.published_number
                                or source == universe_sources.CHALLENGE and parent.kind not in (None, source)):
                            raise ValueError("Choose a valid edition and a catalogue slot from the same source.")
                        row = db.get(CompetitionCatalogueLink, (source, edition))
                        before = row.family_id if row else None
                        if row is None:
                            row = CompetitionCatalogueLink(source=source, edition_id=edition)
                            db.add(row)
                        row.family_id = family
                        db.add(AdminLog(action="competition_catalogue_link", target_type="competition",
                                        target_id=edition, detail=f"{_actor()} · {source} · family {before} → {family}"))
                        report = {"Edition assigned to family": family}
                    else:
                        raise ValueError("Choose a valid operations action.")
                    db.commit()
                except (ValueError, TypeError, IntegrityError) as exc:
                    db.rollback()
                    error = str(exc) if isinstance(exc, ValueError) else "Review the requested operation and retry."
            snapshot = competition_operations.monitor(db, now)
            return render_template("admin_competition_operations.html", monitor=snapshot, report=report, error=error), 400 if error else 200

    @app.post("/api/webapp/events-hub")
    @csrf_exempt
    def webapp_event_hub():
        from services.event_studio_manager import trophies
        return api(lambda db, user, body, now: {"events": play.list_events(db, user, now),
                                                "trophies": trophies(db, user)})

    @app.post("/api/webapp/events-hub/<int:event_id>")
    @csrf_exempt
    def webapp_event_detail(event_id):
        def read(db, user, body, now):
            event, config = play._published(db, event_id)
            if goals.tournament(config):
                page = body.get("news_page", 0)
                studio.rules.integer(page, "News page", 0, 100)
                return {"competition": competitions.dashboard(db, user, event_id, now, news_page=page)}
            return {"event": play.detail(db, user, event_id, now)}
        return api(read)

    @app.post("/api/webapp/events-hub/competitions/<int:event_id>")
    @csrf_exempt
    def webapp_event_competition(event_id):
        def dispatch(db, user, body, now):
            action = body.get("action")
            if action in ("create", "claim_stars", "claim_rewards"):
                limit_action(db, user)
            if action == "create":
                competitions.create(db, user, event_id, body.get("mode"), body.get("team"), now, visibility=body.get("visibility"), consent=body.get("consent") is True, clock=datetime.utcnow)
            elif action in ("claim_stars", "claim_rewards"):
                receipt = competitions.claim(db, user, event_id, now, objective_id=body.get("objective_id"),
                                             reward_id=body.get("reward_id"), wallet=action == "claim_rewards", clock=datetime.utcnow)
                return receipt
            elif action:
                raise ValueError("Unknown competition action.")
            return {"competition": competitions.dashboard(db, user, event_id, now)}
        return api(dispatch, write=True)

    @app.post("/api/webapp/events-hub/lobby")
    @csrf_exempt
    def webapp_event_lobby():
        def dispatch(db, user, body, now):
            invite, room_id = body.get("invite"), body.get("room_id")
            if room_id is not None and (type(room_id) is not int or room_id < 1):
                raise ValueError("Choose a valid lobby.")
            if body.get("action") == "list":
                return {"lobbies": competitions.public_lobbies(db, user, now)}
            if not body.get("action"):
                return {"lobby": competitions.lobby(db, user, room_id, invite=invite, now=now)}
            if body["action"] in ("start", "cancel") and body.get("confirmed") is not True:
                raise ValueError("Confirm the lobby action before continuing.")
            limit_action(db, user)
            if room_id is None:
                room_id = competitions._by_invite(db, invite)
            return {"lobby": competitions.change_lobby(db, user, room_id, body["action"], now,
                       team=body.get("team"), invite=invite, version=body.get("version"), consent=body.get("consent") is True, clock=datetime.utcnow)}
        return api(dispatch, write=True)

    @app.get("/events/studio/<int:event_id>/awards/<int:room_id>/<name>")
    @login_required
    def admin_event_award_art(event_id, room_id, name):
        from base64 import b64decode
        from flask import Response

        with get_session() as db:
            room = db.get(StudioCompetition, room_id)
            media = db.get(StudioAwardMedia, room_id) if room and room.event_id == event_id else None
            if not media or not media.images or name not in media.images:
                abort(404)
            return Response(b64decode(media.images[name]), mimetype="image/png")

    @app.post("/api/webapp/events-hub/honours/<int:event_id>")
    @csrf_exempt
    def webapp_event_honours(event_id):
        def read(db, user, body, now):
            event = competitions._event(db, event_id)
            member = competitions.membership(db, event, user.id)
            if not member or user.is_banned:
                raise ValueError("Your tournament entry is unavailable.")
            media = db.get(StudioAwardMedia, member.competition_id)
            if not media or not media.generated_at:
                raise ValueError("Award graphics are being prepared.")
            return {"images": media.images}
        return api(read)

    @app.post("/api/webapp/events-hub/fixture/<int:fixture_id>")
    @csrf_exempt
    def webapp_event_fixture(fixture_id):
        return api(lambda db, user, body, now: {"setup": competitions.setup(
            db, user, fixture_id, now, action=body.get("action"), version=body.get("version"),
            value=body.get("value"))}, write=True)

    @app.post("/api/webapp/events-hub/fixture-arena/<int:fixture_id>")
    @csrf_exempt
    def webapp_event_fixture_arena(fixture_id):
        def dispatch(db, user, body, now):
            name = body.get("type")
            if name:
                from services.match_webapp_service import APPROACH_ACTION_TYPES, approach_pick_value
                version, value = body.get("version"), body.get("action")
                if name in ("retire_batsman", "live_batting_order", "impact"):
                    if not isinstance(value, dict):
                        raise ValueError("Choose a valid player selection.")
                elif name == "abandon":
                    if body.get("confirmed") is not True:
                        raise ValueError("Confirm forfeiting this fixture.")
                elif name not in ("switch", "resume", "pause", "continue"):
                    name = APPROACH_ACTION_TYPES.get(name)
                    if name not in competitions.picks.PICK_ACTION:
                        raise ValueError("Choose a valid match action.")
                    value = approach_pick_value(name, value)
                competitions.action(db, user, fixture_id, version, name, value, now, source="app")
                db.flush()
            return competitions.arena(db, user, fixture_id, now)
        return api(dispatch, write=True)

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
