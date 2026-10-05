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
        tour = session.get(Tournament, tournament_id)
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
    "✅ <b>The bot's own <code>MatchNo&lt;id&gt;.txt</code> works as it is</b> — "
    "the scorecard file archived for every match it plays. Reply to that file "
    "and nothing needs editing. A Super Over in the file is read and kept out "
    "of the scoreline and the stats, the same way the bot records one itself.\n\n"
    "<b>Or write your own</b>\n<pre>{template}</pre>\n"
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
        row = session.get(TournamentMatch, int(number))
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
        tour = session.get(Tournament, pending["tournament_id"])
        fixture = session.get(TournamentMatch, pending["fixture_id"])
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


# ──────────────────────────────────────────────────────────────────────
# /tratingrule — "at least N players rated X or lower" in every XI
# ──────────────────────────────────────────────────────────────────────

async def tratingrule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read or set the tournament's rating rule.

    ``/tratingrule 83 3`` — every Playing XI must field at least 3 players
    rated 83 or lower. ``/tratingrule off 83`` drops that rule,
    ``/tratingrule clear`` drops them all, and a bare ``/tratingrule`` lists
    them. A leading ``#7`` picks the tournament when more than one is running.
    """
    if not await _require_admin(update):
        return
    from services import rating_rules as RR
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            action, cap, need = RR.parse_command(args)
        except ValueError as exc:
            await _reply(update,
                         f"{html.escape(str(exc))}\nUsage: "
                         "<code>/tratingrule [#id] &lt;max rating&gt; &lt;min players&gt;</code>"
                         " · <code>/tratingrule off 83</code> · "
                         "<code>/tratingrule clear</code>")
            return
        rules = tournament_service.rating_rules(tour)
        if action != "list":
            rules = tournament_service.set_rating_rules(
                session, tour, RR.apply_command(rules, action, cap, need))
            session.commit()
        if not rules:
            await _reply(update,
                         f"⭐ <b>{html.escape(tour.name)}</b> — no rating rule.\n"
                         "Add one with <code>/tratingrule 83 3</code> — every XI "
                         "must field at least 3 players rated 83 or lower.")
            return
        lines = [f"⭐ <b>{html.escape(tour.name)}</b> — rating rule"]
        lines += [f"· {html.escape(RR.describe(rule))} in every XI" for rule in rules]
        lines += ["", "<i>The XI picker shows it as a live check, and the "
                      "bot's XI follows it too.</i>"]
        await _reply(update, "\n".join(lines))
    except Exception:
        session.rollback()
        logger.exception("tratingrule failed")
        await _reply(update, "⚠️ Couldn't update the rating rule — check the logs.")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /tseasons — which past seasons "Total Season Stats" adds up
# ──────────────────────────────────────────────────────────────────────

async def tseasons_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link a running tournament to completed seasons, by number.

    ``/tseasons`` lists 🟢 running tournaments and ✅ completed ones (📦 saved
    seasons included), each numbered. Then:

        /tseasons 2 | 3        running #2 adds completed #3
        /tseasons 2 | 3 5      …and #5
        /tseasons 2 | -3       unlink #3
        /tseasons 2 | auto     back to "every season in the league"
        /tseasons 2            what running #2 is linked to
    """
    if not await _require_admin(update):
        return
    from services import season_archive as SA
    raw = " ".join(context.args or [])
    session = get_session()
    try:
        try:
            action, run_no, numbers = SA.parse_link_command(raw)
        except ValueError as exc:
            await _reply(update, f"{html.escape(str(exc))}\n\n{_TSEASONS_USAGE}")
            return
        running = SA.running_tournaments(session)
        completed = SA.completed_seasons(session)

        if action == "list":
            await _reply_long(update, _tseasons_listing(session, SA, running, completed))
            return
        if not 1 <= run_no <= len(running):
            await _reply_long(update, f"There is no running tournament #{run_no}.\n\n"
                              + _tseasons_listing(session, SA, running, completed))
            return
        tour = running[run_no - 1]
        if numbers and any(not 1 <= n <= len(completed) for n in numbers):
            await _reply_long(update, "Those numbers aren't all on the completed list.\n\n"
                              + _tseasons_listing(session, SA, running, completed))
            return

        note = ""
        if action in ("link", "unlink", "auto"):
            refs = SA.linked_refs(tour)
            chosen = [completed[n - 1]["ref"] for n in (numbers or [])]
            if action == "link":
                refs += [r for r in chosen if r not in refs]
                note = "🔗 Linked."
            elif action == "unlink":
                refs = [r for r in refs if r not in chosen]
                note = "✂️ Unlinked."
            else:
                refs = []
                note = "♻️ Back to automatic."
            SA.set_linked_refs(session, tour, refs)
            session.commit()

        labels = SA.link_labels(session, tour)
        lines = [f"{note}\n" if note else "",
                 f"🟢 <b>{html.escape(tour.name)}</b> — Total Season Stats adds:"]
        if labels:
            lines += [f"   ✅ {html.escape(label)}" for label in labels]
        else:
            lines.append("   <i>every season in "
                         f"{html.escape(tour.league_name or 'the same league')} "
                         "(automatic)</i>")
        await _reply(update, "\n".join(line for line in lines if line is not None).strip())
    except Exception:
        session.rollback()
        logger.exception("tseasons failed")
        await _reply(update, "⚠️ Couldn't update the season links — check the logs.")
    finally:
        session.close()


