"""Handler for /playmatch — full match with endmatch, timeouts, rewards."""

import io, random, logging
from datetime import datetime, timedelta
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

ACTION_TIMEOUT = 60
FINE_COINS = 10000
FINE_GEMS = 20

def _gs(ctx, mid): return ctx.bot_data.get(f"ms_{mid}")
def _ss(ctx, mid, s): ctx.bot_data[f"ms_{mid}"] = s

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

        card_bytes = generate_batsman_card(
            player_dict["name"],
            player_dict["rating"],
            player_dict["bat_rating"],
            stats,
            bat_hand=player_dict.get("bat_hand", "Right"),
            bowl_hand=player_dict.get("bowl_hand", "Right"),
            bowl_style=player_dict.get("bowl_style", "Medium Pacer"),
        )

        if card_bytes:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(card_bytes),
                caption=f"🏏 <b>{player_dict['name']}</b> walks to the crease",
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

        card_bytes = generate_bowler_card(
            player_dict["name"],
            player_dict["rating"],
            player_dict["bowl_rating"],
            stats,
            bat_hand=player_dict.get("bat_hand", "Right"),
            bowl_hand=player_dict.get("bowl_hand", "Right"),
            bowl_style=player_dict.get("bowl_style", "Medium Pacer"),
        )

        if card_bytes:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(card_bytes),
                caption=f"🎳 <b>{player_dict['name']}</b> is bowling",
                parse_mode="HTML")
    except Exception:
        logger.warning(f"Failed to send bowler card for {player_dict.get('name')}")


# ── Timeout helpers ──────────────────────────────────────────────────

def _cancel_action_timer(ctx, mid):
    try:
        for j in ctx.job_queue.get_jobs_by_name(f"act_{mid}"):
            j.schedule_removal()
    except Exception: pass

def _start_action_timer(ctx, mid, user_tg_id, action_label):
    _cancel_action_timer(ctx, mid)
    try:
        if ctx.job_queue:
            s = _gs(ctx, mid)
            ctx.job_queue.run_once(
                _action_timeout, ACTION_TIMEOUT, name=f"act_{mid}",
                data={"match_id": mid, "chat_id": s["chat_id"], "user_tg": user_tg_id, "action": action_label})
    except Exception: pass

async def _action_timeout(context):
    d = context.job.data; mid = d["match_id"]
    s = _gs(context, mid)
    if not s: return
    # Fine the user who didn't act
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == d["user_tg"]).first()
        if u:
            u.total_coins = max(0, u.total_coins - FINE_COINS)
            u.total_gems = max(0, u.total_gems - FINE_GEMS)
            log_activity(session, u.id, "match_fine", f"Timeout fine ({d['action']}): -{FINE_COINS} coins, -{FINE_GEMS} gems",
                         coins_change=-FINE_COINS, gems_change=-FINE_GEMS)
            session.commit()
            uname = u.username or u.first_name
            await context.bot.send_message(d["chat_id"],
                f"⏱️ <b>TIME'S UP!</b>\n\n@{uname} did not {d['action']} within 1 minute.\n"
                f"⚠️ Fine: -{FINE_COINS:,} coins 💰 -{FINE_GEMS} gems 💎\n\nMatch forfeited.",
                parse_mode="HTML")
        # End match
        m_session = get_session()
        try:
            m = m_session.query(Match).get(mid)
            if m and m.status == "playing": m.status = "completed"; m_session.commit()
        except Exception: m_session.rollback()
        finally: m_session.close()
        # Save stats before cleanup
        await _save_match_stats(s)
        # Cleanup
        for k in list(context.bot_data.keys()):
            if str(mid) in k: del context.bot_data[k]
    except Exception: session.rollback(); logger.exception("Timeout fine err")
    finally: session.close()


# ── Reward helper ────────────────────────────────────────────────────

