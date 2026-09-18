"""``/remindmatch`` — tell two teams, in the group and in their DMs, to go and
play the fixture they still owe each other.

A scheduled tournament stalls for the dullest possible reason: two people each
assume the other will start the match, and neither has opened the fixture list
today. Everything the bot had until now — ``/ctfixtures``, ``/clsd`` — only talks
to whoever already typed it, which is never the person holding things up.

    /remindmatch                  everything still unplayed
    /remindmatch SRH              one team's remaining fixtures
    /remindmatch SRH vs RR        just that pair
    /remindmatch SRH force        ignore the 12-hour cooldown

**Bot admins only** — the whole command, its aliases and its buttons. The thing
it does is make the bot tag people and slide into their DMs, which is not a
capability to hand to whoever can type, and a league where anyone could nudge
anyone becomes one where the nudges are ignored. The check is
``services.admin_ids.is_admin``, and it runs three times: on the command, again
when the plan is built, and again on the Send tap — a callback arrives as its
own update, so an admin who lost their rights between the preview and the tap
must not still be able to fire it. A refused caller gets one line and nothing
else, not even a "no such team", because error messages are replies too.

It never fires straight away. The first message back is a **preview**: who will
be pinged, in which fixtures, and what is being held back by the cooldown, with
a Send button under it. Tagging a dozen people is not a thing to do by typo, and
a preview costs one tap.

On Send, two things go out:

  • the group it was run in gets the card (the pair's full card when there is
    one pair, a roundup of every stalled pair when there are several), and
  • every owner and co-owner of both sides gets a DM with their own fixtures —
    one DM per person, however many teams they run.

A reminded fixture then goes quiet for ``COOLDOWN`` (see
``services.match_reminder_service``) so the second nudge still means something.
"""

import asyncio
import logging
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import Tournament, TournamentTeam
from services import cl_tournament_view as ctv
from services import match_reminder_service as mrs
from services import tournament_service
from services.admin_ids import is_admin

logger = logging.getLogger(__name__)

CB_PREFIX = "mrem_"

NO_ACTIVE = ("❌ No tournament is currently running.\n"
             "An admin activates one from the tournament panel.")

NOT_ALLOWED = "⛔ Only bot admins can send match reminders."

# How many stalled pairs one run will chase. A tournament that has stalled
# completely is a conversation with an admin, not forty DMs.
MAX_PAIRS = 12

# At most this many full fixture cards go into one person's DM; the rest are
# named and pointed at /clsd. Telegram's message ceiling is 4096 characters and
# a card is a few hundred.
MAX_CARDS_PER_DM = 3

# Telegram's ceiling for messages to different chats is about 30 a second.
# Reminders are tens of messages, not thousands, so a flat gap is enough.
SEND_GAP_SECONDS = 0.06


# ══════════════════════════════════════════════════════════════════════
# Finding the tournament and reading the arguments
# ══════════════════════════════════════════════════════════════════════

def _live_tournaments(session):
    """Every live tournament a reminder could be about, best bet first."""
    out = []
    challenge = ctv.active_tournament(session)
    if challenge:
        out.append(challenge)
    try:
        from services import lp_tournament_service
        letsplay = lp_tournament_service.active_tournament(session)
    except Exception:
        logger.exception("/remindmatch: Lets Play tournament lookup failed")
        letsplay = None
    if letsplay:
        out.append(letsplay)
    return out


def _split_args(args):
    """``(first name, second name, force)`` from the words that were typed.

    ``vs`` (or ``v``) separates two teams, because team names have spaces in them
    and "SRH vs RR" is how people write it anyway. A trailing ``force`` is pulled
    off first so it can't be mistaken for part of a name.
    """
    words = [w for w in (args or []) if w.strip()]
    force = False
    while words and words[-1].lower() in ("force", "!", "--force"):
        force = True
        words.pop()
    lowered = [w.lower() for w in words]
    for sep in ("vs", "v", "against"):
        if sep in lowered:
            i = lowered.index(sep)
            return (" ".join(words[:i]).strip(),
                    " ".join(words[i + 1:]).strip(), force)
    return " ".join(words).strip(), "", force


def _resolve_team(session, tours, query):
    """``(tour, team, candidates)`` for a typed team name, across live tournaments."""
    near = (None, [])
    for tour in tours:
        team, candidates = ctv.find_team(session, tour.id, query)
        if team is not None:
            return tour, team, []
        if candidates and not near[1]:
            near = (tour, candidates)
    return near[0], None, near[1]


def _tournament_command(session, tour):
    """The command these two would actually type to play the match."""
    try:
        if tournament_service.tournament_kind(tour) == tournament_service.KIND_LETSPLAY:
            return "/lptour"
        from handlers.challenge import _active_tournament_command
        return (_active_tournament_command(session, tour) or "").strip() or None
    except Exception:
        logger.debug("Tournament command lookup failed", exc_info=True)
        return None


