"""Handler for /playmatch — full match with endmatch, timeouts, rewards."""

import asyncio, io, random, logging
from datetime import datetime, timedelta
from sqlalchemy import or_
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, Match, PlayerGameStats
from services.match_constants import random_match_settings, MATCH_EXPIRE
from services.bowling_service import get_delivery_options, is_spinner, AVAILABLE_SHOTS
from services.match_engine import (
    create_match_state, get_striker, get_non_striker, get_bowler,
    is_innings_over, format_score, format_overs, crr, rrr, get_phase,
    add_to_timeline, build_live_scorecard, SYM,
)
from services.flags import get_flag
from services.activity_service import log_activity
from services.batsman_card import generate_batsman_card
from services.bowler_card import generate_bowler_card
from services.scorecard_card import generate_batting_scorecard, generate_bowling_scorecard
from handlers.lineup import format_xi_text

logger = logging.getLogger(__name__)

# Inactivity timeout — two-stage to avoid surprise forfeits.
#   ACTION_WARN_SECONDS: send a "hurry up" warning to the chat
#   ACTION_TIMEOUT:      force a forfeit, with reduced fine
# Bot players (vsbot AI) are not subject to this — only humans are.
ACTION_WARN_SECONDS = 45
ACTION_TIMEOUT = 90
FINE_COINS = 2000   # reduced from 10000 — the forfeit itself is the bigger penalty
FINE_GEMS = 5       # reduced from 20

# Setup-phase expiries so a half-started match never blocks a chat forever.
#   LOBBY_EXPIRE: an unjoined /wpm lobby auto-cancels after this long
#   OVERS_EXPIRE: an accepted /playmatch match auto-expires if no overs are chosen
LOBBY_EXPIRE = 120
OVERS_EXPIRE = 60

# Sentinel telegram ID for the AI bot opponent in /vsbot
BOT_TG_ID_ = -1

# State store (persistent — backed by match_state DB table)
from services.match_state_store import (
    get_state as _store_get,
    save_state as _store_save,
    set_next_action,
    get_next_action,
    cleanup_state,
    increment_ball_seq,
    get_ball_seq,
    get_last_prompt_msg_id,
    get_match_lock,
    release_match_lock,
    A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
    A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER,
    A_INNINGS_BREAK, A_COMPLETED,
)


def _gs(ctx, mid):
    """Get state — checks memory cache first, falls back to DB."""
    return _store_get(ctx, mid)


def _ss(ctx, mid, s, next_action=None, last_prompt_msg_id=None):
    """Save state — writes to memory + DB.
    Optionally update next_action pointer and/or last_prompt_msg_id.
    """
    _store_save(ctx, mid, s, next_action=next_action,
                last_prompt_msg_id=last_prompt_msg_id)

def _pd(e, p):
    return {"roster_id": e.id, "player_id": p.id, "name": p.name, "rating": p.rating,
            "category": p.category, "bat_rating": p.bat_rating, "bowl_rating": p.bowl_rating,
            "bowl_style": p.bowl_style, "bowl_hand": p.bowl_hand, "bat_hand": p.bat_hand}

def _gxi(session, uid):
    rows = (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == uid).order_by(UserRoster.order_position).limit(11).all())
    return [_pd(e, p) for e, p in rows]

def _bowl_label(p, s):
    bws = s["bowl_stats"].get(p["roster_id"], {})
    od = bws.get("overs_done", 0); tb = bws.get("this_over_balls", 0)
    ov_str = f"{od}.{tb}" if tb else str(od)
    h = p.get("bowl_hand", "R")[:1]
    return f"{p['name']} | {h}-{p['bowl_style']} | {ov_str}•{bws.get('runs',0)}•{bws.get('wickets',0)}"


ACTIVE_MATCH_STATUSES = (
    "pending", "accepted", "toss", "selecting", "playing", "active",
)


def _expire_stale_pending_matches(session):
    """Expire invitations whose timer elapsed while no expiry job was running.

    Scheduled expiry jobs are best-effort: they can be missed when the bot is
    restarted or temporarily unavailable.  Lazily cleaning stale invitations
    before active-match lookups prevents those old rows from blocking new
    matches indefinitely.
    """
    expired = (session.query(Match)
               .filter(Match.status == "pending",
                       Match.expires_at.isnot(None),
                       Match.expires_at < datetime.utcnow())
               .update({Match.status: "expired"}, synchronize_session=False))
    if expired:
        session.commit()


def _active_match_in_chat(session, chat_id):
    """Return the newest unfinished match in a chat, if one exists."""
    if not chat_id:
        return None
    _expire_stale_pending_matches(session)
    return (session.query(Match)
            .filter(Match.chat_id == chat_id,
                    Match.status.in_(ACTIVE_MATCH_STATUSES))
            .order_by(Match.id.desc())
            .first())


def _active_match_for_user(session, user_id):
    """Return an unfinished match involving ``user_id``, if one exists."""
    _expire_stale_pending_matches(session)
    return (session.query(Match)
            .filter(or_(Match.user1_id == user_id, Match.user2_id == user_id),
                    Match.status.in_(ACTIVE_MATCH_STATUSES))
            .order_by(Match.id.desc())
            .first())


def _cric_lobby_key(chat_id):
    return f"cric_lobby_{chat_id}"


def _active_cric_match_in_chat(session, chat_id):
    """Return a launched /wpm-style Mini App match in ``chat_id``.

    UnderCover does not treat an abandoned pre-match lobby as an active match:
    its match manager only receives a match after the toss decision.  Keep the
    same boundary here so old ``pending``/``toss`` rows from callback matches
    cannot make a fresh ``/wpm`` lobby look busy.
    """
    if not chat_id:
        return None
    return (session.query(Match)
            .filter(Match.chat_id == chat_id,
                    Match.status.in_(("playing", "active")))
            .order_by(Match.id.desc())
            .first())


def _active_cric_match_for_user(session, user_id):
    """Return a launched /wpm-style Mini App match involving ``user_id``."""
    return (session.query(Match)
            .filter(or_(Match.user1_id == user_id, Match.user2_id == user_id),
                    Match.status.in_(("playing", "active")))
            .order_by(Match.id.desc())
            .first())


def _cric_lobby_for_user(bot_data, user_id):
    """Find an in-memory /wpm lobby containing ``user_id``."""
    return next((lobby for key, lobby in bot_data.items()
                 if key.startswith("cric_lobby_")
                 and user_id in (lobby.get("host_user_id"),
                                 lobby.get("guest_user_id"))), None)


def _user_label(user):
    return f"@{user.username}" if user.username else (user.first_name or "Player")


def _chat_busy_message(match):
    """Friendly 'a match is already running here' message with what-you-can-do
    suggestions, plus a peek at who the current match is between."""
    p1 = p2 = None
    try:
        ses = get_session()
        try:
            u1 = ses.query(User).get(match.user1_id) if match.user1_id else None
            u2 = ses.query(User).get(match.user2_id) if match.user2_id else None
            p1 = (("@" + u1.username) if (u1 and u1.username)
                  else (u1.first_name if u1 else "Player 1"))
            p2 = (("@" + u2.username) if (u2 and u2.username)
                  else (u2.first_name if u2 else "Player 2"))
            if u2 and u2.telegram_id == BOT_TG_ID_:
                p2 = "🤖 Bot"
            if u1 and u1.telegram_id == BOT_TG_ID_:
                p1 = "🤖 Bot"
        finally:
            ses.close()
    except Exception:
        pass

    who = ""
    if p1 and p2:
        who = f"\n🏏 <b>{p1}</b> vs <b>{p2}</b>"

    return (
        f"🚫 <b>A match is already going on here.</b>{who}\n"
        f"<i>Match #{match.id} — status: {match.status}</i>\n\n"
        f"<b>What you can do:</b>\n"
        f"  •  Wait for the current match to finish\n"
        f"  •  Start your match in a different chat or DM the bot\n"
        f"  •  If the match is stuck, the current players can use "
        f"/endmatch (fine applies) or /resume\n"
        f"  •  Spectate with /matchinfo to see the live score"
    )


def _mention(user_or_tg_id, fallback_name=None):
    """Build an HTML mention. Works whether or not the user has a Telegram
    @username (clickable mention via tg://user?id=...).

    Accepts either a User row or a raw int telegram_id (with fallback_name).
    For the bot opponent (BOT_TG_ID_), returns "🤖 Bot" instead of a mention.
    """
    if user_or_tg_id is None:
        return fallback_name or "Player"
    if isinstance(user_or_tg_id, int):
        if user_or_tg_id == BOT_TG_ID_:
            return "🤖 Bot"
        name = fallback_name or "Player"
        return f'<a href="tg://user?id={user_or_tg_id}">{name}</a>'
    u = user_or_tg_id
    if getattr(u, "telegram_id", None) == BOT_TG_ID_:
        return "🤖 Bot"
    name = u.username or u.first_name or fallback_name or "Player"
    label = f"@{u.username}" if u.username else name
    return f'<a href="tg://user?id={u.telegram_id}">{label}</a>'


def _mention_by_tg_id(session, tg_id, fallback="Player"):
    """Convenience: look up the user row by telegram_id and return _mention()."""
    if tg_id == BOT_TG_ID_:
        return "🤖 Bot"
    if not tg_id:
        return fallback
    try:
        u = session.query(User).filter(User.telegram_id == tg_id).first()
        if u:
            return _mention(u)
    except Exception:
        pass
    return _mention(tg_id, fallback_name=fallback)


def _format_dismissal(how, bowler_name, bowl_xi):
    """Build a cricket-style dismissal string given the dismissal `how`
    keyword from probability_engine and the bowler's name.

    Examples:
        Bowled                  → "b Bumrah"
        LBW                     → "lbw b Bumrah"
        Caught                  → "c Kohli b Bumrah"      (random catcher)
        Caught Behind           → "c †Pant b Bumrah"      (random non-bowler)
        Caught & Bowled         → "c & b Bumrah"
        Stumped                 → "st †Pant b Ashwin"     (random non-bowler)
        Run Out                 → "run out (Kohli)"       (random fielder)

    Catchers / fielders / keepers are picked randomly from `bowl_xi`
    (excluding the bowler unless the dismissal is Caught & Bowled).
    """
    how = (how or "Bowled").strip()
    bowler = (bowler_name or "?").split()[-1]  # last name for compactness

    # Helpers — pick a random fielder name from the bowling XI
    def _pick_fielder(exclude_bowler=True):
        if not bowl_xi:
            return "Fielder"
        pool = [p for p in bowl_xi
                if not (exclude_bowler and p.get("name") == bowler_name)]
        if not pool:
            pool = list(bowl_xi)
        import random as _r
        pick = _r.choice(pool)
        # Use last word of the name (cricket convention)
        return pick.get("name", "Fielder").split()[-1]

    h_lower = how.lower()

    if h_lower == "bowled":
        return f"b {bowler}"
    if h_lower == "lbw":
        return f"lbw b {bowler}"
    if h_lower == "caught & bowled" or h_lower == "caught and bowled":
        return f"c & b {bowler}"
    if h_lower == "caught behind":
        # Keeper-style; use † prefix so it's visually distinct
        keeper = _pick_fielder()
        return f"c †{keeper} b {bowler}"
    if h_lower == "caught":
        catcher = _pick_fielder()
        return f"c {catcher} b {bowler}"
    if h_lower == "stumped":
        keeper = _pick_fielder()
        return f"st †{keeper} b {bowler}"
    if h_lower == "run out":
        fielder = _pick_fielder(exclude_bowler=False)
        return f"run out ({fielder})"
    if h_lower == "hit wicket":
        return f"hit wkt b {bowler}"

    # Fallback — unknown dismissal type
    return f"{how.lower()} b {bowler}"


    bws = s["bowl_stats"].get(p["roster_id"], {})
    od = bws.get("overs_done", 0); tb = bws.get("this_over_balls", 0)
    ov_str = f"{od}.{tb}" if tb else str(od)
    h = p.get("bowl_hand", "R")[:1]
    return f"{p['name']} | {h}-{p['bowl_style']} | {ov_str}•{bws.get('runs',0)}•{bws.get('wickets',0)}"


async def _send_batsman_card(ctx, chat_id, player_dict, owner_user_id):
    """Look up PlayerGameStats and send batsman card image."""
    try:
        session = get_session()
        try:
            gs = (session.query(PlayerGameStats)
                  .filter(PlayerGameStats.user_id == owner_user_id,
                          PlayerGameStats.player_id == player_dict["player_id"])
                  .first())
            if gs:
                stats = {
                    "bat_inns": gs.bat_inns, "runs": gs.runs,
                    "fifties": gs.fifties, "hundreds": gs.hundreds,
                    "fours": gs.fours, "sixes": gs.sixes,
                    "bat_avg": gs.bat_avg, "bat_sr": gs.bat_sr,
                    "ducks": gs.ducks, "hs_str": gs.hs_str,
                }
            else:
                stats = {"bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
                         "fours": 0, "sixes": 0, "bat_avg": 0, "bat_sr": 0,
                         "ducks": 0, "hs_str": "-"}
        except Exception:
            stats = {"bat_inns": 0, "runs": 0, "fifties": 0, "hundreds": 0,
                     "fours": 0, "sixes": 0, "bat_avg": 0, "bat_sr": 0,
                     "ducks": 0, "hs_str": "-"}
        finally:
            session.close()

        # In-match arrivals always use the CMU stats-card renderer; regular
        # player-card custom images still apply to /claim, /buypl, /playerinfo.
        card_bytes = generate_batsman_card(
            player_dict["name"],
            player_dict["rating"],
            player_dict["bat_rating"],
            stats,
            bat_hand=player_dict.get("bat_hand", "Right"),
            bowl_hand=player_dict.get("bowl_hand", "Right"),
            bowl_style=player_dict.get("bowl_style", "Medium Pacer"),
        )

        # Compute form for the caption
        form_caption = ""
        try:
            from services.form_service import compute_form_score, form_label
            session2 = get_session()
            try:
                fs = compute_form_score(session2, owner_user_id, player_dict["player_id"])
                if fs >= 6 or fs <= -6:
                    # Only show extreme form to keep it punchy
                    form_caption = f" · {form_label(fs)}"
            finally:
                session2.close()
        except Exception:
            pass

        if card_bytes:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(card_bytes),
                caption=f"🏏 <b>{player_dict['name']}</b> walks to the crease{form_caption}",
                parse_mode="HTML")
    except Exception:
        logger.warning(f"Failed to send batsman card for {player_dict.get('name')}")


async def _send_bowler_card(ctx, chat_id, player_dict, owner_user_id):
    """Look up PlayerGameStats and send bowler card image."""
    try:
        session = get_session()
        try:
            gs = (session.query(PlayerGameStats)
                  .filter(PlayerGameStats.user_id == owner_user_id,
                          PlayerGameStats.player_id == player_dict["player_id"])
                  .first())
            if gs:
                # Calculate BBF (best bowling figures)
                if gs.best_bowl_wickets > 0:
                    bbf_str = f"{gs.best_bowl_wickets}/{gs.best_bowl_runs}"
                else:
                    bbf_str = "-"
                stats = {
                    "bowl_inns": gs.bowl_inns,
                    "wickets_taken": gs.wickets_taken,
                    "runs_conceded": gs.runs_conceded,
                    "balls_bowled": gs.balls_bowled,
                    "bowl_avg": gs.bowl_avg,
                    "bowl_sr": gs.bowl_sr,
                    "econ": gs.bowl_economy,
                    "hat_tricks": getattr(gs, "hat_tricks", 0),
                    "five_fers": gs.five_fers,
                    "three_fers": gs.three_fers,
                    "bbf_str": bbf_str,
                }
            else:
                stats = {"bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
                         "balls_bowled": 0, "bowl_avg": 0, "bowl_sr": 0, "econ": 0,
                         "hat_tricks": 0, "five_fers": 0, "three_fers": 0, "bbf_str": "-"}
        except Exception:
            stats = {"bowl_inns": 0, "wickets_taken": 0, "runs_conceded": 0,
                     "balls_bowled": 0, "bowl_avg": 0, "bowl_sr": 0, "econ": 0,
                     "hat_tricks": 0, "five_fers": 0, "three_fers": 0, "bbf_str": "-"}
        finally:
            session.close()

        # In-match arrivals always use the CMU stats-card renderer; regular
        # player-card custom images still apply to /claim, /buypl, /playerinfo.
        card_bytes = generate_bowler_card(
            player_dict["name"],
            player_dict["rating"],
            player_dict["bowl_rating"],
            stats,
            bat_hand=player_dict.get("bat_hand", "Right"),
            bowl_hand=player_dict.get("bowl_hand", "Right"),
            bowl_style=player_dict.get("bowl_style", "Medium Pacer"),
        )

        # Compute form for the caption
        form_caption = ""
        try:
            from services.form_service import compute_form_score, form_label
            session2 = get_session()
            try:
                fs = compute_form_score(session2, owner_user_id, player_dict["player_id"])
                if fs >= 6 or fs <= -6:
                    form_caption = f" · {form_label(fs)}"
            finally:
                session2.close()
        except Exception:
            pass

        if card_bytes:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(card_bytes),
                caption=f"🎳 <b>{player_dict['name']}</b> is bowling{form_caption}",
                parse_mode="HTML")
    except Exception:
        logger.warning(f"Failed to send bowler card for {player_dict.get('name')}")


# ── Timeout helpers ──────────────────────────────────────────────────

def _cancel_action_timer(ctx, mid):
    """Cancel both the warning and the forfeit timer."""
    try:
        for name in (f"act_{mid}", f"actwarn_{mid}"):
            for j in ctx.job_queue.get_jobs_by_name(name):
                j.schedule_removal()
    except Exception: pass

def _start_action_timer(ctx, mid, user_tg_id, action_label):
    """Start inactivity timers. Two stages:
      - At ACTION_WARN_SECONDS (45s): send a warning that they'll forfeit soon
      - At ACTION_TIMEOUT (90s): forfeit the match

    Bot players (vsbot AI) and spectator/bot-vs-bot matches are exempt.
    """
    _cancel_action_timer(ctx, mid)
    if user_tg_id == BOT_TG_ID_:
        return
    try:
        if ctx.job_queue:
            s = _gs(ctx, mid)
            if not s: return
            if s.get("is_spectator") or s.get("is_bot_vs_bot"):
                return
            snapshot = (
                s.get("current_over"), s.get("current_ball"),
                s.get("total_runs"), s.get("total_wickets"),
                s.get("striker_idx"), s.get("current_delivery"),
            )
            data = {"match_id": mid, "chat_id": s["chat_id"],
                    "user_tg": user_tg_id, "action": action_label,
                    "state_snapshot": snapshot}
            ctx.job_queue.run_once(
                _action_warning, ACTION_WARN_SECONDS,
                name=f"actwarn_{mid}", data=data)
            ctx.job_queue.run_once(
                _action_timeout, ACTION_TIMEOUT,
                name=f"act_{mid}", data=data)
    except Exception: pass


