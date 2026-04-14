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
            player_dict["name"], player_dict["rating"],
            player_dict["bat_rating"], stats)

        if card_bytes:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(card_bytes),
                caption=f"🏏 <b>{player_dict['name']}</b> walks to the crease",
                parse_mode="HTML")
    except Exception:
        logger.warning(f"Failed to send batsman card for {player_dict.get('name')}")


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
            f"⚠️ Fine: -{FINE_COINS:,} Coins 💰 -{FINE_GEMS} Gems 💎", parse_mode="HTML")
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
        bats = [p for p in bxi if p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if len(bats) < 2: bats = bxi[:6]
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']}", callback_data=f"op1_{mid}_{bu.id}_{p['roster_id']}")] for p in bats[:8]]
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
        rem = [p for p in bxi if p["roster_id"] != rid and p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if not rem: rem = [p for p in bxi if p["roster_id"] != rid]
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']}", callback_data=f"op2_{mid}_{buid}_{p['roster_id']}")] for p in rem[:8]]
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
        bowlers = sorted([p for p in bwxi if p["category"] in ("Bowler", "All-rounder")], key=lambda x: x["bowl_rating"], reverse=True)
        if not bowlers: bowlers = sorted(bwxi, key=lambda x: x["bowl_rating"], reverse=True)
        btns = [[InlineKeyboardButton(f"{p['name']} | {p.get('bowl_hand','R')[:1]}-{p['bowl_style']}", callback_data=f"selbowl_{mid}_{bwu.id}_{p['roster_id']}")] for p in bowlers[:8]]
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

        await _show_delivery(context, cid, mid)
    except Exception: session.rollback(); logger.exception("SelBowl err")
    finally: session.close()


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
    await q.answer(); _cancel_action_timer(context, mid)
    bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
    var = opts["variations"][vi]; s["selected_variation"] = var; _ss(context, mid, s)
    ls = opts["lengths"]; btns = []; row = []
    for i, l in enumerate(ls):
        row.append(InlineKeyboardButton(l, callback_data=f"blen_{mid}_{i}"))
        if len(row) == 3: btns.append(row); row = []
    if row: btns.append(row)
    await q.edit_message_text(f"🎳 <b>SELECT LENGTH</b> ({var})", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(context, mid, s["bowl_user_tg"], "select length")

async def length_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, li = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
    length = opts["lengths"][li]; var = s.get("selected_variation", "Seam")
    s["current_delivery"] = f"{var} {length}"; s["selected_variation"] = None; _ss(context, mid, s)
    await q.edit_message_text(f"✅ {bw['name']}: {var} {length}\n⏳ Batsman...", parse_mode="HTML")
    await _show_shot(context, s["chat_id"], mid)

async def spinner_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, di = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bowl_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    bw = get_bowler(s); opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
    d = opts["deliveries"][di]
    if d == "Surprise": ns = [x for x in opts["deliveries"] if x != "Surprise"]; d = random.choice(ns) + " (Surprise)"
    s["current_delivery"] = d; _ss(context, mid, s)
    await q.edit_message_text(f"✅ {bw['name']}: {d}\n⏳ Batsman...", parse_mode="HTML")
    await _show_shot(context, s["chat_id"], mid)


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
    await q.answer(); _cancel_action_timer(context, mid)

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

    if legal: s["current_ball"] += 1; bws["this_over_balls"] += 1
    eoo = False
    if s["current_ball"] >= 6:
        bws["overs_done"] += 1; bws["this_over_balls"] = 0
        s["current_over"] += 1; s["current_ball"] = 0
        s["striker_idx"], s["non_striker_idx"] = s["non_striker_idx"], s["striker_idx"]
        s["prev_bowler_rid"] = bowler["roster_id"]; eoo = True
    _ss(context, mid, s)

    sc = build_live_scorecard(s)
    await q.edit_message_text(f"🎳 {bowler['name']} → {dl}\n🏏 {striker['name']} played {shot}\n\n{rtxt}\n\n{sc}", parse_mode="HTML")

    if is_innings_over(s): await _end_innings(context, mid); return
    if need_new_bat and s["total_wickets"] < 10: await _show_new_batsman(context, mid)
    elif eoo: await _show_new_over_bowler(context, mid)
    else: await _show_delivery(context, s["chat_id"], mid)

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
    if not available: return
    btns = [[InlineKeyboardButton(
        f"{p['name']} — {s['bat_stats'].get(p['roster_id'], {}).get('runs', 0)}({s['bat_stats'].get(p['roster_id'], {}).get('balls', 0)})",
        callback_data=f"newbat_{mid}_{i}"
    )] for i, p in available[:8]]
    await ctx.bot.send_message(s["chat_id"],
        f"🏏 <b>WICKET!</b> Select next batsman:\n\n@{s['bat_username']}, choose:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    _start_action_timer(ctx, mid, s["bat_user_tg"], "select batsman")

async def new_batsman_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; parts = q.data.split("_"); mid, bi = int(parts[1]), int(parts[2])
    s = _gs(context, mid)
    if not s or q.from_user.id != s["bat_user_tg"]: await q.answer("Not yours!"); return
    await q.answer(); _cancel_action_timer(context, mid)
    nb = s["batting_order"][bi]; s["striker_idx"] = bi; _ss(context, mid, s)
    await q.edit_message_text(f"🏏 New batsman: {nb['name']} ({nb['bat_rating']} BAT)", parse_mode="HTML")
    # Send batsman stats card
    await _send_batsman_card(context, s["chat_id"], nb, s["bat_team_id"])
    if is_innings_over(s): await _end_innings(context, mid); return
    if s["current_ball"] == 0 and s["current_over"] > 1: await _show_new_over_bowler(context, mid)
    else: await _show_delivery(context, s["chat_id"], mid)


# ═══════════════════════════ NEW OVER BOWLER ═════════════════════════

async def _show_new_over_bowler(ctx, mid):
    s = _gs(ctx, mid); prev = s.get("prev_bowler_rid")
    avail = [p for p in s["bowl_xi"] if p["roster_id"] != prev and p["category"] in ("Bowler", "All-rounder")]
    if not avail: avail = [p for p in s["bowl_xi"] if p["roster_id"] != prev]
    avail = sorted(avail, key=lambda x: x["bowl_rating"], reverse=True)
    btns = [[InlineKeyboardButton(_bowl_label(p, s), callback_data=f"nbowl_{mid}_{p['roster_id']}")] for p in avail[:8]]
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
    await _show_delivery(context, s["chat_id"], mid)


# ═══════════════════════════ END INNINGS ═════════════════════════════

async def _end_innings(ctx, mid):
    s = _gs(ctx, mid); cid = s["chat_id"]; _cancel_action_timer(ctx, mid)
    if s["innings"] == 1:
        s["inn1_runs"] = s["total_runs"]; s["inn1_wickets"] = s["total_wickets"]
        s["inn1_overs"] = format_overs(s); s["inn1_team"] = s["bat_team_name"]
        target = s["total_runs"] + 1
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
        _ss(ctx, mid, s)
        # CRITICAL: Update bot_data so opener callbacks read correct XI
        ctx.bot_data[f"bat_xi_{mid}"] = s["bat_xi"]
        ctx.bot_data[f"bowl_xi_{mid}"] = s["bowl_xi"]
        ctx.bot_data[f"bat_uname_{mid}"] = s["bat_username"]
        ctx.bot_data[f"bowl_uname_{mid}"] = s["bowl_username"]
        ctx.bot_data[f"bat_uid_{mid}"] = s["bat_team_id"]
        ctx.bot_data[f"bowl_uid_{mid}"] = s["bowl_team_id"]
        bats = [p for p in s["bat_xi"] if p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if len(bats) < 2: bats = s["bat_xi"][:6]
        buid = s["bat_team_id"]
        btns = [[InlineKeyboardButton(f"{p['name']} - {p['rating']}", callback_data=f"op1_{mid}_{buid}_{p['roster_id']}")] for p in bats[:8]]
        await ctx.bot.send_message(cid, f"🏏 <b>2ND INNINGS — SELECT OPENER 1</b>\n\n@{s['bat_username']}, pick:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        # Match complete — give rewards
        session = get_session()
        try:
            m = session.query(Match).get(mid)
            if m: m.status = "completed"; m.completed_at = datetime.utcnow(); session.commit()
        except Exception: session.rollback()
        finally: session.close()

        target = s["target"]; chasing = s["total_runs"]; overs = s.get("overs", 10)
        if chasing >= target:
            winner_name = s["bat_team_name"]; loser_name = s["bowl_team_name"]
            winner_tg = s["bat_user_tg"]; loser_tg = s["bowl_user_tg"]
            margin = f"by {10 - s['total_wickets']} wickets"
        else:
            winner_name = s["bowl_team_name"]; loser_name = s["bat_team_name"]
            winner_tg = s["bowl_user_tg"]; loser_tg = s["bat_user_tg"]
            margin = f"by {target - 1 - chasing} runs"

        wc, wg, lc, lg = await _award_match_rewards(ctx, s, winner_tg, loser_tg, overs)

        await ctx.bot.send_message(cid,
            f"━━━━━━━━━━━━━━━━━━━\n🏆 <b>MATCH RESULT</b>\n\n"
            f"🔴 {s['inn1_team']}: {s['inn1_runs']}/{s['inn1_wickets']} ({s['inn1_overs']})\n"
            f"🟢 {s['bat_team_name']}: {format_score(s)} ({format_overs(s)})\n\n"
            f"🏆 <b>{winner_name} wins {margin}!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎁 <b>REWARDS</b>\n"
            f"🏆 {winner_name}: +{wc:,} Coins 💰 +{wg} Gems 💎\n"
            f"📉 {loser_name}: +{lc:,} Coins 💰 +{lg} Gems 💎\n"
            f"━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")

        for k in list(ctx.bot_data.keys()):
            if str(mid) in k: del ctx.bot_data[k]
