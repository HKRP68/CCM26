"""Cricket Simulator Telegram Bot — main entry point (Phase 1 + 2 + Admin)."""

import os
import logging
import threading
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram import Update as _TGUpdate

from config import BOT_TOKEN
from database import init_db
from logger import setup_logging

# Phase 1 handlers
from handlers.debut import debut_handler
from handlers.claim import (
    claim_handler, retain_callback,
    release_callback, replace_callback, replace_confirm_callback,
)
from handlers.gspin import gspin_handler, gspin_spin_callback
from handlers.daily import daily_handler, daily_claim_callback
from handlers.myroster import myroster_handler, roster_page_callback
from handlers.playerinfo import playerinfo_handler, player_version_callback

# Phase 2 handlers
from handlers.release import (
    releasepl_handler, releasemultiple_handler,
    release_one_callback, release_cancel_callback,
    releasemultiple_confirm_callback,
)
from handlers.trade import (
    trade_handler, trade_rating_callback, trade_myplayer_callback,
    trade_theirplayer_callback, trade_send_callback,
    trade_accept_callback, trade_reject_callback,
    trade_cancel_callback, trade_back_callback,
)

# Phase 3 handlers
from handlers.lineup import playingxi_handler, swapplayers_handler, setcaptain_handler, bench_callback, autobuild_handler
from handlers.search import (
    searchpl_handler, searchovr_handler,
    searchpl_page_callback, searchovr_page_callback,
    search_cancel_callback, noop_callback,
)
from handlers.buy import (
    buypl_handler, buypl_confirm_callback, buypl_cancel_callback,
    player_page_callback, player_page_noop_callback,
)
from handlers.team import teamname_handler, purse_handler, stats_handler
from handlers.leaderboard import leaderboard_handler, leaderboard_callback
from handlers.profile import myprofile_handler, myprofile_callback

# Match handlers
from handlers.match import (
    playmatch_handler, match_accept_callback, match_deny_callback,
    overs_text_handler, toss_decision_callback,
    opener1_callback, opener2_callback, select_bowler_callback,
    variation_callback, length_callback, spinner_delivery_callback,
    shot_callback, new_over_bowler_callback, new_batsman_callback,
    endmatch_handler, endmatch_yes_callback, endmatch_no_callback,
    resume_handler, lastmatch_handler, info_handler,
)

# Trait handlers
from handlers.traits import (
    traits_handler, traitshop_handler, traitapply_handler,
    traitupgrade_handler, traitreplace_handler,
    traitbuy_callback, traitreroll_callback, traitshop_cancel_callback,
    trapply_inv_callback, trapply_pl_callback,
    trup_pt_callback, trup_inv_callback,
    trrep_pt_callback, trrep_inv_callback,
    trait_cancel_callback,
)

# Player market handlers
from handlers.playermarket import (
    playermarket_handler,
    playermarket_buy_callback,
    playermarket_cancel_callback,
    playermarket_select_callback,
    playermarket_back_callback,
    playermarket_noop_callback,
)

# Pack handlers
from handlers.packs import (
    buypack_handler,
    pack_view_callback,
    pack_back_callback,
    pack_close_callback,
    pack_buy_callback,
    pack_noop_callback,
    openpack_handler,
    pack_open_inventory_callback,
    pack_open_close_callback,
)

# vsbot handlers
from handlers.vsbot import (
    vsbot_handler,
    vsbot_pick_callback,
    vsbot_cancel_callback,
    vsbot_toss_callback,
    vsbot_op1_callback,
    vsbot_op2_callback,
    vsbot_selbowl_callback,
)

# Quest handlers
from handlers.quests import (
    myquest_handler,
    quest_tab_callback,
    quest_claim_callback,
    quest_claimall_callback,
    quest_close_callback,
    quest_filter_callback,
    quest_page_callback,
    quest_noop_callback,
)

# Tour handlers
from handlers.tours import (
    cmtours_handler, mytours_handler,
    cmt_matches_callback, cmt_overs_callback, cmt_cancel_callback,
    tour_accept_callback, tour_decline_callback,
    mytours_play_callback, mytours_info_callback,
    mytours_stats_callback, mytours_back_callback,
)

# Achievements handlers
from handlers.achievements import (
    achievements_handler,
    achievements_tab_callback,
    achievements_close_callback,
)