async def _award_match_rewards(ctx, s, winner_tg, loser_tg, overs):
    session = get_session()
    try:
        w = session.query(User).filter(User.telegram_id == winner_tg).first()
        l = session.query(User).filter(User.telegram_id == loser_tg).first()
        w_coins = overs * 300; w_gems = overs * 1
        l_coins = overs * 150; l_gems = max(1, int(overs * 0.5))
        if w:
            w.total_coins += w_coins; w.total_gems += w_gems
            log_activity(session, w.id, "match_reward", f"Win reward: +{w_coins} coins, +{w_gems} gems",
                         coins_change=w_coins, gems_change=w_gems)
        if l:
            l.total_coins += l_coins; l.total_gems += l_gems
            log_activity(session, l.id, "match_reward", f"Loss reward: +{l_coins} coins, +{l_gems} gems",
                         coins_change=l_coins, gems_change=l_gems)
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

        def _update_bat(pid, uid, bs):
            """Update batting stats for a player."""
            if not bs or bs.get("balls", 0) == 0:
                return
            gs = session.query(PlayerGameStats).filter(
                PlayerGameStats.user_id == uid, PlayerGameStats.player_id == pid).first()
            if not gs:
                gs = PlayerGameStats(user_id=uid, player_id=pid)
                session.add(gs)
                session.flush()

            gs.bat_inns += 1
            gs.runs += bs.get("runs", 0)
            gs.balls_faced += bs.get("balls", 0)
            gs.fours += bs.get("fours", 0)
            gs.sixes += bs.get("sixes", 0)

            r = bs.get("runs", 0)
            if r >= 100:
                gs.hundreds += 1
            elif r >= 50:
                gs.fifties += 1
            if bs.get("out", False):
                gs.times_out += 1
                if r == 0:
                    gs.ducks += 1
            if r > gs.highest_score:
                gs.highest_score = r
                gs.highest_score_not_out = not bs.get("out", True)
            elif r == gs.highest_score and not bs.get("out", True):
                gs.highest_score_not_out = True

        def _update_bowl(pid, uid, bws):
            """Update bowling stats for a player."""
            if not bws:
                return
            balls = bws.get("balls", 0)
            wickets = bws.get("wickets", 0)
            runs = bws.get("runs", 0)
            # Skip only if truly nothing happened
            if balls == 0 and wickets == 0 and runs == 0:
                return
            gs = session.query(PlayerGameStats).filter(
                PlayerGameStats.user_id == uid, PlayerGameStats.player_id == pid).first()
            if not gs:
                gs = PlayerGameStats(user_id=uid, player_id=pid)
                session.add(gs)
                session.flush()

            gs.bowl_inns += 1
            gs.wickets_taken += wickets
            gs.runs_conceded += runs
            gs.balls_bowled += balls
            gs.overs_bowled = round(gs.balls_bowled / 6, 1)

            if wickets >= 5:
                gs.five_fers += 1
            elif wickets >= 3:
                gs.three_fers += 1

            if wickets > gs.best_bowl_wickets or (wickets == gs.best_bowl_wickets and runs < gs.best_bowl_runs):
                gs.best_bowl_wickets = wickets
                gs.best_bowl_runs = runs

        # Process 1st innings batting
        for rid, bs in inn1_bat_stats.items():
            rid_int = int(rid) if isinstance(rid, str) else rid
            if rid_int in bat_lookup_1:
                pid, uid = bat_lookup_1[rid_int]
                _update_bat(pid, uid, bs)

        # Process 1st innings bowling
        for rid, bws in inn1_bowl_stats.items():
            rid_int = int(rid) if isinstance(rid, str) else rid
            if rid_int in bowl_lookup_1:
                pid, uid = bowl_lookup_1[rid_int]
                _update_bowl(pid, uid, bws)

        # Process 2nd innings batting
        for rid, bs in inn2_bat_stats.items():
            rid_int = int(rid) if isinstance(rid, str) else rid
            if rid_int in bat_lookup_2:
                pid, uid = bat_lookup_2[rid_int]
                _update_bat(pid, uid, bs)

        # Process 2nd innings bowling
        for rid, bws in inn2_bowl_stats.items():
            rid_int = int(rid) if isinstance(rid, str) else rid
            if rid_int in bowl_lookup_2:
                pid, uid = bowl_lookup_2[rid_int]
                _update_bowl(pid, uid, bws)

        session.commit()
        logger.info(f"Saved match stats for match {s.get('match_id')}")
    except Exception:
        session.rollback()
        logger.exception("Failed to save match stats")
    finally:
        session.close()


# ═══════════════════════════ /resume ═════════════════════════════════

