"""Admin commands that edit a running tournament from the chat.

Three things a tournament needs that neither the play commands nor the read-only
follow-along views cover, and that until now meant opening the admin site:

    /tpoints        dock or award points on the table (slow over rate, a
                    forfeit, a walkover) — and list what is currently applied
    /tpointsclear   drop a team's adjustment
    /taddmatch      record a fixture from a written scorecard, player stats
                    included, by replying to a text file with the card in it
    /tfixsync       un-stick fixtures left showing as "live" after their match
                    ended without recording a result

Everything works on the tournament that is *running* — the active Challenge
League tournament or the active Lets Play one. They share a single engine
(``services.tournament_service``) and these commands only ever touch that shared
part, so one set of commands covers both kinds. When both kinds are running at
once, pass the tournament's id (``#7``) as the first argument to say which.

All four are bot-admin only, and all four are reversible from the admin
dashboard: a points adjustment is a column you can set back to 0, and an
imported match is a recorded match like any other — removing it rebuilds the
standings and the leaderboards without it.
"""

import html
import logging
import re
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import Tournament, TournamentMatch, TournamentTeam
from services import scorecard_import, tournament_service
from services.admin_ids import is_admin
from services.scorecard_import import ScorecardError

logger = logging.getLogger(__name__)

NOT_ADMIN = "⛔ Only bot admins can edit a tournament."
NO_ACTIVE = ("❌ No tournament is running right now.\n"
             "Activate one from the tournament panel, or name it by id: "
             "<code>#7</code>.")

# Callback prefix for the import confirmation. Outside every other tournament
# namespace (``ctv_``, ``lptv_``, ``lptstat_``) so nothing else can claim it.
CB_IMPORT = "timp_"

# A parsed scorecard waiting for its ✅ — the raw text, not the plan, because a
# plan holds database rows that would be stale by the time anyone presses. The
# text is re-parsed and re-planned against the live fixture on confirmation.
_PENDING_TTL = 30 * 60          # half an hour to look at a preview
_MAX_PENDING = 50               # bounded: this lives in bot memory

# A scorecard is a page of text. Anything much bigger is a file somebody
# attached by mistake, and is refused before it is downloaded.
MAX_CARD_BYTES = 256 * 1024


async def _reply(update, text, **kwargs):
    msg = update.effective_message
    if msg is None:
        return None
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await msg.reply_text(text, **kwargs)


async def _require_admin(update):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Which tournament
# ──────────────────────────────────────────────────────────────────────

# ``#7`` and nothing looser: a bare number is a match number, and "T20" is the
# start of a perfectly ordinary team name.
_TOURNAMENT_ID_RE = re.compile(r"^#(\d+)$")


def _pop_tournament_id(args):
    """Strip a leading ``#7`` id from the arguments and return it, or None."""
    if not args:
        return None
    hit = _TOURNAMENT_ID_RE.match(str(args[0]).strip())
    if not hit:
        return None
    args.pop(0)
    return int(hit.group(1))


def _resolve_tournament(session, args):
    """The tournament these arguments are about.

    An explicit id wins. Otherwise the single running tournament is used — and
    when a Challenge League tournament and a Lets Play one are both running,
    neither is guessed: which table a penalty lands on is not something to get
    wrong quietly.

    Raises ``ValueError`` with a message written for the admin.
    """
    tournament_id = _pop_tournament_id(args)
    if tournament_id:
        tour = session.query(Tournament).get(tournament_id)
        if not tour:
            raise ValueError(f"No tournament with id {tournament_id}.")
        return tour

    running = [t for t in (
        tournament_service.get_active_tournament(
            session, kind=tournament_service.KIND_CHALLENGE),
        tournament_service.get_active_tournament(
            session, kind=tournament_service.KIND_LETSPLAY)) if t]
    if not running:
        raise ValueError(NO_ACTIVE)
    if len(running) > 1:
        listed = "\n".join(f"   <code>#{t.id}</code> — {html.escape(t.name)}"
                           for t in running)
        raise ValueError(
            "Two tournaments are running. Say which one by putting its id "
            f"first:\n{listed}")
    return running[0]