# /howto handler
from handlers.howto import (
    howto_handler,
    howto_tab_callback,
    howto_close_callback,
)

# Bot vs Bot
from handlers.botvsbot import (
    botvsbot_handler,
    bvb_pickA_callback,
    bvb_pickB_callback,
    bvb_cancel_callback,
)

# /botmatch spectator mode
from handlers.botmatch import (
    botmatch_handler,
    botmatch_pick_a_callback,
    botmatch_pick_b_callback,
    botmatch_cancel_callback,
)

logger = logging.getLogger(__name__)


async def start_handler(update, context):
    await update.message.reply_text(
        "🏏 <b>Welcome to Cricket Simulator Bot!</b>\n\n"
        "Use /debut (or /d) to create your account and receive your starting squad.\n\n"
        "<b>Commands</b> <i>(short aliases in brackets)</i>:\n"
        "/debut /d - Create account & get 8 players\n"
        "/claim /c - Claim 1 player + coins (hourly)\n"
        "/daily /dl - Daily reward (24h)\n"
        "/gspin /gs - Spin the wheel (8h)\n"
        "/myroster /mr - View your roster\n"
        "/playingxi /pxi /xi - Playing XI\n"
        "/playerinfo /pi [name] - Player details\n"
        "/stats /st [name] - Player game stats\n"
        "/searchpl /sp [name] - Search player\n"
        "/searchovr /so [rating] - Search by OVR\n"
        "/buypl /buy /b [name] - Buy a player\n"
        "/swapplayers /swap [n1] [n2] - Swap positions\n"
        "/setcaptain /cap [name] - Set captain\n"
        "/teamname /tn [name] - Set team name\n"
        "/purse /p - Check balance\n"
        "/release /rel [name|pos] - Release for coins\n"
        "/releasemultiple /relm [from] [to] - Range release\n"
        "/trade /tr @user - Trade players\n"
        "/playmatch /pm @user - Play a match\n"
        "/endmatch /em - End match (fine applies)\n"
        "/resume /rs - If buttons disappear mid-match\n"
        "/myprofile /me - Your profile\n"
        "/traits /tt - Your traits & inventory\n"
        "/traitshop /tshop - Daily trait shop\n"
        "/traitapply /tapply - Apply trait to player\n"
        "/traitupgrade /tup - Level up a trait\n"
        "/traitreplace /trep - Replace a trait\n"
        "/leaderboard /lb /top - Leaderboard",
        parse_mode="HTML",
    )


def start_admin_panel():
    """Run the Flask admin panel in a background thread."""
    try:
        from admin import app as flask_app
        port = int(os.getenv("ADMIN_PORT", os.getenv("PORT", 5000)))
        logger.info(f"Admin panel starting on port {port}...")
        flask_app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        )
    except Exception:
        logger.exception("Admin panel crashed")


