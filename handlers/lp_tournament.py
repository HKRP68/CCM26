"""Lets Play Tournament — the bot commands that run and play the competition.

A Lets Play Tournament is CIPL's structure with people instead of franchises: an
admin enters the field by **Telegram id**, the bot generates a fixture list, and
every fixture is a normal /letsplay match — own rosters, own cards, own traits,
20 overs. Results feed a points table with net run rate, per-player leaderboards
and (optionally) a playoff bracket, all shared with the Challenge League
tournament engine (``services.tournament_service``).

Playing
    /lptour            reply to your opponent (or /lptour @user) to play your
                       scheduled fixture. Runs the full /letsplay flow.

Following
    /lpt               the tournament's front page
    /lptable           points table
    /lptfixtures       full schedule, with your own next matches pulled out
    /lptteams          who is in it
    /lptstats          Top-10 batting / bowling leaderboards

Running it (bot admins only — see /lptadmin)
    /lptnew, /lptadd, /lptremove, /lptrename, /lptsync, /lptschedule,
    /lptknockout, /lptstart, /lptpause, /lptcomplete, /lptcancel, /lptreset,
    /lptlist, /lptuse, /lptdelete

Every admin command works on the *active* Lets Play tournament unless it takes
an explicit id, so the normal run of a season is: /lptnew → /lptadd ×N →
/lptschedule → /lptstart, and then the players use /lptour.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from services import lp_tournament_service as lpt
from services import tournament_service
from services.admin_ids import is_admin
from services.lp_tournament_service import LPTError
from services.telegram_user_service import sync_telegram_user, resolve_command_target

logger = logging.getLogger(__name__)

NOT_ADMIN = "⛔ Only bot admins can manage the Lets Play Tournament."
NO_ACTIVE = ("❌ No Lets Play Tournament is running right now.\n"
             "An admin can start one with <code>/lptnew &lt;name&gt;</code>.")


# ════════════════════════════════════════════════════════════════════
# Shared plumbing
# ════════════════════════════════════════════════════════════════════

async def _reply(update, text, **kwargs):
    """Reply with this module's defaults: HTML, and no link previews."""
    msg = update.effective_message
    if msg is None:
        return None
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await msg.reply_text(text, **kwargs)


def _arg_text(context):
    """The command's arguments as one string."""
    return " ".join(context.args or []).strip()


def _split_pipes(text, count):
    """Split on ``|`` into exactly ``count`` stripped parts, padding with ''."""
    parts = [p.strip() for p in (text or "").split("|")]
    parts += [""] * (count - len(parts))
    return parts[:count]


def _parse_tg_id(token):
    """A positive Telegram user id from a bare token, or None."""
    token = (token or "").strip().lstrip("@")
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value > 0 else None


def _target_tg_id(session, update, context, token):
    """Resolve the Telegram id this command is aimed at.

    Order: a numeric id in the argument (the documented way — it works for
    someone who has never messaged the bot), then a replied-to message, then an
    @username looked up in the database. Returns None when nothing resolves.
    """
    tg_id = _parse_tg_id(token)
    if tg_id:
        return tg_id
    message = update.effective_message
    reply = getattr(message, "reply_to_message", None) if message else None
    target = getattr(reply, "from_user", None) if reply else None
    if target and not getattr(target, "is_bot", False):
        return int(target.id)
    if token:
        # Keep resolve_command_target's view of the arguments consistent with the
        # single token we were handed (the rest may be a team name).
        saved, context.args = context.args, [token]
        try:
            user, _reason = resolve_command_target(session, update, context, "lptadd")
        finally:
            context.args = saved
        if user and user.telegram_id:
            return int(user.telegram_id)
    return None