_TSEASONS_USAGE = ("<code>/tseasons 2 | 3</code> link · "
                   "<code>/tseasons 2 | 3 5</code> several · "
                   "<code>/tseasons 2 | -3</code> unlink · "
                   "<code>/tseasons 2 | auto</code> reset")


def _tseasons_listing(session, SA, running, completed):
    """The two numbered lists, with each running tournament's links."""
    lines = ["📚 <b>Season links</b> — which past seasons each running "
             "tournament's <b>Total Season Stats</b> adds up", "",
             "🟢 <b>Running tournaments</b>"]
    if not running:
        lines.append("   <i>none</i>")
    for n, t in enumerate(running, start=1):
        labels = SA.link_labels(session, t)
        linked = (", ".join(html.escape(l) for l in labels) if labels
                  else "<i>auto: whole league</i>")
        lines.append(f"<code>{n}</code> {html.escape(t.name)}"
                     + (f" <i>({html.escape(t.league_name)})</i>" if t.league_name else ""))
        lines.append(f"     ↳ {linked}")
    lines += ["", "✅ <b>Completed tournaments</b> (📦 = deleted, stats kept)"]
    if not completed:
        lines.append("   <i>none yet</i>")
    for n, c in enumerate(completed, start=1):
        extra = " · ".join(x for x in (c["league"], c["when"]) if x)
        lines.append(f"<code>{n}</code> {'📦 ' if c['saved'] else ''}"
                     f"{html.escape(c['label'])}"
                     + (f" <i>({html.escape(extra)})</i>" if extra else ""))
    lines += ["", _TSEASONS_USAGE]
    return "\n".join(lines)


async def _reply_long(update, text):
    """Send ``text`` split at line breaks into messages Telegram accepts."""
    from utils.message_chunks import chunk_blocks
    # chunk_blocks drops empty blocks; a lone space keeps the blank lines.
    for part in chunk_blocks([line or " " for line in text.split("\n")]):
        await _reply(update, part)


# ──────────────────────────────────────────────────────────────────────
# /tsim — settle a fixture that won't be played
# ──────────────────────────────────────────────────────────────────────
#
# Some sides never get round to their match, and a round-by-round schedule
# can't move on while one fixture sits unplayed. /tsim settles it: team 1 wins,
# team 2 wins, or a coin toss — with a believable score line so net run rate
# still moves — and the result is marked "(simulated)" on the fixture list.

CB_SIM = "tsim_"

