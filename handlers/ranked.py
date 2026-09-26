"""/rank, /ranked and /rivalry — the ranked ladder and rivalries.

Display names are rendered without @-mentions or tg:// links, so looking at a
ladder or a rivalry never pings the people on it (same rule as /h2h).
"""

import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services import ranked_service, rivalry_service
from services.telegram_user_service import resolve_command_target

logger = logging.getLogger(__name__)


def _plain(user):
    if user is None:
        return "Player"
    return html.escape((user.first_name or "").strip() or (user.team_name or "").strip()
                       or (user.username or "Player"))


def _me(session, tg):
    return session.query(User).filter(User.telegram_id == tg.id).first()


def render_rank_card(user, row, rank):
    """The /rank card for ``user`` (``row`` may be None: never played ranked)."""
    lines = [f"📈 <b>RANKED — {_plain(user)}</b>", "━━━━━━━━━━━━━━━"]
    if row is None or not row.played:
        rating = row.rating if row is not None else ranked_service.START_RATING
        div, emoji = ranked_service.division_for(rating)
        lines += [f"{emoji} <b>{div}</b> · {rating} rating",
                  "No ranked matches this season yet.",
                  "",
                  "<i>Every completed match against another player counts — "
                  "/letsplay, /cipl, /playmatch, /wpm…</i>"]
        return "\n".join(lines)
    div, emoji = ranked_service.division_for(row.rating)
    lines.append(f"{emoji} <b>{div}</b> · <b>{row.rating}</b> rating"
                 + (f" · #{rank} this season" if rank else ""))
    lines.append(f"📊 {row.played} played · {row.wins}W {row.losses}L"
                 + (f" {row.draws}T" if row.draws else ""))
    lines.append(f"⛰️ Season peak {row.peak_rating} · career best {row.career_peak}")
    nxt = ranked_service.next_division(row.rating)
    if nxt:
        lines.append(f"⬆️ {nxt[2]} points to {nxt[1]} {nxt[0]}")
    if row.played < ranked_service.PLACEMENT_GAMES:
        left = ranked_service.PLACEMENT_GAMES - row.played
        lines.append(f"🧭 Placement: {left} more match{'es' if left != 1 else ''} "
                     f"at double rating swings")
    coins, gems = ranked_service.division_reward(row.rating)
    if coins or gems:
        need = max(0, ranked_service.MIN_GAMES_FOR_REWARD - row.played)
        tail = f" (play {need} more to qualify)" if need else ""
        lines.append(f"🎁 Season-end reward at this division: {coins:,} coins"
                     + (f" + {gems} 💎" if gems else "") + tail)
    return "\n".join(lines)