def _find_team(session, tour, query):
    """One participating team by the name that was typed.

    Raises ``ValueError`` naming the candidates when the query is ambiguous, so
    an admin never docks points from the wrong side because two teams share a
    prefix.
    """
    teams = (session.query(TournamentTeam)
             .filter_by(tournament_id=tour.id).all())
    if not teams:
        raise ValueError("This tournament has no teams yet.")
    team = scorecard_import.match_team(query, teams)
    if team is None:
        names = ", ".join(sorted((tt.name or "?") for tt in teams))
        raise ValueError(f"No single team matches {query!r}.\nTeams: {names}")
    return team


# ──────────────────────────────────────────────────────────────────────
# /tpoints — manual points adjustments
# ──────────────────────────────────────────────────────────────────────

_POINTS_USAGE = (
    "📊 <b>Points adjustment</b>\n\n"
    "<code>/tpoints &lt;TEAM&gt; | &lt;±N&gt; | &lt;reason&gt;</code>\n\n"
    "Examples:\n"
    "   <code>/tpoints Mumbai Indians | -2 | slow over rate</code>\n"
    "   <code>/tpoints MI | +2 | walkover</code>\n"
    "   <code>/tpoints #7 CSK | =0</code>  — clear it\n\n"
    "A leading <code>=</code> sets the adjustment outright; otherwise it moves "
    "by that many points. Adjustments survive every rebuild of the table, and "
    "<code>/tpoints</code> on its own lists the ones in force."
)


def _adjust_summary(session, tour):
    """The adjustments currently in force, as an HTML block."""
    rows = tournament_service.adjusted_teams(session, tour.id)
    head = f"📊 <b>{html.escape(tour.name)}</b> — points adjustments"
    if not rows:
        return f"{head}\n\nNone in force."
    lines = [head, ""]
    for tt in rows:
        reason = tt.points_adjust_note or "no reason recorded"
        lines.append(f"   <b>{html.escape(tt.name or '—')}</b>: "
                     f"{int(tt.points_adjust):+d} — {html.escape(reason)}")
    return "\n".join(lines)


def _parse_adjustment(token):
    """``"-2"`` → ``(delta=-2, set_to=None)``; ``"=0"`` → ``(None, 0)``."""
    token = str(token or "").strip().replace(" ", "")
    if not token:
        raise ValueError("Say how many points, like -2 or +2.")
    if token.startswith("="):
        body, absolute = token[1:], True
    else:
        body, absolute = token, False
    try:
        value = int(body)
    except ValueError:
        raise ValueError(
            f"{token!r} isn't a number of points. Use -2, +2 or =0.")
    return (None, value) if absolute else (value, None)


async def tpoints_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dock or award points on the tournament table."""
    if not await _require_admin(update):
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return

        raw = " ".join(args).strip()
        if not raw:
            await _reply(update, _adjust_summary(session, tour)
                         + "\n\n" + _POINTS_USAGE)
            return

        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await _reply(update, _POINTS_USAGE)
            return
        team_query, points_token = parts[0], parts[1]
        note = parts[2] if len(parts) > 2 else None

        try:
            team = _find_team(session, tour, team_query)
            delta, set_to = _parse_adjustment(points_token)
            tournament_service.adjust_points(
                session, team.id, delta, set_to=set_to, note=note)
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return

        total = int(team.points_adjust or 0)
        reason = team.points_adjust_note
        body = [f"✅ <b>{html.escape(team.name or '—')}</b> — adjustment now "
                f"<b>{total:+d}</b> point(s)."]
        if reason:
            body.append(f"Reason: {html.escape(reason)}")
        body.append(f"Table points: <b>{int(team.points or 0)}</b> "
                    f"(from {int(team.played or 0)} match(es), adjustment "
                    f"included).")
        await _reply(update, "\n".join(body))
    finally:
        session.close()


async def tpointsclear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a team's points adjustment entirely."""
    if not await _require_admin(update):
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            # _resolve_tournament writes its own HTML (it lists ids to pick from).
            await _reply(update, str(exc))
            return
        try:
            query = " ".join(args).strip()
            if not query:
                raise ValueError("Which team? /tpointsclear <TEAM>")
            team = _find_team(session, tour, query)
            tournament_service.clear_points_adjust(session, team.id)
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        await _reply(update,
                     f"✅ Adjustment cleared for <b>{html.escape(team.name or '—')}</b> "
                     f"— table points: <b>{int(team.points or 0)}</b>.")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /tfixsync — un-stick fixtures left on "live"
