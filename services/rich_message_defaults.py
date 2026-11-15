"""Keep ordinary Telegram text compact; install rich-response compatibility.

Designed rich cards opt in through services.rich_message. Ordinary SDK sends
and edits retain their original text, line breaks, parse mode and controls.
The startup function name is kept for existing bot and worker entry points.
"""

from threading import Lock

_install_lock = Lock()


def install_rich_defaults():
    from services.rich_message_compat import install_message_compat

    with _install_lock:
        install_message_compat()
