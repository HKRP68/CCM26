"""How a manager's name is printed in read-only listings.

Stat boards, owner lists and leaderboards name people constantly. Rendering
those names as ``@handle`` (or as a ``tg://user`` link) turns every one of them
into a notification for somebody who never asked to be pinged — one /gstats on
a popular card used to buzz a dozen phones.

So listings print the handle as plain text: ``lost_in_space14``, not
``@lost_in_space14``. Managers without a handle are printed by name instead.
Nothing here ever produces a mention — anything that genuinely needs to ping
(a match invite, a trade offer) should build its own mention and be explicit
about it.
"""

from __future__ import annotations

from html import escape

FALLBACK = "Manager"


def manager_name(user, *, fallback: str = FALLBACK) -> str:
    """A manager's display name, HTML-escaped and safe to drop into a message.

    Handle first (without the ``@`` that would make it a mention), then the
    Telegram first name, then the team name, then ``fallback``.
    """
    if user is None:
        return escape(fallback)
    for value in (getattr(user, "username", None),
                  getattr(user, "first_name", None),
                  getattr(user, "team_name", None)):
        text = (value or "").strip()
        if text:
            return escape(text)
    return escape(fallback)