async def _require_admin(update):
    """True if the sender is a bot admin; otherwise says so and returns False."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


def _load_active(session):
    """The live Lets Play tournament, or None."""
    return lpt.active_tournament(session)


async def _with_active(update, work, *, admin=False):
    """Run ``work(session, tour)`` against the active tournament.

    Centralises the four things every one of these commands has to do: check
    admin rights, find the active tournament, turn an ``LPTError`` into a plain
    reply, and always close the session. ``work`` returns the reply text and may
    mutate — the commit happens here, only when ``work`` returns without raising.
    """
    if admin and not await _require_admin(update):
        return
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        text = work(session, tour)
        session.commit()
    except LPTError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("Lets Play tournament command failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    if text:
        await _reply(update, text)


# ════════════════════════════════════════════════════════════════════
# /lptour — play an official fixture
# ════════════════════════════════════════════════════════════════════

async def lptour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptour — play your Lets Play Tournament fixture against another player.

    Checks the tournament rules here, then hands off to the ordinary /letsplay
    invitation → pitch → XI → toss → match flow with a tournament context
    attached. The fixture itself is only claimed at launch (in
    ``handlers.letsplay._launch_match``), so an invitation that is denied or
    times out leaves the schedule untouched.
    """
    msg = update.effective_message
    tg = update.effective_user
    if msg is None or tg is None:
        return

    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        host = sync_telegram_user(session, tg)
        if not host:
            await _reply(update, "❌ Do /debut first to get a roster!")
            return
        guest, _reason = resolve_command_target(session, update, context, "lptour")
        if not guest:
            await _reply(
                update,
                f"🏆 <b>{html.escape(tour.name)}</b> — play a fixture\n\n"
                "• Reply to your opponent's message with <code>/lptour</code>, or\n"
                "• <code>/lptour @username</code>\n\n"
                "See who you still have to play with /lptfixtures.")
            return
        try:
            host_team, guest_team = lpt.check_pair(
                session, tour, host.telegram_id, guest.telegram_id)
        except LPTError as exc:
            # LPTError messages are plain text and may quote a tournament or team
            # name, so escape once here rather than in the service.
            await _reply(update, f"⚠️ {html.escape(str(exc))}")
            return
        tour_ctx = {
            "tournament_id": tour.id,
            "tournament_name": tour.name,
            "guest_user_id": guest.id,
            "host_team_id": host_team.id,
            "guest_team_id": guest_team.id,
            "host_team_name": host_team.name,
            "guest_team_name": guest_team.name,
        }
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("/lptour setup failed")
        await _reply(update, "⚠️ Couldn't start the tournament match. Try again.")
        return
    finally:
        session.close()

    from handlers.letsplay import letsplay_handler
    await letsplay_handler(update, context, tour_ctx=tour_ctx)


# ════════════════════════════════════════════════════════════════════
# Public views
# ════════════════════════════════════════════════════════════════════

async def lpt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lpt — the active Lets Play Tournament's front page."""
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        text = lpt.render_overview(session, tour)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Table", callback_data="lptv_table"),
            InlineKeyboardButton("🗓️ Fixtures", callback_data="lptv_fixtures"),
        ], [
            InlineKeyboardButton("👥 Teams", callback_data="lptv_teams"),
            InlineKeyboardButton("🏠 Overview", callback_data="lptv_overview"),
        ]])
        await _reply(update, text, reply_markup=kb)
    except Exception:
        logger.exception("/lpt failed")
        await _reply(update, "⚠️ Could not load the tournament.")
    finally:
        session.close()


async def lpt_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``lptv_<view>`` — switch the /lpt card between overview/table/fixtures/teams.

    Open to anyone in the chat: these are read-only views of a public
    competition, and the fixtures view is personalised to whoever tapped it.
    """
    q = update.callback_query
    view = (q.data or "").split("_", 1)[-1]
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await q.answer("No tournament is running.", show_alert=True)
            return
        if view == "table":
            text = lpt.render_table(session, tour)
        elif view == "fixtures":
            text = lpt.render_fixtures(session, tour, viewer_tg_id=q.from_user.id)
        elif view == "teams":
            text = lpt.render_teams(session, tour)
        else:
            text = lpt.render_overview(session, tour)
        await q.answer()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(("● " if view == "table" else "") + "📊 Table",
                                 callback_data="lptv_table"),
            InlineKeyboardButton(("● " if view == "fixtures" else "") + "🗓️ Fixtures",
                                 callback_data="lptv_fixtures"),
        ], [
            InlineKeyboardButton(("● " if view == "teams" else "") + "👥 Teams",
                                 callback_data="lptv_teams"),
            InlineKeyboardButton(("● " if view == "overview" else "") + "🏠 Overview",
                                 callback_data="lptv_overview"),
        ]])
        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                     disable_web_page_preview=True,
                                     reply_markup=kb)
        except Exception:
            # Tapping the view you are already on is a no-op edit, which Telegram
            # rejects — that is not an error worth showing anybody.
            logger.debug("/lpt view edit skipped", exc_info=True)
    except Exception:
        logger.exception("/lpt view callback failed")
    finally:
        session.close()


async def lptable_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptable — the Lets Play Tournament points table."""
    await _with_active(update, lambda s, t: lpt.render_table(s, t))


