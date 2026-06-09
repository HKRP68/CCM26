"""Lightweight exception tracker stub for the cricket simulation engine.

Replaces the full SimCricketX exception_tracker (which needs Flask + DB)
with a simple logging-only version that satisfies the engine's imports.
"""
import logging
import traceback

logger = logging.getLogger(__name__)


def log_exception(exc=None, source: str = "engine", **_kwargs) -> None:
    """Log an exception to the application logger. No DB write."""
    if exc is not None:
        logger.error("[%s] %s", source, exc, exc_info=exc if isinstance(exc, BaseException) else True)
    else:
        logger.error("[%s] exception in engine:\n%s", source, traceback.format_exc())


def log_data_anomaly(message: str, source: str = "engine", **_kwargs) -> None:
    logger.warning("[%s] data anomaly: %s", source, message)