async def rank_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rank [@user] — a ranked card (yours, or someone else's)."""
    session = get_session()
    try:
        me = _me(session, update.effective_user)
        target = None
        if context.args or (update.message and update.message.reply_to_message):
            target, _reason = resolve_command_target(session, update, context, "rank")
        user = target or me
        if user is None:
            await update.message.reply_text("❌ Use /debut to start first.")
            return
        row = ranked_service.get_rating(session, user.id, create=False)
        rank = ranked_service.rank_of(session, row)
        session.commit()   # a lazy season roll-over may have touched the row
        await update.message.reply_text(render_rank_card(user, row, rank),
                                        parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/rank failed")
        await update.message.reply_text("❌ Couldn't load the ranked card.")
    finally:
        session.close()


def render_ladder(rows, season_key, me_line=None):
    lines = [f"🏆 <b>RANKED LADDER</b> · {html.escape(season_key)}",
             "━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("Nobody has played a ranked match this season yet.")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, r, u in rows:
        _div, emoji = ranked_service.division_for(r.rating)
        lines.append(f"{medals.get(rank, f'{rank}.')} {_plain(u)} — <b>{r.rating}</b> "
                     f"{emoji} · {r.wins}W {r.losses}L")
    if me_line:
        lines += ["", me_line]
    lines += ["", "<i>Divisions: 🥉 Bronze · 🥈 Silver 1050 · 🥇 Gold 1150 · "
                  "🔷 Platinum 1250 · 💎 Diamond 1350 · 👑 Legend 1450</i>",
              "<i>Rewards are paid by division when the month ends.</i>"]
    return "\n".join(lines)


async def ranked_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ranked — the top of this season's ladder."""
    session = get_session()
    try:
        rows = ranked_service.leaderboard(session, limit=15)
        from services.season_service import current_season_key
        me = _me(session, update.effective_user)
        me_line = None
        if me is not None:
            row = ranked_service.get_rating(session, me.id, create=False)
            rank = ranked_service.rank_of(session, row)
            if rank and rank > len(rows):
                me_line = f"📍 You: #{rank} · {row.rating}"
        session.commit()
        await update.message.reply_text(
            render_ladder(rows, current_season_key(), me_line), parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/ranked failed")
        await update.message.reply_text("❌ Couldn't load the ladder.")
    finally:
        session.close()


def render_rivalry(row, me, opp):
    """One rivalry, from ``me``'s side."""
    me_is_a = row.user_a_id == me.id
    my_w, opp_w = (row.a_wins, row.b_wins) if me_is_a else (row.b_wins, row.a_wins)
    my_r, opp_r = (row.rounds_a, row.rounds_b) if me_is_a else (row.rounds_b, row.rounds_a)
    rnd_me, rnd_opp = ((row.round_a_wins, row.round_b_wins) if me_is_a
                       else (row.round_b_wins, row.round_a_wins))
    title = html.escape(row.name or "Rivalry")
    lines = [f"⚔️ <b>{title}</b>", "━━━━━━━━━━━━━━━",
             f"{_plain(me)} <b>{my_w}</b> – <b>{opp_w}</b> {_plain(opp)}"
             + (f"  ·  {row.ties} tied" if row.ties else ""),
             f"🎮 {row.played} meetings"]
    if rivalry_service.is_rivalry(row):
        lines.append(f"🏅 Rounds won: {my_r} – {opp_r}")
        lines.append(f"🔁 Round {row.round_no}: {rnd_me} – {rnd_opp} after "
                     f"{row.round_played}/{rivalry_service.ROUND_LENGTH}")
        lines.append(f"<i>Each rivalry win +{rivalry_service.WIN_BONUS_COINS} coins · "
                     f"each round won +{rivalry_service.ROUND_BONUS_COINS:,} coins "
                     f"+{rivalry_service.ROUND_BONUS_GEMS} 💎</i>")
    else:
        left = rivalry_service.RIVALRY_THRESHOLD - row.played
        lines.append(f"<i>{left} more meeting{'s' if left != 1 else ''} to make it a rivalry.</i>")
    if row.streak and row.streak >= 2 and row.streak_user_id:
        holder = me if row.streak_user_id == me.id else opp
        lines.append(f"🔥 {_plain(holder)} have won the last {row.streak}")
    return "\n".join(lines)


async def rivalry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rivalry [@user] — one rivalry, or your list of rivalries."""
    session = get_session()
    try:
        me = _me(session, update.effective_user)
        if me is None:
            await update.message.reply_text("❌ Use /debut to start first.")
            return
        target = None
        if context.args or (update.message and update.message.reply_to_message):
            target, _reason = resolve_command_target(session, update, context, "rivalry")
        if target is not None:
            if target.id == me.id:
                await update.message.reply_text("🤔 You can't be your own rival.")
                return
            row = rivalry_service.get_rivalry(session, me.id, target.id)
            if row is None:
                # First look at this pair — build it from the match history.
                row = rivalry_service.get_or_create(session, me.id, target.id)
                session.commit()
            if not row.played:
                await update.message.reply_text(
                    f"⚔️ You and {_plain(target)} haven't finished a match yet.")
                return
            await update.message.reply_text(render_rivalry(row, me, target),
                                            parse_mode="HTML")
            return

        pairs = rivalry_service.rivalries_for(session, me.id, limit=10)
        if not pairs:
            await update.message.reply_text(
                "⚔️ <b>Rivalries</b>\n\nNo rivalries yet. Play the same opponent "
                f"{rivalry_service.RIVALRY_THRESHOLD} times and it becomes one.\n"
                "See one pair with <code>/rivalry @username</code>.",
                parse_mode="HTML")
            return
        lines = [f"⚔️ <b>{_plain(me)}'s rivalries</b>", "━━━━━━━━━━━━━━━"]
        for row, opp in pairs:
            me_is_a = row.user_a_id == me.id
            mw, ow = (row.a_wins, row.b_wins) if me_is_a else (row.b_wins, row.a_wins)
            lead = "🔥" if mw > ow else ("❄️" if ow > mw else "🤝")
            lines.append(f"{lead} vs {_plain(opp)} — <b>{mw}–{ow}</b> "
                         f"({row.played} played)")
        lines.append("\n<i>Details: /rivalry @username</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("/rivalry failed")
        await update.message.reply_text("❌ Couldn't load rivalries.")
    finally:
        session.close()