def main():
    setup_logging()
    print("=" * 50)
    print("🏏 CRICKET BOT STARTING...")
    print("=" * 50)

    # Show env status
    print(f"  BOT_TOKEN: {'✅ set' if BOT_TOKEN else '❌ NOT SET'}")
    print(f"  DATABASE_URL: {os.getenv('DATABASE_URL', 'sqlite:///cricket_bot.db')}")
    print(f"  ADMIN_PASSWORD: {'✅ set' if os.getenv('ADMIN_PASSWORD') else '⚠️ using default'}")
    print(f"  PORT: {os.getenv('PORT', os.getenv('ADMIN_PORT', '5000'))}")

    logger.info("Initialising database...")
    try:
        init_db()
        print("  Database: ✅ initialised")
    except Exception:
        logger.exception("Database init failed")
        print("  Database: ❌ FAILED")
        return

    # Seed players if table is empty
    try:
        from database import get_session
        from models import Player
        session = get_session()
        count = session.query(Player).count()
        session.close()
        print(f"  Players in DB: {count}")
        if count == 0:
            print("  Seeding 3,165 players...")
            from seed_players import seed
            seed()
            session = get_session()
            count = session.query(Player).count()
            session.close()
            print(f"  After seed: {count} players")
    except Exception:
        logger.exception("Seed failed")
        print("  Seed: ❌ FAILED (you can seed from admin panel)")

    # Check data file exists
    data_path = os.path.join(os.path.dirname(__file__), "data", "players.json")
    print(f"  data/players.json: {'✅ found' if os.path.exists(data_path) else '❌ NOT FOUND'}")

    # ── Start admin panel FIRST (Render health check needs this) ─────
    admin_thread = threading.Thread(target=start_admin_panel, daemon=True)
    admin_thread.start()
    admin_port = os.getenv("ADMIN_PORT", os.getenv("PORT", 5000))
    print(f"  Admin panel: ✅ starting on port {admin_port}")

    import time
    time.sleep(2)  # give Flask a moment to bind the port

    # ── Start Telegram bot ───────────────────────────────────────────
    if not BOT_TOKEN:
        print("=" * 50)
        print("⚠️  BOT_TOKEN not set — bot will NOT run")
        print("   Admin panel is still running at your Render URL")
        print("   Set BOT_TOKEN in Render env vars to enable the bot")
        print("=" * 50)
        admin_thread.join()
        return

    try:
        print(f"  Telegram bot: ✅ starting...")
        logger.info("Starting bot...")
        app = ApplicationBuilder().token(BOT_TOKEN).build()

        # ── Ban-guard middleware (group=-1, runs before all handlers) ──
        async def _ban_check(update, context):
            user = update.effective_user
            if not user:
                return
            try:
                from database import get_session as _gs
                from models import User as _User
                s = _gs()
                try:
                    u = s.query(_User).filter(_User.telegram_id == user.id).first()
                    if u and u.is_banned:
                        try:
                            reason = (u.ban_reason or "").strip()
                            txt = ("🚫 <b>You are banned from CricMaster Ultra.</b>\n"
                                   + (f"\n<i>Reason: {reason}</i>" if reason else ""))
                            if update.callback_query:
                                await update.callback_query.answer(
                                    "You are banned.", show_alert=True)
                            elif update.message:
                                await update.message.reply_text(txt, parse_mode="HTML")
                        except Exception:
                            pass
                        raise ApplicationHandlerStop
                finally:
                    s.close()
            except ApplicationHandlerStop:
                raise
            except Exception:
                logger.exception("Ban-check middleware failed (non-fatal)")
        app.add_handler(TypeHandler(_TGUpdate, _ban_check), group=-1)

        # ── Command handlers ─────────────────────────────────────────
        # ── Core commands + short aliases ────────────────────────────
        app.add_handler(CommandHandler(["start", "s"], start_handler))
        app.add_handler(CommandHandler(["debut", "d"], debut_handler))
        app.add_handler(CommandHandler(["claim", "c"], claim_handler))
        app.add_handler(CommandHandler(["gspin", "gs"], gspin_handler))
        app.add_handler(CommandHandler(["daily", "dl"], daily_handler))
        app.add_handler(CommandHandler(["myroster", "mr", "roster"], myroster_handler))
        app.add_handler(CommandHandler(["playerinfo", "pi", "info"], playerinfo_handler))
        app.add_handler(CallbackQueryHandler(player_version_callback, pattern=r"^plv_"))
        app.add_handler(CommandHandler(["releasepl", "release", "rel"], releasepl_handler))
        app.add_handler(CommandHandler(["releasemultiple", "relm", "rm"], releasemultiple_handler))
        app.add_handler(CommandHandler(["trade", "tr"], trade_handler))
        app.add_handler(CommandHandler(["playingxi", "pxi", "xi"], playingxi_handler))
        app.add_handler(CommandHandler(["autobuild", "ab", "best11"], autobuild_handler))
        app.add_handler(CommandHandler(["swapplayers", "swappl", "swap"], swapplayers_handler))
        app.add_handler(CommandHandler(["setcaptain", "captain", "cap"], setcaptain_handler))
        app.add_handler(CommandHandler(["searchpl", "search", "sp"], searchpl_handler))
        app.add_handler(CommandHandler(["searchovr", "so"], searchovr_handler))
        app.add_handler(CallbackQueryHandler(searchpl_page_callback, pattern=r"^spl_"))
        app.add_handler(CallbackQueryHandler(searchovr_page_callback, pattern=r"^sovr_"))
        app.add_handler(CallbackQueryHandler(search_cancel_callback, pattern=r"^searchcancel_"))
        app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
        app.add_handler(CommandHandler(["buypl", "buy", "b"], buypl_handler))
        app.add_handler(CommandHandler(["teamname", "tn"], teamname_handler))
        app.add_handler(CommandHandler(["purse", "p"], purse_handler))
        app.add_handler(CommandHandler(["stats", "st"], stats_handler))
        app.add_handler(CommandHandler(["cmuleaderboard", "leaderboard", "lb", "top"], leaderboard_handler))
        app.add_handler(CommandHandler(["myprofile", "profile", "me"], myprofile_handler))
        app.add_handler(CommandHandler(["playmatch", "pm", "match"], playmatch_handler))
        app.add_handler(CommandHandler(["endmatch", "em"], endmatch_handler))
        app.add_handler(CommandHandler(["resume", "r"], resume_handler))
        app.add_handler(CommandHandler(["lastmatch", "lm"], lastmatch_handler))
        app.add_handler(CommandHandler(["matchinfo", "mi"], info_handler))

        # ── Trait system ─────────────────────────────────────────────
        app.add_handler(CommandHandler(["traits", "tt"], traits_handler))
        app.add_handler(CommandHandler(["traitshop", "tshop"], traitshop_handler))
        app.add_handler(CommandHandler(["traitapply", "tapply"], traitapply_handler))
        app.add_handler(CommandHandler(["traitupgrade", "tup"], traitupgrade_handler))
        app.add_handler(CommandHandler(["traitreplace", "trep"], traitreplace_handler))

        app.add_handler(CallbackQueryHandler(traitbuy_callback, pattern=r"^trbuy_"))
        app.add_handler(CallbackQueryHandler(traitreroll_callback, pattern=r"^trreroll_"))
        app.add_handler(CallbackQueryHandler(traitshop_cancel_callback, pattern=r"^trshopcancel_"))
        app.add_handler(CallbackQueryHandler(trapply_inv_callback, pattern=r"^trapply_inv_"))
        app.add_handler(CallbackQueryHandler(trapply_pl_callback, pattern=r"^trapply_pl_"))
        app.add_handler(CallbackQueryHandler(trup_pt_callback, pattern=r"^trup_pt_"))
        app.add_handler(CallbackQueryHandler(trup_inv_callback, pattern=r"^trup_inv_"))
        app.add_handler(CallbackQueryHandler(trrep_pt_callback, pattern=r"^trrep_pt_"))
        app.add_handler(CallbackQueryHandler(trrep_inv_callback, pattern=r"^trrep_inv_"))
        app.add_handler(CallbackQueryHandler(trait_cancel_callback, pattern=r"^trcancel$"))

        # ── Player Market ────────────────────────────────────────────
        app.add_handler(CommandHandler(["playermarket", "pmarket", "market"], playermarket_handler))
        app.add_handler(CallbackQueryHandler(playermarket_select_callback, pattern=r"^pmsel_"))
        app.add_handler(CallbackQueryHandler(playermarket_buy_callback, pattern=r"^pmbuy_"))
        app.add_handler(CallbackQueryHandler(playermarket_back_callback, pattern=r"^pmback_"))
        app.add_handler(CallbackQueryHandler(playermarket_noop_callback, pattern=r"^pmnoop_"))
        app.add_handler(CallbackQueryHandler(playermarket_cancel_callback, pattern=r"^pmcancel_"))

        # ── Packs ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["buypack", "packs", "shop"], buypack_handler))
        app.add_handler(CallbackQueryHandler(pack_view_callback, pattern=r"^pkv_"))
        app.add_handler(CallbackQueryHandler(pack_back_callback, pattern=r"^pkbk_"))
        app.add_handler(CallbackQueryHandler(pack_close_callback, pattern=r"^pkc_"))
        app.add_handler(CallbackQueryHandler(pack_buy_callback, pattern=r"^pkb_"))
        app.add_handler(CallbackQueryHandler(pack_noop_callback, pattern=r"^pknoop_"))
        app.add_handler(CommandHandler(["openpack", "open"], openpack_handler))
        # opkc_ before opk_ so longer prefix matches first
        app.add_handler(CallbackQueryHandler(pack_open_close_callback, pattern=r"^opkc_"))
        app.add_handler(CallbackQueryHandler(pack_open_inventory_callback, pattern=r"^opk_"))

        # ── Tours ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["cmtours", "createtour"], cmtours_handler))
        app.add_handler(CommandHandler(["mytours", "tours"], mytours_handler))
        # `cancel_cmt_` must be registered before `ctm_` to avoid regex confusion;
        # all three picker patterns start with distinct prefixes anyway.
        app.add_handler(CallbackQueryHandler(cmt_cancel_callback, pattern=r"^cancel_cmt_"))
        app.add_handler(CallbackQueryHandler(cmt_matches_callback, pattern=r"^ctm_"))
        app.add_handler(CallbackQueryHandler(cmt_overs_callback, pattern=r"^cto_"))
        app.add_handler(CallbackQueryHandler(tour_accept_callback, pattern=r"^tac_"))
        app.add_handler(CallbackQueryHandler(tour_decline_callback, pattern=r"^tdc_"))
        app.add_handler(CallbackQueryHandler(mytours_play_callback, pattern=r"^mtp_"))
        app.add_handler(CallbackQueryHandler(mytours_info_callback, pattern=r"^mti_"))
        app.add_handler(CallbackQueryHandler(mytours_stats_callback, pattern=r"^mts_"))
        app.add_handler(CallbackQueryHandler(mytours_back_callback, pattern=r"^mtb_"))

        # ── vsbot ────────────────────────────────────────────────────
        app.add_handler(CommandHandler(["vsbot", "vsb"], vsbot_handler))
        app.add_handler(CallbackQueryHandler(vsbot_pick_callback, pattern=r"^vsb_pick_"))
        app.add_handler(CallbackQueryHandler(vsbot_cancel_callback, pattern=r"^vsb_cancel_"))
        app.add_handler(CallbackQueryHandler(vsbot_toss_callback, pattern=r"^vsb_toss_"))
        app.add_handler(CallbackQueryHandler(vsbot_op1_callback, pattern=r"^vsb_op1_"))
        app.add_handler(CallbackQueryHandler(vsbot_op2_callback, pattern=r"^vsb_op2_"))
        app.add_handler(CallbackQueryHandler(vsbot_selbowl_callback, pattern=r"^vsb_selbowl_"))

        # ── Quests ──────────────────────────────────────────────────
        app.add_handler(CommandHandler(["myquest", "mq", "quests"], myquest_handler))
        app.add_handler(CallbackQueryHandler(quest_tab_callback, pattern=r"^qst_tab_"))
        app.add_handler(CallbackQueryHandler(quest_filter_callback, pattern=r"^qst_flt_"))
        app.add_handler(CallbackQueryHandler(quest_page_callback, pattern=r"^qst_pg_"))
        app.add_handler(CallbackQueryHandler(quest_noop_callback, pattern=r"^qst_noop_"))
        # IMPORTANT: claimall must register BEFORE claim so the longer pattern wins
        app.add_handler(CallbackQueryHandler(quest_claimall_callback, pattern=r"^qst_claimall_"))
        app.add_handler(CallbackQueryHandler(quest_claim_callback, pattern=r"^qst_claim_"))
        app.add_handler(CallbackQueryHandler(quest_close_callback, pattern=r"^qst_close_"))

        # ── Achievements ────────────────────────────────────────────
        app.add_handler(CommandHandler(["achievements", "ach", "badges"], achievements_handler))
        app.add_handler(CallbackQueryHandler(achievements_tab_callback, pattern=r"^ach_tab_"))
        app.add_handler(CallbackQueryHandler(achievements_close_callback, pattern=r"^ach_close_"))

        # ── /howto Tutorial ─────────────────────────────────────────
        app.add_handler(CommandHandler(["howto", "help", "guide"], howto_handler))
        app.add_handler(CallbackQueryHandler(howto_tab_callback, pattern=r"^howto_tab_"))
        app.add_handler(CallbackQueryHandler(howto_close_callback, pattern=r"^howto_close_"))

        # ── Bot vs Bot spectator mode ─────────────────────────────────
        app.add_handler(CommandHandler(["botvsbot", "bvb"], botvsbot_handler))
        app.add_handler(CallbackQueryHandler(bvb_pickA_callback, pattern=r"^bvb_pickA_"))
        app.add_handler(CallbackQueryHandler(bvb_pickB_callback, pattern=r"^bvb_pickB_"))
        app.add_handler(CallbackQueryHandler(bvb_cancel_callback, pattern=r"^bvb_cancel_"))

        # ── /botmatch Spectator Mode ────────────────────────────────
        app.add_handler(CommandHandler(["botmatch", "spectate"], botmatch_handler))
        app.add_handler(CallbackQueryHandler(botmatch_pick_a_callback, pattern=r"^botmatch_a_"))
        app.add_handler(CallbackQueryHandler(botmatch_pick_b_callback, pattern=r"^botmatch_b_"))
        app.add_handler(CallbackQueryHandler(botmatch_cancel_callback, pattern=r"^botmatch_cancel_"))

        # ── Claim flow callbacks ─────────────────────────────────────
        app.add_handler(CallbackQueryHandler(retain_callback, pattern=r"^retain_"))
        app.add_handler(CallbackQueryHandler(release_callback, pattern=r"^release_"))
        app.add_handler(CallbackQueryHandler(replace_callback, pattern=r"^replace_"))
        app.add_handler(CallbackQueryHandler(replace_confirm_callback, pattern=r"^repl_"))

        # ── Daily & GSpin callbacks ──────────────────────────────────
        app.add_handler(CallbackQueryHandler(daily_claim_callback, pattern=r"^dailyclaim_"))
        app.add_handler(CallbackQueryHandler(gspin_spin_callback, pattern=r"^gspin_"))

        # ── Release callbacks ────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(release_one_callback, pattern=r"^rlone_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlcancel$"))
        app.add_handler(CallbackQueryHandler(releasemultiple_confirm_callback, pattern=r"^rlm_"))
        # Legacy patterns (ignored gracefully)
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlconfirm_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^relmconf_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rldup_"))
        app.add_handler(CallbackQueryHandler(roster_page_callback, pattern=r"^roster_page_"))
        app.add_handler(CallbackQueryHandler(bench_callback, pattern=r"^viewbench_"))

        # ── Buy callbacks ────────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(buypl_confirm_callback, pattern=r"^buypl_"))
        app.add_handler(CallbackQueryHandler(buypl_cancel_callback, pattern=r"^buycancel"))
        app.add_handler(CallbackQueryHandler(player_page_callback, pattern=r"^plpg_"))
        app.add_handler(CallbackQueryHandler(player_page_noop_callback, pattern=r"^plpgnoop_"))

        # ── Match callbacks ──────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(match_accept_callback, pattern=r"^matchacc_"))
        app.add_handler(CallbackQueryHandler(match_deny_callback, pattern=r"^matchdeny_"))
        app.add_handler(CallbackQueryHandler(toss_decision_callback, pattern=r"^toss_"))
        app.add_handler(CallbackQueryHandler(opener1_callback, pattern=r"^op1_"))
        app.add_handler(CallbackQueryHandler(opener2_callback, pattern=r"^op2_"))
        app.add_handler(CallbackQueryHandler(select_bowler_callback, pattern=r"^selbowl_"))
        app.add_handler(CallbackQueryHandler(variation_callback, pattern=r"^bvar_"))
        app.add_handler(CallbackQueryHandler(length_callback, pattern=r"^blen_"))
        app.add_handler(CallbackQueryHandler(spinner_delivery_callback, pattern=r"^bspin_"))
        app.add_handler(CallbackQueryHandler(shot_callback, pattern=r"^bshot_"))
        app.add_handler(CallbackQueryHandler(new_over_bowler_callback, pattern=r"^nbowl_"))
        app.add_handler(CallbackQueryHandler(new_batsman_callback, pattern=r"^newbat_"))
        app.add_handler(CallbackQueryHandler(endmatch_yes_callback, pattern=r"^endmatch_"))
        app.add_handler(CallbackQueryHandler(endmatch_no_callback, pattern=r"^endmatchno_"))

        # ── Leaderboard & Profile ──────────────────────────────────
        app.add_handler(CallbackQueryHandler(leaderboard_callback, pattern=r"^lb_"))
        app.add_handler(CallbackQueryHandler(myprofile_callback, pattern=r"^mp_"))

        # ── Trade callbacks ──────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(trade_rating_callback, pattern=r"^trate_"))
        app.add_handler(CallbackQueryHandler(trade_myplayer_callback, pattern=r"^tmypl_"))
        app.add_handler(CallbackQueryHandler(trade_theirplayer_callback, pattern=r"^tthpl_"))
        app.add_handler(CallbackQueryHandler(trade_send_callback, pattern=r"^tsend_"))
        app.add_handler(CallbackQueryHandler(trade_accept_callback, pattern=r"^taccept_"))
        app.add_handler(CallbackQueryHandler(trade_reject_callback, pattern=r"^treject_"))
        app.add_handler(CallbackQueryHandler(trade_cancel_callback, pattern=r"^tcancel$"))
        app.add_handler(CallbackQueryHandler(trade_back_callback, pattern=r"^tback_"))

        # ── Text handler for over selection (must be LAST) ───────────
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, overs_text_handler))

        logger.info("Bot is running. Press Ctrl+C to stop.")
        print("=" * 50)
        print("✅ EVERYTHING RUNNING!")
        print(f"   Admin: http://0.0.0.0:{admin_port}")
        print(f"   Bot: polling for Telegram updates")
        print("=" * 50)

        # Start the match heartbeat (keeps in-progress matches from getting stuck)
        try:
            from services.match_heartbeat import start_heartbeat
            start_heartbeat(app)
        except Exception:
            logger.exception("Failed to start match heartbeat")

        # Schedule periodic tour expiry (every hour)
        try:
            async def _tour_expiry_job(ctx):
                from database import get_session as _gs
                from services.tour_service import expire_old_tours, expire_overdue_invites
                s = _gs()
                try:
                    n1 = expire_overdue_invites(s)
                    n2 = expire_old_tours(s)
                    s.commit()
                    if n1 or n2:
                        logger.info(f"Tour expiry: {n1} invites, {n2} tours")
                except Exception:
                    s.rollback()
                    logger.exception("Tour expiry job failed")
                finally:
                    s.close()
            if app.job_queue:
                app.job_queue.run_repeating(_tour_expiry_job, interval=3600, first=60,
                                             name="tour_expiry")
        except Exception:
            logger.exception("Failed to schedule tour expiry")

        # Schedule periodic stuck-match cleanup (every hour). A match is
        # "stuck" if its status='active' but its MatchState hasn't been
        # touched in 24+ hours (server crashed, user vanished, etc).
        # Pending invites older than 1 hour also get auto-expired.
        try:
            async def _stuck_match_cleanup(ctx):
                from database import get_session as _gs
                from models import Match, MatchState
                from datetime import datetime as _dt, timedelta as _td
                s = _gs()
                try:
                    now = _dt.utcnow()
                    stale_cutoff = now - _td(hours=24)
                    invite_cutoff = now - _td(hours=1)

                    # Pending invites that have been hanging around
                    expired_invites = 0
                    pending = (s.query(Match)
                                .filter(Match.status == "pending",
                                        Match.created_at < invite_cutoff)
                                .all())
                    for m in pending:
                        m.status = "expired"
                        m.completed_at = now
                        expired_invites += 1

                    # Active matches with no MatchState updates in 24h
                    abandoned = 0
                    active = (s.query(Match)
                                .filter(Match.status == "active",
                                        Match.created_at < stale_cutoff)
                                .all())
                    for m in active:
                        # Check MatchState's last_modified
                        ms = (s.query(MatchState)
                                .filter(MatchState.match_id == m.id)
                                .first())
                        if ms is None or ms.last_modified < stale_cutoff:
                            m.status = "abandoned"
                            m.completed_at = now
                            m.margin_type = "abandoned"
                            m.margin_value = 0
                            abandoned += 1

                    if expired_invites or abandoned:
                        s.commit()
                        logger.info(
                            f"Match cleanup: {expired_invites} stale invites "
                            f"expired, {abandoned} stuck matches abandoned")
                except Exception:
                    s.rollback()
                    logger.exception("Stuck-match cleanup job failed")
                finally:
                    s.close()
            if app.job_queue:
                app.job_queue.run_repeating(_stuck_match_cleanup, interval=3600,
                                             first=120, name="stuck_match_cleanup")
        except Exception:
            logger.exception("Failed to schedule stuck-match cleanup")

        # Wire up cross-thread bot ref for admin Send-Now button
        try:
            import asyncio as _asyncio
            from admin import set_bot_for_admin
            try:
                loop = _asyncio.get_event_loop()
            except RuntimeError:
                loop = _asyncio.new_event_loop()
            set_bot_for_admin(app.bot, loop)
        except Exception:
            logger.exception("Failed to wire bot for admin notifications")

        app.run_polling(drop_pending_updates=True)

    except Exception:
        logger.exception("Bot crashed — admin panel still running")
        admin_thread.join()


if __name__ == "__main__":
    main()