_SIM_USAGE = (
    "🎲 <b>Simulate a fixture</b>\n\n"
    "<code>/tsim</code> — the matches open right now, with buttons\n"
    "<code>/tsim &lt;match no&gt; 1</code> — team 1 wins\n"
    "<code>/tsim &lt;match no&gt; 2</code> — team 2 wins\n"
    "<code>/tsim &lt;match no&gt; random</code> — either side, at random\n"
    "<code>/tsim &lt;match no&gt; &lt;team name&gt;</code> — that team wins\n"
    "<code>/tsim round</code> — every unplayed match this round, at random\n\n"
    "Only a fixture that hasn't started can be simulated. When both a CIPL and a "
    "Lets Play tournament are running, put the id first: "
    "<code>/tsim #7 12 2</code>."
)

# How many fixtures get buttons in one /tsim listing (Telegram keyboards get
# unwieldy past this; the rest are still simulable by number).
_SIM_BUTTONS = 12


def _open_fixtures(session, tour):
    """Scheduled fixtures, both sides known, in the round that's open now."""
    from services import league_schedule_service
    rows = (session.query(TournamentMatch)
            .filter_by(tournament_id=tour.id, status="scheduled")
            .filter(TournamentMatch.team1_id.isnot(None),
                    TournamentMatch.team2_id.isnot(None))
            .order_by(TournamentMatch.match_no, TournamentMatch.id).all())
    rnd = league_schedule_service.current_round(session, tour.id)
    return [fx for fx in rows
            if not league_schedule_service.is_round_locked(fx, rnd)]


def _sim_tag(fx):
    return f"M{fx.match_no}" if fx.match_no else f"#{fx.id}"


def _sim_line(fx, names):
    sc1 = f"{fx.inn1_runs}/{fx.inn1_wickets}" if fx.inn1_runs is not None else "—"
    sc2 = f"{fx.inn2_runs}/{fx.inn2_wickets}" if fx.inn2_runs is not None else "—"
    return (f"<code>{_sim_tag(fx)}</code> "
            f"{html.escape(names.get(fx.team1_id, 'TBD'))} {sc1} · "
            f"{html.escape(names.get(fx.team2_id, 'TBD'))} {sc2}\n"
            f"   ✅ {html.escape(fx.result_text or 'done')}")