async def _action_warning(context):
    """45s mark: poke the idle user. No forfeit yet."""
    d = context.job.data; mid = d["match_id"]
    s = _gs(context, mid)
    if not s: return

    # Same safety checks as the forfeit handler
    if context.bot_data.get(f"processing_{mid}"):
        return
    snapshot = d.get("state_snapshot")
    if snapshot:
        current_signature = (
            s.get("current_over"), s.get("current_ball"),
            s.get("total_runs"), s.get("total_wickets"),
            s.get("striker_idx"), s.get("current_delivery"),
        )
        if current_signature != snapshot:
            return  # User acted, no warning needed

    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == d["user_tg"]).first()
        if not u:
            return
        u_mention = _mention(u)
        remaining = ACTION_TIMEOUT - ACTION_WARN_SECONDS
        try:
            await context.bot.send_message(
                d["chat_id"],
                f"⏱️ <b>{remaining} seconds remaining</b> {u_mention} "
                f"to {d['action']}.\n"
                f"<i>Match will be forfeited otherwise.</i>",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        logger.exception("Action warning failed")
    finally:
        session.close()


async def _action_timeout(context):
    """90s inactivity → forfeit. The idle user loses; the opponent wins.

    Side effects:
      - Match record: status=completed, winner_id=opponent, margin='forfeit'
      - Idle user fined coins+gems
      - Tour hook fired (if match is part of a tour)
      - Match state cleaned up
      - Announcement message sent to chat
    """
    d = context.job.data; mid = d["match_id"]
    s = _gs(context, mid)
    if not s: return

    # SAFETY: Don't forfeit if a click is currently being processed.
    if context.bot_data.get(f"processing_{mid}"):
        return

    # SAFETY: Don't forfeit if the match has already moved on.
    snapshot = d.get("state_snapshot")
    if snapshot:
        current_signature = (
            s.get("current_over"), s.get("current_ball"),
            s.get("total_runs"), s.get("total_wickets"),
            s.get("striker_idx"), s.get("current_delivery"),
        )
        if current_signature != snapshot:
            return

    # Determine winner = the OTHER user (not the idle one)
    idle_tg = d["user_tg"]
    if s.get("bat_user_tg") == idle_tg:
        winner_tg = s.get("bowl_user_tg")
        loser_tg = idle_tg
    elif s.get("bowl_user_tg") == idle_tg:
        winner_tg = s.get("bat_user_tg")
        loser_tg = idle_tg
    else:
        # Idle user isn't part of this match (stale timer?) — abort
        return

    # If the "winner" would be the bot (vsbot AI), don't apply tour/economy
    # effects — just clean up the match as forfeited.
    winner_is_bot = (winner_tg == BOT_TG_ID_)

    session = get_session()
    try:
        idle_user = session.query(User).filter(User.telegram_id == idle_tg).first()
        winner_user = (None if winner_is_bot
                       else session.query(User).filter(User.telegram_id == winner_tg).first())

        if idle_user:
            idle_user.total_coins = max(0, idle_user.total_coins - FINE_COINS)
            idle_user.total_gems = max(0, idle_user.total_gems - FINE_GEMS)
            log_activity(session, idle_user.id, "match_forfeit",
                         f"Auto-forfeit ({d['action']}): -{FINE_COINS} coins, -{FINE_GEMS} gems",
                         coins_change=-FINE_COINS, gems_change=-FINE_GEMS)

        # Finalize Match record
        m = session.query(Match).get(mid)
        tour_announce = None
        if m and m.status != "completed":
            m.status = "completed"
            m.completed_at = datetime.utcnow()
            m.margin_type = "forfeit"
            m.margin_value = 0
            if winner_user:
                m.winner_id = winner_user.id
                m.loser_id = idle_user.id if idle_user else None
            elif idle_user:
                # Edge: vsbot, human forfeited → no human winner to credit
                m.loser_id = idle_user.id
            # Save innings snapshots from state so the scorecard endpoint isn't empty
            if "inn1_runs" in s:
                m.inn1_runs = s.get("inn1_runs"); m.inn1_wickets = s.get("inn1_wickets")
            m.inn2_runs = s.get("total_runs"); m.inn2_wickets = s.get("total_wickets")

            # Update user stats (skip for vsbot — no real-economy effect on bot losses)
            if not s.get("is_vsbot") and not winner_is_bot:
                if winner_user:
                    winner_user.matches_played = (winner_user.matches_played or 0) + 1
                    winner_user.matches_won = (winner_user.matches_won or 0) + 1
                    winner_user.win_streak = (winner_user.win_streak or 0) + 1
                    winner_user.best_streak = max(winner_user.best_streak or 0,
                                                   winner_user.win_streak)
                if idle_user:
                    idle_user.matches_played = (idle_user.matches_played or 0) + 1
                    idle_user.matches_lost = (idle_user.matches_lost or 0) + 1
                    idle_user.win_streak = 0

            # Tour hook — if this match is part of a tour, update it
            if not s.get("is_vsbot") and winner_user:
                try:
                    from services.tour_service import record_match_result
                    tour_obj = record_match_result(session, mid, winner_user.id, forfeit=True)
                    if tour_obj:
                        u1 = session.query(User).get(tour_obj.user1_id)
                        u2 = session.query(User).get(tour_obj.user2_id)
                        tour_announce = {
                            "completed": tour_obj.status == "completed",
                            "winner_id": tour_obj.winner_id,
                            "u1_label": (f"@{u1.username}" if u1 and u1.username
                                          else (u1.first_name if u1 else "U1")),
                            "u2_label": (f"@{u2.username}" if u2 and u2.username
                                          else (u2.first_name if u2 else "U2")),
                            "u1_id": tour_obj.user1_id,
                            "u1w": tour_obj.user1_wins, "u2w": tour_obj.user2_wins,
                            "match_count": tour_obj.match_count,
                        }
                except Exception:
                    logger.exception("Tour-result hook in forfeit failed")

            session.commit()

        # Save accumulated stats before cleanup
        try:
            await _save_match_stats(s)
        except Exception:
            logger.exception("Stats save during forfeit failed (non-fatal)")

        # Announcement — dramatic forfeit message with clickable mentions
        idle_mention = _mention(idle_user) if idle_user else "Player"
        winner_mention = ("🤖 Bot" if winner_is_bot
                          else (_mention(winner_user) if winner_user else "Opponent"))
        try:
            await context.bot.send_message(
                d["chat_id"],
                f"⏰ <b>Time over</b> 😔\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"{idle_mention} left the game.\n"
                f"<i>(did not {d['action']} within {ACTION_TIMEOUT}s)</i>\n\n"
                f"⚠️ Fined: <b>-{FINE_COINS:,}</b> 🪙   <b>-{FINE_GEMS}</b> 💎\n\n"
                f"🏆 {winner_mention} won the match!",
                parse_mode="HTML")
        except Exception:
            pass

        # Send scorecards for whatever innings have data, so the chat has a
        # visual record of the forfeited match. Wrapped — never block cleanup.
        try:
            current_innings = s.get("innings", 1)
            # Innings 1 scorecards: send if innings 1 was completed (or we're
            # mid-innings 1 with at least some balls played).
            inn1_played = (current_innings >= 2
                           or s.get("total_runs", 0) > 0
                           or s.get("total_wickets", 0) > 0
                           or s.get("current_over", 1) > 1
                           or s.get("current_ball", 0) > 0)
            if inn1_played:
                await _send_innings_scorecards(context, mid, innings_num=1)
            # Innings 2 scorecards: only if we actually reached innings 2
            if current_innings >= 2:
                await _send_innings_scorecards(context, mid, innings_num=2)
        except Exception:
            logger.exception("Forfeit scorecard send failed (non-fatal)")

        # Tour announcement after match forfeit
        if tour_announce:
            try:
                if tour_announce["completed"]:
                    if tour_announce["winner_id"] is None:
                        text = (f"🤝 <b>TOUR DRAWN "
                                f"{tour_announce['u1w']}-{tour_announce['u2w']}</b>")
                    else:
                        wlabel = (tour_announce["u1_label"]
                                  if tour_announce["winner_id"] == tour_announce["u1_id"]
                                  else tour_announce["u2_label"])
                        text = (f"🏆 <b>{wlabel} WINS THE TOUR "
                                f"{max(tour_announce['u1w'], tour_announce['u2w'])}-"
                                f"{min(tour_announce['u1w'], tour_announce['u2w'])}!</b>")
                    await context.bot.send_message(d["chat_id"],
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🏆 <b>TOUR COMPLETE</b>\n\n"
                        f"{tour_announce['u1_label']} {tour_announce['u1w']} — "
                        f"{tour_announce['u2w']} {tour_announce['u2_label']}\n\n"
                        f"{text}\n━━━━━━━━━━━━━━━━━━━",
                        parse_mode="HTML")
                else:
                    done = tour_announce["u1w"] + tour_announce["u2w"]
                    remaining = tour_announce["match_count"] - done
                    await context.bot.send_message(d["chat_id"],
                        f"📋 <b>TOUR UPDATE</b>\n"
                        f"{tour_announce['u1_label']} {tour_announce['u1w']} — "
                        f"{tour_announce['u2w']} {tour_announce['u2_label']}\n"
                        f"<i>{remaining} match{'es' if remaining != 1 else ''} left</i>\n"
                        f"Use /mytours to continue.",
                        parse_mode="HTML")
            except Exception:
                logger.exception("Tour announce in forfeit failed")

        # Cleanup persistent state + lock
        cleanup_state(context, mid)
        release_match_lock(mid)
    except Exception:
        session.rollback()
        logger.exception("Forfeit handler err")
    finally:
        session.close()


# ── Reward helper ────────────────────────────────────────────────────

async def _award_match_rewards(ctx, s, winner_tg, loser_tg, overs):
    session = get_session()
    try:
        from services.config_service import get_config
        cfg = get_config(session)
        w = session.query(User).filter(User.telegram_id == winner_tg).first()
        l = session.query(User).filter(User.telegram_id == loser_tg).first()
        w_coins = int(overs * cfg["match_win_coins_per_over"])
        w_gems = max(0, int(overs * cfg["match_win_gems_per_over"]))
        l_coins = int(overs * cfg["match_loss_coins_per_over"])
        l_gems = max(0, int(overs * cfg["match_loss_gems_per_over"]))
        # Active event coin multiplier (e.g. double-coins weekend) — PvP only
        if not s.get("is_vsbot"):
            try:
                from services.event_service import apply_coin_multiplier
                w_coins, _m = apply_coin_multiplier(session, w_coins)
                l_coins, _m = apply_coin_multiplier(session, l_coins)
            except Exception:
                pass
        if w:
            w.total_coins += w_coins; w.total_gems += w_gems
            log_activity(session, w.id, "match_reward", f"Win reward: +{w_coins} coins, +{w_gems} gems",
                         coins_change=w_coins, gems_change=w_gems)
        if l:
            l.total_coins += l_coins; l.total_gems += l_gems
            log_activity(session, l.id, "match_reward", f"Loss reward: +{l_coins} coins, +{l_gems} gems",
                         coins_change=l_coins, gems_change=l_gems)

        # ── Monthly season: PvP is the heaviest contributor ──
        # Only real PvP (not vsbot) — winner +25 & a win, loser +5 for playing.
        if not s.get("is_vsbot"):
            try:
                from services.season_service import safe_add_season_points
                if w:
                    safe_add_season_points(session, w, points=25, wins=1)
                if l:
                    safe_add_season_points(session, l, points=5)
            except Exception:
                logger.exception("Season points for PvP failed (non-fatal)")

        session.commit()
        return w_coins, w_gems, l_coins, l_gems
    except Exception: session.rollback(); return 0,0,0,0
    finally: session.close()


def _calc_potm(s):
    """Calculate Player of the Match using Impact Points.

    Batting impact: runs + (4s × 1) + (6s × 2) + (bonus if 50/100) - (penalty if out cheap)
    Bowling impact: (wickets × 25) + (20 - economy_rate × 2) per over bowled

    Returns: (name, impact_points, stats_string) or (None, 0, "")
    """
    best_name = None
    best_impact = 0
    best_stats = ""

    def _bat_impact(bs):
        if not bs or bs.get("balls", 0) == 0:
            return 0
        runs = bs.get("runs", 0)
        fours = bs.get("fours", 0)
        sixes = bs.get("sixes", 0)
        balls = bs.get("balls", 1)
        impact = runs + fours * 1 + sixes * 2
        # Strike rate bonus (T20 context)
        sr = (runs / balls) * 100 if balls else 0
        if sr >= 150:
            impact += 10
        elif sr >= 130:
            impact += 5
        # Milestones
        if runs >= 100:
            impact += 30
        elif runs >= 50:
            impact += 15
        return impact

    def _bowl_impact(bws):
        if not bws or bws.get("balls", 0) == 0:
            return 0
        wickets = bws.get("wickets", 0)
        runs = bws.get("runs", 0)
        balls = bws.get("balls", 1)
        overs = balls / 6
        econ = (runs / balls) * 6 if balls else 0
        impact = wickets * 25
        # Economy bonus/penalty (6 runs/over is baseline)
        econ_diff = 8 - econ  # positive = good economy
        impact += econ_diff * overs * 2
        # Milestones
        if wickets >= 5:
            impact += 30
        elif wickets >= 3:
            impact += 15
        return max(0, impact)

    # Gather all players from both innings with their stats
    all_players = {}  # roster_id -> (name, bat_impact, bowl_impact, bat_stats, bowl_stats, team_name)

    # 1st innings
    inn1_bat_xi = s.get("inn1_bat_xi", [])
    inn1_bowl_xi = s.get("inn1_bowl_xi", [])
    inn1_bat_stats = s.get("inn1_bat_stats", {})
    inn1_bowl_stats = s.get("inn1_bowl_stats", {})
    inn1_bat_team = s.get("inn1_team", "")
    inn1_bowl_team = s["bat_team_name"] if s["innings"] == 2 else s["bowl_team_name"]

    for p in inn1_bat_xi:
        rid = p["roster_id"]
        bs = inn1_bat_stats.get(rid, {})
        all_players[rid] = {
            "name": p["name"], "team": inn1_bat_team,
            "bat": bs, "bowl": {}, "bat_impact": _bat_impact(bs), "bowl_impact": 0,
        }
    for p in inn1_bowl_xi:
        rid = p["roster_id"]
        bws = inn1_bowl_stats.get(rid, {})
        if rid in all_players:
            all_players[rid]["bowl"] = bws
            all_players[rid]["bowl_impact"] = _bowl_impact(bws)
        else:
            all_players[rid] = {
                "name": p["name"], "team": inn1_bowl_team,
                "bat": {}, "bowl": bws, "bat_impact": 0, "bowl_impact": _bowl_impact(bws),
            }

    # 2nd innings (only if match reached 2nd)
    if s.get("innings", 1) >= 2:
        inn2_bat_xi = s["bat_xi"]
        inn2_bowl_xi = s["bowl_xi"]
        inn2_bat_team = s["bat_team_name"]
        inn2_bowl_team = s["bowl_team_name"]

        for p in inn2_bat_xi:
            rid = p["roster_id"]
            bs = s["bat_stats"].get(rid, {})
            if rid in all_players:
                all_players[rid]["bat"] = bs
                all_players[rid]["bat_impact"] += _bat_impact(bs)
            else:
                all_players[rid] = {
                    "name": p["name"], "team": inn2_bat_team,
                    "bat": bs, "bowl": {}, "bat_impact": _bat_impact(bs), "bowl_impact": 0,
                }
        for p in inn2_bowl_xi:
            rid = p["roster_id"]
            bws = s["bowl_stats"].get(rid, {})
            if rid in all_players:
                all_players[rid]["bowl"] = bws
                all_players[rid]["bowl_impact"] += _bowl_impact(bws)
            else:
                all_players[rid] = {
                    "name": p["name"], "team": inn2_bowl_team,
                    "bat": {}, "bowl": bws, "bat_impact": 0, "bowl_impact": _bowl_impact(bws),
                }

    # Find max impact
    for rid, data in all_players.items():
        total = data["bat_impact"] + data["bowl_impact"]
        if total > best_impact:
            best_impact = total
            best_name = data["name"]
            parts = []
            bs = data["bat"]
            bws = data["bowl"]
            if bs.get("balls", 0) > 0:
                parts.append(f"🏏 {bs.get('runs', 0)}({bs.get('balls', 0)})")
            if bws.get("balls", 0) > 0:
                overs = bws['balls'] // 6
                rem = bws['balls'] % 6
                ovr_str = f"{overs}.{rem}" if rem else str(overs)
                parts.append(f"🎳 {bws.get('wickets', 0)}/{bws.get('runs', 0)} ({ovr_str})")
            best_stats = " | ".join(parts) if parts else "—"

    return best_name, int(best_impact), best_stats


def _gather_top_performers(s):
    """Return (top_scorer_dict, top_wicket_dict) across both innings.

    Each dict has: name, rating, team, plus sport-specific stats.
    Either may be None if no qualifying player exists.
    """
    top_bat = None  # (runs, dict)
    top_bowl = None  # (wickets, -runs_conceded, dict) — break ties by economy

    def _walk(xi, stats, team_name):
        nonlocal top_bat, top_bowl
        for p in xi:
            rid = p.get("roster_id")
            if rid is None:
                continue
            ps = stats.get(rid)
            if not ps:
                continue
            if "runs" in ps and "balls" in ps and ps.get("balls", 0) > 0:
                # Batting
                runs = ps.get("runs", 0)
                if runs > 0 and (top_bat is None or runs > top_bat[0]):
                    top_bat = (runs, {
                        "name": p.get("name", "—"),
                        "rating": p.get("rating", "—"),
                        "team": team_name,
                        "runs": runs,
                        "balls": ps.get("balls", 0),
                        "fours": ps.get("fours", 0),
                        "sixes": ps.get("sixes", 0),
                    })
            if "wickets" in ps and ps.get("balls", 0) > 0:
                # Bowling
                wk = ps.get("wickets", 0)
                rc = ps.get("runs", 0)
                ov_balls = ps.get("balls", 0)
                ov_str = f"{ov_balls // 6}.{ov_balls % 6}" if ov_balls % 6 else str(ov_balls // 6)
                key = (wk, -rc)
                if wk > 0 and (top_bowl is None or key > (top_bowl[0], -top_bowl[1].get("runs", 0))):
                    top_bowl = (wk, {
                        "name": p.get("name", "—"),
                        "rating": p.get("rating", "—"),
                        "team": team_name,
                        "wickets": wk,
                        "runs": rc,
                        "overs": ov_str,
                    })

    # 1st innings
    inn1_bat_team = s.get("inn1_team", "")
    inn1_bowl_team = s["bat_team_name"] if s.get("innings", 1) == 2 else s.get("bowl_team_name", "")
    _walk(s.get("inn1_bat_xi", []), s.get("inn1_bat_stats", {}), inn1_bat_team)
    _walk(s.get("inn1_bowl_xi", []), s.get("inn1_bowl_stats", {}), inn1_bowl_team)
    # 2nd innings (current)
    if s.get("innings") == 2:
        _walk(s.get("bat_xi", []), s.get("bat_stats", {}), s.get("bat_team_name", ""))
        _walk(s.get("bowl_xi", []), s.get("bowl_stats", {}), s.get("bowl_team_name", ""))

    return (top_bat[1] if top_bat else None,
            top_bowl[1] if top_bowl else None)


def _gather_top_per_team(s, top_n=4):
    """Group top batters and bowlers by their team (innings).

    Returns dict:
      {
        'inn1': {'team': str, 'batters': [{name, runs, balls},...],
                          'bowlers': [{name, overs, runs, wickets, econ},...]},
        'inn2': {...same shape...},
      }
    Used by the new match summary card.
    """
    def _top_batters(xi, stats, top_n):
        rows = []
        for p in xi:
            rid = p.get("roster_id")
            if rid is None: continue
            ps = stats.get(rid)
            if not ps: continue
            if ps.get("balls", 0) <= 0: continue
            rows.append({
                "name": p.get("name", "—"),
                "rating": p.get("rating", "—"),
                "runs": ps.get("runs", 0),
                "balls": ps.get("balls", 0),
                "fours": ps.get("fours", 0),
                "sixes": ps.get("sixes", 0),
                "out": ps.get("out", False),
            })
        rows.sort(key=lambda r: (-r["runs"], r["balls"]))
        return rows[:top_n]

    def _top_bowlers(xi, stats, top_n):
        rows = []
        for p in xi:
            rid = p.get("roster_id")
            if rid is None: continue
            ps = stats.get(rid)
            if not ps: continue
            if ps.get("balls", 0) <= 0: continue
            balls = ps.get("balls", 0)
            ov_str = f"{balls // 6}.{balls % 6}" if balls % 6 else f"{balls // 6}"
            ov_dec = balls / 6.0
            runs = ps.get("runs", 0)
            econ = (runs / ov_dec) if ov_dec > 0 else 0.0
            rows.append({
                "name": p.get("name", "—"),
                "rating": p.get("rating", "—"),
                "overs": ov_str,
                "runs": runs,
                "wickets": ps.get("wickets", 0),
                "econ": econ,
            })
        rows.sort(key=lambda r: (-r["wickets"], r["econ"]))
        return rows[:top_n]

    # inn1: snapshot tables
    inn1_team = s.get("inn1_team", "Team 1")
    inn1_bowl_team = (s.get("bat_team_name", "")
                      if s.get("innings", 1) == 2
                      else s.get("bowl_team_name", "Team 2"))
    inn1 = {
        "team": inn1_team,
        "bowl_team": inn1_bowl_team,
        "batters": _top_batters(s.get("inn1_bat_xi", []),
                                  s.get("inn1_bat_stats", {}), top_n),
        "bowlers": _top_bowlers(s.get("inn1_bowl_xi", []),
                                  s.get("inn1_bowl_stats", {}), top_n),
    }

    # inn2: current state's tables (if we're past innings 1)
    if s.get("innings") == 2:
        inn2 = {
            "team": s.get("bat_team_name", "Team 2"),
            "bowl_team": s.get("bowl_team_name", inn1_team),
            "batters": _top_batters(s.get("bat_xi", []),
                                      s.get("bat_stats", {}), top_n),
            "bowlers": _top_bowlers(s.get("bowl_xi", []),
                                      s.get("bowl_stats", {}), top_n),
        }
    else:
        inn2 = {"team": "", "bowl_team": "", "batters": [], "bowlers": []}

    return {"inn1": inn1, "inn2": inn2}


async def _save_match_stats(s):
    session = get_session()
    try:
        def build_lookup(xi_list, user_id):
            return {p["roster_id"]: (p["player_id"], user_id) for p in xi_list}

        # If still in 1st innings, save current stats as 1st innings
        if s.get("innings") == 1 and not s.get("inn1_bat_team_id"):
            s["inn1_bat_stats"] = dict(s["bat_stats"])
            s["inn1_bowl_stats"] = dict(s["bowl_stats"])
            s["inn1_bat_team_id"] = s["bat_team_id"]
            s["inn1_bowl_team_id"] = s["bowl_team_id"]
            s["inn1_bat_xi"] = list(s["bat_xi"])
            s["inn1_bowl_xi"] = list(s["bowl_xi"])

        inn1_bat_uid = s.get("inn1_bat_team_id")
        inn1_bowl_uid = s.get("inn1_bowl_team_id")
        inn1_bat_xi = s.get("inn1_bat_xi", [])
        inn1_bowl_xi = s.get("inn1_bowl_xi", [])

        # 2nd innings: who batted = current bat_team_id, who bowled = current bowl_team_id
        inn2_bat_uid = s["bat_team_id"]
        inn2_bowl_uid = s["bowl_team_id"]
        inn2_bat_xi = s["bat_xi"]
        inn2_bowl_xi = s["bowl_xi"]

        bat_lookup_1 = build_lookup(inn1_bat_xi, inn1_bat_uid) if inn1_bat_uid and not s.get("inn1_stats_saved") else {}
        bowl_lookup_1 = build_lookup(inn1_bowl_xi, inn1_bowl_uid) if inn1_bowl_uid and not s.get("inn1_stats_saved") else {}
        # Only process 2nd innings if match reached 2nd innings
        if s.get("innings", 1) >= 2:
            bat_lookup_2 = build_lookup(inn2_bat_xi, inn2_bat_uid)
            bowl_lookup_2 = build_lookup(inn2_bowl_xi, inn2_bowl_uid)
        else:
            bat_lookup_2 = {}
            bowl_lookup_2 = {}

        inn1_bat_stats = s.get("inn1_bat_stats", {})
        inn1_bowl_stats = s.get("inn1_bowl_stats", {})
        inn2_bat_stats = s.get("bat_stats", {})
        inn2_bowl_stats = s.get("bowl_stats", {})

        # Persist career batting + bowling stats through the shared service used
        # by /playmatch, /vsbot, /cm, and /wpm.
        from services.player_stats_service import persist_player_game_stats
        saved_counts = persist_player_game_stats(session, s)
        logger.info("Saved player career stats for match %s: %s", s.get("match_id"), saved_counts)

        # ── Record form history per player (last-5 window) ──
        try:
            from services.form_service import record_match_performance
            mid = s.get("match_id")
            # Aggregate per (user, player) across both innings
            agg = {}  # (uid, pid) → {runs, balls, out, wickets, runs_conceded, overs}

            def add_bat(rid, bs, lookup):
                rid_int = int(rid) if isinstance(rid, str) else rid
                if rid_int not in lookup or not bs: return
                pid, uid = lookup[rid_int]
                if uid is None or pid is None: return
                d = agg.setdefault((uid, pid), {
                    "runs": 0, "balls": 0, "out": False,
                    "wickets": 0, "runs_conceded": 0, "overs": 0.0
                })
                d["runs"] += bs.get("runs", 0)
                d["balls"] += bs.get("balls", 0)
                d["out"] = d["out"] or bs.get("out", False)

            def add_bowl(rid, bws, lookup):
                rid_int = int(rid) if isinstance(rid, str) else rid
                if rid_int not in lookup or not bws: return
                pid, uid = lookup[rid_int]
                if uid is None or pid is None: return
                d = agg.setdefault((uid, pid), {
                    "runs": 0, "balls": 0, "out": False,
                    "wickets": 0, "runs_conceded": 0, "overs": 0.0
                })
                d["wickets"] += bws.get("wickets", 0)
                d["runs_conceded"] += bws.get("runs", 0)
                balls = bws.get("balls", 0) or (bws.get("overs_done", 0) * 6 + bws.get("this_over_balls", 0))
                d["overs"] += balls / 6.0

            for rid, bs in inn1_bat_stats.items(): add_bat(rid, bs, bat_lookup_1)
            for rid, bws in inn1_bowl_stats.items(): add_bowl(rid, bws, bowl_lookup_1)
            for rid, bs in inn2_bat_stats.items(): add_bat(rid, bs, bat_lookup_2)
            for rid, bws in inn2_bowl_stats.items(): add_bowl(rid, bws, bowl_lookup_2)

            for (uid, pid), d in agg.items():
                if uid == -1:  # bot
                    continue
                # Skip players who didn't actually feature
                if d["balls"] == 0 and d["overs"] == 0:
                    continue
                record_match_performance(
                    session, uid, pid, mid,
                    runs=d["runs"], balls=d["balls"], out=d["out"],
                    wickets=d["wickets"], runs_conceded=d["runs_conceded"],
                    overs_bowled=d["overs"],
                )
        except Exception:
            logger.exception("Form history recording failed")

        # ── Per-match stats snapshot (for tour leaderboards) ──
        try:
            from models import PlayerMatchStats as _PMS
            mid_v = s.get("match_id")
            if mid_v:
                # Clear any prior snapshot for this match (re-runs)
                session.query(_PMS).filter(_PMS.match_id == mid_v).delete(
                    synchronize_session=False)
                # `agg` was built above with per-(uid, pid) totals
                for (uid_v, pid_v), d in agg.items():
                    if uid_v == -1:  # bot
                        continue
                    if d["balls"] == 0 and d["overs"] == 0:
                        continue
                    # Derive bat fours/sixes from inn1+inn2 bat_stats
                    fours = sixes = 0
                    for inn_stats, lookup in [
                        (inn1_bat_stats, bat_lookup_1),
                        (inn2_bat_stats, bat_lookup_2),
                    ]:
                        for rid, bs in inn_stats.items():
                            rid_int = int(rid) if isinstance(rid, str) else rid
                            if rid_int in lookup and lookup[rid_int] == (pid_v, uid_v):
                                fours += bs.get("fours", 0)
                                sixes += bs.get("sixes", 0)
                    pms = _PMS(
                        match_id=mid_v, player_id=pid_v, user_id=uid_v,
                        bat_runs=d["runs"], bat_balls=d["balls"],
                        bat_fours=fours, bat_sixes=sixes, bat_out=d["out"],
                        bowl_wickets=d["wickets"], bowl_runs=d["runs_conceded"],
                        bowl_balls=int(d["overs"] * 6),
                    )
                    session.add(pms)
        except Exception:
            logger.exception("PlayerMatchStats snapshot failed (non-fatal)")

        session.commit()
        logger.info(f"Saved match stats for match {s.get('match_id')}")
    except Exception:
        session.rollback()
        logger.exception("Failed to save match stats")
    finally:
        session.close()


# ═══════════════════════════ /resume ═════════════════════════════════
# NOTE: resume_handler is defined further below (in the RECOVERY section).
# The new version is more robust and uses _safe_show_next.


# ═══════════════════════════ /lastmatch ══════════════════════════════

async def recentmatches_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the user's recent completed matches, each with a button that opens
    the read-only scorecard in the Mini App."""
    import os as _os
    tg = update.effective_user
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u:
            await update.message.reply_text(
                "❌ You haven't played yet. Use /debut then /playmatch.")
            return
        matches = (session.query(Match)
                   .filter(Match.status == "completed",
                           or_(Match.user1_id == u.id, Match.user2_id == u.id))
                   .order_by(Match.completed_at.desc().nullslast(), Match.id.desc())
                   .limit(8).all())
        if not matches:
            await update.message.reply_text(
                "🏏 <b>No completed matches yet.</b>\n\nPlay one with "
                "<code>/playmatch @user</code>.", parse_mode="HTML")
            return

        bot_username = (_os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
        miniapp_name = (_os.getenv("MINIAPP_NAME", "") or "").strip()

        def _label(uid):
            usr = session.query(User).get(uid) if uid else None
            if not usr:
                return "—"
            return f"@{usr.username}" if usr.username else (usr.first_name or "Player")

        lines = ["🏏 <b>YOUR RECENT MATCHES</b>", "━━━━━━━━━━━━━━━━━━━"]
        btns = []
        for m in matches:
            opp_id = m.user2_id if m.user1_id == u.id else m.user1_id
            opp = _label(opp_id)
            won = (m.winner_id == u.id)
            tied = (m.margin_type == "tie")
            icon = "🏆" if won else ("🤝" if tied else "❌")
            margin = ""
            if m.margin_type == "tie":
                margin = "Tied"
            elif m.margin_type == "forfeit":
                margin = "Forfeit"
            elif m.margin_type and m.margin_value is not None:
                margin = f"by {m.margin_value} {m.margin_type}"
            score = ""
            if m.inn1_runs is not None and m.inn2_runs is not None:
                score = f" · {m.inn1_runs}/{m.inn1_wickets or 0} vs {m.inn2_runs}/{m.inn2_wickets or 0}"
            when = m.completed_at.strftime("%d %b") if m.completed_at else ""
            lines.append(f"{icon} vs {opp} — {margin}{score} <i>({when})</i>")
            # Scorecard button (deep link into Mini App read-only scorecard)
            if bot_username:
                if miniapp_name:
                    url = f"https://t.me/{bot_username}/{miniapp_name}?startapp=sc_{m.id}"
                else:
                    url = f"https://t.me/{bot_username}?start=sc_{m.id}"
                btns.append([InlineKeyboardButton(
                    f"📋 Scorecard: vs {opp} ({when})", url=url)])

        kb = InlineKeyboardMarkup(btns) if btns else None
        await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                        reply_markup=kb,
                                        disable_web_page_preview=True)
    except Exception:
        logger.exception("recentmatches err")
        await update.message.reply_text("❌ Couldn't load your recent matches.")
    finally:
        session.close()


async def lastmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's most recent completed match by re-sending a summary
    and (if we still have it) the original result message id for a jump link."""
    tg = update.effective_user
    cid = update.effective_chat.id
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u:
            await update.message.reply_text(
                "❌ You haven't played yet. Use /debut to start, then /playmatch to play.")
            return

        # Most recent completed match this user was in
        m = (session.query(Match)
             .filter(Match.status == "completed",
                     or_(Match.user1_id == u.id, Match.user2_id == u.id))
             .order_by(Match.completed_at.desc().nullslast(),
                       Match.id.desc())
             .first())
        if not m:
            await update.message.reply_text(
                "🏏 <b>No completed matches yet.</b>\n\n"
                "Play one with <code>/playmatch @user</code> or "
                "<code>/vsbot</code>.", parse_mode="HTML")
            return

        # Build a compact recap. Re-fetch user labels.
        u1 = session.query(User).get(m.user1_id)
        u2 = session.query(User).get(m.user2_id)
        u1_label = f"@{u1.username}" if u1 and u1.username else (
            u1.first_name if u1 else "User1")
        u2_label = f"@{u2.username}" if u2 and u2.username else (
            u2.first_name if u2 else "User2")
        # If a side is the bot, label it
        if u1 and u1.telegram_id == BOT_TG_ID_:
            u1_label = "🤖 Bot"
        if u2 and u2.telegram_id == BOT_TG_ID_:
            u2_label = "🤖 Bot"

        winner = session.query(User).get(m.winner_id) if m.winner_id else None
        winner_label = "—"
        if winner:
            winner_label = (f"@{winner.username}" if winner.username
                            else (winner.first_name or "Winner"))
            if winner.telegram_id == BOT_TG_ID_:
                winner_label = "🤖 Bot"

        margin = ""
        if m.margin_type == "forfeit":
            margin = "by forfeit"
        elif m.margin_type and m.margin_value is not None:
            margin = f"by {m.margin_value} {m.margin_type}"

        when = m.completed_at.strftime("%d %b, %H:%M UTC") if m.completed_at else "?"

        lines = [
            f"🏏 <b>YOUR LAST MATCH</b>",
            f"━━━━━━━━━━━━━━━━━━━",
            f"{u1_label} <b>vs</b> {u2_label}",
            f"📅 {when}",
            f"🏟️ {m.stadium or '—'} · {m.pitch_type or '—'}",
            f"",
        ]
        if m.inn1_runs is not None:
            lines.append(f"🔴 1st: <b>{m.inn1_runs}/{m.inn1_wickets or 0}</b>")
        if m.inn2_runs is not None:
            lines.append(f"🟢 2nd: <b>{m.inn2_runs}/{m.inn2_wickets or 0}</b>")
        lines.append("")
        if winner:
            lines.append(f"🏆 <b>{winner_label}</b> won {margin}".strip())
        else:
            lines.append("🤝 No winner recorded")

        # POTM
        if m.potm_player_id:
            from models import Player as _P
            pl = session.query(_P).get(m.potm_player_id)
            if pl:
                lines.append(f"⭐ POTM: <b>{pl.name}</b>")

        # Jump link to original result message (if we still have it AND we're
        # in the same chat where the match was played)
        if m.result_message_id and m.chat_id == cid:
            try:
                await update.message.reply_text(
                    "\n".join(lines), parse_mode="HTML",
                    reply_to_message_id=m.result_message_id,
                )
                return
            except Exception:
                # Old message may have been deleted — fall through
                pass

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("lastmatch_handler err")
        await update.message.reply_text(
            "⚠️ Couldn't load your last match. Try again in a moment.")
    finally:
        session.close()




async def testwpm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostic command for /wpm and /cm completion broadcasts.

    Usage:
      /testwpm            -> use the caller's latest Mini-App match
      /testwpm <match_id> -> test a specific match id

    The command is controlled from the website's Commands page via the
    ``testwpm`` BotCommand row.
    """
    tg = update.effective_user
    cid = update.effective_chat.id
    session = get_session()
    try:
        from services.command_config_service import is_command_enabled, get_disabled_message
        if not is_command_enabled(session, "testwpm"):
            await update.message.reply_text(
                get_disabled_message(session, "testwpm"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first, then run /testwpm.")
            return

        match = None
        if context.args:
            try:
                match_id = int(context.args[0])
            except (TypeError, ValueError):
                await update.message.reply_text(
                    "Usage: <code>/testwpm [match_id]</code>", parse_mode="HTML")
                return
            match = session.query(Match).get(match_id)
            if match and user.id not in (match.user1_id, match.user2_id):
                await update.message.reply_text("❌ You are not a participant in that match.")
                return
        else:
            # Prefer this chat's most recent match for this user, then any
            # recent Mini-App match by the user.  Completed matches let admins
            # verify summary delivery repeatedly; active terminal matches also
            # exercise the self-healing finalizer.
            match = (session.query(Match)
                     .filter(or_(Match.user1_id == user.id, Match.user2_id == user.id),
                             Match.chat_id == cid,
                             Match.status.in_(["playing", "completed"]))
                     .order_by(Match.completed_at.desc().nullslast(), Match.id.desc())
                     .first())
            if not match:
                match = (session.query(Match)
                         .filter(or_(Match.user1_id == user.id, Match.user2_id == user.id),
                                 Match.status.in_(["playing", "completed"]))
                         .order_by(Match.completed_at.desc().nullslast(), Match.id.desc())
                         .first())

        if not match:
            await update.message.reply_text(
                "🏏 No /wpm or /cm match found to test. Complete a match first, "
                "or pass a match id: <code>/testwpm 123</code>.", parse_mode="HTML")
            return

        finalized = None
        if match.status != "completed":
            try:
                from services.match_webapp_service import ensure_webapp_match_completed
                finalized = ensure_webapp_match_completed(session, match.id)
                if finalized:
                    session.refresh(match)
            except Exception:
                logger.exception("/testwpm finalize check failed")

        if match.status != "completed":
            try:
                from services.match_webapp_access import get_state, get_next_action
                state = get_state(match.id) or {}
                next_action = get_next_action(match.id)
                innings = state.get("innings", "?")
                score = f"{state.get('total_runs', 0)}/{state.get('total_wickets', 0)}"
                await update.message.reply_text(
                    "🧪 <b>TestWPM diagnostic</b>\n\n"
                    f"Match <code>{match.id}</code> is not completed yet.\n"
                    f"Status: <code>{match.status}</code> · Innings: <code>{innings}</code> · "
                    f"Next: <code>{next_action}</code> · Score: <code>{score}</code>\n\n"
                    "Finish the 2nd innings or chase, then run <code>/testwpm</code> again.",
                    parse_mode="HTML")
            except Exception:
                await update.message.reply_text(
                    f"🧪 Match <code>{match.id}</code> is not completed yet.", parse_mode="HTML")
            return

        queued = False
        try:
            from admin import send_testwpm_summary_to_chat
            queued = send_testwpm_summary_to_chat(
                match.id, cid, (finalized or {}).get("result") if isinstance(finalized, dict) else None)
        except Exception:
            logger.exception("/testwpm summary queue failed")

        if queued:
            await update.message.reply_text(
                "✅ <b>TestWPM queued.</b>\n\n"
                f"Match <code>{match.id}</code> is completed and the match-summary "
                "card/text fallback is being sent to this chat.",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                "⚠️ Match is completed, but I could not queue the summary send. "
                "Check BOT_TOKEN/network logs.")
    except Exception:
        logger.exception("testwpm_handler err")
        await update.message.reply_text("❌ /testwpm failed. Check logs for details.")
    finally:
        session.close()


# ═══════════════════════════ /info (during match) ═════════════════════

async def info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """While a match is in progress, show striker / non-striker / bowler /
    score / target so the user can see what's happening without scrolling.
    Outside a match, fall through to /playerinfo for a player lookup."""
    tg = update.effective_user
    cid = update.effective_chat.id

    # Find active match for this user (same lookup endmatch uses)
    mid = None
    s = None
    for k, v in context.bot_data.items():
        if k.startswith("ms_") and isinstance(v, dict):
            if v.get("bat_user_tg") == tg.id or v.get("bowl_user_tg") == tg.id:
                mid = int(k.replace("ms_", ""))
                s = v
                break

    # Fallback: load from DB if nothing in memory but the user thinks they're in a match
    if not s:
        await update.message.reply_text(
            "ℹ️ <b>No active match.</b>\n\n"
            "Use <code>/playerinfo @player_name</code> to look up a specific player, "
            "or <code>/lastmatch</code> to see your most recent match.",
            parse_mode="HTML")
        return

    # Pull current striker / non-striker / bowler from state
    bat_order = s.get("batting_order", [])
    striker = bat_order[s.get("striker_idx", 0)] if bat_order else None
    non_striker = bat_order[s.get("non_striker_idx", 1)] if len(bat_order) > 1 else None
    bowler = s.get("current_bowler")

    bat_stats = s.get("bat_stats", {})
    bowl_stats = s.get("bowl_stats", {})

    lines = [
        f"ℹ️ <b>MATCH INFO</b> — Innings {s.get('innings', 1)}/2",
        f"━━━━━━━━━━━━━━━━━━━",
        f"🏏 Batting: <b>{s.get('bat_team_name', '?')}</b>",
        f"   {format_score(s)} ({format_overs(s)})",
    ]
    if s.get("target"):
        rem = s["target"] - s.get("total_runs", 0)
        balls_left = (s.get("overs", 20) * 6
                       - s.get("current_over", 1) * 6 + 6
                       - s.get("current_ball", 0))
        lines.append(f"   🎯 Target: <b>{s['target']}</b> · "
                      f"need <b>{rem}</b> off <b>{max(0, balls_left)}</b>")
    lines.append("")

    # Current batters
    if striker:
        bs = bat_stats.get(striker["roster_id"], {})
        runs = bs.get("runs", 0); balls = bs.get("balls", 0)
        lines.append(
            f"🔴 <b>{striker['name']}</b> ({striker.get('rating', '?')}) "
            f"— {runs} ({balls})*")
    if non_striker:
        bs = bat_stats.get(non_striker["roster_id"], {})
        runs = bs.get("runs", 0); balls = bs.get("balls", 0)
        lines.append(
            f"⚪ <b>{non_striker['name']}</b> ({non_striker.get('rating', '?')}) "
            f"— {runs} ({balls})")
    lines.append("")

    # Current bowler
    if bowler:
        bws = bowl_stats.get(bowler["roster_id"], {})
        balls = bws.get("balls", 0)
        ov_done = balls // 6
        ov_balls = balls % 6
        overs_str = f"{ov_done}.{ov_balls}"
        runs = bws.get("runs", 0); wkts = bws.get("wickets", 0)
        style = bowler.get("bowl_style", "")
        lines.append(
            f"🎳 Bowling: <b>{bowler['name']}</b> ({bowler.get('rating', '?')})\n"
            f"   {overs_str} ov · {runs} runs · {wkts} wkt"
            + (f" · <i>{style}</i>" if style else ""))

    # Pitch + stadium for context
    if s.get("pitch_type") or s.get("stadium"):
        lines.append("")
        bits = []
        if s.get("stadium"): bits.append(f"🏟️ {s['stadium']}")
        if s.get("pitch_type"): bits.append(f"📍 {s['pitch_type']}")
        lines.append(" · ".join(bits))

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════ /endmatch ═══════════════════════════════

async def endmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user; cid = update.effective_chat.id
    # Find active match for this user
    mid = None
    for k, v in context.bot_data.items():
        if k.startswith("ms_") and isinstance(v, dict):
            if v.get("bat_user_tg") == tg.id or v.get("bowl_user_tg") == tg.id:
                mid = int(k.replace("ms_", "")); break
    if not mid:
        await update.message.reply_text("❌ No active match found."); return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"endmatch_{mid}_{tg.id}"),
        InlineKeyboardButton("❌ No", callback_data=f"endmatchno_{mid}"),
    ]])
    await update.message.reply_text(
        f"🏏 <b>/endmatch</b> ⚡\n\nDo you want to End the match? 🛑\n"
        f"You will get a fine of {FINE_COINS:,} Coins 💰 and {FINE_GEMS} Gems 💎\n\n"
        f"✅ Yes — You get fined ⚠️\n❌ No — Match continues 🔄",
        parse_mode="HTML", reply_markup=kb)

async def endmatch_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, uid_tg = int(parts[1]), int(parts[2])
    if q.from_user.id != uid_tg: await q.answer("Not your action!"); return
    await q.answer()
    _cancel_action_timer(context, mid)
    # Save stats before anything else
    s = _gs(context, mid)
    if s:
        await _save_match_stats(s)
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == uid_tg).first()
        if u:
            u.total_coins = max(0, u.total_coins - FINE_COINS)
            u.total_gems = max(0, u.total_gems - FINE_GEMS)
            log_activity(session, u.id, "endmatch", f"Ended match #{mid}: -{FINE_COINS} coins, -{FINE_GEMS} gems",
                         coins_change=-FINE_COINS, gems_change=-FINE_GEMS)
        m = session.query(Match).get(mid)
        if m: m.status = "completed"; m.completed_at = datetime.utcnow()
        session.commit()
        u_mention = _mention(u) if u else "Player"
        await q.edit_message_text(
            f"🛑 <b>MATCH ENDED</b>\n\n{u_mention} ended the match.\n"
            f"⚠️ Fine: -{FINE_COINS:,} Coins 💰 -{FINE_GEMS} Gems 💎\n"
            f"📊 Player stats saved.", parse_mode="HTML")
    except Exception:
        session.rollback(); logger.exception("endmatch_yes_callback failed")
        try: await q.answer("⚠️ Something went wrong, try again.", show_alert=True)
        except Exception: pass
    finally: session.close()

    # Scorecards for visual record (best-effort, non-fatal)
    try:
        if s:
            current_innings = s.get("innings", 1)
            inn1_played = (current_innings >= 2
                           or s.get("total_runs", 0) > 0
                           or s.get("total_wickets", 0) > 0
                           or s.get("current_over", 1) > 1
                           or s.get("current_ball", 0) > 0)
            if inn1_played:
                await _send_innings_scorecards(context, mid, innings_num=1)
            if current_innings >= 2:
                await _send_innings_scorecards(context, mid, innings_num=2)
    except Exception:
        logger.exception("Endmatch scorecard send failed (non-fatal)")

    cleanup_state(context, mid)
    release_match_lock(mid)

async def endmatch_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🔄 Match continues!")


# ════════════════════════ /wpm Mini-App lobby ════════════════════════

async def wpm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create an UnderCover-style chat lobby that launches the cricket Mini App."""
    tg = update.effective_user
    cid = update.effective_chat.id
    try:
        overs = int(context.args[0]) if context.args else 1
    except ValueError:
        overs = 0
    if overs < 1 or overs > 5:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/wpm &lt;overs (1-5)&gt;</code> to start a match lobby.",
            parse_mode="HTML")
        return

    session = get_session()
    try:
        host = session.query(User).filter(User.telegram_id == tg.id).first()
        if not host:
            await update.message.reply_text("❌ Use /debut first!")
            return
        existing = _active_cric_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(_chat_busy_message(existing), parse_mode="HTML")
            return
        if _active_cric_match_for_user(session, host.id):
            await update.message.reply_text("⚠️ You already have an active match running!")
            return
        if context.bot_data.get(_cric_lobby_key(cid)):
            await update.message.reply_text("⚠️ There is already a match lobby waiting in this chat!")
            return
        if _cric_lobby_for_user(context.bot_data, host.id):
            await update.message.reply_text("⚠️ You already have an active match lobby!")
            return

        from handlers.lineup import validate_xi, _get_ordered_roster
        valid, errors = validate_xi(_get_ordered_roster(session, host.id))
        if not valid:
            await update.message.reply_text(
                "❌ <b>Lobby creation failed — your XI is invalid:</b>\n"
                + "\n".join(f"• {error}" for error in errors), parse_mode="HTML")
            return

        context.bot_data[_cric_lobby_key(cid)] = {
            "host_user_id": host.id,
            "host_tg_id": host.telegram_id,
            "host_label": _user_label(host),
            "overs": overs,
            "original_lobby_chat_id": cid,
        }
        lobby_msg = await update.message.reply_text(
            "🏏 <b>CRICKET MATCH LOBBY CREATED!</b> 🏏\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> {_user_label(host)}\n"
            f"• <b>Length:</b> {overs} Over(s)\n\n"
            "Click the button below to join the match!\n"
            f"⏳ <i>Expires in {LOBBY_EXPIRE // 60} min if no one joins.</i>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤝 Join Match", callback_data="cric_join"),
                InlineKeyboardButton("❌ Cancel Lobby", callback_data="cric_cancel_lobby"),
            ]]))
        # Remember the message id so we can edit it on expiry, and schedule the
        # auto-cancel job (mirrors /playmatch's _auto_expire).
        context.bot_data[_cric_lobby_key(cid)]["lobby_msg_id"] = lobby_msg.message_id
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_lobby, LOBBY_EXPIRE, name=f"lobby_{cid}",
                    data={"chat_id": cid, "lobby_msg_id": lobby_msg.message_id})
        except Exception:
            logger.exception("Failed to schedule /wpm lobby expiry")
    except Exception:
        logger.exception("/wpm lobby creation failed")
        await update.message.reply_text("❌ Failed to create cricket lobby.")
    finally:
        session.close()


