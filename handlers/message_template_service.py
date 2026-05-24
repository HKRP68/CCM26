"""Message template service — admin-editable bot reply messages.

Handlers call `msg(session, key, default, **vars)` to get the user-facing
text for a given key. If the DB has a row, it's used (with str.format
substitution); else the default is used. Format errors fall back to default.
"""

import logging

logger = logging.getLogger(__name__)


def msg(session, key, default, **fmt_vars):
    """Get a message template by key, with placeholder substitution.

    Falls back to `default` if:
      - no row exists for this key
      - the row is disabled
      - the template's text fails to .format() with the given vars

    `default` is also passed through .format() so handlers can use the same
    placeholders in both the in-code default and the DB template.
    """
    try:
        from models import MessageTemplate
        row = (session.query(MessageTemplate)
                      .filter(MessageTemplate.key == key,
                              MessageTemplate.enabled == True)
                      .first())
        if row is not None and row.text:
            try:
                return row.text.format(**fmt_vars)
            except (KeyError, IndexError, ValueError) as e:
                logger.warning(
                    f"Message template '{key}' has bad placeholders ({e}); "
                    "falling back to default.")
    except Exception:
        logger.exception(f"get_message({key}) failed; using default")

    # Default path — also format() it for consistency
    try:
        return default.format(**fmt_vars) if fmt_vars else default
    except (KeyError, IndexError, ValueError):
        return default


def get_template(session, key):
    """Return the MessageTemplate row by key (admin UI helper)."""
    try:
        from models import MessageTemplate
        return (session.query(MessageTemplate)
                       .filter(MessageTemplate.key == key)
                       .first())
    except Exception:
        return None