def _sim_listing(session, tour):
    """``(text, keyboard)`` for the fixtures that can be simulated now."""
    from services import league_schedule_service
    names = {tt.id: tt.name or "—" for tt in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
    open_fx = _open_fixtures(session, tour)
    head = f"🎲 <b>{html.escape(tour.name)}</b> — simulate a fixture"
    progress = league_schedule_service.round_progress(session, tour.id)
    banner = league_schedule_service.round_banner(progress, 0, tour)
    if banner:
        head += f"\n🔵 {banner}"
    if not open_fx:
        return (f"{head}\n\nNothing to simulate — no unplayed fixture with both "
                "teams set is open right now.", None)
    lines = [head, ""]
    buttons = []
    for fx in open_fx:
        a, b = names.get(fx.team1_id, "TBD"), names.get(fx.team2_id, "TBD")
        lines.append(f"<code>{_sim_tag(fx)}</code> {html.escape(a)} vs "
                     f"{html.escape(b)}")
        if len(buttons) < _SIM_BUTTONS:
            buttons.append([
                InlineKeyboardButton(f"✅ {a[:14]}",
                                     callback_data=f"{CB_SIM}{fx.id}_1"),
                InlineKeyboardButton(f"✅ {b[:14]}",
                                     callback_data=f"{CB_SIM}{fx.id}_2"),
                InlineKeyboardButton(f"🎲 {_sim_tag(fx)}",
                                     callback_data=f"{CB_SIM}{fx.id}_random"),
            ])
    if len(open_fx) > 1:
        buttons.append([InlineKeyboardButton(
            f"🎲 Simulate all {len(open_fx)} at random",
            callback_data=f"{CB_SIM}round{tour.id}_random")])
    lines += ["", "Pick the winner for a match, or 🎲 for either side. "
                  "Or type <code>/tsim &lt;match no&gt; 1|2|random</code>."]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _simulate(session, tour, fixtures, outcome):
    """Simulate ``fixtures`` in turn; returns the reply text. Caller commits."""
    names = {tt.id: tt.name or "—" for tt in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
    done, news = [], []
    for fx in fixtures:
        tm = tournament_service.simulate_fixture(session, fx.id, outcome)
        done.append(_sim_line(tm, names))
        line = tournament_service.schedule_news(session, tm)
        if line and line not in news:
            news.append(line)
    out = [f"🎲 <b>{html.escape(tour.name)}</b> — "
           f"{len(done)} fixture{'s' if len(done) != 1 else ''} simulated", ""]
    out += done
    if news:
        out += [""] + news
    return "\n".join(out)


def _sim_outcome(session, tour, fx, token):
    """``"1"``/``"2"``/``"random"`` from what the admin typed."""
    t = (token or "").strip().lower()
    if t in ("1", "2", "random"):
        return t
    if t in ("r", "rand", "either", "any", "toss"):
        return "random"
    team = _find_team(session, tour, token)
    if team.id == fx.team1_id:
        return "1"
    if team.id == fx.team2_id:
        return "2"
    raise ValueError(f"{team.name} isn't playing {_sim_tag(fx)}.")


async def tsim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tsim — simulate an unplayed fixture (team 1, team 2 or random)."""
    if not await _require_admin(update):
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            if args and args[0].lower() in ("help", "?"):
                await _reply(update, _SIM_USAGE)
                return
            if not args:
                text, kb = _sim_listing(session, tour)
                await _reply(update, text, reply_markup=kb)
                return
            if args[0].lower() == "round":
                fixtures = _open_fixtures(session, tour)
                if not fixtures:
                    raise ValueError("Nothing to simulate — no unplayed fixture "
                                     "is open right now.")
                outcome = "random"
            else:
                tag = args[0].lstrip("mM#")
                if not tag.isdigit():
                    raise ValueError(_SIM_USAGE)
                fx = (session.query(TournamentMatch)
                      .filter_by(tournament_id=tour.id, match_no=int(tag)).first())
                if fx is None:
                    raise ValueError(f"No match number {tag} in {tour.name}.")
                outcome = _sim_outcome(session, tour, fx,
                                       " ".join(args[1:]) or "random")
                fixtures = [fx]
            text = _simulate(session, tour, fixtures, outcome)
            session.commit()
        except ValueError as exc:
            session.rollback()
            msg = str(exc)
            await _reply(update, msg if msg == _SIM_USAGE
                         else f"⚠️ {html.escape(msg)}")
            return
        await _reply(update, text)
    except Exception:
        session.rollback()
        logger.exception("tsim failed")
        await _reply(update, "⚠️ Couldn't simulate that — check the logs.")
    finally:
        session.close()


async def tsim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A /tsim button: ``tsim_<fixture>_<1|2|random>`` or ``tsim_round<tour>_random``."""
    query = update.callback_query
    if query is None:
        return
    if not is_admin(query.from_user.id if query.from_user else 0):
        await query.answer(NOT_ADMIN, show_alert=True)
        return
    target, _, outcome = (query.data or "")[len(CB_SIM):].partition("_")
    session = get_session()
    try:
        try:
            if target.startswith("round"):
                tour = session.get(Tournament, int(target[len("round"):]))
                if tour is None:
                    raise ValueError("That tournament is gone.")
                fixtures = _open_fixtures(session, tour)
                if not fixtures:
                    raise ValueError("Nothing left to simulate this round.")
            else:
                fx = session.get(TournamentMatch, int(target))
                if fx is None:
                    raise ValueError("That fixture is gone.")
                tour = session.get(Tournament, fx.tournament_id)
                fixtures = [fx]
            text = _simulate(session, tour, fixtures, outcome or "random")
            session.commit()
        except ValueError as exc:
            session.rollback()
            await query.answer(str(exc)[:200], show_alert=True)
            return
    except Exception:
        session.rollback()
        logger.exception("tsim callback failed")
        await query.answer("Couldn't simulate it — check the logs.", show_alert=True)
        return
    finally:
        session.close()
    await query.answer("Simulated.")
    msg = query.message
    if msg is not None:
        await msg.reply_text(text, parse_mode="HTML",
                             disable_web_page_preview=True)


# ──────────────────────────────────────────────────────────────────────
# The tournament watch job, /tsetchat and /tdeadline
# ──────────────────────────────────────────────────────────────────────

def _markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)
                                  for label, data in row] for row in rows])


