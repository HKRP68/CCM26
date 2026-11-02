"""Dedicated Franchise Auction Telegram bot.

Run ``python franchise_bot.py`` with a new FRANCHISE_BOT_TOKEN and the existing
DATABASE_URL (or an explicit FRANCHISE_DATABASE_URL). Importing this module
performs no database, network or scheduler work. Both bots run in separate processes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
import sys
import threading
from typing import Mapping

logger = logging.getLogger("franchise_bot")
_ADMIN_KEYS = ("BOT_ADMIN_IDS", "ADMIN_IDS", "ADMIN_USER_IDS", "SUDO_USERS",
               "OWNER_IDS", "ADMIN_CHAT_ID", "BOT_OWNER_IDS")
_LINK_KEYS = ("WEBAPP_URL", "MINIAPP_NAME", "FRANCHISE_WEBSITE_URL")


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    database_url: str = field(repr=False)
    owner_ids: tuple[int, ...]
    admin_ids: tuple[int, ...] = ()
    username: str = ""
    health_port: int | None = None
    solo_league: bool = False


def _ids(value: str, name: str, *, required=False) -> tuple[int, ...]:
    parts = re.split(r"[,;\s]+", value.strip()) if value.strip() else []
    if required and not parts:
        raise ValueError(f"Set {name} to the new bot owner's Telegram user ID.")
    if any(not p.isascii() or not p.isdigit() or not 0 < int(p) < 2**63 for p in parts):
        raise ValueError(f"{name} requires positive Telegram user IDs.")
    return tuple(dict.fromkeys(int(p) for p in parts))


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    token = values.get("FRANCHISE_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Set FRANCHISE_BOT_TOKEN to the new bot's BotFather token.")
    database_url = (values.get("FRANCHISE_DATABASE_URL", "").strip()
                    or values.get("DATABASE_URL", "").strip())
    if (not database_url or "\n" in database_url or "\r" in database_url or
            not database_url.startswith(("sqlite:///", "postgresql://", "postgres://", "postgresql+psycopg2://"))):
        raise ValueError("Set DATABASE_URL or FRANCHISE_DATABASE_URL to your SQLite or PostgreSQL database.")
    # Sharing auction records is supported; polling the same Telegram identity
    # from two processes is not. The old bot hands off its clock separately.
    if values.get("FRANCHISE_DEDICATED_RUNTIME") != "1":
        if token == values.get("BOT_TOKEN", "").strip():
            raise ValueError("Use a new bot token, not the existing cricket bot token.")
    owners = _ids(values.get("FRANCHISE_OWNER_IDS", ""), "FRANCHISE_OWNER_IDS", required=True)
    admins = _ids(values.get("FRANCHISE_ADMIN_IDS", ""), "FRANCHISE_ADMIN_IDS")
    username = values.get("FRANCHISE_BOT_USERNAME", "").strip().lstrip("@")
    if username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("FRANCHISE_BOT_USERNAME must be a Telegram bot username.")
    raw_port = values.get("FRANCHISE_HEALTH_PORT", values.get("PORT", "")).strip()
    try:
        health_port = int(raw_port) if raw_port else None
    except ValueError:
        raise ValueError("The health port must be a number.") from None
    if health_port is not None and not 1 <= health_port <= 65535:
        raise ValueError("The health port must be between 1 and 65535.")
    return Settings(token=token, database_url=database_url, owner_ids=owners,
                    admin_ids=admins, username=username, health_port=health_port,
                    solo_league=values.get("FRANCHISE_SOLO_LEAGUE", "false").strip().lower() in ("1", "true", "yes"))


def configure_environment(settings: Settings) -> None:
    """Map only dedicated credentials before shared modules read configuration."""
    existing = sys.modules.get("config")
    if existing is not None and (getattr(existing, "DATABASE_URL", settings.database_url) != settings.database_url
                                 or getattr(existing, "BOT_TOKEN", settings.token) != settings.token):
        raise RuntimeError("Start franchise_bot.py in its own process before importing the cricket application.")
    existing_database = sys.modules.get("database")
    if existing_database is not None and getattr(existing_database, "DATABASE_URL", settings.database_url) != settings.database_url:
        raise RuntimeError("The dedicated bot cannot reuse an already initialized cricket database connection.")
    os.environ["BOT_TOKEN"] = settings.token
    os.environ["DATABASE_URL"] = settings.database_url
    os.environ["BOT_USERNAME"] = settings.username
    for key in (*_ADMIN_KEYS, *_LINK_KEYS):
        os.environ[key] = ""
    owner_ids = ",".join(map(str, settings.owner_ids))
    os.environ["OWNER_IDS"] = owner_ids
    os.environ["BOT_OWNER_IDS"] = owner_ids
    os.environ["BOT_ADMIN_IDS"] = ",".join(map(str, settings.admin_ids))
    os.environ["FRANCHISE_DEDICATED_RUNTIME"] = "1"


class _RedactingFormatter(logging.Formatter):
    def __init__(self, settings: Settings):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = (settings.token, settings.database_url)

    def format(self, record):
        value = super().format(record)
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]{20,}", "bot[redacted]", value)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_RedactingFormatter(settings))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class HealthServer:
    """A readiness endpoint for Render; it exposes no website or bot records."""
    def __init__(self, port: int, *, host="0.0.0.0"):
        self.ready = False
        self.server = None
        self.thread = None
        self.port, self.host = port, host

    def start(self):
        health = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/healthz", "/healthz/"):
                    self.send_error(404)
                    return
                payload = json.dumps({"service":"franchise-auction-bot", "status":"ready" if health.ready else "starting"}).encode()
                self.send_response(200 if health.ready else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return
        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="franchise-health", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.ready = False
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None


async def _error_handler(update, context):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        logger.error("Another process is polling this dedicated bot token. Run only one instance.")
        context.application.stop_running()
    else:
        # Network exceptions can contain request URLs with a bot token.
        logger.error("Franchise update failed (%s).", type(context.error).__name__)


async def _post_init(application):
    from telegram import BotCommand
    from services.auction_scheduler import SWEEP_JOB_NAME, start_auction_scheduler
    if application.job_queue is None:
        raise RuntimeError("The dedicated auction bot requires the JobQueue dependency.")
    if not application.bot_data.get("franchise_scheduler_started"):
        start_auction_scheduler(application)
        if not application.job_queue.get_jobs_by_name(SWEEP_JOB_NAME):
            raise RuntimeError("The auction clock could not start.")
        application.bot_data["franchise_scheduler_started"] = True
    username = getattr(application.bot, "username", None)
    if username:
        os.environ["BOT_USERNAME"] = username
    from handlers.franchise_bot import BOT_COMMANDS
    commands = [item if isinstance(item, BotCommand) else BotCommand(*item) for item in BOT_COMMANDS]
    await application.bot.set_my_commands(commands[:100])
    health = application.bot_data.get("franchise_health")
    if health is not None:
        health.ready = True


async def _post_shutdown(application):
    health = application.bot_data.get("franchise_health")
    if health is not None:
        health.ready = False


def build_application(settings: Settings | None = None, *, register_extra=None):
    """Construct the auction application without initializing the DB or polling."""
    settings = settings or load_settings()
    configure_environment(settings)
    from telegram.ext import ApplicationBuilder
    from services.franchise_bot_registry import register_auction_handlers
    application = (ApplicationBuilder().token(settings.token).concurrent_updates(16)
                   .connection_pool_size(64).pool_timeout(30).connect_timeout(15)
                   .read_timeout(35).write_timeout(20).get_updates_read_timeout(40)
                   .post_init(_post_init).post_shutdown(_post_shutdown).build())
    application.bot_data["franchise_dedicated"] = True
    from handlers.franchise_bot import register as register_menu
    from handlers.franchise_player_import import register as register_import
    register_menu(application)
    register_import(application)
    register_auction_handlers(application, include_solo_league=settings.solo_league)
    if register_extra is not None:
        register_extra(application)
    application.add_error_handler(_error_handler)
    return application


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(os.getenv('FRANCHISE_ENV_FILE', '.env'))
    try:
        settings = load_settings()
        configure_environment(settings)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    configure_logging(settings)
    health = None
    try:
        if settings.health_port:
            health = HealthServer(settings.health_port).start()
        from database import init_db
        init_db()
        application = build_application(settings)
        application.bot_data["franchise_health"] = health
        from telegram import Update
        logger.info("Dedicated Franchise Auction bot starting; existing bot is independent.")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                                timeout=25, close_loop=True)
        return 0
    except Exception as exc:
        logger.error("Dedicated bot could not start (%s). Check its configuration and database.", type(exc).__name__)
        return 1
    finally:
        if health:
            health.close()


if __name__ == "__main__":
    raise SystemExit(main())