# ══════════════════════════════════════════════════════════════════════
# Working out what a run would send
# ══════════════════════════════════════════════════════════════════════

class _Plan:
    """What one ``/remindmatch`` run resolved to, before anything is sent."""

    def __init__(self, tour, nudges, scope, force=False, error=None):
        self.tour = tour
        self.nudges = nudges
        self.scope = scope          # what was asked for, for the preview's header
        self.force = force
        self.error = error

    @property
    def sending(self):
        """The nudges that would actually go out (cooldown survivors)."""
        return [n for n in self.nudges if n.skipped_until is None][:MAX_PAIRS]

    @property
    def quiet(self):
        """The pairs being held back because they were reminded recently."""
        return [n for n in self.nudges if n.skipped_until is not None]

    @property
    def people(self):
        """Everyone at least one nudge would reach, without duplicates."""
        seen = {}
        for nudge in self.sending:
            for tg_id, mention in nudge.recipients:
                seen.setdefault(tg_id, mention)
        return seen


def build_plan(session, tg_id, team_id=None, opponent_id=None, force=False,
               tour_id=None):
    """Resolve a run into the nudges it would send, or an error to show instead.

    Kept apart from the command handler so the preview and the Send button both
    work out what to do the same way — and so the Send re-resolves rather than
    trusting a button: a fixture played between the preview and the tap must
    drop out, not get a reminder to play a match that is already done.
    """
    if tour_id is not None:
        # The Send button carries the tournament the preview was built for, and
        # that row is the authority: a tournament deactivated between the two
        # taps must say so rather than quietly redirect the reminders at
        # whichever one is live now.
        tour = session.get(Tournament, int(tour_id))
    else:
        tours = _live_tournaments(session)
        tour = tours[0] if tours else None
    if tour is None:
        return _Plan(None, [], "", error=NO_ACTIVE)

    # Admin-only, and re-checked here rather than only at the command: the Send
    # button arrives as its own update, and an admin who lost their rights
    # between the preview and the tap must not still be able to fire it.
    if not is_admin(tg_id):
        return _Plan(tour, [], "", error=NOT_ALLOWED)

    if team_id is not None:
        team = session.get(TournamentTeam, int(team_id))
        if not team or team.tournament_id != tour.id:
            return _Plan(tour, [], "",
                         error="❌ That team is no longer in the tournament.")
        opponent = (session.get(TournamentTeam, int(opponent_id))
                    if opponent_id is not None else None)
        fixtures = mrs.pending_fixtures(
            session, tour.id, team_id=team.id,
            opponent_id=opponent.id if opponent else None)
        scope = (f"{team.name} vs {opponent.name}" if opponent
                 else f"{team.name}")
    else:
        fixtures = mrs.pending_fixtures(session, tour.id)
        scope = "every unplayed fixture"

    nudges = mrs.build_nudges(session, tour, fixtures,
                              command=_tournament_command(session, tour),
                              force=force)
    return _Plan(tour, nudges, scope, force=force)


# ══════════════════════════════════════════════════════════════════════
# The preview
# ══════════════════════════════════════════════════════════════════════

def render_preview(session, plan):
    """What the run would do, before it does it."""
    out = [f"📣 <b>Match reminder</b> — {escape(plan.tour.name or 'Tournament')}"]
    if plan.scope:
        out.append(f"<i>{escape(plan.scope)}</i>")
    out.append("")

    if not plan.sending:
        if plan.quiet:
            out += ["😴 Every pending fixture was reminded about recently.",
                    "", "<b>Quiet until</b>"]
            out += [f"• {escape(n.title)} — "
                    f"{n.skipped_until:%d %b %H:%M} UTC" for n in plan.quiet[:10]]
            out += ["", "<i>Add <code>force</code> to send anyway.</i>"]
        else:
            out.append("✅ Nothing is waiting to be played — "
                       "every fixture is done or under way.")
        return "\n".join(out)

    people = plan.people
    fixtures = sum(len(n.fixtures) for n in plan.sending)
    out.append(f"<b>{len(plan.sending)}</b> pending "
               f"{'pair' if len(plan.sending) == 1 else 'pairs'} · "
               f"<b>{fixtures}</b> unplayed "
               f"{'match' if fixtures == 1 else 'matches'} · "
               f"<b>{len(people)}</b> "
               f"{'person' if len(people) == 1 else 'people'} to notify")
    out.append("")
    for nudge in plan.sending:
        left = f" ×{len(nudge.fixtures)}" if len(nudge.fixtures) > 1 else ""
        out.append(f"• <b>{escape(nudge.title)}</b>{left}")
        if nudge.recipients:
            out.append("   " + "  ".join(m for _tg, m in nudge.recipients))
        else:
            out.append("   <i>nobody is assigned to these teams — "
                       "the group post will still go up</i>")
    if plan.quiet:
        out += ["", f"😴 <b>{len(plan.quiet)}</b> more held back "
                    "(reminded in the last 12 hours)."]
    total = len(plan.nudges)
    if total > MAX_PAIRS:
        out.append(f"<i>Only the first {MAX_PAIRS} pairs are chased in one run.</i>")
    out += ["", "<i>Tap Send to post this in the chat and DM everyone named "
                "above.</i>"]
    if not plan.force:
        out.append("<i>Add <code>force</code> to override the 12-hour "
                   "cooldown.</i>")
    return "\n".join(out)