async def lptfixtures_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptfixtures — the fixture list, with your own next matches highlighted."""
    viewer = update.effective_user.id if update.effective_user else None
    await _with_active(
        update, lambda s, t: lpt.render_fixtures(s, t, viewer_tg_id=viewer))


async def lptteams_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptteams — the participating squad list."""
    await _with_active(update, lambda s, t: lpt.render_teams(s, t))


# ── /lptstats: the same Top-10 leaderboards /tournamentstats renders, but for
#    the active *Lets Play* tournament. The rendering and the category set are
#    imported rather than duplicated; only the tournament lookup differs.

async def lptstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptstats — Top-10 Lets Play Tournament leaderboards with category buttons."""
    from handlers.tournament import _leaders_for, _render
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        opener = update.effective_user.id if update.effective_user else 0
        rows = _leaders_for(session, tour, "runs")
        await _reply(update, _render(tour, "runs", rows),
                     reply_markup=_stats_keyboard("runs", opener))
    except Exception:
        logger.exception("/lptstats failed")
        await _reply(update, "⚠️ Could not load tournament stats.")
    finally:
        session.close()


def _stats_keyboard(active, opener_tg):
    """Category keyboard for /lptstats, bound to the user who opened it."""
    from handlers.tournament import _CATEGORIES
    rows, row = [], []
    for key, label in _CATEGORIES:
        mark = "● " if key == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}",
                                        callback_data=f"lptstat_{key}_{opener_tg}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def lptstats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``lptstat_<cat>_<opener>`` — switch the /lptstats category."""
    from handlers.tournament import _leaders_for, _render, _CAT_LABELS
    q = update.callback_query
    try:
        _, cat, opener = q.data.split("_", 2)
        opener = int(opener)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    if q.from_user.id != opener:
        await q.answer("Only the person who opened this can use these buttons.",
                       show_alert=True)
        return
    if cat not in _CAT_LABELS:
        await q.answer("Unknown category.", show_alert=True)
        return
    await q.answer()
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await q.edit_message_text(NO_ACTIVE, parse_mode="HTML")
            return
        rows = _leaders_for(session, tour, cat)
        await q.edit_message_text(_render(tour, cat, rows), parse_mode="HTML",
                                  reply_markup=_stats_keyboard(cat, opener))
    except Exception:
        logger.exception("/lptstats callback failed")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Admin — setting a tournament up
# ════════════════════════════════════════════════════════════════════

ADMIN_HELP = (
    "🏆 <b>Lets Play Tournament — admin commands</b>\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "<b>Set it up</b>\n"
    "<code>/lptnew Name | format | playoffs | maxteams</code>\n"
    "   format: <code>single</code> (default) or <code>double</code>\n"
    "   playoffs: <code>none</code> (default), <code>top4</code>, "
    "<code>playoffs</code>, <code>knockout</code>\n"
    "   e.g. <code>/lptnew Summer Smash | double | top4 | 10</code>\n"
    "<code>/lptadd &lt;telegram_id&gt; | Team Name</code> — enter a player "
    "(team name optional; reply to a message or @username also work)\n"
    "<code>/lptremove &lt;telegram_id&gt;</code>\n"
    "<code>/lptrename &lt;telegram_id&gt; | New Name</code>\n"
    "<code>/lptsync</code> — fill in names for players who have since /debut'd\n"
    "<code>/lptschedule [single|double]</code> — build the fixture list\n"
    "<code>/lptknockout [top4|playoffs|knockout]</code> — seed the playoffs\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "<b>Run it</b>\n"
    "<code>/lptstart</code> · <code>/lptpause</code> · <code>/lptcomplete</code> "
    "· <code>/lptcancel</code>\n"
    "<code>/lptreset</code> — wipe results, keep the teams and schedule\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "<b>Switch between tournaments</b>\n"
    "<code>/lptlist</code> · <code>/lptuse &lt;id&gt;</code> · "
    "<code>/lptdelete &lt;id&gt;</code>\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "Players then play their fixtures with <code>/lptour</code> and follow it "
    "with /lpt, /lptable, /lptfixtures, /lptstats.\n\n"
    "Leave the schedule ungenerated for a free-play tournament: any two entered "
    "players may then meet any number of times, and every result still counts."
)