async def _expire_lobby(ctx):
    """Auto-cancel a /wpm lobby that nobody joined.

    Only fires for a still-open lobby (no guest). If the lobby was joined or
    already cancelled, this is a no-op.
    """
    d = ctx.job.data
    cid = d["chat_id"]
    key = _cric_lobby_key(cid)
    lobby = ctx.bot_data.get(key)
    if not lobby or lobby.get("guest_user_id"):
        return  # joined or already gone
    ctx.bot_data.pop(key, None)
    try:
        msg_id = d.get("lobby_msg_id") or lobby.get("lobby_msg_id")
        if msg_id:
            await ctx.bot.edit_message_text(
                "⏰ <b>Lobby expired</b> — no one joined.\nStart again with /wpm.",
                chat_id=cid, message_id=msg_id, parse_mode="HTML")
        else:
            await ctx.bot.send_message(
                cid, "⏰ Match lobby expired — no one joined. Start again with /wpm.")
    except Exception:
        logger.exception("Lobby expiry message failed")


def _cancel_lobby_timer(ctx, cid):
    """Remove the pending /wpm lobby auto-expiry job for a chat."""
    try:
        if ctx.job_queue:
            for j in ctx.job_queue.get_jobs_by_name(f"lobby_{cid}"):
                j.schedule_removal()
    except Exception:
        logger.exception("Failed to cancel lobby timer")


