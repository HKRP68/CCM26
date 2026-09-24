"""Public commands for following a Challenge League Tournament.

The competition already had a stats leaderboard (/tournamentstats) but no way
for a player to see the schedule. These commands close that gap:

    /ctour        the hub card — overview, with tabs for the rest
    /cttable      points table
    /ctfixtures   the fixture list: pitch, home side, and the played ones
                  struck through
    /ctteams      the field, with each team's owner
    /ctinjuries   the treatment room, when the tournament has injuries on
    /clsd <team>  one team's whole tournament: where they stand, their form,
                  what they play next and every result so far
    /teamtourstats [team]  the same team by the numbers — standing, what it
                  scores and concedes, its best and worst days, and which of
                  its own players are carrying it

Every view is sent twice over: as a Bot API 10.1 rich message when the server
takes one (``services/cl_tournament_rich.py`` — the points table, the fixture
list and a team's card are native tables there, not ``<code>`` padded to a
guessed width), and as the HTML in ``services/cl_tournament_view.py`` whenever
the rich send is refused. Both renderings are live; see
``docs/rich-text-messages.md``.

Every one of them is read-only and open to anyone; starting a match is still the
league's own (gated) tournament command. A league may also publish its own alias
for the hub — ``ChallengeLeague.fixtures_command`` — which routes here from
``handlers.challenge``.
"""

import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from services import cl_tournament_rich as ctr
from services import cl_tournament_view as ctv
from services import rich_message as R
from services import tournament_service

logger = logging.getLogger(__name__)

NO_ACTIVE = ("❌ No Challenge League Tournament is currently active.\n"
             "An admin activates one from the tournament panel.")

# /clsd searches the Lets Play tournament too, so its "nothing running" line
# must not name only the Challenge League — a player in a chat with neither
# would otherwise go looking for a Challenge League that was never the point.
NO_ACTIVE_ANY = ("❌ No tournament is currently running.\n"
                 "An admin activates one from the tournament panel.")

# Callback prefix. ``ctv_`` is deliberately outside the ``cl_`` namespace the
# Challenge League match callbacks own, and the ``lptv_`` one Lets Play uses.
CB_PREFIX = "ctv_"


_LABELS = {
    "table": "📊 Table", "fixtures": "🗓️ Fixtures", "teams": "👥 Teams",
    "injuries": "🚑 Injuries", "overview": "🏠 Overview",
}


def _keyboard(tour, active="overview"):
    """The hub's tab row, marking whichever view is showing.

    The Injuries tab only appears when the tournament has an injury system —
    a tab that always answers "there isn't one" is just noise.
    """
    from services import cl_tournament_view as _ctv
    views = [v for v in ("table", "fixtures", "teams", "injuries", "overview")
             if v in _ctv.views_for(tour)]
    buttons = [InlineKeyboardButton(("● " if v == active else "") + _LABELS[v],
                                    callback_data=CB_PREFIX + v)
               for v in views]
    return InlineKeyboardMarkup([buttons[i:i + 2]
                                 for i in range(0, len(buttons), 2)])


async def _reply(update, text, reply_markup=None, blocks=None):
    """Answer with ``blocks`` when the server takes them, else the HTML.

    ``blocks=None`` is the ordinary case for the short prompts here (a "which
    team?" question, an error) — those have nothing a table would improve, and
    ``rich_message`` sends the HTML straight through for them.
    """
    msg = update.effective_message
    if msg is None:
        return
    await R.reply_rich(msg, blocks, text, reply_markup=reply_markup)