async def lptadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptadmin — the admin command reference."""
    if not await _require_admin(update):
        return
    await _reply(update, ADMIN_HELP)


async def lptnew_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptnew <name> [| format [| playoffs [| max teams]]] — create and activate."""
    if not await _require_admin(update):
        return
    raw = _arg_text(context)
    if not raw:
        await _reply(update,
                     "Usage: <code>/lptnew Name | format | playoffs | maxteams</code>\n"
                     "Only the name is required. See /lptadmin for the options.")
        return
    name, fmt, ko, cap = _split_pipes(raw, 4)
    fmt = (fmt or "single").lower()
    ko = (ko or "").lower()
    if ko in ("", "none", "no", "-"):
        ko = None
    session = get_session()
    try:
        tour = lpt.create_tournament(
            session, name, league_format=fmt, knockout=ko,
            max_teams=int(cap) if cap.isdigit() else 8)
        # A freshly created tournament becomes the active one — an admin creating
        # it is the person about to fill it, and a draft nobody can reach is
        # useless. It stays in draft, so /lptstart is still the go-live step.
        lpt.activate(session, tour.id, keep_draft=True)
        session.commit()
        label, cap_text, ko_text = tour.name, tour.max_teams, tour.knockout_type
    except LPTError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except ValueError:
        session.rollback()
        await _reply(update, "⚠️ Max teams must be a number.")
        return
    except Exception:
        session.rollback()
        logger.exception("/lptnew failed")
        await _reply(update, "⚠️ Could not create the tournament.")
        return
    finally:
        session.close()
    await _reply(
        update,
        f"✅ Created <b>{html.escape(label)}</b> and made it the active Lets Play "
        f"Tournament.\n"
        f"Format: {html.escape(fmt)} round-robin"
        + (f" + {html.escape(ko_text)}" if ko_text else "")
        + f" · up to {cap_text} teams\n\n"
        "Next: add players with <code>/lptadd &lt;telegram_id&gt;</code>, then "
        "<code>/lptschedule</code>, then <code>/lptstart</code>.")