async def cric_cancel_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    key = _cric_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby:
        await q.answer("No active lobby in this chat.", show_alert=True)
        return
    is_admin = False
    try:
        member = await context.bot.get_chat_member(cid, q.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        pass
    if q.from_user.id != lobby["host_tg_id"] and not is_admin:
        await q.answer("Only the host or a chat admin can cancel this lobby.", show_alert=True)
        return
    context.bot_data.pop(key, None)
    _cancel_lobby_timer(context, cid)
    await q.answer("Lobby cancelled.")
    await q.edit_message_text("❌ Match lobby has been cancelled.")


async def cric_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    key = _cric_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby:
        await q.answer("No active lobby in this chat.", show_alert=True)
        return
    if q.from_user.id == lobby["host_tg_id"]:
        await q.answer("You cannot join your own lobby!", show_alert=True)
        return

    session = get_session()
    try:
        host = session.query(User).get(lobby["host_user_id"])
        guest = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not guest:
            await q.answer("Use /debut first!", show_alert=True)
            return
        if not host:
            context.bot_data.pop(key, None)
            await q.answer("Lobby host no longer exists.", show_alert=True)
            return
        if lobby.get("guest_user_id"):
            await q.answer("Lobby is already full.", show_alert=True)
            return
        existing = _active_cric_match_in_chat(session, cid)
        if existing:
            context.bot_data.pop(key, None)
            await q.answer("A match is already active in this chat.", show_alert=True)
            return
        if _active_cric_match_for_user(session, host.id):
            context.bot_data.pop(key, None)
            await q.answer("The lobby host is already in another active match.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, guest.id)
                or _cric_lobby_for_user(context.bot_data, guest.id)):
            await q.answer("You already have an active match or lobby!", show_alert=True)
            return

        from handlers.lineup import validate_xi, _get_ordered_roster
        valid, errors = validate_xi(_get_ordered_roster(session, guest.id))
        if not valid:
            await q.answer("Join failed: your playing XI is invalid. Use /xi to fix it.", show_alert=True)
            return

        winner = random.choice([host, guest])
        lobby.update({
            "guest_user_id": guest.id,
            "guest_tg_id": guest.telegram_id,
            "guest_label": _user_label(guest),
            "toss_winner_user_id": winner.id,
            "toss_winner_tg_id": winner.telegram_id,
        })
        # Lobby is now joined — stop the auto-expiry job.
        _cancel_lobby_timer(context, cid)
        await q.answer("Joined match lobby!")
        await q.edit_message_text(
            "🪙 <b>TOSS COMPLETED!</b> 🪙\n"
            "═════════════════════════════\n"
            f"• Host: {_user_label(host)}\n"
            f"• Guest: {_user_label(guest)}\n\n"
            f"🎉 <b>{_user_label(winner)}</b> won the toss!\n"
            "Choose your decision:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Bat First 🏏", callback_data="cric_decision:bat"),
                InlineKeyboardButton("Bowl First 🎳", callback_data="cric_decision:bowl"),
            ]]))
    except Exception:
        session.rollback()
        logger.exception("/wpm lobby join failed")
        await q.answer("Failed to join lobby.", show_alert=True)
    finally:
        session.close()


async def cric_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Launch a joined /wpm lobby after its toss winner chooses bat or bowl.

    This mirrors UnderCover's lifecycle: the lobby remains in memory through
    the toss and only becomes a persisted, active Mini App match here.
    """
    q = update.callback_query
    cid = q.message.chat_id
    key = _cric_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby or not lobby.get("guest_user_id"):
        await q.answer("No joined lobby in this chat.", show_alert=True)
        return
    if q.from_user.id != lobby.get("toss_winner_tg_id"):
        await q.answer("Only the toss winner can make the decision!", show_alert=True)
        return

    decision = q.data.split(":", 1)[1]
    if decision not in ("bat", "bowl"):
        await q.answer("Invalid toss decision.", show_alert=True)
        return
    session = get_session()
    match = None
    try:
        host = session.query(User).get(lobby["host_user_id"])
        guest = session.query(User).get(lobby["guest_user_id"])
        if not host or not guest:
            context.bot_data.pop(key, None)
            await q.answer("Lobby players no longer exist.", show_alert=True)
            return
        if _active_cric_match_in_chat(session, cid):
            context.bot_data.pop(key, None)
            await q.answer("A match is already active in this chat.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, host.id)
                or _active_cric_match_for_user(session, guest.id)):
            context.bot_data.pop(key, None)
            await q.answer("A lobby player is already in another active match.", show_alert=True)
            return

        winner_id = lobby["toss_winner_user_id"]
        opponent_id = guest.id if winner_id == host.id else host.id
        settings = random_match_settings()
        match = Match(
            user1_id=host.id, user2_id=guest.id, status="toss",
            overs=lobby["overs"], toss_winner_id=winner_id,
            toss_decision=decision,
            batting_first_id=winner_id if decision == "bat" else opponent_id,
            bowling_first_id=opponent_id if decision == "bat" else winner_id,
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=cid, created_at=datetime.utcnow(),
        )
        session.add(match)
        session.commit()

        from services.match_webapp_service import init_match_for_webapp
        ok, message = init_match_for_webapp(session, match.id)
        if not ok:
            session.delete(match)
            session.commit()
            await q.answer(f"Failed to launch match: {message}", show_alert=True)
            return

        context.bot_data.pop(key, None)
        winner = host if winner_id == host.id else guest
        await q.answer()
        await q.edit_message_text(
            f"✅ {_user_label(winner)} elected to {'BAT' if decision == 'bat' else 'BOWL'} FIRST")
        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bat_team = bat_user.team_name or f"@{bat_user.username}'s XI"
        bowl_team = bowl_user.team_name or f"@{bowl_user.username}'s XI"
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, cid, match, bat_team, bowl_team,
            _mention(bat_user), _mention(bowl_user))
    except Exception:
        session.rollback()
        logger.exception("/wpm toss decision failed")
        await q.answer("Failed to launch cricket match.", show_alert=True)
    finally:
        session.close()


# ═══════════════════════════ /playmatch ══════════════════════════════

async def playmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user; cid = update.effective_chat.id
    if not context.args: await update.message.reply_text("Usage: /playmatch @username"); return
    t = context.args[0].lstrip("@").strip()
    if not t: await update.message.reply_text("Usage: /playmatch @username"); return
    session = get_session()
    try:
        # One match per chat
        existing = _active_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(
                _chat_busy_message(existing), parse_mode="HTML")
            return

        u1 = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u1: await update.message.reply_text("❌ /debut first!"); return
        u2 = session.query(User).filter(User.username.ilike(t)).first()
        if not u2:
            await update.message.reply_text(
                f"❌ Couldn't find @{t}. They need to have set a Telegram @username "
                f"and used /debut to be challenged.")
            return
        # Block self-play by user id (robust even if usernames are missing/changed)
        if u2.id == u1.id:
            await update.message.reply_text("❌ Can't play yourself")
            return
        r1 = session.query(UserRoster).filter(UserRoster.user_id == u1.id).count()
        r2 = session.query(UserRoster).filter(UserRoster.user_id == u2.id).count()
        if r1 < 11: await update.message.reply_text(f"❌ You need 11+ ({r1})."); return
        if r2 < 11: await update.message.reply_text(f"❌ @{u2.username} needs 11+."); return

        # Validate XI composition
        from handlers.lineup import validate_xi, _get_ordered_roster
        r1_roster = _get_ordered_roster(session, u1.id)
        valid1, errs1 = validate_xi(r1_roster)
        if not valid1:
            await update.message.reply_text(
                f"❌ <b>Your XI is invalid:</b>\n" + "\n".join(f"• {e}" for e in errs1),
                parse_mode="HTML")
            return

        r2_roster = _get_ordered_roster(session, u2.id)
        valid2, errs2 = validate_xi(r2_roster)
        if not valid2:
            await update.message.reply_text(
                f"❌ <b>@{u2.username}'s XI is invalid:</b>\n" + "\n".join(f"• {e}" for e in errs2),
                parse_mode="HTML")
            return
        st = random_match_settings(); now = datetime.utcnow()
        m = Match(user1_id=u1.id, user2_id=u2.id, status="pending", stadium=st["stadium"],
                  pitch_type=st["pitch_type"], weather=st["weather"], temperature=st["temperature"],
                  umpire1=st["umpire1"], umpire2=st["umpire2"], chat_id=cid, created_at=now,
                  expires_at=now + timedelta(seconds=MATCH_EXPIRE))
        session.add(m); session.commit()
        t1 = u1.team_name or f"@{u1.username}'s XI"; t2 = u2.team_name or f"@{u2.username}'s XI"
        await update.message.reply_text(
            f"🔔 <b>NEW MATCH INVITATION!</b>\n\nFrom: @{u1.username} to @{u2.username}\n\n"
            f"🏏 <b>CRICKET GURU MATCH</b>\n\n{t1} vs {t2}\n📍 {st['pitch_type']} | 🌤️ {st['weather']} | 🌡️ {st['temperature']}°C\n"
            f"🏟️ {st['stadium']}\n🎩 {st['umpire1']} | {st['umpire2']}\n\n⏳ Expires: {MATCH_EXPIRE}s",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"matchacc_{m.id}_{u2.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"matchdeny_{m.id}_{u2.id}")]]))
        try:
            if context.job_queue: context.job_queue.run_once(_auto_expire, MATCH_EXPIRE, name=f"match_{m.id}", data={"match_id": m.id, "chat_id": cid})
        except Exception: pass
    except Exception: session.rollback(); logger.exception("Playmatch err"); await update.message.reply_text("⚠️ Error.")
    finally: session.close()

async def _auto_expire(ctx):
    d = ctx.job.data; session = get_session()
    try:
        m = session.query(Match).get(d["match_id"])
        # Expire invites that were never accepted, and (defensively) any match
        # left "accepted" without overs being chosen — both block the chat.
        if m and m.status in ("pending", "accepted"):
            if m.status == "accepted" and m.user2_id:
                u2 = session.query(User).get(m.user2_id)
                if u2:
                    ctx.bot_data.pop(f"awaiting_overs_{u2.telegram_id}", None)
            m.status = "expired"; session.commit()
            await ctx.bot.send_message(d["chat_id"], "⏰ Match expired.")
    except Exception:
        session.rollback(); logger.exception("_auto_expire failed")
    finally: session.close()


async def _expire_overs(ctx):
    """Auto-expire a /playmatch match that was accepted but never got overs."""
    d = ctx.job.data; session = get_session()
    try:
        ctx.bot_data.pop(f"awaiting_overs_{d.get('guest_tg')}", None)
        m = session.query(Match).get(d["match_id"])
        if m and m.status == "accepted":
            m.status = "expired"; session.commit()
            await ctx.bot.send_message(
                d["chat_id"], "⏰ Match setup expired — no overs were chosen.")
    except Exception:
        session.rollback(); logger.exception("_expire_overs failed")
    finally: session.close()


def _cancel_overs_timer(ctx, mid):
    """Remove the pending overs-selection expiry job for a match."""
    try:
        if ctx.job_queue:
            for j in ctx.job_queue.get_jobs_by_name(f"overs_{mid}"):
                j.schedule_removal()
    except Exception:
        logger.exception("Failed to cancel overs timer")

async def match_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user; cid = q.message.chat_id
    parts = q.data.split("_"); mid, auid = int(parts[1]), int(parts[2])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != auid: await q.answer("Only invited!"); return
        await q.answer(); m = session.query(Match).get(mid)
        if not m or m.status != "pending": await q.edit_message_text("❌ Not available."); return
        m.status = "accepted"; session.commit()
        try:
            for j in context.job_queue.get_jobs_by_name(f"match_{mid}"): j.schedule_removal()
        except Exception: pass
        u1 = session.query(User).get(m.user1_id); u2 = session.query(User).get(m.user2_id)
        t1 = u1.team_name or f"@{u1.username}'s XI"; t2 = u2.team_name or f"@{u2.username}'s XI"
        # Inline overs picker — far more reliable than a free-text reply in busy
        # groups. "✍️ Custom" falls back to the typed-number path below.
        overs_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1", callback_data=f"oversset_{mid}_{u2.id}_1"),
             InlineKeyboardButton("2", callback_data=f"oversset_{mid}_{u2.id}_2"),
             InlineKeyboardButton("5", callback_data=f"oversset_{mid}_{u2.id}_5")],
            [InlineKeyboardButton("10", callback_data=f"oversset_{mid}_{u2.id}_10"),
             InlineKeyboardButton("20", callback_data=f"oversset_{mid}_{u2.id}_20")],
            [InlineKeyboardButton("✍️ Custom (1-20)", callback_data=f"overscustom_{mid}_{u2.id}")],
        ])
        await q.edit_message_text(
            f"✅ <b>MATCH ACCEPTED!</b>\n\n🏟️ {t1} vs {t2}\n\n"
            f"{_mention(u2)}, choose the match length:",
            parse_mode="HTML", reply_markup=overs_kb)
        context.bot_data[f"awaiting_overs_{u2.telegram_id}"] = mid
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_overs, OVERS_EXPIRE, name=f"overs_{mid}",
                    data={"match_id": mid, "chat_id": cid, "guest_tg": u2.telegram_id})
        except Exception:
            logger.exception("Failed to schedule overs expiry")
    except Exception:
        session.rollback(); logger.exception("match_accept_callback failed")
        try: await q.answer("⚠️ Something went wrong, try again.", show_alert=True)
        except Exception: pass
    finally: session.close()

async def match_deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, auid = int(parts[1]), int(parts[2])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not u or u.id != auid: await q.answer("Only invited!"); return
        await q.answer(); m = session.query(Match).get(mid)
        if m and m.status == "pending": m.status = "expired"; session.commit()
        await q.edit_message_text("❌ Match denied.")
    except Exception:
        session.rollback(); logger.exception("match_deny_callback failed")
        try: await q.answer("⚠️ Something went wrong, try again.", show_alert=True)
        except Exception: pass
    finally: session.close()

async def overs_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Custom-overs fallback: the invited user types a number (1-20)."""
    tg = update.effective_user; cid = update.effective_chat.id
    key = f"awaiting_overs_{tg.id}"; mid = context.bot_data.get(key)
    if not mid: return
    txt = update.message.text.strip().lower().replace("overs","").replace("over","").strip()
    try: overs = int(txt)
    except ValueError: await update.message.reply_text("❌ Enter 1-20"); return
    if overs < 1 or overs > 20: await update.message.reply_text("❌ 1-20"); return
    del context.bot_data[key]
    await _confirm_overs(context, cid, mid, overs)


async def overs_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Invited user tapped a preset overs button (oversset_{mid}_{auid}_{n})."""
    q = update.callback_query; cid = q.message.chat_id
    parts = q.data.split("_"); mid, auid, overs = int(parts[1]), int(parts[2]), int(parts[3])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not u or u.id != auid: await q.answer("Only the invited player can choose!"); return
    finally:
        session.close()
    await q.answer()
    context.bot_data.pop(f"awaiting_overs_{q.from_user.id}", None)
    try: await q.edit_message_reply_markup(reply_markup=None)
    except Exception: pass
    await _confirm_overs(context, cid, mid, overs)


async def overs_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Invited user tapped "Custom" — prompt them to type a number (1-20)."""
    q = update.callback_query
    parts = q.data.split("_"); mid, auid = int(parts[1]), int(parts[2])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not u or u.id != auid: await q.answer("Only the invited player can choose!"); return
    finally:
        session.close()
    await q.answer()
    context.bot_data[f"awaiting_overs_{q.from_user.id}"] = mid
    try:
        await q.edit_message_text(
            f"✍️ {_mention(q.from_user.id, fallback_name=(q.from_user.username or 'Player'))}, "
            f"reply with the number of overs (1-20):\n📝 e.g. <code>12</code>",
            parse_mode="HTML")
    except Exception:
        logger.exception("overs_custom_callback prompt failed")


