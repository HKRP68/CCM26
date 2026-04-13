"""Cricket Simulator Telegram Bot — main entry point (Phase 1 + 2 + Admin)."""

import os
import logging
import threading
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

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
from handlers.playerinfo import playerinfo_handler

# Phase 2 handlers
from handlers.release import (
    releasepl_handler, releasemultiple_handler,
    release_confirm_callback, release_cancel_callback, release_dup_callback,
)
from handlers.trade import (
    trade_handler, trade_rating_callback, trade_myplayer_callback,
    trade_theirplayer_callback, trade_send_callback,
    trade_accept_callback, trade_reject_callback,
    trade_cancel_callback, trade_back_callback,
)

# Phase 3 handlers
from handlers.lineup import playingxi_handler, swapplayers_handler, setcaptain_handler
from handlers.search import searchpl_handler, searchovr_handler
from handlers.buy import buypl_handler, buypl_confirm_callback, buypl_cancel_callback
from handlers.team import teamname_handler, purse_handler, stats_handler

# Match handlers
from handlers.match import (
    playmatch_handler, match_accept_callback, match_deny_callback,
    overs_text_handler, toss_decision_callback,
    opener1_callback, opener2_callback, select_bowler_callback,
    variation_callback, length_callback, spinner_delivery_callback,
    shot_callback, new_over_bowler_callback, new_batsman_callback,
    endmatch_handler, endmatch_yes_callback, endmatch_no_callback,
)

logger = logging.getLogger(__name__)


async def start_handler(update, context):
    await update.message.reply_text(
        "🏏 <b>Welcome to Cricket Simulator Bot!</b>\n\n"
        "Use /debut to create your account and receive your starting squad.\n\n"
        "<b>Commands:</b>\n"
        "/debut - Create account & get 8 players\n"
        "/claim - Claim 1 player + 500 coins (hourly)\n"
        "/daily - Daily reward (24h)\n"
        "/gspin - Spin the wheel (8h)\n"
        "/myroster - View your roster\n"
        "/pxi - Playing XI\n"
        "/playerinfo [name] - Player details\n"
        "/stats [name] - Player game stats\n"
        "/searchpl [name] - Search player\n"
        "/searchovr [rating] - Search by OVR\n"
        "/buypl [name] - Buy a player\n"
        "/swapplayers [n1] [n2] - Swap positions\n"
        "/setcaptain [name] - Set captain\n"
        "/teamname [name] - Set team name\n"
        "/purse - Check balance\n"
        "/release [name] - Release for coins\n"
        "/releasemultiple - Release duplicates\n"
        "/trade @user - Trade players\n"
        "/playmatch @user - Play a match\n"
        "/endmatch - End match (fine applies)",
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

        # ── Command handlers ─────────────────────────────────────────
        app.add_handler(CommandHandler("start", start_handler))
        app.add_handler(CommandHandler("debut", debut_handler))
        app.add_handler(CommandHandler("claim", claim_handler))
        app.add_handler(CommandHandler("gspin", gspin_handler))
        app.add_handler(CommandHandler("daily", daily_handler))
        app.add_handler(CommandHandler("myroster", myroster_handler))
        app.add_handler(CommandHandler("playerinfo", playerinfo_handler))
        app.add_handler(CommandHandler("releasepl", releasepl_handler))
        app.add_handler(CommandHandler("release", releasepl_handler))
        app.add_handler(CommandHandler("releasemultiple", releasemultiple_handler))
        app.add_handler(CommandHandler("trade", trade_handler))
        app.add_handler(CommandHandler("pxi", playingxi_handler))
        app.add_handler(CommandHandler("playingxi", playingxi_handler))
        app.add_handler(CommandHandler("swapplayers", swapplayers_handler))
        app.add_handler(CommandHandler("swappl", swapplayers_handler))
        app.add_handler(CommandHandler("setcaptain", setcaptain_handler))
        app.add_handler(CommandHandler("searchpl", searchpl_handler))
        app.add_handler(CommandHandler("searchovr", searchovr_handler))
        app.add_handler(CommandHandler("buypl", buypl_handler))
        app.add_handler(CommandHandler("teamname", teamname_handler))
        app.add_handler(CommandHandler("purse", purse_handler))
        app.add_handler(CommandHandler("stats", stats_handler))
        app.add_handler(CommandHandler("playmatch", playmatch_handler))
        app.add_handler(CommandHandler("endmatch", endmatch_handler))

        # ── Claim flow callbacks ─────────────────────────────────────
        app.add_handler(CallbackQueryHandler(retain_callback, pattern=r"^retain_"))
        app.add_handler(CallbackQueryHandler(release_callback, pattern=r"^release_"))
        app.add_handler(CallbackQueryHandler(replace_callback, pattern=r"^replace_"))
        app.add_handler(CallbackQueryHandler(replace_confirm_callback, pattern=r"^repl_"))

        # ── Daily & GSpin callbacks ──────────────────────────────────
        app.add_handler(CallbackQueryHandler(daily_claim_callback, pattern=r"^dailyclaim_"))
        app.add_handler(CallbackQueryHandler(gspin_spin_callback, pattern=r"^gspin_"))

        # ── Release callbacks ────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(release_confirm_callback, pattern=r"^rlconfirm_"))
        app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlcancel$"))
        app.add_handler(CallbackQueryHandler(release_dup_callback, pattern=r"^rldup_"))
        app.add_handler(CallbackQueryHandler(roster_page_callback, pattern=r"^roster_page_"))

        # ── Buy callbacks ────────────────────────────────────────────
        app.add_handler(CallbackQueryHandler(buypl_confirm_callback, pattern=r"^buypl_"))
        app.add_handler(CallbackQueryHandler(buypl_cancel_callback, pattern=r"^buycancel$"))

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
        app.run_polling(drop_pending_updates=True)

    except Exception:
        logger.exception("Bot crashed — admin panel still running")
        admin_thread.join()


if __name__ == "__main__":
    main()