async def lptadd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptadd <telegram_id> [| Team Name] — enter a player in the tournament."""
    if not await _require_admin(update):
        return
    raw = _arg_text(context)
    id_part, name_part = _split_pipes(raw, 2)
    token = id_part.split()[0] if id_part.split() else ""
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        tg_id = _target_tg_id(session, update, context, token)
        if not tg_id:
            await _reply(
                update,
                "Usage: <code>/lptadd &lt;telegram_id&gt; | Team Name</code>\n"
                "You can also reply to the player's message with "
                "<code>/lptadd</code>, or pass <code>@username</code>.")
            return
        team, cleared = lpt.add_participant(session, tour.id, tg_id, name_part)
        label = team.name
        count = len(lpt.participants(session, tour.id))
        tour_label = tour.name
        session.commit()
    except LPTError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/lptadd failed")
        await _reply(update, "⚠️ Could not add that player.")
        return
    finally:
        session.close()
    note = ("\n♻️ The unplayed schedule was cleared — run <code>/lptschedule</code> "
            "again once the field is final." if cleared else "")
    await _reply(
        update,
        f"✅ Added <b>{html.escape(label)}</b> (<code>{tg_id}</code>) to "
        f"<b>{html.escape(tour_label)}</b>.\n"
        f"👥 {count} team(s) entered.{note}")


async def lptremove_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptremove <telegram_id> — take a player out of the tournament."""
    if not await _require_admin(update):
        return
    token = (_arg_text(context).split() or [""])[0]
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        tg_id = _target_tg_id(session, update, context, token)
        if not tg_id:
            await _reply(update,
                         "Usage: <code>/lptremove &lt;telegram_id&gt;</code> "
                         "(or reply to the player's message).")
            return
        label, cleared = lpt.remove_participant(session, tour.id, tg_id)
        count = len(lpt.participants(session, tour.id))
        session.commit()
    except LPTError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/lptremove failed")
        await _reply(update, "⚠️ Could not remove that player.")
        return
    finally:
        session.close()
    note = ("\n♻️ The unplayed schedule was cleared — run <code>/lptschedule</code> "
            "again." if cleared else "")
    await _reply(update, f"✅ Removed <b>{html.escape(label)}</b>.\n"
                         f"👥 {count} team(s) remaining.{note}")


async def lptrename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptrename <telegram_id> | New Name — rename a participant's team."""
    if not await _require_admin(update):
        return
    id_part, name_part = _split_pipes(_arg_text(context), 2)
    token = (id_part.split() or [""])[0]
    session = get_session()
    try:
        tour = _load_active(session)
        if not tour:
            await _reply(update, NO_ACTIVE)
            return
        tg_id = _target_tg_id(session, update, context, token)
        if not tg_id or not name_part:
            await _reply(update,
                         "Usage: <code>/lptrename &lt;telegram_id&gt; | New Name</code>")
            return
        old, new = lpt.rename_participant(session, tour.id, tg_id, name_part)
        session.commit()
    except LPTError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/lptrename failed")
        await _reply(update, "⚠️ Could not rename that team.")
        return
    finally:
        session.close()
    await _reply(update, f"✅ <b>{html.escape(old)}</b> → "
                         f"<b>{html.escape(new)}</b>")


async def lptsync_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptsync — resolve placeholder names for players who have since registered."""
    def work(session, tour):
        changed = lpt.sync_participant_names(session, tour.id)
        if not changed:
            return ("Nothing to update — every team already has a real name.")
        return f"✅ Updated {changed} team name(s) from their Telegram accounts."
    await _with_active(update, work, admin=True)


async def lptschedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptschedule [single|double] — generate the round-robin fixture list."""
    mode = (_arg_text(context) or "").lower().strip()

    def work(session, tour):
        if mode in lpt.LEAGUE_FORMATS:
            tour.league_format = lpt.LEAGUE_FORMATS[mode][0]
        elif mode:
            raise LPTError("Format must be 'single' or 'double'.")
        count = lpt.generate_schedule(session, tour.id)
        return (f"✅ Scheduled <b>{count}</b> fixture(s) for "
                f"<b>{html.escape(tour.name)}</b>.\n"
                "Players see theirs with /lptfixtures. Only scheduled pairings "
                "can be played, and each pairing only once.")
    await _with_active(update, work, admin=True)


