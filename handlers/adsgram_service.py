"""Deprecated stale copy — use :mod:`services.ad_service`.

This module was an early duplicate of the ad helpers that nothing imports; it
had already drifted behind ``services/adsgram_service.py`` (no no-fill passes,
no scoped tokens). It is kept only so an import that predates the move still
resolves, and it now re-exports the real, provider-agnostic implementation
rather than shipping a second copy of the token logic.
"""

from services.ad_service import *  # noqa: F401,F403
from services.ad_service import (  # noqa: F401
    _CLIENT_TOKENS,
    _consume_token,
    _gc_tokens,
    _issue_token,
)
