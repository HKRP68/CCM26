"""Logging configuration."""

import logging
import os
from config import LOG_LEVEL

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    # Root logger
    logging.basicConfig(level=level, format=fmt)

    # File handler – general
    fh = logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)

    # File handler – errors only
    eh = logging.FileHandler(os.path.join(LOG_DIR, "errors.log"), encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(eh)

    # Quieten noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