async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually re-show the prompt for the current match state — recovery from API hiccups."""
    tg = update.effective_user
    cid = update.effective_chat.id
    mid = None
    s = None
    for k, v in context.bot_data.items():
        if k.startswith("ms_") and isinstance(v, dict):
            if v.get("bat_user_tg") == tg.id or v.get("bowl_user_tg") == tg.id:
                if v.get("chat_id") == cid:
                    mid = int(k.split("_")[1])
                    s = v
                    break
    if not s:
        await update.message.reply_text(
            "❌ No active match found in this chat.\n"
            "If you're mid-match, make sure you're in the right group.")
        return

    # Clear any stuck processing lock
    context.bot_data.pop(f"processing_{mid}", None)
    _cancel_action_timer(context, mid)

    try:
        # Check if we need a new batsman
        striker_out = False
        striker_idx = s.get("striker_idx")
        if striker_idx is not None and striker_idx < len(s.get("batting_order", [])):
            striker = s["batting_order"][striker_idx]
            bs = s["bat_stats"].get(striker["roster_id"], {})
            if bs.get("out"):
                striker_out = True

        # Check if end of over (ball=0 after an over completed)
        end_of_over = (s.get("current_ball", 0) == 0 and s.get("current_over", 1) > 1
                       and s.get("prev_bowler_rid") is not None)

        if is_innings_over(s):
            await update.message.reply_text("🔁 Resuming... innings ended, wrapping up.")
            await _end_innings(context, mid)
            return

        if striker_out and s["total_wickets"] < 10:
            await update.message.reply_text("🔁 Resuming... showing new batsman prompt.")
            await _show_new_batsman(context, mid)
            return

        if end_of_over:
            await update.message.reply_text("🔁 Resuming... showing new over bowler prompt.")
            await _show_new_over_bowler(context, mid)
            return

        await update.message.reply_text("🔁 Resuming... showing next delivery prompt.")
        await _show_delivery(context, cid, mid)
    except Exception:
        logger.exception("Resume failed")
        await update.message.reply_text("⚠️ Couldn't auto-resume. Use /endmatch if stuck.")


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
        uname = u.username if u else "Unknown"
        await q.edit_message_text(
            f"🛑 <b>MATCH ENDED</b>\n\n@{uname} ended the match.\n"
            f"⚠️ Fine: -{FINE_COINS:,} Coins 💰 -{FINE_GEMS} Gems 💎\n"
            f"📊 Player stats saved.", parse_mode="HTML")
    except Exception: session.rollback()
    finally: session.close()
    for k in list(context.bot_data.keys()):
        if str(mid) in k: del context.bot_data[k]

async def endmatch_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🔄 Match continues!")


# ═══════════════════════════ /playmatch ══════════════════════════════

async def playmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user; cid = update.effective_chat.id
    if not context.args: await update.message.reply_text("Usage: /playmatch @username"); return
    t = context.args[0].lstrip("@").strip()
    session = get_session()
    try:
        u1 = session.query(User).filter(User.telegram_id == tg.id).first()
        if not u1: await update.message.reply_text("❌ /debut first!"); return
        if t.lower() == (u1.username or "").lower(): await update.message.reply_text("❌ Can't play yourself"); return
        u2 = session.query(User).filter(User.username.ilike(t)).first()
        if not u2: await update.message.reply_text(f"❌ @{t} not found."); return
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
        if m and m.status == "pending": m.status = "expired"; session.commit(); await ctx.bot.send_message(d["chat_id"], "⏰ Match expired.")
    except Exception: session.rollback()
    finally: session.close()

async def match_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user; parts = q.data.split("_"); mid, auid = int(parts[1]), int(parts[2])
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
        await q.edit_message_text(f"✅ <b>MATCH ACCEPTED!</b>\n\n🏟️ {t1} vs {t2}\n\n@{u2.username}, select overs (1-20):\n📝 Reply: <code>20</code>", parse_mode="HTML")
        context.bot_data[f"awaiting_overs_{u2.telegram_id}"] = mid
    except Exception: session.rollback()
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
    except Exception: session.rollback()
    finally: session.close()

async def overs_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user; cid = update.effective_chat.id
    key = f"awaiting_overs_{tg.id}"; mid = context.bot_data.get(key)
    if not mid: return
    txt = update.message.text.strip().lower().replace("overs","").replace("over","").strip()
    try: overs = int(txt)
    except ValueError: await update.message.reply_text("❌ Enter 1-20"); return
    if overs < 1 or overs > 20: await update.message.reply_text("❌ 1-20"); return
    del context.bot_data[key]
    session = get_session()
    try:
        m = session.query(Match).get(mid)
        if not m or m.status != "accepted": return
        m.overs = overs; m.status = "toss"; session.commit()
        u1 = session.query(User).get(m.user1_id); u2 = session.query(User).get(m.user2_id)
        t1 = u1.team_name or f"@{u1.username}'s XI"; t2 = u2.team_name or f"@{u2.username}'s XI"

        w_coins = overs * 300; l_coins = overs * 150
        await update.message.reply_text(
            f"✅ <b>MATCH CONFIRMED!</b>\n\n🏏 {t1} vs {t2}\n📍 {overs} Overs | {m.stadium}\n"
            f"📍 {m.pitch_type} | {m.weather} {m.temperature}°C\n🎩 {m.umpire1} | {m.umpire2}\n\n"
            f"🎁 <b>Rewards:</b>\n🏆 Winner: {w_coins:,} Coins + {overs} Gems\n"
            f"📉 Loser: {l_coins:,} Coins + {max(1,int(overs*0.5))} Gems\n\n🔄 Toss...", parse_mode="HTML")

        wid = random.choice([m.user1_id, m.user2_id]); m.toss_winner_id = wid; session.commit()
        w = session.query(User).get(wid)
        await context.bot.send_message(cid, f"🪙 <b>TOSS!</b>\n\n🏆 @{w.username} wins!\n\n@{w.username}, choose:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"toss_bat_{mid}_{wid}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"toss_bowl_{mid}_{wid}")]]))
    except Exception: session.rollback(); logger.exception("Overs err")
    finally: session.close()


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
        # Show ALL 11 players for opener selection
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']} | {p['category']}", callback_data=f"op1_{mid}_{bu.id}_{p['roster_id']}")] for p in bxi]
        await context.bot.send_message(cid, f"🏏 <b>SELECT OPENER 1</b>\n\n@{bu.username}, pick:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
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
        await q.edit_message_text(f"✅ Opener 1: {pk['name']}\n\n🏏 <b>SELECT OPENER 2</b>\n\n@{u.username}, pick:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
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
        # Show ALL 11 players sorted by bowl rating
        all_bowlers = sorted(bwxi, key=lambda x: x["bowl_rating"], reverse=True)
        btns = [[InlineKeyboardButton(
            f"{p['name']} | {p.get('bowl_hand','R')[:1]}-{p.get('bowl_style','Medium')} | BWL {p['bowl_rating']}",
            callback_data=f"selbowl_{mid}_{bwu.id}_{p['roster_id']}"
        )] for p in all_bowlers]
        await context.bot.send_message(cid, f"🎳 <b>SELECT OPENING BOWLER</b>\n\n@{bwu.username}, pick:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
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
            _ss(context, mid, s)

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
            _ss(context, mid, s)

            await q.edit_message_text(f"✅ Bowler: {bowler['name']}\n\n⏳ Starting...", parse_mode="HTML")
            await context.bot.send_message(cid,
                f"🏏 <b>MATCH STARTING!</b>\n\n🏟️ {m.stadium}\n{bt} vs {bwt} | {m.overs} Overs\n"
                f"🏏 {op1['name']} & {op2['name']}\n🎳 {bowler['name']}\n━━━━━━━━━━━━━━━━━━━",
                parse_mode="HTML")
            # Send opener cards
            await _send_batsman_card(context, cid, op1, s["bat_team_id"])
            await _send_batsman_card(context, cid, op2, s["bat_team_id"])
            await _send_bowler_card(context, cid, bowler, s["bowl_team_id"])

        await _show_delivery(context, cid, mid)
    except Exception: session.rollback(); logger.exception("SelBowl err")
    finally: session.close()


# ═══════════════════════════ RECOVERY ═════════════════════════════════

async def _recover_stuck(ctx, mid, where):
    """Called when any callback fails mid-flow. Notifies players + suggests /resume."""
    s = _gs(ctx, mid)
    if not s: return
    try:
        await ctx.bot.send_message(
            s["chat_id"],
            f"⚠️ <b>Hit a hiccup</b> ({where}).\n"
            f"Type <code>/resume</code> to continue from where you left off.",
            parse_mode="HTML")
    except Exception:
        pass


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume a stuck match. Finds the live match for this chat and re-shows whatever screen is next."""
    cid = update.effective_chat.id
    # Find any ms_* state with this chat_id
    found_mid = None
    found_state = None
    for k, v in list(context.bot_data.items()):
        if k.startswith("ms_") and isinstance(v, dict) and v.get("chat_id") == cid:
            found_mid = int(k.split("_", 1)[1])
            found_state = v
            break

    if not found_mid or not found_state:
        await update.message.reply_text("❌ No active match in this chat to resume.")
        return

    # Clear any stuck processing lock
    context.bot_data.pop(f"processing_{found_mid}", None)
    _cancel_action_timer(context, found_mid)

    # Determine what step we're at
    s = found_state
    current_delivery = s.get("current_delivery")
    selected_variation = s.get("selected_variation")

    await update.message.reply_text("🔄 <b>Resuming match...</b>", parse_mode="HTML")

    try:
        if current_delivery:
            # Delivery already chosen → show shot buttons
            await _show_shot(context, cid, found_mid)
        elif selected_variation:
            # Variation chosen, need length — re-show delivery (pacer variation already picked)
            # Simpler: just restart delivery selection
            s["selected_variation"] = None
            _ss(context, found_mid, s)
            await _show_delivery(context, cid, found_mid)
        else:
            # Fresh ball — show delivery selection
            await _show_delivery(context, cid, found_mid)
    except Exception:
        logger.exception(f"resume_handler failed mid={found_mid}")
        await update.message.reply_text(
            "⚠️ Could not resume. Type /endmatch if match can't continue.")


