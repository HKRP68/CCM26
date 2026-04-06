"""Cricket Simulator Telegram Bot — main entry point (Phase 1 + 2 + Admin)."""

import os
import logging
import threading
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
)

from config import BOT_TOKEN
from database import init_db
from logger import setup_logging

# Phase 1 handlers
from handlers.debut import debut_handler
from handlers.claim import claim_handler, retain_callback, release_callback as claim_release_callback
from handlers.gspin import gspin_handler
from handlers.daily import daily_handler
from handlers.myroster import myroster_handler, roster_page_callback
from handlers.playerinfo import playerinfo_handler

# Phase 2 handlers
from handlers.release import (
    release_handler,
    releasemultiple_handler,
    release_confirm_callback,
    release_cancel_callback,
    release_dup_callback,
)
from handlers.trade import (
    trade_handler,
    trade_rating_callback,
    trade_myplayer_callback,
    trade_theirplayer_callback,
    trade_send_callback,
    trade_accept_callback,
    trade_reject_callback,
    trade_cancel_callback,
    trade_back_callback,
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
        "/playerinfo [name] - Player details\n"
        "/release [name] - Release a player for coins\n"
        "/releasemultiple - Release duplicate players\n"
        "/trade @user - Trade players with someone",
        parse_mode="HTML",
    )


def start_admin_panel():
    """Run the Flask admin panel in a background thread."""
    from admin import app as flask_app
    port = int(os.getenv("ADMIN_PORT", os.getenv("PORT", 5000)))
    logger.info(f"Admin panel starting on port {port}...")
    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,  # important: no reloader in thread
    )


def main():
    setup_logging()
    logger.info("Initialising database...")
    init_db()

    # Seed players if table is empty
    from database import get_session
    from models import Player
    session = get_session()
    count = session.query(Player).count()
    session.close()
    if count == 0:
        logger.info("No players in DB — running seed...")
        from seed_players import seed
        seed()

    # ── Start admin panel in background thread ───────────────────────
    admin_thread = threading.Thread(target=start_admin_panel, daemon=True)
    admin_thread.start()
    admin_port = os.getenv("ADMIN_PORT", os.getenv("PORT", 5000))
    logger.info(f"Admin panel running at http://0.0.0.0:{admin_port}")

    # ── Start Telegram bot ───────────────────────────────────────────
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        print("❌ BOT_TOKEN not set. Copy .env.example → .env and add your token.")
        # Keep admin alive even without bot token
        admin_thread.join()
        return

    logger.info("Starting bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ── Command handlers ─────────────────────────────────────────────
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("debut", debut_handler))
    app.add_handler(CommandHandler("claim", claim_handler))
    app.add_handler(CommandHandler("gspin", gspin_handler))
    app.add_handler(CommandHandler("daily", daily_handler))
    app.add_handler(CommandHandler("myroster", myroster_handler))
    app.add_handler(CommandHandler("playerinfo", playerinfo_handler))
    app.add_handler(CommandHandler("release", release_handler))
    app.add_handler(CommandHandler("releasemultiple", releasemultiple_handler))
    app.add_handler(CommandHandler("trade", trade_handler))

    # ── Phase 1 callbacks (claim retain/release) ────────────────────
    app.add_handler(CallbackQueryHandler(retain_callback, pattern=r"^retain_"))
    app.add_handler(CallbackQueryHandler(claim_release_callback, pattern=r"^release_"))

    # ── Phase 2 callbacks — roster pagination ───────────────────────
    app.add_handler(CallbackQueryHandler(roster_page_callback, pattern=r"^roster_page_"))

    # ── Phase 2 callbacks — release ─────────────────────────────────
    app.add_handler(CallbackQueryHandler(release_confirm_callback, pattern=r"^rlconfirm_"))
    app.add_handler(CallbackQueryHandler(release_cancel_callback, pattern=r"^rlcancel$"))
    app.add_handler(CallbackQueryHandler(release_dup_callback, pattern=r"^rldup_"))

    # ── Phase 2 callbacks — trading ─────────────────────────────────
    app.add_handler(CallbackQueryHandler(trade_rating_callback, pattern=r"^trate_"))
    app.add_handler(CallbackQueryHandler(trade_myplayer_callback, pattern=r"^tmypl_"))
    app.add_handler(CallbackQueryHandler(trade_theirplayer_callback, pattern=r"^tthpl_"))
    app.add_handler(CallbackQueryHandler(trade_send_callback, pattern=r"^tsend_"))
    app.add_handler(CallbackQueryHandler(trade_accept_callback, pattern=r"^taccept_"))
    app.add_handler(CallbackQueryHandler(trade_reject_callback, pattern=r"^treject_"))
    app.add_handler(CallbackQueryHandler(trade_cancel_callback, pattern=r"^tcancel$"))
    app.add_handler(CallbackQueryHandler(trade_back_callback, pattern=r"^tback_"))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