def _keyboard(opener, team_id, opponent_id, force, tour_id):
    """Send / Cancel, bound to the admin who opened the preview.

    Everything the Send needs is in the button — the plan is re-resolved from
    the database on the tap, never restored from a card that may be hours old.
    """
    payload = (f"{opener}_{tour_id}_{team_id or 0}_{opponent_id or 0}_"
               f"{1 if force else 0}")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 Send reminders",
                             callback_data=f"{CB_PREFIX}go_{payload}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{CB_PREFIX}no_{opener}"),
    ]])


# ══════════════════════════════════════════════════════════════════════
# /remindmatch
# ══════════════════════════════════════════════════════════════════════

async def remindmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remindmatch [team] [vs team] [force] — preview a nudge, then send it."""
    message = update.effective_message
    if message is None:
        return
    tg_id = update.effective_user.id if update.effective_user else None
    # Refused before anything is looked up. A non-admin must not be able to use
    # the error messages to probe the field either — "no team called X" is a
    # reply, and replies are what a refused command must not hand out.
    if not is_admin(tg_id):
        await message.reply_text(NOT_ALLOWED)
        return
    session = get_session()
    try:
        tours = _live_tournaments(session)
        if not tours:
            await message.reply_text(NO_ACTIVE)
            return

        first, second, force = _split_args(context.args)
        team_id = opponent_id = None
        tour = tours[0]
        if first:
            tour, team, candidates = _resolve_team(session, tours, first)
            if team is None:
                await message.reply_text(_not_found(first, candidates),
                                         parse_mode="HTML")
                return
            team_id = team.id
            if second:
                _t2, opponent, cands2 = _resolve_team(session, [tour], second)
                if opponent is None:
                    await message.reply_text(_not_found(second, cands2),
                                             parse_mode="HTML")
                    return
                if opponent.id == team.id:
                    await message.reply_text(
                        "❌ A team can't be reminded to play itself.")
                    return
                opponent_id = opponent.id

        plan = build_plan(session, tg_id, team_id=team_id,
                          opponent_id=opponent_id, force=force, tour_id=tour.id)
        if plan.error:
            await message.reply_text(plan.error, parse_mode="HTML")
            return
        markup = (_keyboard(tg_id, team_id, opponent_id, plan.force, plan.tour.id)
                  if plan.sending else None)
        await message.reply_text(render_preview(session, plan), parse_mode="HTML",
                                 disable_web_page_preview=True,
                                 reply_markup=markup)
    except Exception:
        logger.exception("/remindmatch failed")
        await message.reply_text("⚠️ Could not build the reminder right now.")
    finally:
        session.close()


def _not_found(query, candidates):
    """The 'which team did you mean?' reply."""
    if candidates:
        names = " · ".join(escape(tt.name or "—") for tt in candidates[:8])
        return (f"🤔 <b>{escape(query)}</b> could be several teams: {names}\n"
                "Type the full name.")
    return (f"❌ No team called <b>{escape(query)}</b> is in the tournament.\n"
            "Check the field with /ctteams.")


# ══════════════════════════════════════════════════════════════════════
# Sending
# ══════════════════════════════════════════════════════════════════════

async def _send(bot, chat_id, text):
    """One message out, never raising. Returns True when it landed."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.info("Reminder to %s failed: %s", chat_id, exc)
        return False


def _dm_body(session, tour, nudges, command, tg_id):
    """One person's whole DM: their stalled fixtures, theirs named as theirs."""
    cards = [mrs.render_reminder(session, tour, n.fixtures, n.team_a, n.team_b,
                                 command=command, for_tg_id=tg_id)
             for n in nudges[:MAX_CARDS_PER_DM]]
    body = "\n\n➖➖➖\n\n".join(cards)
    extra = nudges[MAX_CARDS_PER_DM:]
    if extra:
        names = ", ".join(escape(n.title) for n in extra)
        body += (f"\n\n<i>…and {len(extra)} more: {names}. "
                 "See them all with /clsd &lt;team name&gt;.</i>")
    return body