async def _send_post(bot, chat_id, post, with_buttons=True):
    markup = _markup(post.buttons) if with_buttons else None
    if post.photo:
        import io
        caption = post.text if len(post.text) <= 1024 else None
        await bot.send_photo(chat_id, io.BytesIO(post.photo), caption=caption,
                             parse_mode="HTML", reply_markup=markup)
        if caption is None and post.text:
            await bot.send_message(chat_id, post.text, parse_mode="HTML",
                                   disable_web_page_preview=True)
        return
    from utils.message_chunks import chunk_blocks
    parts = chunk_blocks([post.text])
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part, parse_mode="HTML",
                               disable_web_page_preview=True,
                               reply_markup=markup if i == len(parts) - 1 else None)


async def tournament_watch_job(context):
    """Every couple of minutes: send whatever the tournament watch says is due."""
    from services import tournament_watch
    session = get_session()
    try:
        due = tournament_watch.tick_all(session)
        # Commit first: the bookkeeping is what stops a post going out twice,
        # and a lost post is better than one repeated every two minutes.
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("tournament watch tick failed")
        return
    finally:
        session.close()
    bot = context.bot
    for _tid, post in due:
        if post.chat_id:
            try:
                await _send_post(bot, post.chat_id, post)
            except Exception:
                logger.warning("tournament watch: group post to %s failed",
                               post.chat_id, exc_info=True)
        for tg_id, text in post.dms:
            try:
                dm = type(post)(text=text, photo=post.photo,
                                buttons=post.buttons if post.dm_buttons else [])
                await _send_post(bot, tg_id, dm)
            except Exception:
                logger.info("tournament watch: DM to %s failed", tg_id)


async def tsetchat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tsetchat — post this tournament's news (recaps, deadlines…) in this group."""
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.id > 0:
        await _reply(update, "Run <code>/tsetchat</code> inside the group the "
                             "tournament news should go to.")
        return
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        tour.announce_chat_id = chat.id
        session.commit()
        await _reply(update, f"📣 <b>{html.escape(tour.name)}</b> — round recaps, "
                             "deadline alerts, the bracket and the awards "
                             "ceremony will be posted here.")
    finally:
        session.close()


_DEADLINE_USAGE = (
    "⏳ <b>Round deadlines</b>\n\n"
    "<code>/tdeadline 48h</code> — every round gets 48 hours (or <code>2d</code>)\n"
    "<code>/tdeadline +12h</code> — give the current round 12 more hours\n"
    "<code>/tdeadline off</code> — no deadlines\n\n"
    "Reminders go out 24h and 2h before the end. When a round's time runs out "
    "the admins are alerted — <b>nothing is simulated automatically</b>; settle "
    "a match with <code>/tsim</code> or extend the round."
)


def _deadline_summary(tour):
    from services import tournament_watch as tw
    if not tour.round_hours:
        return f"⏳ <b>{html.escape(tour.name)}</b> — no round deadlines."
    line = (f"⏳ <b>{html.escape(tour.name)}</b> — each round gets "
            f"<b>{tour.round_hours}h</b>.")
    left = tw.deadline_text(tour)
    if left:
        line += f"\nCurrent round (Round {tour.round_tracked}): {left}."
    return line