async def _confirm_overs(context, cid, mid, overs):
    """Lock in the chosen overs, run the toss animation, and prompt the toss
    winner for a bat/bowl decision. Shared by the button and text-entry paths."""
    _cancel_overs_timer(context, mid)
    session = get_session()
    try:
        m = session.query(Match).get(mid)
        if not m or m.status != "accepted": return
        m.overs = overs; m.status = "toss"; session.commit()
        u1 = session.query(User).get(m.user1_id); u2 = session.query(User).get(m.user2_id)
        t1 = u1.team_name or f"@{u1.username}'s XI"; t2 = u2.team_name or f"@{u2.username}'s XI"

        w_coins = overs * 300; l_coins = overs * 150
        await context.bot.send_message(cid,
            f"✅ <b>MATCH CONFIRMED!</b>\n\n🏏 {t1} vs {t2}\n📍 {overs} Overs | {m.stadium}\n"
            f"📍 {m.pitch_type} | {m.weather} {m.temperature}°C\n🎩 {m.umpire1} | {m.umpire2}\n\n"
            f"🎁 <b>Rewards:</b>\n🏆 Winner: {w_coins:,} Coins + {overs} Gems\n"
            f"📉 Loser: {l_coins:,} Coins + {max(1,int(overs*0.5))} Gems\n\n🔄 Toss...", parse_mode="HTML")

        wid = random.choice([m.user1_id, m.user2_id]); m.toss_winner_id = wid; session.commit()
        w = session.query(User).get(wid)

        # ── Animated coin toss ──
        import asyncio as _asyncio
        toss_msg = await context.bot.send_message(cid,
            "🪙 <b>TOSS</b>\n\n<i>Calling captain to the centre...</i>", parse_mode="HTML")
        await _asyncio.sleep(0.7)

        # Spin frames
        spin_frames = [
            "🪙 <b>TOSS</b>\n\n     ⬆️\n   ╱  🪙  ╲\n\n<i>Captain flicks the coin into the air...</i>",
            "🪙 <b>TOSS</b>\n\n          🌀\n        🪙\n\n<i>It spins higher and higher...</i>",
            "🪙 <b>TOSS</b>\n\n     🌀 🪙 🌀\n\n<i>Tumbling end over end...</i>",
            "🪙 <b>TOSS</b>\n\n          ⬇️\n        🪙\n\n<i>Coming down now!</i>",
        ]
        for f in spin_frames:
            try:
                await toss_msg.edit_text(f, parse_mode="HTML")
            except Exception:
                pass
            await _asyncio.sleep(0.55)

        # Final reveal
        winner_name = w.username or w.first_name or "Captain"
        w_mention = _mention(w)
        try:
            await toss_msg.edit_text(
                f"🪙 <b>TOSS RESULT</b>\n\n"
                f"🏆 {w_mention} wins the toss!\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Pitch: <b>{m.pitch_type}</b> · 🌤️ {m.weather}\n"
                f"🏟️ {m.stadium}\n\n"
                f"<i>{_pitch_hint(m.pitch_type)}</i>",
                parse_mode="HTML")
        except Exception:
            pass
        await _asyncio.sleep(0.4)

        # Decision prompt
        await context.bot.send_message(cid,
            f"⚖️ {w_mention}, choose your call:\n\n"
            f"🏏 <b>Bat First:</b> Set a target on a {('fresh' if m.pitch_type in ('Flat','Hard') else 'tricky')} pitch\n"
            f"🎳 <b>Bowl First:</b> Pitch typically {('eases' if m.pitch_type == 'Green' else 'wears')} as match goes on",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"toss_bat_{mid}_{wid}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"toss_bowl_{mid}_{wid}")]]))
    except Exception: session.rollback(); logger.exception("Overs err")
    finally: session.close()


def _pitch_hint(pitch_type):
    """One-line tactical hint about the pitch."""
    return {
        "Flat":  "Batters' paradise — high scores expected.",
        "Hard":  "Bouncy, true bounce — rewards aggressive shots.",
        "Green": "Seam movement up front — bowlers will love early overs.",
        "Dry":   "Slow and low — tough to time the ball cleanly.",
        "Dusty": "Spinners will turn it square as it wears.",
    }.get(pitch_type, "A balanced wicket.")


# ═══════════════════════════ TOSS ════════════════════════════════════

async def toss_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user
    parts = q.data.split("_"); dec, mid, wid = parts[1], int(parts[2]), int(parts[3])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != wid: await q.answer("Toss winner only!"); return
        await q.answer(); m = session.query(Match).get(mid)
        if not m or m.status != "toss": await q.edit_message_text("❌ Error."); return
        m.toss_decision = dec
        if dec == "bat": m.batting_first_id = wid; m.bowling_first_id = m.user2_id if wid == m.user1_id else m.user1_id
        else: m.bowling_first_id = wid; m.batting_first_id = m.user2_id if wid == m.user1_id else m.user1_id
        m.status = "selecting"; session.commit()
        await q.edit_message_text(f"✅ @{u.username} elected to {'BAT' if dec=='bat' else 'BOWL'} FIRST")
        cid = q.message.chat_id
        bu = session.query(User).get(m.batting_first_id); bwu = session.query(User).get(m.bowling_first_id)
        bxi = _gxi(session, bu.id); bwxi = _gxi(session, bwu.id)
        bt = bu.team_name or f"@{bu.username}'s XI"; bwt = bwu.team_name or f"@{bwu.username}'s XI"
        bat_r = (session.query(UserRoster, Player).join(Player).filter(UserRoster.user_id == bu.id).order_by(UserRoster.order_position).limit(11).all())
        bowl_r = (session.query(UserRoster, Player).join(Player).filter(UserRoster.user_id == bwu.id).order_by(UserRoster.order_position).limit(11).all())
        await context.bot.send_message(cid, format_xi_text(bat_r, f"🏏 {bt} (Batting)", bu.captain_roster_id), parse_mode="HTML")
        await context.bot.send_message(cid, format_xi_text(bowl_r, f"🎳 {bwt} (Bowling)", bwu.captain_roster_id), parse_mode="HTML")
        context.bot_data[f"bat_xi_{mid}"] = bxi; context.bot_data[f"bowl_xi_{mid}"] = bwxi
        context.bot_data[f"bat_uname_{mid}"] = bu.username; context.bot_data[f"bowl_uname_{mid}"] = bwu.username
        context.bot_data[f"bat_uid_{mid}"] = bu.id; context.bot_data[f"bowl_uid_{mid}"] = bwu.id
        # Match style is a global website setting, not a per-match choice.
        # Default to the original Telegram callback flow so gameplay stays in
        # the bot unless an admin explicitly enables the Mini App board.
        from services.config_service import get_match_style
        force_cric_miniapp = context.bot_data.pop(f"cric_miniapp_{mid}", False)
        if force_cric_miniapp or get_match_style(session) == "webapp":
            m.status = "playing"; session.commit()
            try:
                from services.match_webapp_service import init_match_for_webapp
                init_match_for_webapp(session, mid)
            except Exception:
                logger.exception("webapp match init at toss failed")
            try:
                from services.match_broadcast import send_match_ready_message
                await send_match_ready_message(
                    context, cid, m, bt, bwt, _mention(bu), _mention(bwu))
            except Exception:
                logger.exception("match-ready mini app message failed")
        else:
            # Original bot gameplay: show all 11 players for opener selection.
            btns = [[InlineKeyboardButton(
                f"{p['name']} - {p['rating']} | {p['category']}",
                callback_data=f"op1_{mid}_{bu.id}_{p['roster_id']}")]
                for p in bxi]
            await context.bot.send_message(
                cid,
                f"🏏 <b>SELECT OPENER 1</b>\n\n{_mention(bu)}, pick the opening batter:",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception: session.rollback(); logger.exception("Toss err")
    finally: session.close()


# ═══════════════════════════ OPENERS ═════════════════════════════════

async def opener1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user
    parts = q.data.split("_"); mid, buid, rid = int(parts[1]), int(parts[2]), int(parts[3])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != buid: await q.answer("Not yours!"); return
        await q.answer()
        bxi = context.bot_data.get(f"bat_xi_{mid}", []); pk = next((p for p in bxi if p["roster_id"] == rid), None)
        if not pk: return
        context.bot_data[f"opener1_{mid}"] = pk
        # Show ALL remaining players for opener 2
        rem = [p for p in bxi if p["roster_id"] != rid]
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']} | {p['category']}", callback_data=f"op2_{mid}_{buid}_{p['roster_id']}")] for p in rem]
        u_mention = _mention(u)
        await q.edit_message_text(f"✅ Opener 1: {pk['name']}\n\n🏏 <b>SELECT OPENER 2</b>\n\n{u_mention}, pick the second opener:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception: logger.exception("Op1 err")
    finally: session.close()

async def opener2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user; cid = q.message.chat_id
    parts = q.data.split("_"); mid, buid, rid = int(parts[1]), int(parts[2]), int(parts[3])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != buid: await q.answer("Not yours!"); return
        await q.answer()
        bxi = context.bot_data.get(f"bat_xi_{mid}", []); pk = next((p for p in bxi if p["roster_id"] == rid), None)
        if not pk: return
        context.bot_data[f"opener2_{mid}"] = pk; op1 = context.bot_data.get(f"opener1_{mid}", {})
        await q.edit_message_text(f"✅ Openers: {op1.get('name')} & {pk['name']}\n\n⏳ Bowler...", parse_mode="HTML")

        # Get bowling user from bot_data (works for both innings)
        bowl_uid = context.bot_data.get(f"bowl_uid_{mid}")
        if bowl_uid:
            bwu = session.query(User).get(bowl_uid)
        else:
            # Fallback: 1st innings — read from Match record
            m = session.query(Match).get(mid)
            bwu = session.query(User).get(m.bowling_first_id)

        bwxi = context.bot_data.get(f"bowl_xi_{mid}", [])

        # ── If the bowling user is the bot (vsbot innings 2 with user batting),
        # auto-pick the bowler instead of showing a picker tagged to the bot
        # (the bot can't click buttons; it'd freeze the match).
        if bwu and bwu.telegram_id == BOT_TG_ID_:
            # Auto-pick best bowler (highest bowl_rating), avoiding prev_bowler if set
            existing = _gs(context, mid)
            prev_rid = (existing or {}).get("prev_bowler_rid")
            candidates = [b for b in bwxi
                          if b.get("roster_id") != prev_rid] or bwxi
            opening_bowler = max(candidates, key=lambda p: p.get("bowl_rating", 0))

            if existing and existing.get("innings") == 2:
                # Wire up the state for innings 2 startup, same as
                # select_bowler_callback does for the human case.
                s = existing
                s["current_bowler"] = opening_bowler
                # Rebuild batting_order with user-selected openers at index 0/1
                order = [op1, pk]
                for p in s["bat_xi"]:
                    if p["roster_id"] not in (op1.get("roster_id"),
                                                pk.get("roster_id")):
                        order.append(p)
                s["batting_order"] = order
                s["striker_idx"] = 0
                s["non_striker_idx"] = 1
                s["next_batsman_idx"] = 2
                s["prev_bowler_rid"] = None
                s["selected_variation"] = None
                _ss(context, mid, s, next_action=A_PICK_DELIVERY)

                await context.bot.send_message(
                    cid,
                    f"🤖 Opening bowler: <b>{opening_bowler['name']}</b>",
                    parse_mode="HTML")
                await context.bot.send_message(
                    cid,
                    f"🏏 <b>2ND INNINGS!</b>\n\n"
                    f"🟢 {s['bat_team_name']} needs {s['target']} to win\n"
                    f"🏏 {op1.get('name', '?')} & {pk['name']}\n"
                    f"🎳 {opening_bowler['name']}\n━━━━━━━━━━━━━━━━━━━",
                    parse_mode="HTML")
                await _send_batsman_card(context, cid, op1, s["bat_team_id"])
                await _send_batsman_card(context, cid, pk, s["bat_team_id"])
                await _send_bowler_card(context, cid, opening_bowler, s["bowl_team_id"])
                await render_screen(context, mid)
                return
            # If this is innings 1 (shouldn't happen — vsbot innings 1 has its own
            # path in handlers/vsbot.py) we fall through to the normal picker.

        # Otherwise: human bowling user, show ALL 11 bowlers sorted by bowl rating
        all_bowlers = sorted(bwxi, key=lambda x: x["bowl_rating"], reverse=True)
        btns = [[InlineKeyboardButton(
            f"{p['name']} | {p.get('bowl_hand','R')[:1]}-{p.get('bowl_style','Medium')} | BWL {p['bowl_rating']}",
            callback_data=f"selbowl_{mid}_{bwu.id}_{p['roster_id']}"
        )] for p in all_bowlers]
        bwu_mention = _mention(bwu)
        await context.bot.send_message(cid, f"🎳 <b>SELECT OPENING BOWLER</b>\n\n{bwu_mention}, pick your opening bowler:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception: logger.exception("Op2 err")
    finally: session.close()


# ═══════════════════════════ FIRST BOWLER → START ════════════════════

async def select_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user; cid = q.message.chat_id
    parts = q.data.split("_"); mid, bwuid, rid = int(parts[1]), int(parts[2]), int(parts[3])
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u or u.id != bwuid: await q.answer("Not yours!"); return
        await q.answer()

        bwxi = context.bot_data.get(f"bowl_xi_{mid}", [])
        bowler = next((p for p in bwxi if p["roster_id"] == rid), None)
        if not bowler: return

        existing_state = _gs(context, mid)

        if existing_state and existing_state.get("innings") == 2:
            # 2nd innings — update existing state with new bowler
            s = existing_state
            s["current_bowler"] = bowler
            s["batting_order"] = list(s["bat_xi"])  # reset batting order
            # Re-apply openers
            op1 = context.bot_data.get(f"opener1_{mid}", {})
            op2 = context.bot_data.get(f"opener2_{mid}", {})
            order = [op1, op2]
            for p in s["bat_xi"]:
                if p["roster_id"] not in (op1.get("roster_id"), op2.get("roster_id")):
                    order.append(p)
            s["batting_order"] = order
            s["striker_idx"] = 0; s["non_striker_idx"] = 1; s["next_batsman_idx"] = 2
            s["prev_bowler_rid"] = None
            s["selected_variation"] = None
            _ss(context, mid, s, next_action=A_PICK_DELIVERY)

            await q.edit_message_text(f"✅ Bowler: {bowler['name']}\n\n⏳ 2nd Innings Starting...", parse_mode="HTML")
            await context.bot.send_message(cid,
                f"🏏 <b>2ND INNINGS!</b>\n\n"
                f"🟢 {s['bat_team_name']} needs {s['target']} to win\n"
                f"🏏 {op1.get('name', '?')} & {op2.get('name', '?')}\n🎳 {bowler['name']}\n━━━━━━━━━━━━━━━━━━━",
                parse_mode="HTML")
            # Send opener cards for 2nd innings
            await _send_batsman_card(context, cid, op1, s["bat_team_id"])
            await _send_batsman_card(context, cid, op2, s["bat_team_id"])
            await _send_bowler_card(context, cid, bowler, s["bowl_team_id"])
        else:
            # 1st innings — create fresh state
            m = session.query(Match).get(mid); m.status = "playing"; session.commit()
            bxi = context.bot_data.get(f"bat_xi_{mid}", [])
            op1 = context.bot_data.get(f"opener1_{mid}", {}); op2 = context.bot_data.get(f"opener2_{mid}", {})
            bat_uid = context.bot_data.get(f"bat_uid_{mid}", m.batting_first_id)
            bowl_uid_db = context.bot_data.get(f"bowl_uid_{mid}", m.bowling_first_id)
            bu = session.query(User).get(bat_uid); bwu = session.query(User).get(bowl_uid_db)
            bt = bu.team_name or f"@{bu.username}'s XI"; bwt = bwu.team_name or f"@{bwu.username}'s XI"

            s = create_match_state(mid, m.overs, bat_uid, bowl_uid_db, bxi, bwxi, op1, op2, bowler)
            s["chat_id"] = cid; s["bat_user_tg"] = bu.telegram_id; s["bowl_user_tg"] = bwu.telegram_id
            s["bat_team_name"] = bt; s["bowl_team_name"] = bwt
            s["bat_username"] = bu.username; s["bowl_username"] = bwu.username
            s["pitch_type"] = m.pitch_type
            if context.bot_data.get(f"challenge_{mid}"):
                s["wicket_limit"] = 2
                s["is_challenge"] = True
            # Persist initial state with PICK_DELIVERY action
            _ss(context, mid, s, next_action=A_PICK_DELIVERY)

            await q.edit_message_text(f"✅ Bowler: {bowler['name']}\n\n⏳ Starting...", parse_mode="HTML")
            await context.bot.send_message(cid,
                f"🏏 <b>MATCH STARTING!</b>\n\n🏟️ {m.stadium}\n{bt} vs {bwt} | {m.overs} Overs\n"
                f"🏏 {op1['name']} & {op2['name']}\n🎳 {bowler['name']}\n━━━━━━━━━━━━━━━━━━━",
                parse_mode="HTML")
            # Send opener cards
            await _send_batsman_card(context, cid, op1, s["bat_team_id"])
            await _send_batsman_card(context, cid, op2, s["bat_team_id"])
            await _send_bowler_card(context, cid, bowler, s["bowl_team_id"])

        await render_screen(context, mid)
    except Exception: session.rollback(); logger.exception("SelBowl err")
    finally: session.close()


# ═══════════════════════════ RENDER DISPATCHER ════════════════════════
#
# render_screen(ctx, mid) is the SINGLE function any code path calls when
# it needs to "show whatever screen the user should see now". It reads
# next_action from the persistent store and dispatches to the right renderer.
#
# Code paths that call this:
#   - shot_callback (after processing a ball)
#   - length_callback / spinner_delivery_callback (after delivery picked)
#   - new_batsman_callback / new_over_bowler_callback (after picker resolved)
#   - resume_handler (/r command)
#   - heartbeat (every 30s for stuck matches)
#
# The internal _show_* functions are the actual renderers.
# DO NOT call _show_* functions directly outside of render_screen anymore —
# they assume the state has already been validated.


async def render_screen(ctx, mid):
    """SINGLE dispatcher — reads next_action and renders the correct screen.

    Returns True if a screen was sent, False otherwise. Always best-effort —
    never raises. Always releases the processing lock first.

    State machine actions handled:
      A_PICK_DELIVERY   → _show_delivery (or vsbot AI if bot bowling)
      A_PICK_LENGTH     → _show_length_picker
      A_PICK_SHOT       → _show_shot (or vsbot AI if bot batting)
      A_PICK_NEW_BATSMAN→ _show_new_batsman (or vsbot AI)
      A_PICK_NEW_BOWLER → _show_new_over_bowler (or vsbot AI)
      A_INNINGS_BREAK   → _end_innings
      A_COMPLETED       → no-op
    """
    s = _gs(ctx, mid)
    if not s:
        return False

    cid = s["chat_id"]

    # Always release any stale processing lock first
    ctx.bot_data.pop(f"processing_{mid}", None)

    try:
        # Always check innings-over first (regardless of next_action)
        if is_innings_over(s):
            await _end_innings(ctx, mid)
            return True

        next_act = get_next_action(ctx, mid) or A_PICK_DELIVERY

        # vsbot first: if bot is the actor for this action, route to AI
        if s.get("is_vsbot"):
            from handlers.vsbot import vsbot_auto_continue
            handled = await vsbot_auto_continue(ctx, mid)
            if handled:
                return True

        # Dispatch by action
        if next_act == A_PICK_NEW_BATSMAN:
            await _show_new_batsman(ctx, mid)
        elif next_act == A_PICK_NEW_BOWLER:
            await _show_new_over_bowler(ctx, mid)
        elif next_act == A_PICK_SHOT:
            await _show_shot(ctx, cid, mid)
        elif next_act == A_PICK_LENGTH:
            if s.get("selected_variation"):
                await _show_length_picker(ctx, cid, mid)
            else:
                # Inconsistent state — fall back to fresh ball
                _ss(ctx, mid, s, next_action=A_PICK_DELIVERY)
                await _show_delivery(ctx, cid, mid)
        elif next_act == A_COMPLETED:
            return False  # nothing to render
        else:
            # Default: PICK_DELIVERY (or unknown — start of new ball)
            s["current_delivery"] = None
            s["selected_variation"] = None
            _ss(ctx, mid, s, next_action=A_PICK_DELIVERY)
            await _show_delivery(ctx, cid, mid)

        return True

    except Exception:
        logger.exception(f"render_screen failed for match {mid}")
        return False


async def _safe_show_next(ctx, mid):
    """Backwards-compat alias — calls the new dispatcher.
    Kept so existing code paths still work without rewriting every call site.
    """
    return await render_screen(ctx, mid)


async def _safe_show_next_OLD(ctx, mid):  # noqa
    """Old implementation kept for reference. Not called.

    The new render_screen() dispatcher is the canonical path.
    """
    return False


# ── Legacy code from the old recovery function below was removed. ──
# (The new render_screen() above replaces it.)


async def _show_length_picker(ctx, cid, mid):
    """Re-render the length picker for a state where variation was picked but length wasn't.
    Used by render_screen when next_action == PICK_LENGTH.
    """
    s = _gs(ctx, mid)
    if not s: return
    var = s.get("selected_variation")
    if not var:
        # No variation set — fall back to delivery
        await _show_delivery(ctx, cid, mid)
        return
    bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
    ls = opts["lengths"]; btns = []; row = []
    for i, l in enumerate(ls):
        row.append(InlineKeyboardButton(l, callback_data=f"blen_{mid}_{i}"))
        if len(row) == 3: btns.append(row); row = []
    if row: btns.append(row)
    try:
        sent = await ctx.bot.send_message(cid,
            f"🎳 <b>SELECT LENGTH</b> ({var})", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns))
        if sent and hasattr(sent, "message_id"):
            _ss(ctx, mid, s, last_prompt_msg_id=sent.message_id)
        _start_action_timer(ctx, mid, s["bowl_user_tg"], "select length")
    except Exception:
        logger.exception(f"_show_length_picker failed for match {mid}")
        _schedule_recovery(ctx, mid, "length picker")


def _schedule_recovery(ctx, mid, where, delay=1.0):
    """Retry a failed match prompt automatically without requiring `/r`."""
    key = f"match_recovery_{mid}"
    existing = ctx.bot_data.get(key)
    if existing and not existing.done():
        return existing

    async def _runner():
        await asyncio.sleep(delay)
        for attempt in range(3):
            if await _safe_show_next(ctx, mid):
                return
            await asyncio.sleep(1.0 + attempt)
        state = _gs(ctx, mid)
        if state:
            try:
                await ctx.bot.send_message(
                    state["chat_id"],
                    "⚠️ Still reconnecting the match automatically. Please wait a moment; "
                    "your current turn is saved.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    task = asyncio.create_task(_runner(), name=key)
    ctx.bot_data[key] = task

    def _cleanup(done_task):
        if ctx.bot_data.get(key) is done_task:
            ctx.bot_data.pop(key, None)

    task.add_done_callback(_cleanup)
    return task


async def _recover_stuck(ctx, mid, where):
    """Auto-recover from a stuck callback. Tries to re-render the correct screen.
    Falls back to a help message if recovery fails.
    """
    s = _gs(ctx, mid)
    if not s:
        return

    # Always clear the lock first
    ctx.bot_data.pop(f"processing_{mid}", None)

    success = await _safe_show_next(ctx, mid)
    if not success:
        _schedule_recovery(ctx, mid, where)
        try:
            await ctx.bot.send_message(
                s["chat_id"],
                f"⚠️ Match hit a hiccup ({where}). Reconnecting automatically…",
                parse_mode="HTML")
        except Exception:
            pass


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume a stuck match — finds the live match for this chat and re-renders buttons."""
    cid = update.effective_chat.id

    # Find any ms_* state with this chat_id
    found_mid = None
    for k, v in list(context.bot_data.items()):
        if k.startswith("ms_") and isinstance(v, dict) and v.get("chat_id") == cid:
            try:
                found_mid = int(k.split("_", 1)[1])
                break
            except (ValueError, IndexError):
                continue

    if not found_mid:
        await update.message.reply_text("❌ No active match in this chat to resume.")
        return

    await update.message.reply_text("🔄 <b>Resuming match...</b>", parse_mode="HTML")

    success = await _safe_show_next(context, found_mid)
    if not success:
        await update.message.reply_text(
            "⚠️ Could not reconnect immediately. Automatic recovery is still active.\n"
            "Please wait a moment, or use /endmatch to end the match (fine applies).")


# ═══════════════════════════ DELIVERY ════════════════════════════════

async def _show_delivery(ctx, cid, mid):
    s = _gs(ctx, mid)
    if not s: return
    try:
        bw = get_bowler(s); st = get_striker(s); ph = get_phase(s)
        ov = s["current_over"]; bl = s["current_ball"] + 1
        opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
        bowl_mention = _mention(s.get("bowl_user_tg"), fallback_name=s.get("bowl_username") or "Bowler")
        hdr = (f"🎳 <b>OVER {ov} • BALL {bl}</b>\n\n📊 {format_score(s)} | {format_overs(s)} ov | CRR {crr(s)}\n\n"
               f"🎳 {bw['name']} ({bw['bowl_rating']} BWL)\n🏏 vs {st['name']} ({st['bat_rating']} BAT)\n📍 {ph}\n\n"
               f"━━━━━━━━━━━━━━━━━━━\n\n{bowl_mention}, choose your delivery:\n\n")
        if opts["is_spinner"]:
            ds = opts["deliveries"]; btns = []; row = []
            for i, d in enumerate(ds):
                row.append(InlineKeyboardButton(d, callback_data=f"bspin_{mid}_{i}"))
                if len(row) == 3: btns.append(row); row = []
            if row: btns.append(row)
            text_to_send = hdr + "🎯 <b>SELECT DELIVERY</b>"
        else:
            vs = opts["variations"]; btns = []; row = []
            for i, v in enumerate(vs):
                row.append(InlineKeyboardButton(v, callback_data=f"bvar_{mid}_{i}"))
                if len(row) == 3: btns.append(row); row = []
            if row: btns.append(row)
            text_to_send = hdr + "🎯 <b>SELECT VARIATION</b>"

        # Send with retry-once
        try:
            await ctx.bot.send_message(cid, text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e1:
            logger.warning(f"_show_delivery first attempt failed: {e1}")
            import asyncio
            await asyncio.sleep(0.5)
            await ctx.bot.send_message(cid, text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))

        _start_action_timer(ctx, mid, s["bowl_user_tg"], "select delivery")
    except Exception:
        logger.exception(f"_show_delivery failed for match {mid}")
        try:
            await ctx.bot.send_message(
                cid,
                "⚠️ Couldn't show delivery buttons. Retrying automatically…",
                parse_mode="HTML")
            _schedule_recovery(ctx, mid, "delivery prompt")
        except Exception:
            pass

async def variation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, vi = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not your bowl!"); return

    from services.match_state_store import get_match_lock
    lock = get_match_lock(mid)

    async with lock:
        try:
            from services.match_state_store import get_next_action
            if get_next_action(context, mid) != A_PICK_DELIVERY:
                try: await q.answer("⏳ Already processed")
                except Exception: pass
                return

            await q.answer()
            _cancel_action_timer(context, mid)
            try: await q.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
            bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
            var = opts["variations"][vi]; s["selected_variation"] = var
            _ss(context, mid, s, next_action=A_PICK_LENGTH)
            ls = opts["lengths"]; btns = []; row = []
            for i, l in enumerate(ls):
                row.append(InlineKeyboardButton(l, callback_data=f"blen_{mid}_{i}"))
                if len(row) == 3: btns.append(row); row = []
            if row: btns.append(row)
            sent_msg = None
            try:
                sent_msg = await q.edit_message_text(f"🎳 <b>SELECT LENGTH</b> ({var})", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            except Exception:
                sent_msg = await context.bot.send_message(s["chat_id"],
                    f"🎳 <b>SELECT LENGTH</b> ({var})",
                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
            if sent_msg and hasattr(sent_msg, "message_id"):
                _ss(context, mid, s, last_prompt_msg_id=sent_msg.message_id)
            _start_action_timer(context, mid, s["bowl_user_tg"], "select length")
        except Exception:
            logger.exception(f"variation_callback failed mid={mid}")
            await _recover_stuck(context, mid, "variation")


async def length_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, li = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return

    from services.match_state_store import get_match_lock
    lock = get_match_lock(mid)

    async with lock:
        try:
            # Already moved past length picking? drop duplicate click
            from services.match_state_store import get_next_action
            if get_next_action(context, mid) != A_PICK_LENGTH:
                try: await q.answer("⏳ Already processed")
                except Exception: pass
                return

            await q.answer()
            _cancel_action_timer(context, mid)
            try: await q.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
            bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
            length = opts["lengths"][li]; var = s.get("selected_variation", "Seam")
            s["current_delivery"] = f"{var} {length}"; s["selected_variation"] = None
            _ss(context, mid, s, next_action=A_PICK_SHOT)
            try:
                await q.edit_message_text(f"✅ {bw['name']}: {var} {length}\n⏳ Batsman...", parse_mode="HTML")
            except Exception: pass
        except Exception:
            logger.exception(f"length_callback failed mid={mid}")
            await _recover_stuck(context, mid, "length")
            return

    # Outside lock — route to next step
    try:
        await render_screen(context, mid)
    except Exception:
        logger.exception(f"length_callback routing failed mid={mid}")
        await _recover_stuck(context, mid, "length-route")


async def spinner_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, di = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return

    from services.match_state_store import get_match_lock
    lock = get_match_lock(mid)

    async with lock:
        try:
            from services.match_state_store import get_next_action
            if get_next_action(context, mid) != A_PICK_DELIVERY:
                try: await q.answer("⏳ Already processed")
                except Exception: pass
                return

            await q.answer()
            _cancel_action_timer(context, mid)
            try: await q.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
            bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
            d = opts["deliveries"][di]
            if d == "Surprise":
                ns = [x for x in opts["deliveries"] if x != "Surprise"]
                d = random.choice(ns) + " (Surprise)"
            s["current_delivery"] = d
            _ss(context, mid, s, next_action=A_PICK_SHOT)
            try:
                await q.edit_message_text(f"✅ {bw['name']}: {d}\n⏳ Batsman...", parse_mode="HTML")
            except Exception: pass
        except Exception:
            logger.exception(f"spinner_delivery_callback failed mid={mid}")
            await _recover_stuck(context, mid, "spinner_delivery")
            return

    # Outside lock — route
    try:
        await render_screen(context, mid)
    except Exception:
        logger.exception(f"spinner_delivery routing failed mid={mid}")
        await _recover_stuck(context, mid, "spinner-delivery-route")


# ═══════════════════════════ SHOT ════════════════════════════════════

async def _show_shot(ctx, cid, mid):
    """Show shot selection buttons. Resilient — retries once on Telegram failure."""
    s = _gs(ctx, mid)
    if not s:
        return
    try:
        st = get_striker(s); bw = get_bowler(s); dl = s.get("current_delivery", "?")
        bs = s["bat_stats"][st["roster_id"]]
        bat_mention = _mention(s.get("bat_user_tg"), fallback_name=s.get("bat_username") or "Batsman")
        txt = (f"🏏 <b>OVER {s['current_over']} • BALL {s['current_ball'] + 1}</b>\n\n"
               f"📊 {format_score(s)} | {format_overs(s)} ov | CRR {crr(s)}\n\n"
               f"🎳 {bw['name']}: {dl}\n🏏 {st['name']} ({st['bat_rating']} BAT) — {bs['runs']}({bs['balls']})\n\n"
               f"━━━━━━━━━━━━━━━━━━━\n\n{bat_mention}, play your shot:")
        btns = []; row = []
        for i, sh in enumerate(AVAILABLE_SHOTS):
            row.append(InlineKeyboardButton(sh, callback_data=f"bshot_{mid}_{i}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row)

        # Try once; if it fails, retry once with a small delay
        try:
            await ctx.bot.send_message(cid, txt, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e1:
            logger.warning(f"_show_shot first attempt failed: {e1}")
            import asyncio
            await asyncio.sleep(0.5)
            await ctx.bot.send_message(cid, txt, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))

        _start_action_timer(ctx, mid, s["bat_user_tg"], "choose shot")
    except Exception:
        logger.exception(f"_show_shot failed for match {mid}")
        # Last-ditch: send a plain text fallback so user knows what to do
        try:
            await ctx.bot.send_message(
                cid,
                "⚠️ Couldn't show shot buttons. Retrying automatically…",
                parse_mode="HTML")
            _schedule_recovery(ctx, mid, "shot prompt")
        except Exception:
            pass


async def _process_shot_core(context, mid, si, *, q=None):
    """Single shared implementation for both user and bot shot processing.

    Wrapped in an asyncio.Lock per match — eliminates double-click races.
    Uses ball_seq idempotency — even if a click somehow doubles, the second
    one sees the seq advanced and aborts cleanly.

    Args:
      context: telegram context
      mid: match id
      si: shot index (into AVAILABLE_SHOTS)
      q: callback query (None for bot processing)
    """
    from services.match_state_store import get_match_lock, get_ball_seq

    lock = get_match_lock(mid)

    # Capture the ball sequence BEFORE we acquire the lock. If anyone else
    # advances it while we wait, our click was a stale duplicate and we abort.
    seq_before = get_ball_seq(mid)

    async with lock:
        # Idempotency check — if seq advanced while we waited for the lock,
        # another handler already processed this ball. Drop the duplicate.
        seq_now = get_ball_seq(mid)
        if seq_now != seq_before:
            if q is not None:
                try: await q.answer("⏳ Already processed")
                except Exception: pass
            return

        # Acknowledge click + remove buttons immediately for snappy UX
        if q is not None:
            try: await q.answer()
            except Exception: pass
            try: await q.edit_message_reply_markup(reply_markup=None)
            except Exception: pass

        s = _gs(context, mid)
        if not s:
            return

        _cancel_action_timer(context, mid)

        try:
            shot = AVAILABLE_SHOTS[si]
            dl = s.get("current_delivery", "?")
            striker = get_striker(s)
            bowler = get_bowler(s)

            # Defensive init
            if striker["roster_id"] not in s["bat_stats"]:
                s["bat_stats"][striker["roster_id"]] = {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                    "out": False, "how_out": "", "bowled_by": ""
                }
            bs = s["bat_stats"][striker["roster_id"]]
            bws = s["bowl_stats"].setdefault(bowler["roster_id"], {
                "balls": 0, "runs": 0, "wickets": 0,
                "overs_done": 0, "this_over_balls": 0,
            })

            # Snapshot pre-ball values for milestone detection (fifty/hundred)
            prev_bat_runs = bs.get("runs", 0)

            oc = _calc(s, striker, bowler, shot, dl)
            legal = True
            need_new_bat = False

            # Apply outcome
            if oc["type"] == "wide":
                s["total_runs"] += 1; s["extras_total"] += 1; s["wides"] += 1; bws["runs"] += 1
                bws["this_over_runs"] = bws.get("this_over_runs", 0) + 1
                add_to_timeline(s, SYM["WD"]); legal = False
                rtxt = "↔️ <b>WIDE!</b> +1"
            elif oc["type"] == "noball":
                runs = oc.get("runs", 1); s["total_runs"] += runs + 1; s["extras_total"] += 1; s["noballs"] += 1
                bws["runs"] += runs + 1; bs["balls"] += 1
                bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs + 1
                if runs > 0: bs["runs"] += runs
                add_to_timeline(s, SYM["NB"] + (SYM.get(runs, str(runs)) if runs > 0 else "")); legal = False
                rtxt = f"🄽🄱 <b>NO BALL!</b> +{runs + 1}"
            elif oc["type"] == "legbye":
                runs = oc.get("runs", 1); s["total_runs"] += runs; s["extras_total"] += runs; s["legbyes"] += runs
                bws["runs"] += runs; bs["balls"] += 1
                bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
                s["partnership_balls"] += 1; s["partnership_runs"] += runs
                add_to_timeline(s, str(runs) + " 𓂾" if runs > 1 else "𓂾")
                rtxt = f"𓂾 <b>LEG BYE!</b> +{runs}"
                if runs % 2 == 1:
                    s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]
            elif oc["type"] == "wicket":
                runs = oc.get("runs", 0); s["total_runs"] += runs; s["total_wickets"] += 1
                bws["wickets"] += 1; bws["runs"] += runs; bs["balls"] += 1; bs["out"] = True
                bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
                how_raw = oc.get("how", "Bowled")
                bs["how_out"] = how_raw
                bs["bowled_by"] = bowler["name"]
                # Build the full cricket-style dismissal string NOW (with random
                # catcher/keeper/fielder) so it stays stable across re-renders.
                bs["dismissal_text"] = _format_dismissal(
                    how_raw, bowler["name"], s.get("bowl_xi", []))
                add_to_timeline(s, SYM["W"])
                s["partnership_runs"] = 0; s["partnership_balls"] = 0
                need_new_bat = True
                if "fow" not in s:
                    s["fow"] = []
                over_now = s["current_over"] - 1
                ball_now = s["current_ball"] + 1
                if ball_now >= 6:
                    over_now += 1; ball_now = 0
                fow_over = f"{over_now}.{ball_now}" if ball_now else str(over_now)
                s["fow"].append((s["total_runs"], fow_over))
                rtxt = f"🟥 <b>WICKET!</b> {striker['name']} — {oc.get('how', 'OUT')}!"
            else:
                runs = oc.get("runs", 0); s["total_runs"] += runs; bs["runs"] += runs; bs["balls"] += 1
                bws["runs"] += runs; s["partnership_runs"] += runs; s["partnership_balls"] += 1
                bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
                if runs == 4: bs["fours"] += 1
                elif runs == 6: bs["sixes"] += 1
                add_to_timeline(s, SYM.get(runs, str(runs)))
                if runs == 0:
                    rtxt = "0️⃣ <b>DOT!</b>"
                elif runs == 4:
                    rtxt = "4️⃣ <b>FOUR!</b> 🔥"
                elif runs == 6:
                    rtxt = "6️⃣ <b>SIX!</b> 💥"
                else:
                    rtxt = f"{SYM.get(runs, str(runs))} <b>{runs} RUN{'S' if runs != 1 else ''}!</b>"
                if runs % 2 == 1:
                    s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]

            if legal:
                s["current_ball"] += 1
                bws["this_over_balls"] += 1
                bws["balls"] = bws.get("balls", 0) + 1

            eoo = False
            is_maiden = False
            if s["current_ball"] >= 6:
                bws["overs_done"] += 1
                bws["this_over_balls"] = 0
                # Maiden over: 0 runs conceded across all 6 legal balls
                over_runs_scored = bws.get("this_over_runs", 0)
                if over_runs_scored == 0:
                    bws["maidens"] = bws.get("maidens", 0) + 1
                    is_maiden = True
                # Track over-by-over runs for Manhattan chart
                if "over_runs" not in s:
                    s["over_runs"] = []
                s["over_runs"].append(over_runs_scored)
                bws["this_over_runs"] = 0  # reset for next over
                s["current_over"] += 1
                s["current_ball"] = 0
                s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]
                s["prev_bowler_rid"] = bowler["roster_id"]
                eoo = True

            # Build the result message
            sc = build_live_scorecard(s)
            traits_line = ""
            activated = oc.get("traits_activated") or []
            if activated:
                unique_act = list(dict.fromkeys(activated))[:3]
                traits_line = "\n💎 " + " · ".join(unique_act)
            commentary_line = _maybe_pick_commentary(oc, striker, bowler,
                                                     runs_for_commentary=oc.get("runs", 0))
            commentary_block = f"\n💬 <i>{commentary_line}</i>" if commentary_line else ""
            head = (
                f"🎳 {bowler['name']} → {dl}\n"
                f"🏏 {striker['name']} played {shot}\n\n"
                f"{rtxt}{commentary_block}{traits_line}\n\n"
                f"{sc}"
            )

            # Reset transient state
            s["current_delivery"] = None
            s["selected_variation"] = None

            # Determine canonical next_action
            if is_innings_over(s):
                next_act = A_INNINGS_BREAK
            elif need_new_bat and s["total_wickets"] < s.get("wicket_limit", 10):
                next_act = A_PICK_NEW_BATSMAN
            elif eoo:
                next_act = A_PICK_NEW_BOWLER
            else:
                next_act = A_PICK_DELIVERY

            # Persist state + advance ball seq atomically (still inside lock)
            _ss(context, mid, s, next_action=next_act)
            increment_ball_seq(context, mid)

            # Send result message (lock still held, but send is fast — no artificial delay)
            try:
                if q is not None:
                    try:
                        await q.edit_message_text(head, parse_mode="HTML")
                    except Exception:
                        await context.bot.send_message(s["chat_id"], head, parse_mode="HTML")
                else:
                    await context.bot.send_message(s["chat_id"], head, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send scorecard update")

            # ── Fire event media (GIFs) for celebratory events ──
            # Wrapped to NEVER break the match flow if media misbehaves.
            try:
                from services.event_media_service import fire_event_media
                runs_this_ball = oc.get("runs", 0)
                cur_bat_runs = bs.get("runs", 0)
                if oc["type"] == "wicket":
                    await fire_event_media(context, s["chat_id"], "wicket")
                elif runs_this_ball == 6:
                    await fire_event_media(context, s["chat_id"], "six")
                elif runs_this_ball == 4:
                    await fire_event_media(context, s["chat_id"], "four")
                # Milestone detection — striker's runs crossed 50 or 100
                if prev_bat_runs < 50 <= cur_bat_runs:
                    await fire_event_media(context, s["chat_id"], "fifty")
                elif prev_bat_runs < 100 <= cur_bat_runs:
                    await fire_event_media(context, s["chat_id"], "century")
                # Maiden over at end-of-over
                if eoo and is_maiden:
                    await fire_event_media(context, s["chat_id"], "maiden_over")
            except Exception:
                logger.exception("event media hook failed (non-fatal)")

        except Exception:
            logger.exception(f"_process_shot_core FATAL for match {mid}")
            await _recover_stuck(context, mid, "shot processing")
            return

    # ── Lock released here ──
    # Route to next step (outside lock so render can do its own work)
    try:
        if is_innings_over(s):
            await _end_innings(context, mid)
            return

        if s.get("is_vsbot"):
            from handlers.vsbot import vsbot_auto_continue
            handled = await vsbot_auto_continue(context, mid)
            if handled:
                return

        await render_screen(context, mid)
    except Exception:
        logger.exception(f"Next-step routing failed for match {mid}")
        await _recover_stuck(context, mid, "next-step")


async def shot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User clicked a shot button. Validates ownership, then defers to core."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        mid, si = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        await q.answer("Invalid")
        return

    s = _gs(context, mid)
    if not s or tg.id != s["bat_user_tg"]:
        await q.answer("Not your bat!")
        return

    await _process_shot_core(context, mid, si, q=q)


async def _bot_process_shot(context, mid, si):
    """Programmatic shot — used by vsbot when bot is batting."""
    await _process_shot_core(context, mid, si, q=None)


def _get_roster_traits(roster_id):
    """Fetch active traits for a roster entry. Returns list of trait dicts
    formatted for the probability engine.
    """
    if not roster_id:
        return []
    session = get_session()
    try:
        from models import PlayerTrait, Trait
        rows = (session.query(PlayerTrait, Trait)
                .join(Trait, PlayerTrait.trait_id == Trait.id)
                .filter(PlayerTrait.roster_id == roster_id,
                        Trait.is_active == True)
                .all())
        return [
            {
                "effect_key": t.effect_key,
                "level": pt.level,
                "display_name": t.name,
                "emoji": t.emoji,
                "category": t.category,
            }
            for pt, t in rows
        ]
    except Exception:
        logger.exception(f"Failed to fetch traits for roster {roster_id}")
        return []
    finally:
        session.close()


def _maybe_pick_commentary(oc, striker, bowler, runs_for_commentary=0):
    """Pick a single commentary line for this ball based on outcome type.
    Returns None if no commentary configured."""
    try:
        otype = oc.get("type")
        runs = oc.get("runs", 0)

        # Map outcome to event_key
        if otype == "wicket":
            how = (oc.get("how") or "").lower()
            if "bowled" in how:
                key = "wicket_bowled"
            elif "lbw" in how:
                key = "wicket_lbw"
            elif "stump" in how:
                key = "wicket_stumped"
            elif "keeper" in how or "kept" in how:
                key = "wicket_caught_keeper"
            elif "caught" in how or "catch" in how:
                key = "wicket_caught_fielder"
            else:
                key = "wicket_bowled"
        elif otype == "wide" or otype == "noball" or otype == "legbye":
            key = "extras"
        elif otype == "runs":
            if runs == 0:   key = "dot"
            elif runs == 1: key = "one"
            elif runs == 2: key = "two"
            elif runs == 3: key = "three"
            elif runs == 4: key = "four"
            elif runs == 6: key = "six"
            else: key = "dot"  # fallback
        else:
            key = "general"

        from database import get_session
        from services.commentary_service import pick_commentary
        ses = get_session()
        try:
            return pick_commentary(
                ses, key,
                batsman=striker.get("name", ""),
                bowler=bowler.get("name", ""),
                runs=runs_for_commentary,
            )
        finally:
            ses.close()
    except Exception:
        return None


def _calc(s, striker, bowler, shot, delivery):
    from services.probability_engine import calculate_outcome
    # Parse delivery into variation + length
    parts = delivery.replace(" (Surprise)", "").strip()
    from services.bowling_service import is_spinner as _is_spin
    if _is_spin(bowler.get("bowl_style", "")):
        variation = parts
        length = None
    else:
        known_lengths = {"Hard", "Good", "Full", "Yorker", "Bouncer",
                         "Good Length", "Full Length", "Short of Length", "Back of Length",
                         "Hit the Deck"}
        variation = parts
        length = None
        for ln in sorted(known_lengths, key=len, reverse=True):
            if parts.endswith(ln):
                variation = parts[:len(parts) - len(ln)].strip()
                length = ln
                break
        if not length:
            words = parts.rsplit(" ", 1)
            if len(words) == 2:
                variation, length = words
            else:
                variation = parts
                length = "Good"

    pitch = s.get("pitch_type", "Flat")
    over = s["current_over"]
    total_overs = s["overs"]
    innings = s.get("innings", 1)

    # Compute pitch wear for this ball
    from services.probability_engine import calc_pitch_wear
    pitch_wear = calc_pitch_wear(innings, over, total_overs)

    # Apply player form (modifies effective rating)
    from services.form_service import compute_form_score, form_to_rating_mod
    bat_form_mod = 0.0
    bowl_form_mod = 0.0
    try:
        # Find the owning user_id for striker/bowler (positive roster_id = real)
        striker_rid = striker.get("roster_id", 0)
        bowler_rid = bowler.get("roster_id", 0)
        if striker_rid > 0:
            from database import get_session as _gs_db
            from models import UserRoster as _UR
            ses = _gs_db()
            try:
                ur = ses.query(_UR).get(striker_rid)
                if ur:
                    bat_form_mod = form_to_rating_mod(
                        compute_form_score(ses, ur.user_id, ur.player_id))
            finally:
                ses.close()
        if bowler_rid > 0:
            from database import get_session as _gs_db
            from models import UserRoster as _UR
            ses = _gs_db()
            try:
                ur = ses.query(_UR).get(bowler_rid)
                if ur:
                    bowl_form_mod = form_to_rating_mod(
                        compute_form_score(ses, ur.user_id, ur.player_id))
            finally:
                ses.close()
    except Exception:
        pass

    eff_bat = striker["bat_rating"] + bat_form_mod
    eff_bowl = bowler["bowl_rating"] + bowl_form_mod

    # Fetch traits for striker and bowler
    striker_traits = _get_roster_traits(striker.get("roster_id"))
    bowler_traits = _get_roster_traits(bowler.get("roster_id"))

    # Build trait context for activation conditions
    bs = s.get("bat_stats", {}).get(striker.get("roster_id"), {})
    bat_balls_faced = bs.get("balls", 0)
    # Compute RRR if chasing
    rrr = 0.0
    if s.get("innings") == 2 and s.get("target"):
        target = s["target"]
        chased = s.get("total_runs", 0)
        remaining_runs = max(0, target - chased)
        balls_left = (total_overs - (over - 1)) * 6 - s.get("current_ball", 0)
        if balls_left > 0:
            rrr = (remaining_runs / balls_left) * 6

    trait_ctx = {
        "over": over,
        "total_overs": total_overs,
        "rrr": rrr,
        "bat_balls_faced": bat_balls_faced,
        "target": s.get("target", 0),
        "total_runs": s.get("total_runs", 0),
    }

    return calculate_outcome(
        bowler.get("bowl_style", "Medium Pacer"),
        bowler.get("bowl_hand", "Right"),
        variation, length, pitch,
        over, total_overs, shot,
        eff_bat, eff_bowl,
        striker_traits=striker_traits,
        bowler_traits=bowler_traits,
        trait_ctx=trait_ctx,
        pitch_wear=pitch_wear,
    )


# ═══════════════════════════ NEW BATSMAN ═════════════════════════════

async def _show_new_batsman(ctx, mid):
    s = _gs(ctx, mid)
    if not s: return
    try:
        available = []
        for i, p in enumerate(s["batting_order"]):
            if i == s["striker_idx"] or i == s["non_striker_idx"]: continue
            bs = s["bat_stats"].get(p["roster_id"], {})
            if not bs.get("out", False): available.append((i, p))
        if not available:
            logger.warning(f"Match {mid}: No available batsmen but wickets={s['total_wickets']}, forcing innings end")
            await _end_innings(ctx, mid)
            return
        btns = [[InlineKeyboardButton(
            f"{p['name']} — {s['bat_stats'].get(p['roster_id'], {}).get('runs', 0)}({s['bat_stats'].get(p['roster_id'], {}).get('balls', 0)})",
            callback_data=f"newbat_{mid}_{i}"
        )] for i, p in available]
        bat_mention = _mention(s.get("bat_user_tg"), fallback_name=s.get("bat_username") or "Batsman")
        text_to_send = f"🏏 <b>WICKET!</b> Select next batsman:\n\n{bat_mention}, pick the next batter:"

        try:
            await ctx.bot.send_message(s["chat_id"], text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e1:
            logger.warning(f"_show_new_batsman first attempt failed: {e1}")
            import asyncio
            await asyncio.sleep(0.5)
            await ctx.bot.send_message(s["chat_id"], text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))

        _start_action_timer(ctx, mid, s["bat_user_tg"], "select batsman")
    except Exception:
        logger.exception(f"_show_new_batsman failed for match {mid}")
        try:
            await ctx.bot.send_message(
                s["chat_id"],
                "⚠️ Couldn't show batsman picker. Retrying automatically…",
                parse_mode="HTML")
            _schedule_recovery(ctx, mid, "batsman picker")
        except Exception:
            pass

async def new_batsman_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, bi = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bat_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    nb = s["batting_order"][bi]; s["striker_idx"] = bi
    # Determine what's next: end of over → bowler picker; else next delivery
    if s["current_ball"] == 0 and s["current_over"] > 1:
        next_act = A_PICK_NEW_BOWLER
    else:
        next_act = A_PICK_DELIVERY
    _ss(context, mid, s, next_action=next_act)
    try:
        await q.edit_message_text(f"🏏 New batsman: {nb['name']} ({nb['bat_rating']} BAT)", parse_mode="HTML")
    except Exception:
        pass
    # Send batsman stats card (best-effort — don't let it strand the match)
    try:
        await _send_batsman_card(context, s["chat_id"], nb, s["bat_team_id"])
    except Exception:
        logger.warning("Batsman card send failed but continuing")

    try:
        if is_innings_over(s):
            await _end_innings(context, mid)
            return
        # Single dispatcher — handles vsbot routing internally
        await render_screen(context, mid)
    except Exception:
        logger.exception(f"new_batsman_callback next-step failed for match {mid}")
        try:
            await context.bot.send_message(
                s["chat_id"],
                "⚠️ Hit a hiccup. Reconnecting automatically…",
                parse_mode="HTML")
            _schedule_recovery(context, mid, "new batsman")
        except Exception:
            pass


# ═══════════════════════════ NEW OVER BOWLER ═════════════════════════

async def _show_new_over_bowler(ctx, mid):
    s = _gs(ctx, mid)
    if not s: return
    try:
        prev = s.get("prev_bowler_rid")
        avail = [p for p in s["bowl_xi"] if p["roster_id"] != prev]
        avail = sorted(avail, key=lambda x: x["bowl_rating"], reverse=True)
        btns = [[InlineKeyboardButton(_bowl_label(p, s), callback_data=f"nbowl_{mid}_{p['roster_id']}")] for p in avail]
        bowl_mention = _mention(s.get("bowl_user_tg"), fallback_name=s.get("bowl_username") or "Bowler")
        text_to_send = (f"🎳 <b>OVER {s['current_over']}</b> — Select bowler:\n📊 {format_score(s)} | "
                        f"{format_overs(s)} ov\n\n{bowl_mention}, pick a new bowler:")

        try:
            await ctx.bot.send_message(s["chat_id"], text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e1:
            logger.warning(f"_show_new_over_bowler first attempt failed: {e1}")
            import asyncio
            await asyncio.sleep(0.5)
            await ctx.bot.send_message(s["chat_id"], text_to_send, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(btns))

        _start_action_timer(ctx, mid, s["bowl_user_tg"], "select bowler")
    except Exception:
        logger.exception(f"_show_new_over_bowler failed for match {mid}")
        try:
            await ctx.bot.send_message(
                s["chat_id"],
                "⚠️ Couldn't show bowler picker. Retrying automatically…",
                parse_mode="HTML")
            _schedule_recovery(ctx, mid, "bowler picker")
        except Exception:
            pass

async def new_over_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, rid = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    bw = next((p for p in s["bowl_xi"] if p["roster_id"] == rid), None)
    if not bw: return
    s["current_bowler"] = bw
    bws = s["bowl_stats"].setdefault(bw["roster_id"], {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0, "this_over_balls": 0})
    bws["this_over_balls"] = 0
    _ss(context, mid, s, next_action=A_PICK_DELIVERY)
    await q.edit_message_text(f"🎳 Over {s['current_over']}: {bw['name']} | {bw.get('bowl_hand','R')[:1]}-{bw['bowl_style']}", parse_mode="HTML")
    if not bw.get("is_bot_player"):
        await _send_bowler_card(context, s["chat_id"], bw, s["bowl_team_id"])
    # Single dispatcher (handles vsbot routing internally)
    await render_screen(context, mid)


# ═══════════════════════════ END INNINGS ═════════════════════════════

async def _send_innings_scorecards(ctx, mid, innings_num):
    """Send batting + bowling scorecards for the innings that just ended.

    For 1st innings: sent at end of 1st innings
    For 2nd innings: sent at match completion
    """
    s = _gs(ctx, mid)
    if not s:
        return

    cid = s["chat_id"]

    try:
        # Pull data for the innings that just ENDED (not current state)
        if innings_num == 1:
            # 1st innings data (already saved in inn1_* keys)
            bat_team = s.get("inn1_team", s.get("bat_team_name", "Team"))
            bowl_team = s.get("bowl_team_name", "Opponent")
            if s.get("innings", 1) == 2:
                # We've already swapped — bowl_team_name is now the one who batted in 1st inns' bowlers
                bowl_team = s.get("bat_team_name", "Opponent")
            total_runs = s.get("inn1_runs", 0)
            total_wickets = s.get("inn1_wickets", 0)
            overs_str = s.get("inn1_overs", "0.0")
            bat_stats_map = s.get("inn1_bat_stats", {})
            bowl_stats_map = s.get("inn1_bowl_stats", {})
            bat_xi = s.get("inn1_bat_xi", [])
            bowl_xi = s.get("inn1_bowl_xi", [])
            fow = s.get("inn1_fow", [])
        else:
            # 2nd innings — current state is the 2nd innings
            bat_team = s.get("bat_team_name", "Team")
            bowl_team = s.get("bowl_team_name", "Opponent")
            total_runs = s.get("total_runs", 0)
            total_wickets = s.get("total_wickets", 0)
            overs_str = format_overs(s)
            bat_stats_map = s.get("bat_stats", {})
            bowl_stats_map = s.get("bowl_stats", {})
            bat_xi = s.get("bat_xi", [])
            bowl_xi = s.get("bowl_xi", [])
            fow = s.get("fow", [])

        # Build batsmen rows — order by batting order
        order = s.get("inn1_batting_order", s.get("batting_order", bat_xi)) if innings_num == 1 \
            else s.get("batting_order", bat_xi)
        # Dedupe while preserving order
        seen = set()
        bat_order_unique = []
        for p in order:
            if p["roster_id"] not in seen:
                seen.add(p["roster_id"]); bat_order_unique.append(p)
        # Append any batsmen who didn't appear in order but are in XI
        for p in bat_xi:
            if p["roster_id"] not in seen:
                seen.add(p["roster_id"]); bat_order_unique.append(p)

        batsmen_rows = []
        for p in bat_order_unique:
            bs = bat_stats_map.get(p["roster_id"], {})
            balls = bs.get("balls", 0)
            is_out = bs.get("out", False)
            # Three statuses to render:
            #   out      → batted and dismissed
            #   not_out  → batted and survived (or marked retired)
            #   dnb      → in XI but never faced a ball and not given out
            if balls == 0 and not is_out:
                status = "dnb"
                dismissal = "did not bat"
            elif is_out:
                status = "out"
                # Prefer the pre-formatted dismissal string (set at wicket
                # fall with a random catcher). Fallback: format on-the-fly
                # from how_out + bowled_by for backward compat with old states.
                dismissal = bs.get("dismissal_text")
                if not dismissal:
                    how_raw = bs.get("how_out", "—") or "—"
                    bowler_raw = bs.get("bowled_by", "")
                    # Use the bowling XI from the appropriate innings
                    bowl_xi_for_fmt = (s.get("inn1_bowl_xi", []) if innings_num == 1
                                       else s.get("bowl_xi", []))
                    if how_raw not in ("—", ""):
                        dismissal = _format_dismissal(
                            how_raw, bowler_raw, bowl_xi_for_fmt)
                    else:
                        dismissal = "—"
            else:
                status = "not_out"
                dismissal = "not out"

            runs = bs.get("runs", 0)
            sr = (runs / balls * 100) if balls > 0 else 0.0
            batsmen_rows.append({
                "rating": p.get("rating", 0),
                "name": p.get("name", "?"),
                "dismissal": dismissal,
                "runs": runs,
                "balls": balls,
                "fours": bs.get("fours", 0),
                "sixes": bs.get("sixes", 0),
                "strike_rate": round(sr, 1),
                "status": status,
            })

        # Build bowlers rows
        bowlers_rows = []
        for p in bowl_xi:
            bws = bowl_stats_map.get(p["roster_id"], {})
            balls = bws.get("balls", 0)
            if balls == 0 and bws.get("wickets", 0) == 0 and bws.get("runs", 0) == 0:
                continue
            overs_complete = balls // 6
            ball_rem = balls % 6
            overs_str_bw = f"{overs_complete}.{ball_rem}" if ball_rem else str(overs_complete)
            runs_conceded = bws.get("runs", 0)
            econ = (runs_conceded / balls * 6) if balls > 0 else 0.0
            bowlers_rows.append({
                "name": p.get("name", "?"),
                "overs": overs_str_bw,
                "maidens": bws.get("maidens", 0),
                "runs_conceded": runs_conceded,
                "wickets": bws.get("wickets", 0),
                "economy": round(econ, 2),
            })

        # Extras
        extras = {
            "wd": s.get("wides_1" if innings_num == 1 else "wides", s.get("wides", 0)),
            "nb": s.get("noballs_1" if innings_num == 1 else "noballs", s.get("noballs", 0)),
            "b": 0,
            "lb": s.get("legbyes_1" if innings_num == 1 else "legbyes", s.get("legbyes", 0)),
        }
        extras["total"] = extras["wd"] + extras["nb"] + extras["b"] + extras["lb"]

        # Match title
        match_title = "MATCH"

        is_first = (innings_num == 1)

        # Compute target + chase outcome for innings 2
        target = None
        chase_outcome = None
        if not is_first:
            target = (s.get("inn1_runs", 0) or 0) + 1
            inn2_runs = total_runs
            if inn2_runs >= target:
                chase_outcome = "won"
            elif inn2_runs == target - 1:
                chase_outcome = "tied"
            else:
                chase_outcome = "lost"

        # Load admin-tunable accent color
        from services.config_service import get_config as _get_cfg
        _cfg = _get_cfg()
        accent_hex = (_cfg.get("scorecard_color_inn1") if is_first
                      else _cfg.get("scorecard_color_inn2"))
        text_settings = _cfg.get("scorecard_text_settings")

        # Generate batting scorecard
        bat_card_bytes = generate_batting_scorecard(
            bat_team, bowl_team,
            total_runs, total_wickets, overs_str,
            batsmen_rows, fow, extras,
            is_first_innings=is_first, match_title=match_title,
            target=target, chase_outcome=chase_outcome,
            stadium=s.get("stadium"), match_no=mid, accent_hex=accent_hex,
            text_settings=text_settings,
        )

        # Generate bowling scorecard — team name is the bowling team
        # Pass the opponent's (batting) score so the "RUN SCORED BY OPPONENTS"
        # panel can render.
        bowl_card_bytes = generate_bowling_scorecard(
            bowl_team, bowlers_rows, fow,
            is_first_innings=is_first, match_title=match_title,
            opponent_name=bat_team,
            opp_score=total_runs, opp_wickets=total_wickets, opp_overs=overs_str,
            stadium=s.get("stadium"),
            match_no=mid, accent_hex=accent_hex,
            text_settings=text_settings,
        )

        # Best-effort durable value snapshot in the Telegram storage channel.
        # Images are still sent to the match chat; the JSON file keeps all values
        # needed to regenerate batting/bowling cards after ephemeral storage resets.
        try:
            from services import tg_storage_service
            await tg_storage_service.upload_json_async({
                "type": "innings_scorecards",
                "match_id": mid,
                "innings": innings_num,
                "batting_scorecard": {
                    "team": bat_team, "opponent": bowl_team,
                    "score": total_runs, "wickets": total_wickets,
                    "overs": overs_str, "rows": batsmen_rows,
                    "fall_of_wickets": fow, "extras": extras,
                    "target": target, "chase_outcome": chase_outcome,
                },
                "bowling_scorecard": {
                    "team": bowl_team, "opponent": bat_team,
                    "opponent_score": total_runs,
                    "opponent_wickets": total_wickets,
                    "opponent_overs": overs_str,
                    "rows": bowlers_rows, "fall_of_wickets": fow,
                },
                "style": {
                    "accent_hex": accent_hex,
                    "text_settings": text_settings,
                    "match_title": match_title,
                    "stadium": s.get("stadium"),
                },
            }, f"match-{mid}-innings-{innings_num}-scorecards.json",
               caption=f"Scorecard values · Match {mid} · Innings {innings_num}")
        except Exception:
            logger.exception("scorecard storage snapshot failed (non-fatal)")

        # Send in the order specified by the user: Batting first, then Bowling
        if bat_card_bytes:
            await ctx.bot.send_photo(
                chat_id=cid, photo=io.BytesIO(bat_card_bytes),
                caption=f"🏏 <b>{bat_team}</b> — Batting Scorecard",
                parse_mode="HTML")
        if bowl_card_bytes:
            await ctx.bot.send_photo(
                chat_id=cid, photo=io.BytesIO(bowl_card_bytes),
                caption=f"🎳 <b>{bowl_team}</b> — Bowling Scorecard",
                parse_mode="HTML")
    except Exception:
        logger.exception(f"Failed to send innings {innings_num} scorecards")


async def _end_innings(ctx, mid):
    s = _gs(ctx, mid); cid = s["chat_id"]; _cancel_action_timer(ctx, mid)
    if s["innings"] == 1:
        s["inn1_runs"] = s["total_runs"]; s["inn1_wickets"] = s["total_wickets"]
        s["inn1_overs"] = format_overs(s); s["inn1_team"] = s["bat_team_name"]
        target = s["total_runs"] + 1

        # SAVE 1st innings stats before reset
        s["inn1_bat_stats"] = dict(s["bat_stats"])
        s["inn1_bowl_stats"] = dict(s["bowl_stats"])
        s["inn1_bat_team_id"] = s["bat_team_id"]
        s["inn1_bowl_team_id"] = s["bowl_team_id"]
        s["inn1_bat_xi"] = list(s["bat_xi"])
        s["inn1_bowl_xi"] = list(s["bowl_xi"])
        s["inn1_fow"] = list(s.get("fow", []))
        s["inn1_wides"] = s.get("wides", 0)
        s["inn1_noballs"] = s.get("noballs", 0)
        s["inn1_legbyes"] = s.get("legbyes", 0)

        # Save 1st innings stats to DB immediately (in case 2nd innings abandoned)
        await _save_match_stats(s)
        s["inn1_stats_saved"] = True  # prevent double-save at match end

        # Save batting order snapshot for scorecard display
        s["inn1_batting_order"] = list(s.get("batting_order", []))

        # Send innings 1 scorecards (bowling then batting)
        await _send_innings_scorecards(ctx, mid, innings_num=1)

        await ctx.bot.send_message(cid,
            f"━━━━━━━━━━━━━━━━━━━\n📊 <b>END OF 1ST INNINGS</b>\n\n"
            f"🔴 {s['bat_team_name']}: {format_score(s)} ({format_overs(s)})\n\n"
            f"🎯 Target: {target}\n🏏 {s['bowl_team_name']} needs {target}\n━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
        s["innings"] = 2; s["target"] = target; s["total_runs"] = 0; s["total_wickets"] = 0
        s["extras_total"] = 0; s["wides"] = 0; s["noballs"] = 0; s["legbyes"] = 0
        s["current_over"] = 1; s["current_ball"] = 0; s["timeline"] = []; s["partnership_runs"] = 0; s["partnership_balls"] = 0
        s["bat_team_id"], s["bowl_team_id"] = s["bowl_team_id"], s["bat_team_id"]
        s["bat_user_tg"], s["bowl_user_tg"] = s["bowl_user_tg"], s["bat_user_tg"]
        s["bat_team_name"], s["bowl_team_name"] = s["bowl_team_name"], s["bat_team_name"]
        s["bat_username"], s["bowl_username"] = s["bowl_username"], s["bat_username"]
        s["bat_xi"], s["bowl_xi"] = s["bowl_xi"], s["bat_xi"]
        s["batting_order"] = list(s["bat_xi"]); s["striker_idx"] = 0; s["non_striker_idx"] = 1; s["next_batsman_idx"] = 2
        s["prev_bowler_rid"] = None; s["selected_variation"] = None
        s["bat_stats"] = {p["roster_id"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "out": False, "how_out": "", "bowled_by": ""} for p in s["bat_xi"]}
        s["bowl_stats"] = {p["roster_id"]: {
            "balls": 0, "runs": 0, "wickets": 0,
            "overs_done": 0, "this_over_balls": 0,
            "maidens": 0, "this_over_runs": 0,
        } for p in s["bowl_xi"]}
        s["fow"] = []  # reset for 2nd innings
        # Recovery-safe: set the action pointer so /resume during the pause shows the right thing
        _ss(ctx, mid, s, next_action=A_PICK_DELIVERY)
        # CRITICAL: Update bot_data so opener callbacks read correct XI
        ctx.bot_data[f"bat_xi_{mid}"] = s["bat_xi"]
        ctx.bot_data[f"bowl_xi_{mid}"] = s["bowl_xi"]
        ctx.bot_data[f"bat_uname_{mid}"] = s["bat_username"]
        ctx.bot_data[f"bowl_uname_{mid}"] = s["bowl_username"]
        ctx.bot_data[f"bat_uid_{mid}"] = s["bat_team_id"]
        ctx.bot_data[f"bowl_uid_{mid}"] = s["bowl_team_id"]

        # If 2nd innings batting side is the bot, auto-pick openers (no UI)
        if s.get("is_vsbot") and s["bat_user_tg"] == BOT_TG_ID_:
            op1 = s["bat_xi"][0]
            op2 = s["bat_xi"][1]
            ctx.bot_data[f"opener1_{mid}"] = op1
            ctx.bot_data[f"opener2_{mid}"] = op2
            s["striker_idx"] = 0
            s["non_striker_idx"] = 1
            # Rebuild batting_order so openers are at index 0/1
            s["batting_order"] = list(s["bat_xi"])
            _ss(ctx, mid, s, next_action=A_PICK_DELIVERY)
            await ctx.bot.send_message(
                cid,
                f"🤖 Bot openers: <b>{op1['name']}</b> & <b>{op2['name']}</b>",
                parse_mode="HTML",
            )
            # If bowling user is human, ask them for opening bowler.
            # If bowling user is also bot (botvsbot), pick automatically.
            if s["bowl_user_tg"] == BOT_TG_ID_:
                # Bot vs bot — pick opening bowler too
                opening_bowler = max(s["bowl_xi"], key=lambda p: p.get("bowl_rating", 0))
                s["current_bowler"] = opening_bowler
                s["prev_bowler_rid"] = None
                _ss(ctx, mid, s, next_action=A_PICK_DELIVERY)
                await ctx.bot.send_message(
                    cid,
                    f"🤖 Opening bowler: <b>{opening_bowler['name']}</b>",
                    parse_mode="HTML",
                )
                # Kick off the match loop
                from handlers.vsbot import vsbot_auto_continue
                await vsbot_auto_continue(ctx, mid)
            else:
                # User bowling — show bowler picker for the user
                from handlers.vsbot import _show_user_opening_bowler
                from database import get_session as _gs2
                _ses = _gs2()
                try:
                    user_obj = _ses.query(User).filter(User.telegram_id == s["bowl_user_tg"]).first()
                    if user_obj:
                        await _show_user_opening_bowler(ctx, cid, mid, user_obj, s["bowl_xi"])
                finally:
                    _ses.close()
        else:
            # Show ALL 11 players for 2nd innings opener (user batting)
            buid = s["bat_team_id"]
            btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']} | {p['category']}", callback_data=f"op1_{mid}_{buid}_{p['roster_id']}")] for p in s["bat_xi"]]
            bat_mention2 = _mention(s.get("bat_user_tg"), fallback_name=s.get("bat_username") or "Captain")
            await ctx.bot.send_message(cid, f"🏏 <b>2ND INNINGS — SELECT OPENER 1</b>\n\n{bat_mention2}, pick the opening batter:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        # Match complete — give rewards
        target = s["target"]; chasing = s["total_runs"]; overs = s.get("overs", 10)

        # ── TIED MATCH → BOWL-OUT TIEBREAKER ──────────────────────────
        if chasing == target - 1:
            if (s.get("is_spectator") or s.get("is_bot_vs_bot")
                    or s.get("bat_user_tg") == BOT_TG_ID_
                    or s.get("bowl_user_tg") == BOT_TG_ID_):
                # AI tie — bowling side declared winner for stats; margin=0 → "(tied)"
                winner_name = s["bowl_team_name"]; loser_name = s["bat_team_name"]
                winner_tg = s["bowl_user_tg"]; loser_tg = s["bat_user_tg"]
                winner_uid = s["bowl_team_id"]; loser_uid = s["bat_team_id"]
                margin_type = "runs"; margin_val = 0; margin = "(tied)"
            else:
                # Human vs human — fire bowl-out
                try:
                    await ctx.bot.send_message(
                        s["chat_id"],
                        f"🤝 <b>TIED!</b>\nBoth teams ended on <b>{chasing}</b>.\n\n"
                        f"🎯 Bowl-out tiebreaker starting...",
                        parse_mode="HTML")
                except Exception: pass

                try:
                    db_t = get_session()
                    try:
                        m = db_t.query(Match).get(mid)
                        if m:
                            m.status = "completed"
                            m.completed_at = datetime.utcnow()
                            m.margin_type = "bowlout_pending"
                            m.margin_value = 0
                            db_t.commit()
                    finally:
                        db_t.close()
                except Exception:
                    logger.exception("Failed to mark tied match completed")

                # NOTE: don't send scorecards here. They'll be sent by
                # _finalize_bowlout in handlers/bowlout.py once the bowl-out
                # finishes, with the winner declared.

                try:
                    from handlers.bowlout import start_bowlout
                    import asyncio as _asyncio
                    await _asyncio.sleep(1.0)
                    await start_bowlout(
                        ctx,
                        chat_id=s["chat_id"],
                        user1_id=s["bat_team_id"],
                        user2_id=s["bowl_team_id"],
                        user1_team_name=s["bat_team_name"],
                        user2_team_name=s["bowl_team_name"],
                        first_picker_user_id=s["bat_team_id"],
                        match_id=mid,
                    )
                except Exception:
                    logger.exception("start_bowlout failed")

                # NOTE: do NOT cleanup_state here. The bowl-out's
                # _finalize_bowlout reads state to render the post-bowl-out
                # scorecards, and cleans up after.
                release_match_lock(mid)
                return

        elif chasing >= target:
            winner_name = s["bat_team_name"]; loser_name = s["bowl_team_name"]
            winner_tg = s["bat_user_tg"]; loser_tg = s["bowl_user_tg"]
            winner_uid = s["bat_team_id"]; loser_uid = s["bowl_team_id"]
            margin_type = "wickets"
            margin_val = 10 - s['total_wickets']
            margin = f"by {margin_val} wickets"
        else:
            winner_name = s["bowl_team_name"]; loser_name = s["bat_team_name"]
            winner_tg = s["bowl_user_tg"]; loser_tg = s["bat_user_tg"]
            winner_uid = s["bowl_team_id"]; loser_uid = s["bat_team_id"]
            margin_type = "runs"
            margin_val = target - 1 - chasing
            margin = f"by {margin_val} runs"

        # Skip all real-economy effects for spectator matches AND bot-vs-bot
        if s.get("is_spectator") or s.get("is_bot_vs_bot"):
            wc, wg, lc, lg = 0, 0, 0, 0
        else:
            wc, wg, lc, lg = await _award_match_rewards(ctx, s, winner_tg, loser_tg, overs)
            await _save_match_stats(s)
        potm_name, potm_impact, potm_stats = _calc_potm(s)

        # Get POTM player_id and the OWNER USER ID
        potm_pid = None
        potm_owner_uid = None
        if potm_name:
            all_xi_lists = [
                (s.get("inn1_bat_xi", []), s.get("inn1_bat_team_id")),
                (s.get("inn1_bowl_xi", []), s.get("inn1_bowl_team_id")),
                (s.get("bat_xi", []), s.get("bat_team_id")),
                (s.get("bowl_xi", []), s.get("bowl_team_id")),
            ]
            seen_rids = set()
            for xi, owner in all_xi_lists:
                for p in xi:
                    rid = p.get("roster_id")
                    if p.get("name") == potm_name and rid not in seen_rids:
                        potm_pid = p.get("player_id")
                        potm_owner_uid = owner
                        seen_rids.add(rid)
                        break
                if potm_pid:
                    break

        # Increment PlayerGameStats.potm
        if potm_pid and potm_owner_uid:
            _ses2 = get_session()
            try:
                gs_potm = (_ses2.query(PlayerGameStats)
                           .filter(PlayerGameStats.user_id == potm_owner_uid,
                                   PlayerGameStats.player_id == potm_pid).first())
                if gs_potm:
                    gs_potm.potm = (gs_potm.potm or 0) + 1
                else:
                    gs_potm = PlayerGameStats(user_id=potm_owner_uid, player_id=potm_pid, potm=1)
                    _ses2.add(gs_potm)
                _ses2.commit()
            except Exception:
                _ses2.rollback()
                logger.exception("Failed to increment POTM count")
            finally:
                _ses2.close()

        # Update Match record + User stats
        session = get_session()
        try:
            m = session.query(Match).get(mid)
            if m:
                m.status = "completed"
                m.completed_at = datetime.utcnow()
                m.winner_id = winner_uid; m.loser_id = loser_uid
                m.margin_type = margin_type; m.margin_value = margin_val
                m.inn1_runs = s["inn1_runs"]; m.inn1_wickets = s["inn1_wickets"]
                m.inn2_runs = s["total_runs"]; m.inn2_wickets = s["total_wickets"]
                m.potm_player_id = potm_pid; m.potm_impact = potm_impact

            # ── Tour result hook ──
            # If this match is part of a tour, mark the TourMatch done and
            # finalize the Tour if all matches are complete.
            if not s.get("is_spectator") and not s.get("is_bot_vs_bot") and not s.get("is_vsbot"):
                try:
                    from services.tour_service import record_match_result
                    tour_after = record_match_result(session, mid, winner_uid)
                    if tour_after is not None:
                        # Stash a flag for the post-commit announcement below
                        s["_tour_update"] = {
                            "tour_id": tour_after.id,
                            "completed": tour_after.status == "completed",
                            "winner_id": tour_after.winner_id,
                            "u1_wins": tour_after.user1_wins,
                            "u2_wins": tour_after.user2_wins,
                        }
                except Exception:
                    logger.exception("Tour-result hook failed (non-fatal)")

            # Update user counters — skip entirely for spectator matches AND bot-vs-bot
            today = datetime.utcnow().date()
            if s.get("is_spectator") or s.get("is_bot_vs_bot"):
                pass  # No user stats update
            else:
                for uid, is_winner in [(winner_uid, True), (loser_uid, False)]:
                    u = session.query(User).get(uid)
                    if u:
                        u.matches_played = (u.matches_played or 0) + 1
                        if is_winner:
                            u.matches_won = (u.matches_won or 0) + 1
                            u.win_streak = (u.win_streak or 0) + 1
                            u.best_streak = max(u.best_streak or 0, u.win_streak)
                        else:
                            u.matches_lost = (u.matches_lost or 0) + 1
                            u.win_streak = 0
                        # Active days
                        last = u.last_match_date
                        if not last or last.date() != today:
                            u.active_days = (u.active_days or 0) + 1
                        u.last_match_date = datetime.utcnow()

                    # Quest tracking — skip bot user (telegram_id = -1)
                    try:
                        if u.telegram_id != -1:
                            from services.quest_service import safe_track
                            is_vsbot = bool(s.get("is_vsbot"))
                            safe_track(session, u.id, "match_played", 1)
                            if is_vsbot:
                                safe_track(session, u.id, "vsbot_played", 1)
                            if is_winner:
                                safe_track(session, u.id, "match_won", 1)
                                if is_vsbot:
                                    safe_track(session, u.id, "vsbot_won", 1)
                            # Aggregate runs/wkts/50s/100s for this user across innings
                            from models import UserRoster
                            # Aggregates across all of this user's players
                            runs_total = wkts_total = fifties = hundreds = 0
                            sixes_total = fours_total = 0
                            hattricks_total = 0
                            maidens_total = 0
                            # Single-match maxes
                            max_runs_in_innings = 0
                            max_wickets_in_match = 0
                            max_sixes_in_innings = 0
                            max_boundaries_in_innings = 0
                            # Per-match accumulators (for allrounder match — best single
                            # match this game, not summed across innings)
                            user_match_runs = 0    # batting runs this match
                            user_match_wkts = 0    # bowling wkts this match
                            user_match_not_outs = 0  # batters who remained not out
                            # Economy thresholds (any user bowler with ≥4 overs that beat the threshold)
                            cleanest_econ = None  # smallest economy seen this match (≥4 overs)
                            # Bowler with 4+ overs and ZERO extras (wides+nb=0) → clean spell
                            had_clean_spell = False

                            for xi_key, stats_key, is_bat in [
                                ("inn1_bat_xi", "inn1_bat_stats", True),
                                ("inn1_bowl_xi", "inn1_bowl_stats", False),
                                ("bat_xi", "bat_stats", True),
                                ("bowl_xi", "bowl_stats", False),
                            ]:
                                xi = s.get(xi_key, [])
                                stats = s.get(stats_key, {})
                                for p in xi:
                                    rid = p.get("roster_id")
                                    if rid is None or rid <= 0:
                                        continue
                                    pst = stats.get(rid)
                                    if not pst:
                                        continue
                                    ur = session.query(UserRoster).get(rid)
                                    if not ur or ur.user_id != u.id:
                                        continue
                                    if is_bat:
                                        r = pst.get("runs", 0)
                                        balls = pst.get("balls", 0)
                                        runs_total += r
                                        user_match_runs += r
                                        max_runs_in_innings = max(max_runs_in_innings, r)
                                        if r >= 100: hundreds += 1
                                        elif r >= 50: fifties += 1
                                        # Not-out check: batter played (balls > 0) and didn't get out
                                        if balls > 0 and not pst.get("out"):
                                            user_match_not_outs += 1
                                        sixes_p = pst.get("sixes", 0)
                                        fours_p = pst.get("fours", 0)
                                        sixes_total += sixes_p
                                        fours_total += fours_p
                                        max_sixes_in_innings = max(max_sixes_in_innings, sixes_p)
                                        max_boundaries_in_innings = max(
                                            max_boundaries_in_innings, sixes_p + fours_p)
                                    else:
                                        w = pst.get("wickets", 0)
                                        wkts_total += w
                                        user_match_wkts += w
                                        max_wickets_in_match = max(max_wickets_in_match, w)
                                        if pst.get("hattrick"): hattricks_total += 1
                                        # Maidens count
                                        maidens_total += pst.get("maidens", 0)
                                        # Economy / clean spell — only if bowler bowled enough
                                        balls_b = pst.get("balls", 0)
                                        runs_b = pst.get("runs", 0)
                                        if balls_b >= 24:  # 4+ overs
                                            econ = (runs_b * 6.0) / balls_b if balls_b else 999
                                            if cleanest_econ is None or econ < cleanest_econ:
                                                cleanest_econ = econ
                                            # Clean spell: 4+ overs, 0 wides, 0 no-balls,
                                            # 3+ wickets (per the quest "No-Ball Nightmare" / "No Extras Challenge")
                                            # We don't track per-bowler extras separately, so use a
                                            # heuristic: if maidens > 0 AND wkts >= 3, count it.
                                            if pst.get("maidens", 0) >= 1 and w >= 3:
                                                had_clean_spell = True

                            # Fire cumulative events (existing)
                            if runs_total > 0:
                                safe_track(session, u.id, "runs_scored", runs_total)
                            if wkts_total > 0:
                                safe_track(session, u.id, "wickets_taken", wkts_total)
                            if sixes_total > 0:
                                safe_track(session, u.id, "sixes_hit", sixes_total)
                            if (sixes_total + fours_total) > 0:
                                safe_track(session, u.id, "boundaries_hit", sixes_total + fours_total)
                            for _ in range(fifties):
                                safe_track(session, u.id, "fifty", 1)
                            for _ in range(hundreds):
                                safe_track(session, u.id, "hundred", 1)
                            for _ in range(hattricks_total):
                                safe_track(session, u.id, "hattrick", 1)
                            if maidens_total > 0:
                                safe_track(session, u.id, "maiden_over", maidens_total)
                            for _ in range(user_match_not_outs):
                                safe_track(session, u.id, "not_out_innings", 1)

                            # Single-match max events
                            if max_runs_in_innings > 0:
                                safe_track(session, u.id, "runs_in_innings", max_runs_in_innings, mode="max")
                            if max_wickets_in_match > 0:
                                safe_track(session, u.id, "wickets_in_match", max_wickets_in_match, mode="max")
                            if max_sixes_in_innings > 0:
                                safe_track(session, u.id, "sixes_in_match", max_sixes_in_innings, mode="max")
                            if max_boundaries_in_innings > 0:
                                safe_track(session, u.id, "boundaries_in_match", max_boundaries_in_innings, mode="max")

                            # Allrounder match: 30+ runs AND 2+ wickets in same game
                            if user_match_runs >= 30 and user_match_wkts >= 2:
                                safe_track(session, u.id, "allrounder_match", 1)

                            # Economy tier triggers (cumulative count of "clean spells")
                            if cleanest_econ is not None:
                                if cleanest_econ < 4.5:
                                    safe_track(session, u.id, "economy_under_4_5", 1)
                                if cleanest_econ < 5.0:
                                    safe_track(session, u.id, "economy_under_5", 1)
                                if cleanest_econ < 6.0:
                                    safe_track(session, u.id, "economy_under_6", 1)
                                if cleanest_econ < 7.0:
                                    safe_track(session, u.id, "economy_under_7", 1)

                            if had_clean_spell:
                                safe_track(session, u.id, "clean_spell", 1)

                            # Chase win: user won AND batted second
                            if winner_id == u.id:
                                # In our state, "bat_xi" is the second-innings batting lineup
                                second_inn_user = s.get("bat_team_id")
                                if second_inn_user == u.id:
                                    safe_track(session, u.id, "chase_won", 1)
                    except Exception:
                        logger.exception("Match-end quest tracking failed")
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Match finalize err")
        finally: session.close()

        msg = (
            f"━━━━━━━━━━━━━━━━━━━\n🏆 <b>MATCH RESULT</b>\n\n"
            f"🔴 {s['inn1_team']}: {s['inn1_runs']}/{s['inn1_wickets']} ({s['inn1_overs']})\n"
            f"🟢 {s['bat_team_name']}: {format_score(s)} ({format_overs(s)})\n\n"
            f"🏆 <b>{winner_name} wins {margin}!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if potm_name:
            msg += (f"⭐ <b>PLAYER OF THE MATCH</b>\n"
                    f"🌟 {potm_name}\n"
                    f"{potm_stats}\n"
                    f"💫 Impact Points: {potm_impact}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━\n\n")
        if s.get("is_spectator"):
            msg += (f"🎬 <b>SPECTATOR MATCH</b>\n"
                    f"<i>No rewards distributed — pure entertainment.</i>\n"
                    f"━━━━━━━━━━━━━━━━━━━")
        else:
            msg += (f"🎁 <b>REWARDS</b>\n"
                    f"🏆 {winner_name}: +{wc:,} Coins 💰 +{wg} Gems 💎\n"
                    f"📉 {loser_name}: +{lc:,} Coins 💰 +{lg} Gems 💎\n"
                    f"━━━━━━━━━━━━━━━━━━━")

        # ── Send innings-2 scorecards (new graphics request: every innings
        # ends with bat + bowl cards). The match summary card below adds
        # the high-level recap on top.
        await _send_innings_scorecards(ctx, mid, innings_num=2)

        # ── Match summary card (NEW design: team-sections + result bar)
        try:
            from services.match_summary_card import generate_match_summary
            from services.config_service import get_config as _get_summary_cfg
            _summary_cfg = _get_summary_cfg()
            top_scorer, top_wicket = _gather_top_performers(s)
            top_per_team = _gather_top_per_team(s, top_n=4)
            # Determine POTM team (which side they were on)
            potm_team = None
            if potm_name:
                for xi_list, team_name in (
                    (s.get("inn1_bat_xi", []), s.get("inn1_team", "")),
                    (s.get("inn1_bowl_xi", []),
                     s["bat_team_name"] if s.get("innings", 1) == 2 else s.get("bowl_team_name", "")),
                    (s.get("bat_xi", []), s.get("bat_team_name", "")),
                    (s.get("bowl_xi", []), s.get("bowl_team_name", "")),
                ):
                    if any(p.get("name") == potm_name for p in xi_list):
                        potm_team = team_name
                        break
            potm_rating = None
            if potm_name:
                for xi_list in (s.get("inn1_bat_xi", []), s.get("inn1_bowl_xi", []),
                                s.get("bat_xi", []), s.get("bowl_xi", [])):
                    for p in xi_list:
                        if p.get("name") == potm_name:
                            potm_rating = p.get("rating")
                            break
                    if potm_rating: break

            summary_bytes = generate_match_summary(
                inn1_team=s.get("inn1_team", "Team 1"),
                inn1_runs=s.get("inn1_runs", 0),
                inn1_wickets=s.get("inn1_wickets", 0),
                inn1_overs=s.get("inn1_overs", "0"),
                inn2_team=s.get("bat_team_name", "Team 2"),
                inn2_runs=s.get("total_runs", 0),
                inn2_wickets=s.get("total_wickets", 0),
                inn2_overs=format_overs(s),
                winner_name=winner_name,
                win_margin_text=margin,
                overs_total=overs,
                potm_name=potm_name,
                potm_rating=potm_rating,
                potm_team=potm_team,
                potm_stats=potm_stats,
                potm_impact=potm_impact,
                top_scorer=top_scorer,
                top_wicket=top_wicket,
                top_per_team=top_per_team,
                stadium=s.get("stadium"),
                match_date=datetime.utcnow(),
                is_spectator=bool(s.get("is_spectator")),
                match_no=mid,
                text_settings=_summary_cfg.get("scorecard_text_settings"),
            )
            if summary_bytes:
                try:
                    from services import tg_storage_service
                    await tg_storage_service.upload_json_async({
                        "type": "match_summary_scorecard",
                        "match_id": mid,
                        "teams": {
                            "innings_1": s.get("inn1_team", "Team 1"),
                            "innings_2": s.get("bat_team_name", "Team 2"),
                        },
                        "scores": {
                            "innings_1": {"runs": s.get("inn1_runs", 0), "wickets": s.get("inn1_wickets", 0), "overs": s.get("inn1_overs", "0")},
                            "innings_2": {"runs": s.get("total_runs", 0), "wickets": s.get("total_wickets", 0), "overs": format_overs(s)},
                        },
                        "result": {"winner": winner_name, "margin": margin},
                        "potm": {
                            "name": potm_name, "rating": potm_rating, "team": potm_team,
                            "stats": potm_stats, "impact": potm_impact,
                        },
                        "top_per_team": top_per_team,
                        "stadium": s.get("stadium"),
                        "style": {"text_settings": _summary_cfg.get("scorecard_text_settings")},
                    }, f"match-{mid}-summary-scorecard.json",
                       caption=f"Match summary values · Match {mid}")
                except Exception:
                    logger.exception("match summary storage snapshot failed (non-fatal)")
                await ctx.bot.send_photo(
                    chat_id=cid, photo=io.BytesIO(summary_bytes),
                    caption=f"🏆 <b>Match Summary</b> — {winner_name} wins {margin}!",
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("match summary card failed (non-fatal)")

        sent = await ctx.bot.send_message(cid, msg, parse_mode="HTML")

        # ── Tour update announcement ──
        # If this match was part of a tour, send a follow-up showing the
        # tour score (and the tour winner if it just completed).
        tour_update = s.get("_tour_update")
        if tour_update:
            try:
                tu_session = get_session()
                try:
                    from models import Tour as _Tour
                    tour_obj = tu_session.query(_Tour).get(tour_update["tour_id"])
                    if tour_obj:
                        u1 = tu_session.query(User).get(tour_obj.user1_id)
                        u2 = tu_session.query(User).get(tour_obj.user2_id)
                        u1_label = f"@{u1.username}" if u1 and u1.username else (
                            u1.first_name if u1 else "User1")
                        u2_label = f"@{u2.username}" if u2 and u2.username else (
                            u2.first_name if u2 else "User2")
                        u1w = tour_update["u1_wins"]; u2w = tour_update["u2_wins"]
                        if tour_update["completed"]:
                            if tour_update["winner_id"] is None:
                                outcome = f"🤝 <b>TOUR DRAWN {u1w}-{u2w}</b>"
                            else:
                                wlabel = u1_label if tour_update["winner_id"] == tour_obj.user1_id else u2_label
                                outcome = f"🏆 <b>{wlabel} WINS THE TOUR {max(u1w, u2w)}-{min(u1w, u2w)}!</b>"
                            await ctx.bot.send_message(
                                cid,
                                f"━━━━━━━━━━━━━━━━━━━\n"
                                f"🏆 <b>TOUR COMPLETE</b>\n\n"
                                f"{u1_label} {u1w} — {u2w} {u2_label}\n\n"
                                f"{outcome}\n"
                                f"━━━━━━━━━━━━━━━━━━━",
                                parse_mode="HTML")
                        else:
                            # Tour ongoing — show next match cue
                            from services.tour_service import get_tour_matches
                            tour_matches = get_tour_matches(tu_session, tour_obj.id)
                            done = sum(1 for tm in tour_matches if tm.status == "done")
                            remaining = tour_obj.match_count - done
                            await ctx.bot.send_message(
                                cid,
                                f"📋 <b>TOUR UPDATE</b>\n"
                                f"{u1_label} {u1w} — {u2w} {u2_label}\n"
                                f"<i>{remaining} match{'es' if remaining != 1 else ''} left</i>\n"
                                f"Use /mytours to continue.",
                                parse_mode="HTML")
                finally:
                    tu_session.close()
            except Exception:
                logger.exception("Tour announcement failed (non-fatal)")

        # Save message id for /jump
        session = get_session()
        try:
            m = session.query(Match).get(mid)
            if m and sent:
                m.result_message_id = sent.message_id
                session.commit()
        except Exception:
            session.rollback()
        finally: session.close()

        # ── Achievement check for both users — skip for spectator matches
        if not s.get("is_spectator"):
            try:
                from services.achievement_service import check_and_notify
                ach_session = get_session()
                try:
                    # Re-fetch user IDs from match record
                    m = ach_session.query(Match).get(mid)
                    if m:
                        for uid in [m.user1_id, m.user2_id]:
                            u = ach_session.query(User).get(uid)
                            if u and u.telegram_id != -1:
                                await check_and_notify(ctx, cid, ach_session, u.id)
                finally:
                    ach_session.close()
            except Exception:
                logger.exception("Achievement check after match failed")

        # Match complete — cleanup persistent state + lock
        cleanup_state(ctx, mid)
        release_match_lock(mid)