# ──────────────────────────────────────────────────────────────────────

async def tfixsync_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revert fixtures still showing as live after their match ended."""
    if not await _require_admin(update):
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        from services import league_schedule_service
        healed = league_schedule_service.heal_live_fixtures(session, tour.id)
        session.commit()
        if not healed:
            await _reply(update,
                         f"✅ <b>{html.escape(tour.name)}</b> — nothing stuck. "
                         "Every fixture showing as live has a match still being "
                         "played.")
            return
        names = {tt.id: tt.name for tt in
                 session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
        lines = [f"🔧 <b>{html.escape(tour.name)}</b> — "
                 f"{len(healed)} fixture(s) put back to scheduled:", ""]
        for fx in healed:
            tag = f"M{fx.match_no}" if fx.match_no else f"#{fx.id}"
            pair = (f"{names.get(fx.team1_id, 'TBD')} vs "
                    f"{names.get(fx.team2_id, 'TBD')}")
            lines.append(f"   <code>{tag}</code> {html.escape(pair)} — "
                         f"{html.escape(getattr(fx, '_heal_reason', 'stale'))}")
        lines += ["", "They can be replayed, or recorded from a scorecard with "
                      "<code>/taddmatch</code>."]
        await _reply(update, "\n".join(lines))
    except Exception:
        session.rollback()
        logger.exception("tfixsync failed")
        await _reply(update, "⚠️ Couldn't sync the fixtures — check the logs.")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /taddmatch — record a fixture from a written scorecard
# ──────────────────────────────────────────────────────────────────────

_ADDMATCH_USAGE = (
    "📄 <b>Record a match from a scorecard</b>\n\n"
    "Send the scorecard as a <b>.txt file</b> (or as a plain message), then "
    "<b>reply to it</b> with:\n"
    "   <code>/taddmatch &lt;match number&gt;</code>\n\n"
    "The match number is the one on the fixture list "
    "(<code>/ctfixtures</code>, <code>/lptfixtures</code>).\n\n"
    "<b>Format</b>\n<pre>{template}</pre>\n"
    "Batting lines are <code>Name runs (balls)</code> — add <code>6x4</code> / "
    "<code>2x6</code> for boundaries, <code>not out</code> or <code>*</code> for "
    "an unbeaten knock, and a dismissal if you have one. Bowling lines are "
    "<code>Name O-M-R-W</code>. A <code>Name, runs, balls, fours, sixes, "
    "out</code> comma form works too.\n\n"
    "The <b>Bowling</b> list under an innings is the fielding side's, exactly "
    "as a printed card reads. Nothing is written until you confirm the preview."
)


def _usage_text():
    return _ADDMATCH_USAGE.format(
        template=html.escape(scorecard_import.TEMPLATE))


def _pending(context):
    store = context.bot_data.setdefault("tour_import", {})
    # Drop anything nobody confirmed, oldest first, so a long-running bot never
    # accumulates abandoned previews.
    cutoff = time.time() - _PENDING_TTL
    for key in [k for k, v in store.items() if v.get("ts", 0) < cutoff]:
        store.pop(key, None)
    while len(store) > _MAX_PENDING:
        store.pop(min(store, key=lambda k: store[k].get("ts", 0)), None)
    return store


async def _read_card_text(update, context):
    """The scorecard text from the replied-to message, or None.

    A document is downloaded and decoded; a plain replied-to message is taken as
    the card itself, which is how a short one usually arrives.
    """
    message = update.effective_message
    reply = getattr(message, "reply_to_message", None) if message else None
    if reply is None:
        return None
    document = getattr(reply, "document", None)
    if document is None:
        return (reply.text or reply.caption or "").strip() or None
    size = getattr(document, "file_size", 0) or 0
    if size > MAX_CARD_BYTES:
        raise ScorecardError(
            f"That file is {size // 1024} KB — a scorecard should be a page of "
            f"text (limit {MAX_CARD_BYTES // 1024} KB).")
    handle = await context.bot.get_file(document.file_id)
    blob = bytes(await handle.download_as_bytearray())
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return blob.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ScorecardError(
        "Couldn't read that file as text — save it as a plain .txt file "
        "(UTF-8) and try again.")


def _find_fixture(session, tour, number):
    """The fixture an admin means by "match 45".

    Matches the number shown on the fixture list first, then falls back to the
    row id, so a free-play tournament (whose fixtures have no match number) can
    still be addressed.
    """
    fixture = (session.query(TournamentMatch)
               .filter_by(tournament_id=tour.id, match_no=int(number))
               .order_by(TournamentMatch.round_no, TournamentMatch.id).first())
    if fixture is None:
        # Fall back to the row id, but only for a fixture that carries no match
        # number of its own. Without that guard "/taddmatch 45" would land on
        # whatever row happens to have id 45 — which is some other match number
        # entirely, and an admin reading "45" has no way to see the difference.
        row = session.query(TournamentMatch).get(int(number))
        if row is not None and row.tournament_id == tour.id and not row.match_no:
            fixture = row
    if fixture is None:
        raise ScorecardError(
            f"No match {number} in {tour.name}. Check the number on "
            "/ctfixtures (or /lptfixtures).")
    return fixture


def _render_preview(tour, fixture, plan):
    """What the import would record, as an HTML message body."""
    first, second = plan["innings"]
    tag = f"Match {fixture.match_no}" if fixture.match_no else f"Fixture #{fixture.id}"
    lines = [
        f"📄 <b>{html.escape(tour.name)}</b> — {tag}",
        "",
        f"<b>{html.escape(first['team'].name or '—')}</b> "
        f"{first['runs']}/{first['wickets']} ({first['overs'] or '0'})",
        f"<b>{html.escape(second['team'].name or '—')}</b> "
        f"{second['runs']}/{second['wickets']} ({second['overs'] or '0'})",
        "",
        f"🏆 {html.escape(plan['result_text'])}",
    ]

    batters = [ln for ln in plan["lines"] if ln["batted"]]
    bowlers = [ln for ln in plan["lines"] if ln["bowled"]]
    if batters:
        top = sorted(batters, key=lambda ln: -ln["bat_runs"])[:5]
        lines += ["", "<b>Top scorers</b>"]
        lines += [f"   {html.escape(ln['name'])} {ln['bat_runs']}"
                  f"{'' if ln['bat_out'] else '*'} ({ln['bat_balls']})"
                  for ln in top]
    if bowlers:
        top = sorted(bowlers, key=lambda ln: (-ln["bowl_wickets"], ln["bowl_runs"]))[:5]
        lines += ["", "<b>Best bowling</b>"]
        lines += [f"   {html.escape(ln['name'])} "
                  f"{ln['bowl_wickets']}/{ln['bowl_runs']}" for ln in top]
    lines += ["", f"<i>{len(batters)} batting and {len(bowlers)} bowling "
                  f"line(s) will be added to the leaderboards.</i>"]
    if plan["swap_sides"]:
        lines.append("<i>The fixture's sides will be swapped so the team that "
                     "batted first is listed first (net run rate depends on "
                     "it).</i>")
    if plan["warnings"]:
        lines += ["", "⚠️ <b>Check these</b>"]
        lines += [f"   • {html.escape(w)}" for w in plan["warnings"][:8]]
        if len(plan["warnings"]) > 8:
            lines.append(f"   • …and {len(plan['warnings']) - 8} more.")
    lines += ["", "Nothing has been recorded yet — press <b>Record</b> to "
                  "write it to the table."]
    return "\n".join(lines)


async def taddmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview a written scorecard against a fixture, ready to be recorded."""
    if not await _require_admin(update):
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return

        number = args[0].strip().lstrip("#mM") if args else ""
        if not number.isdigit():
            await _reply(update, _usage_text())
            return

        try:
            text = await _read_card_text(update, context)
            if not text:
                await _reply(update,
                             "📄 Reply to the message or text file holding the "
                             "scorecard.\n\n" + _usage_text())
                return

            # A fixture whose match died without recording is still marked live,
            # and would be refused below. Clear those first so the very matches
            # this command exists to rescue are importable.
            from services import league_schedule_service
            if league_schedule_service.heal_live_fixtures(session, tour.id):
                session.commit()

            fixture = _find_fixture(session, tour, number)
            parsed = scorecard_import.parse_scorecard(text)
            plan = scorecard_import.plan_import(session, fixture, parsed)
        except ScorecardError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return

        token = f"{int(time.time())}{fixture.id}"
        _pending(context)[token] = {
            "text": text, "fixture_id": fixture.id, "tournament_id": tour.id,
            "ts": time.time(),
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Record it", callback_data=f"{CB_IMPORT}ok_{token}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB_IMPORT}no_{token}"),
        ]])
        await _reply(update, _render_preview(tour, fixture, plan),
                     reply_markup=keyboard)
    except Exception:
        session.rollback()
        logger.exception("taddmatch preview failed")
        await _reply(update, "⚠️ Couldn't read that scorecard — check the logs.")
    finally:
        session.close()