async def tdeadline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tdeadline [48h | +12h | off] — round deadlines (alert only)."""
    if not await _require_admin(update):
        return
    from services import tournament_watch as tw
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            if not args:
                await _reply(update, _deadline_summary(tour) + "\n\n" + _DEADLINE_USAGE)
                return
            token = args[0].lower()
            if token in ("off", "none", "clear", "0"):
                tw.set_round_hours(session, tour, None)
            else:
                hours, relative = tw.parse_hours(token)
                if relative:
                    tw.extend_deadline(session, tour, hours)
                else:
                    tw.set_round_hours(session, tour, hours)
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        await _reply(update, "✅ " + _deadline_summary(tour))
    finally:
        session.close()


async def tdeadline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The "⏳ +24h" button on a deadline alert."""
    from services import tournament_watch as tw
    query = update.callback_query
    if query is None:
        return
    if not is_admin(query.from_user.id if query.from_user else 0):
        await query.answer(NOT_ADMIN, show_alert=True)
        return
    tid, _, hours = (query.data or "")[len(tw.CB_EXTEND):].partition("_")
    session = get_session()
    try:
        tour = session.get(Tournament, int(tid))
        if tour is None:
            await query.answer("That tournament is gone.", show_alert=True)
            return
        try:
            tw.extend_deadline(session, tour, int(hours or 24))
            session.commit()
        except ValueError as exc:
            session.rollback()
            await query.answer(str(exc)[:200], show_alert=True)
            return
        text = (f"⏳ <b>{html.escape(tour.name)}</b> — Round {tour.round_tracked} "
                f"extended by {int(hours or 24)}h: {tw.deadline_text(tour)}.")
    finally:
        session.close()
    await query.answer("Extended.")
    if query.message is not None:
        await query.message.reply_text(text, parse_mode="HTML")


# ──────────────────────────────────────────────────────────────────────
# /ttiebreak — how teams level on points are separated
# ──────────────────────────────────────────────────────────────────────