async def remindmatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``mrem_go_…`` / ``mrem_no_…`` — send the previewed reminders, or drop them."""
    q = update.callback_query
    data = (q.data or "")[len(CB_PREFIX):]

    if data.startswith("no_"):
        try:
            opener = int(data[3:])
        except ValueError:
            await q.answer("Invalid selection.", show_alert=True)
            return
        if not is_admin(q.from_user.id):
            await q.answer(NOT_ALLOWED, show_alert=True)
            return
        if q.from_user.id != opener:
            await q.answer("Only the admin who opened this can use these buttons.",
                           show_alert=True)
            return
        await q.answer("Cancelled.")
        try:
            await q.edit_message_text("❌ Reminder cancelled — nothing was sent.")
        except Exception:
            logger.debug("Reminder cancel edit skipped", exc_info=True)
        return

    if not data.startswith("go_"):
        await q.answer("Invalid selection.", show_alert=True)
        return
    try:
        opener, tour_id, team_id, opponent_id, force = (
            int(part) for part in data[3:].split("_"))
    except (TypeError, ValueError):
        await q.answer("Invalid selection.", show_alert=True)
        return
    if not is_admin(q.from_user.id):
        await q.answer(NOT_ALLOWED, show_alert=True)
        return
    if q.from_user.id != opener:
        await q.answer("Only the admin who opened this can use these buttons.",
                       show_alert=True)
        return

    await q.answer("Sending…")
    session = get_session()
    try:
        # Re-resolved, not restored: a fixture played since the preview must not
        # get a reminder to go and play it.
        plan = build_plan(session, opener, team_id=team_id or None,
                          opponent_id=opponent_id or None, force=bool(force),
                          tour_id=tour_id)
        if plan.error or not plan.sending:
            await q.edit_message_text(
                plan.error or "✅ Nothing left to remind — those matches have "
                              "been played.")
            return

        tour = plan.tour
        command = _tournament_command(session, tour)
        nudges = plan.sending
        chat = update.effective_chat
        chat_id = chat.id if chat else None

        # ── The group post ──
        # One pair reads best as its own card; several read best as a roundup,
        # because eight cards in a row is a flood people scroll past.
        group_ok = False
        if chat_id is not None:
            if len(nudges) == 1:
                body = nudges[0].text
            else:
                body = mrs.render_roundup(session, tour, nudges, command=command)
            group_ok = await _send(context.bot, chat_id, body)

        # ── The DMs ──
        # By person, not by fixture: someone who runs two stalled teams gets one
        # message carrying both, and never two notifications for one nudge.
        by_person = {}
        for nudge in nudges:
            for tg_id, _mention in nudge.recipients:
                by_person.setdefault(tg_id, []).append(nudge)

        delivered = failed = 0
        reached = set()
        for tg_id, theirs in by_person.items():
            ok = await _send(context.bot, tg_id,
                             _dm_body(session, tour, theirs, command, tg_id))
            if ok:
                delivered += 1
                reached.add(tg_id)
            else:
                failed += 1
            await asyncio.sleep(SEND_GAP_SECONDS)

        # ── Remember it, so the next nudge has to wait ──
        for nudge in nudges:
            got = sum(1 for tg_id, _m in nudge.recipients if tg_id in reached)
            mrs.record_send(session, tour, nudge, sent_by_tg_id=opener,
                            chat_id=chat_id, delivered=got,
                            failed=len(nudge.recipients) - got)
        session.commit()

        await q.edit_message_text(
            _report(plan, delivered, failed, group_ok, chat_id),
            parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        session.rollback()
        logger.exception("/remindmatch send failed")
        try:
            await q.edit_message_text("⚠️ The reminder run failed partway. "
                                      "Nothing further was sent.")
        except Exception:
            logger.debug("Reminder failure edit skipped", exc_info=True)
    finally:
        session.close()


def _report(plan, delivered, failed, group_ok, chat_id):
    """The delivery report the preview turns into."""
    fixtures = sum(len(n.fixtures) for n in plan.sending)
    out = [f"📣 <b>Reminders sent</b> — {escape(plan.tour.name or 'Tournament')}",
           "",
           f"✅ <b>{len(plan.sending)}</b> "
           f"{'pair' if len(plan.sending) == 1 else 'pairs'} chased · "
           f"<b>{fixtures}</b> unplayed "
           f"{'match' if fixtures == 1 else 'matches'}",
           f"📬 <b>{delivered}</b> DM{'' if delivered == 1 else 's'} delivered"]
    if failed:
        out.append(f"🔕 <b>{failed}</b> could not be reached — they have never "
                   "started a chat with the bot, or have blocked it.")
    if chat_id is not None and not group_ok:
        out.append("⚠️ The group post could not be sent.")
    out += ["", "<i>These fixtures now go quiet for 12 hours. "
                "Add <code>force</code> to override.</i>"]
    return "\n".join(out)