# ═══════════════════════════ DELIVERY ════════════════════════════════

async def _show_delivery(ctx, cid, mid):
    s = _gs(ctx, mid)
    if not s: return
    bw = get_bowler(s); st = get_striker(s); ph = get_phase(s)
    ov = s["current_over"]; bl = s["current_ball"] + 1
    opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
    hdr = (f"🎳 <b>OVER {ov} • BALL {bl}</b>\n\n📊 {format_score(s)} | {format_overs(s)} ov | CRR {crr(s)}\n\n"
           f"🎳 {bw['name']} ({bw['bowl_rating']} BWL)\n🏏 vs {st['name']} ({st['bat_rating']} BAT)\n📍 {ph}\n\n"
           f"━━━━━━━━━━━━━━━━━━━\n\n@{s['bowl_username']}, choose your delivery:\n\n")
    if opts["is_spinner"]:
        ds = opts["deliveries"]; btns = []; row = []
        for i, d in enumerate(ds):
            row.append(InlineKeyboardButton(d, callback_data=f"bspin_{mid}_{i}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row)
        await ctx.bot.send_message(cid, hdr + "🎯 <b>SELECT DELIVERY</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        vs = opts["variations"]; btns = []; row = []
        for i, v in enumerate(vs):
            row.append(InlineKeyboardButton(v, callback_data=f"bvar_{mid}_{i}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row)
        await ctx.bot.send_message(cid, hdr + "🎯 <b>SELECT VARIATION</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(ctx, mid, s["bowl_user_tg"], "select delivery")

async def variation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, vi = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not your bowl!"); return

    lock_key = f"processing_{mid}"
    if context.bot_data.get(lock_key):
        await q.answer("⏳ Processing...")
        return
    context.bot_data[lock_key] = True

    try:
        await q.answer(); _cancel_action_timer(context, mid)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
        var = opts["variations"][vi]; s["selected_variation"] = var; _ss(context, mid, s)
        ls = opts["lengths"]; btns = []; row = []
        for i, l in enumerate(ls):
            row.append(InlineKeyboardButton(l, callback_data=f"blen_{mid}_{i}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row)
        try:
            await q.edit_message_text(f"🎳 <b>SELECT LENGTH</b> ({var})", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        except Exception:
            # Fallback: send new message if edit failed
            await context.bot.send_message(s["chat_id"],
                f"🎳 <b>SELECT LENGTH</b> ({var})",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        _start_action_timer(context, mid, s["bowl_user_tg"], "select length")
    except Exception:
        logger.exception(f"variation_callback failed mid={mid}")
        await _recover_stuck(context, mid, "variation")
    finally:
        context.bot_data.pop(lock_key, None)


async def length_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, li = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return

    lock_key = f"processing_{mid}"
    if context.bot_data.get(lock_key):
        await q.answer("⏳ Processing...")
        return
    context.bot_data[lock_key] = True

    try:
        await q.answer(); _cancel_action_timer(context, mid)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
        length = opts["lengths"][li]; var = s.get("selected_variation", "Seam")
        s["current_delivery"] = f"{var} {length}"; s["selected_variation"] = None; _ss(context, mid, s)
        try:
            await q.edit_message_text(f"✅ {bw['name']}: {var} {length}\n⏳ Batsman...", parse_mode="HTML")
        except Exception: pass
        context.bot_data.pop(lock_key, None)  # release before next step
        await _show_shot(context, s["chat_id"], mid)
    except Exception:
        logger.exception(f"length_callback failed mid={mid}")
        context.bot_data.pop(lock_key, None)
        await _recover_stuck(context, mid, "length")


async def spinner_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, di = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return

    lock_key = f"processing_{mid}"
    if context.bot_data.get(lock_key):
        await q.answer("⏳ Processing...")
        return
    context.bot_data[lock_key] = True

    try:
        await q.answer(); _cancel_action_timer(context, mid)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
        d = opts["deliveries"][di]
        if d == "Surprise":
            ns = [x for x in opts["deliveries"] if x != "Surprise"]
            d = random.choice(ns) + " (Surprise)"
        s["current_delivery"] = d; _ss(context, mid, s)
        try:
            await q.edit_message_text(f"✅ {bw['name']}: {d}\n⏳ Batsman...", parse_mode="HTML")
        except Exception: pass
        context.bot_data.pop(lock_key, None)
        await _show_shot(context, s["chat_id"], mid)
    except Exception:
        logger.exception(f"spinner_delivery_callback failed mid={mid}")
        context.bot_data.pop(lock_key, None)
        await _recover_stuck(context, mid, "spinner_delivery")


# ═══════════════════════════ SHOT ════════════════════════════════════

async def _show_shot(ctx, cid, mid):
    s = _gs(ctx, mid); st = get_striker(s); bw = get_bowler(s); dl = s.get("current_delivery", "?")
    bs = s["bat_stats"][st["roster_id"]]
    txt = (f"🏏 <b>OVER {s['current_over']} • BALL {s['current_ball'] + 1}</b>\n\n"
           f"📊 {format_score(s)} | {format_overs(s)} ov | CRR {crr(s)}\n\n"
           f"🎳 {bw['name']}: {dl}\n🏏 {st['name']} ({st['bat_rating']} BAT) — {bs['runs']}({bs['balls']})\n\n"
           f"━━━━━━━━━━━━━━━━━━━\n\n@{s['bat_username']}, choose your shot:")
    btns = []; row = []
    for i, sh in enumerate(AVAILABLE_SHOTS):
        row.append(InlineKeyboardButton(sh, callback_data=f"bshot_{mid}_{i}"))
        if len(row) == 3: btns.append(row); row = []
    if row: btns.append(row)
    await ctx.bot.send_message(cid, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(ctx, mid, s["bat_user_tg"], "choose shot")


async def shot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; tg = q.from_user
    parts = q.data.split("_"); mid, si = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or tg.id != s["bat_user_tg"]: await q.answer("Not your bat!"); return

    # Prevent double-click
    lock_key = f"processing_{mid}"
    if context.bot_data.get(lock_key):
        await q.answer("⏳ Processing...")
        return
    context.bot_data[lock_key] = True

    await q.answer(); _cancel_action_timer(context, mid)
    # Remove buttons immediately
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    shot = AVAILABLE_SHOTS[si]; dl = s.get("current_delivery", "?")
    striker = get_striker(s); bowler = get_bowler(s)
    bs = s["bat_stats"][striker["roster_id"]]
    bws = s["bowl_stats"].setdefault(bowler["roster_id"], {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0, "this_over_balls": 0})
    oc = _calc(s, striker, bowler, shot, dl)
    legal = True; need_new_bat = False

    if oc["type"] == "wide":
        s["total_runs"] += 1; s["extras_total"] += 1; s["wides"] += 1; bws["runs"] += 1
        add_to_timeline(s, SYM["WD"]); legal = False; rtxt = "↔️ <b>WIDE!</b> +1"
    elif oc["type"] == "noball":
        runs = oc.get("runs", 1); s["total_runs"] += runs + 1; s["extras_total"] += 1; s["noballs"] += 1
        bws["runs"] += runs + 1; bs["balls"] += 1
        if runs > 0: bs["runs"] += runs
        add_to_timeline(s, SYM["NB"] + (SYM.get(runs, str(runs)) if runs > 0 else "")); legal = False
        rtxt = f"🄽🄱 <b>NO BALL!</b> +{runs + 1}"
    elif oc["type"] == "legbye":
        runs = oc.get("runs", 1); s["total_runs"] += runs; s["extras_total"] += runs; s["legbyes"] += runs
        bws["runs"] += runs; bs["balls"] += 1; s["partnership_balls"] += 1; s["partnership_runs"] += runs
        add_to_timeline(s, str(runs) + " 𓂾" if runs > 1 else "𓂾")
        rtxt = f"𓂾 <b>LEG BYE!</b> +{runs}"
        if runs % 2 == 1: s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]
    elif oc["type"] == "wicket":
        runs = oc.get("runs", 0); s["total_runs"] += runs; s["total_wickets"] += 1
        bws["wickets"] += 1; bws["runs"] += runs; bs["balls"] += 1; bs["out"] = True
        bs["how_out"] = oc.get("how", "Bowled"); bs["bowled_by"] = bowler["name"]
        add_to_timeline(s, SYM["W"]); s["partnership_runs"] = 0; s["partnership_balls"] = 0
        need_new_bat = True
        # Track fall of wickets: (score, over-string) for this wicket
        if "fow" not in s:
            s["fow"] = []
        # Calculate over string AS OF this ball (before current_ball is incremented below)
        over_now = s["current_over"] - 1
        ball_now = s["current_ball"] + 1  # the ball just bowled
        if ball_now >= 6:
            over_now += 1; ball_now = 0
        fow_over = f"{over_now}.{ball_now}" if ball_now else str(over_now)
        s["fow"].append((s["total_runs"], fow_over))
        rtxt = f"🟥 <b>WICKET!</b> {striker['name']} — {oc.get('how', 'OUT')}!"
    else:
        runs = oc.get("runs", 0); s["total_runs"] += runs; bs["runs"] += runs; bs["balls"] += 1
        bws["runs"] += runs; s["partnership_runs"] += runs; s["partnership_balls"] += 1
        if runs == 4: bs["fours"] += 1
        elif runs == 6: bs["sixes"] += 1
        add_to_timeline(s, SYM.get(runs, str(runs)))
        if runs == 0: rtxt = "0️⃣ <b>DOT!</b>"
        elif runs == 4: rtxt = "4️⃣ <b>FOUR!</b> 🔥"
        elif runs == 6: rtxt = "6️⃣ <b>SIX!</b> 💥"
        else: rtxt = f"{SYM.get(runs, str(runs))} <b>{runs} RUN{'S' if runs != 1 else ''}!</b>"
        if runs % 2 == 1: s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]

    if legal: s["current_ball"] += 1; bws["this_over_balls"] += 1; bws["balls"] = bws.get("balls", 0) + 1
    eoo = False
    if s["current_ball"] >= 6:
        bws["overs_done"] += 1; bws["this_over_balls"] = 0
        s["current_over"] += 1; s["current_ball"] = 0
        s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]
        s["prev_bowler_rid"] = bowler["roster_id"]; eoo = True
    _ss(context, mid, s)

    sc = build_live_scorecard(s)
    try:
        await q.edit_message_text(f"🎳 {bowler['name']} → {dl}\n🏏 {striker['name']} played {shot}\n\n{rtxt}\n\n{sc}", parse_mode="HTML")
    except Exception:
        try:
            await context.bot.send_message(s["chat_id"], f"🎳 {bowler['name']} → {dl}\n🏏 {striker['name']} played {shot}\n\n{rtxt}\n\n{sc}", parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send scorecard update")

    # Release lock before next step
    context.bot_data.pop(f"processing_{mid}", None)

    # Route to next step — wrap each in try/except so a Telegram API hiccup doesn't strand the match
    try:
        if is_innings_over(s):
            await _end_innings(context, mid)
            return
        if need_new_bat and s["total_wickets"] < 10:
            await _show_new_batsman(context, mid)
        elif eoo:
            await _show_new_over_bowler(context, mid)
        else:
            await _show_delivery(context, s["chat_id"], mid)
    except Exception:
        logger.exception(f"Next-step routing failed for match {mid}")
        context.bot_data.pop(f"processing_{mid}", None)
        await _recover_stuck(context, mid, "next-step")

def _calc(s, striker, bowler, shot, delivery):
    from services.probability_engine import calculate_outcome
    # Parse delivery into variation + length
    # For spinners: delivery is just "Off Break" or "Googly (Surprise)"
    # For pacers: delivery is "Outswing Good" or "Leg Cutter Yorker"
    parts = delivery.replace(" (Surprise)", "").strip()
    from services.bowling_service import is_spinner as _is_spin
    if _is_spin(bowler.get("bowl_style", "")):
        variation = parts
        length = None
    else:
        # Last word is length for pacers, rest is variation
        # Handle multi-word lengths like "Good Length", "Hit the Deck"
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
            # Simple split: last word is length
            words = parts.rsplit(" ", 1)
            if len(words) == 2:
                variation, length = words
            else:
                variation = parts
                length = "Good"

    pitch = s.get("pitch_type", "Flat")
    over = s["current_over"]
    total_overs = s["overs"]

    return calculate_outcome(
        bowler.get("bowl_style", "Medium Pacer"),
        bowler.get("bowl_hand", "Right"),
        variation, length, pitch,
        over, total_overs, shot,
        striker["bat_rating"], bowler["bowl_rating"]
    )


# ═══════════════════════════ NEW BATSMAN ═════════════════════════════

async def _show_new_batsman(ctx, mid):
    s = _gs(ctx, mid)
    if not s: return
    available = []
    for i, p in enumerate(s["batting_order"]):
        if i == s["striker_idx"] or i == s["non_striker_idx"]: continue
        bs = s["bat_stats"].get(p["roster_id"], {})
        if not bs.get("out", False): available.append((i, p))
    if not available:
        # No more batsmen — innings should be over, force it
        logger.warning(f"Match {mid}: No available batsmen but wickets={s['total_wickets']}, forcing innings end")
        await _end_innings(ctx, mid)
        return
    # Show ALL available (not capped to 8) so user never loses options
    btns = [[InlineKeyboardButton(
        f"{p['name']} — {s['bat_stats'].get(p['roster_id'], {}).get('runs', 0)}({s['bat_stats'].get(p['roster_id'], {}).get('balls', 0)})",
        callback_data=f"newbat_{mid}_{i}"
    )] for i, p in available]
    await ctx.bot.send_message(s["chat_id"],
        f"🏏 <b>WICKET!</b> Select next batsman:\n\n@{s['bat_username']}, choose:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(ctx, mid, s["bat_user_tg"], "select batsman")

async def new_batsman_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, bi = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bat_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    nb = s["batting_order"][bi]; s["striker_idx"] = bi; _ss(context, mid, s)
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
        if s["current_ball"] == 0 and s["current_over"] > 1:
            await _show_new_over_bowler(context, mid)
        else:
            await _show_delivery(context, s["chat_id"], mid)
    except Exception:
        logger.exception(f"new_batsman_callback next-step failed for match {mid}")
        try:
            await context.bot.send_message(
                s["chat_id"],
                "⚠️ Hit a hiccup. Type <code>/resume</code> to continue.",
                parse_mode="HTML")
        except Exception:
            pass


# ═══════════════════════════ NEW OVER BOWLER ═════════════════════════

async def _show_new_over_bowler(ctx, mid):
    s = _gs(ctx, mid); prev = s.get("prev_bowler_rid")
    # Show ALL players except the one who just bowled (can't bowl consecutive)
    avail = [p for p in s["bowl_xi"] if p["roster_id"] != prev]
    avail = sorted(avail, key=lambda x: x["bowl_rating"], reverse=True)
    btns = [[InlineKeyboardButton(_bowl_label(p, s), callback_data=f"nbowl_{mid}_{p['roster_id']}")] for p in avail]
    await ctx.bot.send_message(s["chat_id"],
        f"🎳 <b>OVER {s['current_over']}</b> — Select bowler:\n📊 {format_score(s)} | {format_overs(s)} ov\n\n@{s['bowl_username']}, choose:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(ctx, mid, s["bowl_user_tg"], "select bowler")

async def new_over_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, rid = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    bw = next((p for p in s["bowl_xi"] if p["roster_id"] == rid), None)
    if not bw: return
    s["current_bowler"] = bw
    bws = s["bowl_stats"].setdefault(bw["roster_id"], {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0, "this_over_balls": 0})
    bws["this_over_balls"] = 0; _ss(context, mid, s)
    await q.edit_message_text(f"🎳 Over {s['current_over']}: {bw['name']} | {bw.get('bowl_hand','R')[:1]}-{bw['bowl_style']}", parse_mode="HTML")
    await _send_bowler_card(context, s["chat_id"], bw, s["bowl_team_id"])
    await _show_delivery(context, s["chat_id"], mid)


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
            # Only include players who actually batted (faced a ball) OR were marked out
            if bs.get("balls", 0) == 0 and not bs.get("out"):
                # Did not bat — skip
                continue
            runs = bs.get("runs", 0)
            balls = bs.get("balls", 0)
            sr = (runs / balls * 100) if balls > 0 else 0.0
            dismissal = bs.get("how_out", "") if bs.get("out") else "not out"
            batsmen_rows.append({
                "rating": p.get("rating", 0),
                "name": p.get("name", "?"),
                "dismissal": dismissal,
                "runs": runs,
                "balls": balls,
                "fours": bs.get("fours", 0),
                "sixes": bs.get("sixes", 0),
                "strike_rate": round(sr, 1),
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

        # Generate batting scorecard
        bat_card_bytes = generate_batting_scorecard(
            bat_team, bowl_team,
            total_runs, total_wickets, overs_str,
            batsmen_rows, fow, extras,
            is_first_innings=is_first, match_title=match_title,
        )

        # Generate bowling scorecard — team name is the bowling team
        bowl_card_bytes = generate_bowling_scorecard(
            bowl_team, bowlers_rows, fow,
            is_first_innings=is_first, match_title=match_title,
        )

        # Send in the order asked: Bowling first, then Batting (per user spec)
        if bowl_card_bytes:
            await ctx.bot.send_photo(
                chat_id=cid, photo=io.BytesIO(bowl_card_bytes),
                caption=f"🎳 <b>{bowl_team}</b> — Bowling Scorecard",
                parse_mode="HTML")
        if bat_card_bytes:
            await ctx.bot.send_photo(
                chat_id=cid, photo=io.BytesIO(bat_card_bytes),
                caption=f"🏏 <b>{bat_team}</b> — Batting Scorecard",
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
        s["bowl_stats"] = {p["roster_id"]: {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0, "this_over_balls": 0} for p in s["bowl_xi"]}
        s["fow"] = []  # reset for 2nd innings
        _ss(ctx, mid, s)
        # CRITICAL: Update bot_data so opener callbacks read correct XI
        ctx.bot_data[f"bat_xi_{mid}"] = s["bat_xi"]
        ctx.bot_data[f"bowl_xi_{mid}"] = s["bowl_xi"]
        ctx.bot_data[f"bat_uname_{mid}"] = s["bat_username"]
        ctx.bot_data[f"bowl_uname_{mid}"] = s["bowl_username"]
        ctx.bot_data[f"bat_uid_{mid}"] = s["bat_team_id"]
        ctx.bot_data[f"bowl_uid_{mid}"] = s["bowl_team_id"]
        # Show ALL 11 players for 2nd innings opener
        buid = s["bat_team_id"]
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']} | {p['category']}", callback_data=f"op1_{mid}_{buid}_{p['roster_id']}")] for p in s["bat_xi"]]
        await ctx.bot.send_message(cid, f"🏏 <b>2ND INNINGS — SELECT OPENER 1</b>\n\n@{s['bat_username']}, pick:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        # Match complete — give rewards
        target = s["target"]; chasing = s["total_runs"]; overs = s.get("overs", 10)
        if chasing >= target:
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

            # Update user counters
            today = datetime.utcnow().date()
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
        msg += (f"🎁 <b>REWARDS</b>\n"
                f"🏆 {winner_name}: +{wc:,} Coins 💰 +{wg} Gems 💎\n"
                f"📉 {loser_name}: +{lc:,} Coins 💰 +{lg} Gems 💎\n"
                f"━━━━━━━━━━━━━━━━━━━")

        # Send 2nd innings scorecards (bowling then batting) BEFORE result message
        await _send_innings_scorecards(ctx, mid, innings_num=2)

        sent = await ctx.bot.send_message(cid, msg, parse_mode="HTML")

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

        for k in list(ctx.bot_data.keys()):
            if str(mid) in k: del ctx.bot_data[k]