async def ttiebreak_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ttiebreak [h2h | nrr] — head-to-head or wins/NRR after points."""
    if not await _require_admin(update):
        return
    from services import standings
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        if not args:
            cur = tour.tiebreak or "nrr"
            await _reply(update,
                         f"⚖️ <b>{html.escape(tour.name)}</b> — tiebreak: "
                         f"<b>{standings.TIEBREAK_LABEL[cur]}</b>\n\n"
                         "<code>/ttiebreak h2h</code> — head-to-head first\n"
                         "<code>/ttiebreak nrr</code> — wins, then net run rate")
            return
        choice = args[0].lower().replace("-", "")
        choice = {"headtohead": "h2h", "h2h": "h2h", "nrr": "nrr",
                  "netrunrate": "nrr", "wins": "nrr"}.get(choice)
        if choice is None:
            await _reply(update, "⚠️ Use <code>h2h</code> or <code>nrr</code>.")
            return
        tour.tiebreak = choice
        session.commit()
        await _reply(update, f"⚖️ <b>{html.escape(tour.name)}</b> — tiebreak is now "
                             f"<b>{standings.TIEBREAK_LABEL[choice]}</b>. The table "
                             "and playoff seeding follow it.")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /tstadiums and /thome — where the tournament is played
# ──────────────────────────────────────────────────────────────────────

_STADIUMS_USAGE = (
    "🏟️ <b>Tournament stadiums</b> — picked from <b>Stadium Data</b> (admin site → "
    "Conditions → Stadium; add new grounds there)\n\n"
    "<code>/tstadiums</code> — this tournament's grounds\n"
    "<code>/tstadiums all</code> — every stadium in Stadium Data\n"
    "<code>/tstadiums add Wankhede Stadium</code>\n"
    "<code>/tstadiums remove Wankhede Stadium</code>\n"
    "<code>/tstadiums clear</code>\n\n"
    "Home grounds: <code>/thome &lt;team&gt; | &lt;stadium&gt;</code>. A league match "
    "is played at the home team's ground; otherwise at one of the tournament's "
    "grounds (knockouts always at a neutral one from the list)."
)


def _stadium_list_text(session, tour):
    from services import tournament_stadiums as TS
    names = TS.tour_stadiums(tour)
    homes = [(t.name, t.home_stadium) for t in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id)
             .order_by(TournamentTeam.sort_order, TournamentTeam.id).all()]
    lines = [f"🏟️ <b>{html.escape(tour.name)}</b> — stadiums", ""]
    if names:
        lines += [f"• {html.escape(TS.describe(n))}" for n in names]
    else:
        lines.append("<i>No list — matches without a home ground get a random venue.</i>")
    lines += ["", "<b>Home grounds</b>"]
    lines += [f"• {html.escape(name or '—')}: "
              + (html.escape(home) if home else "<i>none</i>") for name, home in homes]
    return "\n".join(lines)


async def tstadiums_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tstadiums [all | add <name> | remove <name> | clear]."""
    if not await _require_admin(update):
        return
    from services import tournament_stadiums as TS
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            sub = (args[0].lower() if args else "")
            rest = " ".join(args[1:]).strip()
            if sub in ("", "list"):
                await _reply(update, _stadium_list_text(session, tour) + "\n\n"
                             + _STADIUMS_USAGE)
                return
            if sub in ("all", "data"):
                rows = TS.all_rows()
                lines = [f"🏟️ <b>Stadium Data</b> — {len(rows)} grounds", ""]
                lines += [f"• {html.escape(r.get('name'))}"
                          + (f" <i>({html.escape(r.get('city'))})</i>" if r.get("city") else "")
                          for r in rows]
                lines += ["", "Add more on the admin site → Conditions → Stadium."]
                await _reply_long(update, "\n".join(lines))
                return
            if sub == "add":
                if not rest:
                    raise ValueError("Name the stadium: /tstadiums add Eden Gardens")
                canon = TS.add_tour_stadium(tour, rest)
                note = f"➕ Added <b>{html.escape(canon)}</b>."
            elif sub in ("remove", "rm", "del"):
                canon = TS.remove_tour_stadium(tour, rest)
                note = f"➖ Removed <b>{html.escape(canon)}</b>."
            elif sub == "clear":
                TS.set_tour_stadiums(tour, [])
                note = "🧹 Cleared the stadium list."
            else:
                raise ValueError("Use add, remove, clear, all — or nothing to list.")
            session.flush()
            n = TS.assign_venues(session, tour.id, overwrite=(sub != "add"))
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        await _reply(update, f"{note} {n} unplayed fixture venue(s) updated.\n\n"
                     + _stadium_list_text(session, tour))
    finally:
        session.close()


async def thome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/thome <team> | <stadium> — a team's home ground (admin, or its owner)."""
    from services import tournament_stadiums as TS
    user = update.effective_user
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            text = " ".join(args)
            if "|" not in text:
                await _reply(update, "🏠 <b>Home ground</b>\n\n"
                             "<code>/thome &lt;team&gt; | &lt;stadium&gt;</code> — e.g. "
                             "<code>/thome Mumbai | Wankhede Stadium</code>\n"
                             "<code>/thome Mumbai | none</code> — clear it\n\n"
                             "Stadiums come from Stadium Data — <code>/tstadiums all</code> "
                             "lists them.")
                return
            team_q, stadium_q = (p.strip() for p in text.split("|", 1))
            team = _find_team(session, tour, team_q)
            uid = user.id if user else None
            if not (is_admin(uid) or tournament_service.is_team_member(team, uid)):
                raise ValueError(f"Only an admin or {team.name}'s owner can set its "
                                 "home ground.")
            clear = stadium_q.lower() in ("", "none", "clear", "-")
            canon = TS.set_home_stadium(session, team, None if clear else stadium_q)
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        if canon:
            await _reply(update, f"🏠 <b>{html.escape(team.name)}</b> now play their "
                                 f"home matches at <b>{html.escape(TS.describe(canon))}</b>. "
                                 "Unplayed home fixtures were moved there.")
        else:
            await _reply(update, f"🏠 <b>{html.escape(team.name)}</b> has no home "
                                 "ground now.")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /tprize — prizes paid when the final is decided