async def _show(update, view):
    """Render one view of the active tournament, with the tab row attached."""
    session = get_session()
    try:
        tour = ctv.active_tournament(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        viewer = update.effective_user.id if update.effective_user else None
        text = ctv.render(session, tour, view, viewer_tg_id=viewer)
        blocks = ctr.render_blocks(session, tour, view, viewer_tg_id=viewer)
        await _reply(update, text, reply_markup=_keyboard(tour, view),
                     blocks=blocks)
    except Exception:
        logger.exception("Challenge League Tournament view %r failed", view)
        await _reply(update, "⚠️ Could not load the tournament right now.")
    finally:
        session.close()


async def ctour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ctour — the active Challenge League Tournament's front page."""
    await _show(update, "overview")


async def cttable_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cttable — the points table."""
    await _show(update, "table")


async def ctfixtures_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ctfixtures — the schedule, with your own next matches pulled out."""
    await _show(update, "fixtures")


async def ctteams_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ctteams — the participating teams and who owns them."""
    await _show(update, "teams")


async def ctinjuries_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ctinjuries — who is ruled out, and for how many more matches."""
    await _show(update, "injuries")


# ══════════════════════════════════════════════════════════════════════
# /clsd <TEAM NAME> — one team's schedule
# ══════════════════════════════════════════════════════════════════════
#
# /ctfixtures is the competition from above: every fixture, in order. That is
# the wrong shape for the question a team owner actually asks — "what do WE
# play next, and how are we doing?" — because their three remaining matches are
# scattered through forty lines belonging to everyone else.
#
# /clsd answers that question for one named team, and it takes the NAME rather
# than requiring ownership so a captain can scout the side they are about to
# face, and so a chat can look up any team without every member of it being
# assigned an owner first.
#
# It reads whichever tournament is live: the Challenge League one, and failing
# that the Lets Play one. Both store their teams and fixtures in the same two
# tables (``TournamentTeam`` / ``TournamentMatch``), so one card serves both,
# and a player typing /clsd in a chat running a Lets Play tournament gets an
# answer rather than "no tournament is active".

# Callback prefix for the disambiguation buttons. Deliberately NOT ``ctv_``:
# that handler reads everything after the first underscore as a view name.
SD_PREFIX = "ctsd_"


def _live_tournaments(session):
    """Every live tournament a /clsd lookup should search, best bet first."""
    out = []
    challenge = ctv.active_tournament(session)
    if challenge:
        out.append(challenge)
    try:
        from services import lp_tournament_service
        letsplay = lp_tournament_service.active_tournament(session)
    except Exception:
        logger.exception("/clsd: Lets Play tournament lookup failed")
        letsplay = None
    if letsplay:
        out.append(letsplay)
    return out


def _resolve(session, query):
    """``(tour, team, candidates)`` for a typed team name.

    Searches each live tournament in turn and returns the first *resolved* hit.
    Ambiguity in one tournament does not stop the search — a query that is
    ambiguous in the Challenge League but names exactly one Lets Play team
    should still find that team — but it is remembered, so a query that resolves
    nowhere can still show the near misses rather than a bare "not found".
    """
    near = (None, [])
    for tour in _live_tournaments(session):
        team, candidates = ctv.find_team(session, tour.id, query)
        if team is not None:
            return tour, team, []
        if candidates and not near[1]:
            near = (tour, candidates)
    return near[0], None, near[1]


def _pick_keyboard(candidates, limit=8):
    """Buttons for the teams a query could have meant."""
    rows = [[InlineKeyboardButton(
        (tt.name or "—")[:40], callback_data=f"{SD_PREFIX}{tt.id}")]
        for tt in candidates[:limit]]
    return InlineKeyboardMarkup(rows) if rows else None


def _team_list_text(session, tours, *, header=None, usage=None):
    """The 'which team?' prompt, listing what there is to ask about.

    ``header`` and ``usage`` let the sibling commands that take a team name
    reuse the list under their own heading — the field of teams is the same
    either way, and only the line telling the user what to type differs.

    Team and tournament names are typed by admins, so they are escaped: one
    stray ``&`` in a franchise name makes Telegram reject the whole message, and
    the prompt that explains how to use the command is the worst place to lose.
    """
    lines = [header or "🗓️ <b>Team schedule</b>",
             usage or ("Usage: <code>/clsd &lt;team name&gt;</code> — e.g. "
                       "<code>/clsd Mumbai Indians</code>")]
    for tour in tours:
        rows = ctv.teams(session, tour.id)
        if not rows:
            continue
        lines += ["", f"<b>{escape(tour.name or '—')}</b>",
                  " · ".join(escape(tt.name or "—") for tt in rows[:20])]
    return "\n".join(lines)


async def clsd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clsd <TEAM NAME> — one team's fixtures, form and results."""
    session = get_session()
    try:
        tours = _live_tournaments(session)
        if not tours:
            await _reply(update, NO_ACTIVE_ANY)
            return
        viewer = update.effective_user.id if update.effective_user else None
        query = " ".join(context.args or []).strip()

        if not query:
            # No name typed. If the viewer runs a team, that is almost certainly
            # the one they meant, so show it rather than making them type it.
            for tour in tours:
                for tt in ctv.teams(session, tour.id):
                    if viewer is not None and tournament_service.is_team_member(
                            tt, viewer):
                        await _reply(
                            update,
                            ctv.render_team_schedule(session, tour, tt,
                                                     viewer_tg_id=viewer),
                            blocks=ctr.team_schedule_blocks(
                                session, tour, tt, viewer_tg_id=viewer))
                        return
            await _reply(update, _team_list_text(session, tours),
                         reply_markup=_pick_keyboard(
                             [tt for tour in tours
                              for tt in ctv.teams(session, tour.id)]))
            return

        tour, team, candidates = _resolve(session, query)
        if team is not None:
            await _reply(
                update,
                ctv.render_team_schedule(session, tour, team,
                                         viewer_tg_id=viewer),
                blocks=ctr.team_schedule_blocks(session, tour, team,
                                                viewer_tg_id=viewer))
            return
        if candidates:
            await _reply(
                update,
                f"🤔 <b>{escape(query)}</b> could be "
                f"{len(candidates)} teams. Which one?",
                reply_markup=_pick_keyboard(candidates))
            return
        await _reply(
            update,
            f"❌ No team called <b>{escape(query)}</b> is in the tournament.\n\n"
            + _team_list_text(session, tours))
    except Exception:
        logger.exception("/clsd failed")
        await _reply(update, "⚠️ Could not load that team's schedule right now.")
    finally:
        session.close()


async def clsd_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``ctsd_<tournament_team_id>`` — show the team picked from the prompt.

    The id is re-read from the database rather than trusted from the button, so
    a stale card (the tournament ended, the team was removed) says so instead of
    rendering a card for a team that is no longer in the competition.
    """
    q = update.callback_query
    raw = (q.data or "")[len(SD_PREFIX):]
    session = get_session()
    try:
        try:
            team_id = int(raw)
        except ValueError:
            await q.answer("Unknown team.", show_alert=True)
            return
        from models import Tournament, TournamentTeam
        team = session.get(TournamentTeam, team_id)
        tour = session.get(Tournament, team.tournament_id) if team else None
        if not team or not tour:
            await q.answer("That team is no longer in the tournament.",
                           show_alert=True)
            return
        text = ctv.render_team_schedule(session, tour, team,
                                        viewer_tg_id=q.from_user.id)
        blocks = ctr.team_schedule_blocks(session, tour, team,
                                          viewer_tg_id=q.from_user.id)
        await q.answer()
        await R.edit_rich(q, blocks, text)
    except Exception:
        logger.exception("/clsd pick callback failed")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# /teamtourstats [TEAM NAME] — one team's tournament by the numbers
# ══════════════════════════════════════════════════════════════════════
#
# /tournamentstats ranks individual players across the whole competition, and
# /clsd shows one team's fixtures. Neither answers the question a team owner
# opens the bot to ask: "how is MY team doing?" — what we score, what we concede,
# our best and worst days with the bat, and who in our own XI is actually
# winning us matches.
#
# It defaults to the viewer's own team, which is the whole point of the command,
# and still takes a name so a captain can scout the side they are about to face.
# Like /clsd it reads whichever tournament is live — the Challenge League one,
# and failing that the Lets Play one, where a team *is* a person playing their
# own roster.

# Its own callback namespace: ``ctsd_`` belongs to /clsd's picker.
TS_PREFIX = "ctts_"

_TS_HEADER = "📊 <b>Team stats</b>"
_TS_USAGE = ("Usage: <code>/teamtourstats &lt;team name&gt;</code> — or run it "
             "with no name and you get your own team.")


def _my_teams(session, tours, viewer_tg_id):
    """``[(tour, team), …]`` — every team this viewer runs in a live tournament.

    A Lets Play team is the viewer themselves, so it is matched on
    ``user_tg_id``; a Challenge League team is matched on ownership, which
    covers co-owners too — a franchise run by three people is all three
    people's team.
    """
    out = []
    if viewer_tg_id is None:
        return out
    for tour in tours:
        for tt in ctv.teams(session, tour.id):
            if (tournament_service.is_team_member(tt, viewer_tg_id)
                    or (tt.user_tg_id
                        and int(tt.user_tg_id) == int(viewer_tg_id))):
                out.append((tour, tt))
    return out


def _ts_pick_keyboard(pairs, limit=8):
    """Buttons for the teams a /teamtourstats query could have meant."""
    rows = [[InlineKeyboardButton((tt.name or "—")[:40],
                                  callback_data=f"{TS_PREFIX}{tt.id}")]
            for _tour, tt in pairs[:limit]]
    return InlineKeyboardMarkup(rows) if rows else None


async def teamtourstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/teamtourstats [TEAM NAME] — a team's standing, scoring and top players."""
    session = get_session()
    try:
        tours = _live_tournaments(session)
        if not tours:
            await _reply(update, NO_ACTIVE_ANY)
            return
        viewer = update.effective_user.id if update.effective_user else None
        query = " ".join(context.args or []).strip()

        if not query:
            mine = _my_teams(session, tours, viewer)
            if len(mine) == 1:
                tour, team = mine[0]
                await _reply(
                    update,
                    ctv.render_team_stats(session, tour, team,
                                          viewer_tg_id=viewer),
                    blocks=ctr.team_stats_blocks(session, tour, team,
                                                 viewer_tg_id=viewer))
                return
            if len(mine) > 1:
                # Somebody who runs several franchises has to say which.
                await _reply(
                    update,
                    f"{_TS_HEADER}\nYou run {len(mine)} teams — which one?",
                    reply_markup=_ts_pick_keyboard(mine))
                return
            # Not a team owner: they have to name the team they want.
            await _reply(update, _team_list_text(
                session, tours, header=_TS_HEADER, usage=_TS_USAGE))
            return

        tour, team, candidates = _resolve(session, query)
        if team is not None:
            await _reply(
                update,
                ctv.render_team_stats(session, tour, team, viewer_tg_id=viewer),
                blocks=ctr.team_stats_blocks(session, tour, team,
                                             viewer_tg_id=viewer))
            return
        if candidates:
            await _reply(
                update,
                f"🤔 <b>{escape(query)}</b> could be "
                f"{len(candidates)} teams. Which one?",
                reply_markup=_ts_pick_keyboard([(tour, tt) for tt in candidates]))
            return
        await _reply(
            update,
            f"❌ No team called <b>{escape(query)}</b> is in the tournament.\n\n"
            + _team_list_text(session, tours,
                              header=_TS_HEADER, usage=_TS_USAGE))
    except Exception:
        logger.exception("/teamtourstats failed")
        await _reply(update, "⚠️ Could not load that team's stats right now.")
    finally:
        session.close()


async def teamtourstats_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``ctts_<tournament_team_id>`` — show the team picked from the prompt."""
    q = update.callback_query
    raw = (q.data or "")[len(TS_PREFIX):]
    session = get_session()
    try:
        try:
            team_id = int(raw)
        except ValueError:
            await q.answer("Unknown team.", show_alert=True)
            return
        from models import Tournament, TournamentTeam
        team = session.get(TournamentTeam, team_id)
        tour = session.get(Tournament, team.tournament_id) if team else None
        if not team or not tour:
            await q.answer("That team is no longer in the tournament.",
                           show_alert=True)
            return
        text = ctv.render_team_stats(session, tour, team,
                                     viewer_tg_id=q.from_user.id)
        blocks = ctr.team_stats_blocks(session, tour, team,
                                       viewer_tg_id=q.from_user.id)
        await q.answer()
        await R.edit_rich(q, blocks, text)
    except Exception:
        logger.exception("/teamtourstats pick callback failed")
    finally:
        session.close()


async def ct_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``ctv_<view>`` — switch the hub card between its four views.

    Open to anyone in the chat: these are read-only views of a public
    competition, and the fixtures view is personalised to whoever tapped it.
    """
    q = update.callback_query
    view = (q.data or "").split("_", 1)[-1]
    if view not in ctv.VIEWS:
        view = "overview"
    session = get_session()
    try:
        tour = ctv.active_tournament(session)
        if not tour:
            await q.answer("No tournament is running.", show_alert=True)
            return
        text = ctv.render(session, tour, view, viewer_tg_id=q.from_user.id)
        blocks = ctr.render_blocks(session, tour, view,
                                   viewer_tg_id=q.from_user.id)
        await q.answer()
        # Tapping the view you are already on is a no-op edit, which Telegram
        # rejects — ``edit_rich`` reads that as "already drawn", not an error.
        await R.edit_rich(q, blocks, text, reply_markup=_keyboard(tour, view))
    except Exception:
        logger.exception("/ctour view callback failed")
    finally:
        session.close()
