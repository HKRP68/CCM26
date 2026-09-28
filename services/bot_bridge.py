"""Run bot-side match code from the Mini App's Flask thread.

The Flask app runs in a thread of the same process as the Telegram bot (see
``bot.py``), but the over-by-over match flow (``handlers.cipl_play``) lives on
the bot's asyncio loop and reads ``bot_data`` / ``bot`` / ``job_queue`` off a
PTB context. This module holds a reference to the running Application and its
loop, builds a PTB-shaped context for it, and lets a request thread hand a
coroutine to that loop.
"""

import asyncio
import concurrent.futures
import logging

logger = logging.getLogger(__name__)

_REF = {"app": None, "loop": None}


class BotContext:
    """A PTB-shaped context for code running outside a handler.

    Everything the match flow reaches — the state store, the renderers, the
    Challenge League resume, the inactivity timers — reads ``bot_data``, ``bot``
    and ``job_queue`` off the context.
    """

    def __init__(self, app):
        self.application = app

    @property
    def bot_data(self):
        return self.application.bot_data

    @property
    def bot(self):
        return self.application.bot

    @property
    def job_queue(self):
        return getattr(self.application, "job_queue", None)


def configure(app, loop):
    """Called once at startup (via ``admin.set_bot_for_admin``)."""
    _REF["app"] = app
    _REF["loop"] = loop


def available():
    return _REF["app"] is not None and _REF["loop"] is not None


def submit_and_wait_for_accept(coro_factory, timeout=15.0):
    """Run ``coro_factory(ctx, accept)`` on the bot loop; wait for acceptance.

    ``accept`` is an async callable the coroutine awaits the moment its input
    has been validated. The request thread returns as soon as that happens (or
    the coroutine finishes, whichever is first) — the slow tail of the step
    (the AI captain's think delays, the over simulation, chat posts) keeps
    running on the bot loop without holding the HTTP request open.

    Returns whatever the coroutine returned if it finished first, ``(True,
    None)`` once accepted, or ``(False, reason)`` when the bridge is not wired
    or the loop did not answer in time.
    """
    if not available():
        return False, "The match engine isn't reachable right now — try the chat buttons."
    app, loop = _REF["app"], _REF["loop"]
    accepted = concurrent.futures.Future()

    async def _accept():
        if not accepted.done():
            accepted.set_result((True, None))

    async def _runner():
        try:
            result = await coro_factory(BotContext(app), _accept)
        except Exception as exc:  # surfaced to the request, then logged
            logger.exception("bot bridge task failed")
            if not accepted.done():
                accepted.set_result((False, "Something went wrong — try again."))
            raise exc
        if not accepted.done():
            accepted.set_result(result)
        return result

    try:
        asyncio.run_coroutine_threadsafe(_runner(), loop)
    except Exception:
        logger.exception("bot bridge could not schedule on the bot loop")
        return False, "The match engine isn't reachable right now."
    try:
        return accepted.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return False, "The match is busy — try again in a moment."