# ──────────────────────────────────────────────────────────────────────

_PRIZE_USAGE = (
    "💰 <b>Tournament prizes</b> — paid automatically when the final is decided\n\n"
    "<code>/tprize champion 5000 50</code> — coins, then gems\n"
    "<code>/tprize runnerup 2500 20</code>\n"
    "<code>/tprize orange 1000</code> · <code>/tprize purple 1000</code> · "
    "<code>/tprize mvp 1500 10</code>\n"
    "<code>/tprize champion 0</code> — remove a prize\n\n"
    "Team awards go to the team's owner; caps and MVP to the player's owner. "
    "Winners are listed in /halloffame → 🏆 Tournaments."
)


def _prize_summary(tour):
    from services import tournament_awards as TA
    table = TA.prizes(tour)
    lines = [f"💰 <b>{html.escape(tour.name)}</b> — prizes"]
    for award in TA.AWARDS:
        lines.append(f"{TA.AWARD_LABEL[award]}: "
                     + (TA.prize_text(table.get(award)) or "<i>none</i>"))
    if tour.awards_given_at:
        lines.append("\n<i>Already awarded — changes no longer pay out.</i>")
    return "\n".join(lines)


async def tprize_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tprize <award> <coins> [gems] — set a prize; bare lists them."""
    if not await _require_admin(update):
        return
    from services import tournament_awards as TA
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
            if not args:
                await _reply(update, _prize_summary(tour) + "\n\n" + _PRIZE_USAGE)
                return
            if len(args) < 2:
                raise ValueError("Give an award and an amount: /tprize champion 5000 50")
            try:
                coins = int(args[1].replace(",", ""))
                gems = int(args[2].replace(",", "")) if len(args) > 2 else 0
            except ValueError:
                raise ValueError("Amounts must be whole numbers: /tprize mvp 1500 10")
            TA.set_prize(tour, args[0], coins, gems)
            session.commit()
        except ValueError as exc:
            session.rollback()
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        await _reply(update, "✅ " + _prize_summary(tour))
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
# /tteam — Team of the Tournament (anyone)
# ──────────────────────────────────────────────────────────────────────

async def tteam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tteam — the best XI of the running tournament, as cards."""
    import asyncio
    import io
    from services import team_of_tournament as TOT
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        xi = TOT.pick_xi(session, tour.id)
        text = TOT.render_text(tour, xi)
        photo = await asyncio.to_thread(TOT.render_image, tour, xi) if xi else None
    finally:
        session.close()
    msg = update.effective_message
    if photo and msg is not None:
        await msg.reply_photo(io.BytesIO(photo), caption=text[:1024], parse_mode="HTML")
        if len(text) > 1024:
            await _reply(update, text)
    else:
        await _reply(update, text)


# ──────────────────────────────────────────────────────────────────────
# /tbracket — the playoff bracket as a picture (anyone)
# ──────────────────────────────────────────────────────────────────────

async def tbracket_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tbracket — the running tournament's playoff bracket image."""
    import asyncio
    import io
    from services import bracket_image
    args = list(context.args or [])
    session = get_session()
    try:
        try:
            tour = _resolve_tournament(session, args)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        name = tour.name
        png = await asyncio.to_thread(bracket_image.render, session, tour)
    finally:
        session.close()
    msg = update.effective_message
    if not png:
        await _reply(update, f"🏆 <b>{html.escape(name)}</b> — no playoff bracket "
                             "yet. It appears once the league stage is over.")
        return
    if msg is not None:
        await msg.reply_photo(io.BytesIO(png),
                              caption=f"🏆 <b>{html.escape(name)}</b> — playoffs",
                              parse_mode="HTML")