async def lptknockout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptknockout [top4|playoffs|knockout] — seed the playoff bracket."""
    mode = (_arg_text(context) or "").lower().strip() or None

    def work(session, tour):
        count = lpt.generate_knockout(session, tour.id, mode)
        return (f"✅ Seeded a <b>{count}</b>-match bracket from the table.\n"
                "The bracket fills in as rounds are played — /lptfixtures shows it.")
    await _with_active(update, work, admin=True)


# ════════════════════════════════════════════════════════════════════
# Admin — lifecycle
# ════════════════════════════════════════════════════════════════════

_LIFECYCLE = {
    "start": ("active", "🟢 Started — players can now use /lptour."),
    "pause": ("paused", "⏸️ Paused — no new tournament matches can start."),
    "complete": ("completed", "🏁 Completed."),
    "cancel": ("cancelled", "❌ Cancelled."),
}


def _lifecycle_handler(action):
    """Build the /lptstart · /lptpause · /lptcomplete · /lptcancel handler."""
    status, note = _LIFECYCLE[action]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        def work(session, tour):
            if action == "start" and len(lpt.participants(session, tour.id)) < 2:
                raise LPTError("Add at least two teams before starting.")
            tournament_service.set_status(session, tour.id, status)
            return f"<b>{html.escape(tour.name)}</b> — {note}"
        await _with_active(update, work, admin=True)

    handler.__name__ = f"lpt{action}_handler"
    handler.__doc__ = f"/lpt{action} — set the active Lets Play tournament to {status}."
    return handler


lptstart_handler = _lifecycle_handler("start")
lptpause_handler = _lifecycle_handler("pause")
lptcomplete_handler = _lifecycle_handler("complete")
lptcancel_handler = _lifecycle_handler("cancel")


async def lptreset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptreset — clear every result, keeping the teams and the fixture list."""
    def work(session, tour):
        tournament_service.reset_tournament(session, tour.id)
        return (f"♻️ Reset <b>{html.escape(tour.name)}</b> — results, standings and "
                "player stats cleared. The teams and the schedule are untouched; "
                "regenerate the playoff bracket when the league is replayed.")
    await _with_active(update, work, admin=True)


async def lptlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptlist — every Lets Play tournament, with the active one marked."""
    if not await _require_admin(update):
        return
    session = get_session()
    try:
        rows = lpt.list_tournaments(session)
        if not rows:
            await _reply(update, "No Lets Play tournaments exist yet. Create one "
                                 "with <code>/lptnew &lt;name&gt;</code>.")
            return
        lines = ["🏆 <b>Lets Play Tournaments</b>", ""]
        for t in rows:
            teams = len(lpt.participants(session, t.id))
            played, total = tournament_service.league_progress(session, t.id)
            mark = "▶️" if t.is_active else "  "
            lines.append(
                f"{mark} <code>{t.id}</code> <b>{html.escape(t.name)}</b> — "
                f"{lpt.status_label(t)} · {teams} team(s)"
                + (f" · {played}/{total} played" if total else ""))
        lines += ["", "Switch with <code>/lptuse &lt;id&gt;</code>."]
        await _reply(update, "\n".join(lines))
    except Exception:
        logger.exception("/lptlist failed")
        await _reply(update, "⚠️ Could not list the tournaments.")
    finally:
        session.close()


async def lptuse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptuse <id> — make a Lets Play tournament the active one."""
    if not await _require_admin(update):
        return
    token = (_arg_text(context).split() or [""])[0]
    if not token.isdigit():
        await _reply(update, "Usage: <code>/lptuse &lt;id&gt;</code> — see /lptlist.")
        return
    session = get_session()
    try:
        tour = lpt.get_tournament(session, int(token))
        if not tour:
            await _reply(update, "⚠️ No Lets Play tournament with that id.")
            return
        lpt.activate(session, tour.id)
        label, status = tour.name, lpt.status_label(tour)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("/lptuse failed")
        await _reply(update, "⚠️ Could not switch tournament.")
        return
    finally:
        session.close()
    await _reply(update, f"✅ <b>{html.escape(label)}</b> is now the active Lets "
                         f"Play Tournament — {status}")


async def lptdelete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lptdelete <id> — delete a Lets Play tournament and all of its data."""
    if not await _require_admin(update):
        return
    token = (_arg_text(context).split() or [""])[0]
    if not token.isdigit():
        await _reply(update,
                     "Usage: <code>/lptdelete &lt;id&gt;</code> — see /lptlist.\n"
                     "This removes its teams, fixtures and stats for good.")
        return
    session = get_session()
    try:
        tour = lpt.get_tournament(session, int(token))
        if not tour:
            await _reply(update, "⚠️ No Lets Play tournament with that id.")
            return
        label = tour.name
        session.delete(tour)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("/lptdelete failed")
        await _reply(update, "⚠️ Could not delete that tournament.")
        return
    finally:
        session.close()
    await _reply(update, f"🗑️ Deleted <b>{html.escape(label)}</b> and all of its data.")
