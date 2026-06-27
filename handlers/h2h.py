"""Handler for /h2h — head-to-head record between two players.

Shows the win/loss series between the requester and another user across every
completed PvP match stored in the unified ``matches`` table (covers /playmatch,
/wpm, /cm, /cipl and /challenge).

Display names are rendered WITHOUT an @-mention or tg:// link on purpose, so
running /h2h never pings the players it talks about.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import or_, and_

from database import get_session
from models import User, Match
from services.telegram_user_service import resolve_command_target

logger = logging.getLogger(__name__)


def _plain_name(usr):
    """A non-pinging display name: first name, else team name, else 'Player'.

    Deliberately avoids @username / tg:// mentions so /h2h does not notify
    the users it references.
    """
    if not usr:
        return "Player"
    name = (usr.first_name or "").strip() or (usr.team_name or "").strip()
    return name or "Player"


def tally_h2h(matches, me_id, target_id):
    """Count the series from ``me``'s perspective.

    Returns ``(me_wins, opp_wins, ties)``. A row counts as a tie when it is
    explicitly a tie or has no recorded winner/loser. Pure function so it is
    unit-testable without a DB or Telegram.
    """
    me_wins = opp_wins = ties = 0
    for m in matches:
        if m.margin_type == "tie" or (m.winner_id is None and m.loser_id is None):
            ties += 1
        elif m.winner_id == me_id:
            me_wins += 1
        elif m.winner_id == target_id:
            opp_wins += 1
        else:
            ties += 1
    return me_wins, opp_wins, ties


def _margin_text(m):
    if m.margin_type == "tie":
        return "Tied"
    if m.margin_type == "forfeit":
        return "Forfeit"
    if m.margin_type == "super_over":
        return "Super Over"
    if m.margin_type and m.margin_value is not None:
        return f"by {m.margin_value} {m.margin_type}"
    return ""


async def h2h_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the head-to-head series between the caller and a mentioned user."""
    tg = update.effective_user
    session = get_session()
    try:
        me = session.query(User).filter(User.telegram_id == tg.id).first()
        if not me:
            await update.message.reply_text(
                "❌ You haven't played yet. Use /debut to start, then /playmatch.")
            return

        target, reason = resolve_command_target(session, update, context, "h2h")
        if not target:
            await update.message.reply_text(
                "🏏 <b>Head-to-Head</b>\n\n"
                "Tell me who to compare against:\n"
                "• <code>/h2h @username</code>\n"
                "• or reply to their message with <code>/h2h</code>",
                parse_mode="HTML")
            return

        if target.id == me.id:
            await update.message.reply_text(
                "🤔 You can't play a head-to-head against yourself.")
            return

        matches = (session.query(Match)
                   .filter(Match.status == "completed",
                           or_(and_(Match.user1_id == me.id, Match.user2_id == target.id),
                               and_(Match.user1_id == target.id, Match.user2_id == me.id)))
                   .order_by(Match.completed_at.desc().nullslast(), Match.id.desc())
                   .all())

        me_name = _plain_name(me)
        opp_name = _plain_name(target)

        if not matches:
            await update.message.reply_text(
                f"🏏 <b>HEAD-TO-HEAD</b>\n"
                f"<b>{me_name}</b> vs <b>{opp_name}</b>\n"
                "━━━━━━━━━━━━━━━━━━━\n\n"
                "No completed matches between you two yet.\n"
                "Settle it with <code>/playmatch</code> or <code>/cipl</code>.",
                parse_mode="HTML")
            return

        me_wins, opp_wins, ties = tally_h2h(matches, me.id, target.id)

        lines = [
            "🏏 <b>HEAD-TO-HEAD</b>",
            f"<b>{me_name}</b> vs <b>{opp_name}</b>",
            "━━━━━━━━━━━━━━━━━━━",
            f"Played <b>{len(matches)}</b>",
            f"🏆 {me_name} <b>{me_wins}</b>  —  <b>{opp_wins}</b> {opp_name}",
        ]
        if ties:
            lines.append(f"🤝 Ties: <b>{ties}</b>")

        if me_wins > opp_wins:
            lines.append(f"\n🔥 <b>{me_name}</b> leads the series.")
        elif opp_wins > me_wins:
            lines.append(f"\n🔥 <b>{opp_name}</b> leads the series.")
        else:
            lines.append("\n🤝 The series is level.")

        lines.append("")
        lines.append("<b>Recent results</b> (your side):")
        for m in matches[:5]:
            if m.margin_type == "tie":
                icon = "🤝"
            elif m.winner_id == me.id:
                icon = "🏆"
            elif m.winner_id == target.id:
                icon = "❌"
            else:
                icon = "🤝"
            margin = _margin_text(m)
            score = ""
            if m.inn1_runs is not None and m.inn2_runs is not None:
                score = (f" · {m.inn1_runs}/{m.inn1_wickets or 0}"
                         f" vs {m.inn2_runs}/{m.inn2_wickets or 0}")
            when = m.completed_at.strftime("%d %b") if m.completed_at else ""
            tail = f" — {margin}" if margin else ""
            when_txt = f" <i>({when})</i>" if when else ""
            lines.append(f"{icon}{tail}{score}{when_txt}")

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("h2h err")
        await update.message.reply_text("❌ Couldn't load the head-to-head record.")
    finally:
        session.close()
