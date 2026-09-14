"""Public commands for following a Challenge League Tournament.

The competition already had a stats leaderboard (/tournamentstats) but no way
for a player to see the schedule. These commands close that gap:

    /ctour        the hub card — overview, with tabs for the rest
    /cttable      points table
    /ctfixtures   the fixture list: pitch, home side, and the played ones
                  struck through
    /ctteams      the field, with each team's owner

Every one of them is read-only and open to anyone; starting a match is still the
league's own (gated) tournament command. A league may also publish its own alias
for the hub — ``ChallengeLeague.fixtures_command`` — which routes here from
``handlers.challenge``.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from services import cl_tournament_view as ctv

logger = logging.getLogger(__name__)

NO_ACTIVE = ("❌ No Challenge League Tournament is currently active.\n"
             "An admin activates one from the tournament panel.")

# Callback prefix. ``ctv_`` is deliberately outside the ``cl_`` namespace the
# Challenge League match callbacks own, and the ``lptv_`` one Lets Play uses.
CB_PREFIX = "ctv_"


def _keyboard(active="overview"):
    """The hub's tab row, marking whichever view is showing."""
    def _b(view, label):
        return InlineKeyboardButton(("● " if view == active else "") + label,
                                    callback_data=CB_PREFIX + view)
    return InlineKeyboardMarkup([
        [_b("table", "📊 Table"), _b("fixtures", "🗓️ Fixtures")],
        [_b("teams", "👥 Teams"), _b("overview", "🏠 Overview")],
    ])


async def _reply(update, text, reply_markup=None):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(text, parse_mode="HTML",
                         disable_web_page_preview=True,
                         reply_markup=reply_markup)


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
        await _reply(update, text, reply_markup=_keyboard(view))
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
        await q.answer()
        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                      disable_web_page_preview=True,
                                      reply_markup=_keyboard(view))
        except Exception:
            # Tapping the view you are already on is a no-op edit, which Telegram
            # rejects — that is not an error worth showing anybody.
            logger.debug("/ctour view edit skipped", exc_info=True)
    except Exception:
        logger.exception("/ctour view callback failed")
    finally:
        session.close()