async def taddmatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record (or drop) a previewed scorecard."""
    query = update.callback_query
    if query is None:
        return
    data = (query.data or "")[len(CB_IMPORT):]
    action, _, token = data.partition("_")
    store = _pending(context)

    if action == "no":
        store.pop(token, None)
        await query.answer("Cancelled.")
        await query.edit_message_text("✖️ Import cancelled — nothing was recorded.")
        return

    pending = store.get(token)
    if not pending:
        await query.answer("That preview has expired — run /taddmatch again.",
                           show_alert=True)
        return
    if not is_admin(query.from_user.id if query.from_user else 0):
        await query.answer(NOT_ADMIN, show_alert=True)
        return

    session = get_session()
    try:
        tour = session.query(Tournament).get(pending["tournament_id"])
        fixture = session.query(TournamentMatch).get(pending["fixture_id"])
        if tour is None or fixture is None:
            raise ScorecardError("That fixture is gone — nothing was recorded.")
        # Re-parse and re-plan: the fixture may have been played, edited or
        # deleted between the preview and this press, and the plan must be built
        # against what is true now.
        parsed = scorecard_import.parse_scorecard(pending["text"])
        plan = scorecard_import.plan_import(session, fixture, parsed)
        scorecard_import.record_import(session, plan)
        session.commit()
        store.pop(token, None)
        # Read everything the confirmation needs while the session is still
        # open — a commit expires these rows, and they are about to detach.
        done = {
            "tour": tour.name or "—",
            "tag": (f"Match {fixture.match_no}" if fixture.match_no
                    else f"Fixture #{fixture.id}"),
            "result": plan["result_text"],
        }
    except ScorecardError as exc:
        session.rollback()
        await query.answer()
        await query.edit_message_text(f"⚠️ {html.escape(str(exc))}",
                                      parse_mode="HTML")
        return
    except Exception:
        session.rollback()
        logger.exception("taddmatch record failed")
        await query.answer("Couldn't record it — check the logs.", show_alert=True)
        return
    finally:
        session.close()

    await query.answer("Recorded.")
    await query.edit_message_text(
        f"✅ <b>{html.escape(done['tour'])}</b> — {done['tag']} recorded.\n"
        f"{html.escape(done['result'])}\n\n"
        "Points table, net run rate and the player leaderboards have been "
        "rebuilt. Remove it from the admin dashboard to undo.",
        parse_mode="HTML")
