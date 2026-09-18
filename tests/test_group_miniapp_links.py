"""Regression coverage for Group Mini App links.

A ``t.me/<bot>?startapp=...`` URL can open the bot's direct-message chat when
the Main Mini App is not configured. Group buttons must therefore require the
named-Mini-App URL form.
"""

import importlib
import sys
import types


def _install_telegram_stub(monkeypatch):
    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, url=None, web_app=None):
            self.text = text
            self.url = url
            self.web_app = web_app

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class WebAppInfo:
        def __init__(self, url):
            self.url = url

    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.WebAppInfo = WebAppInfo
    monkeypatch.setitem(sys.modules, "telegram", telegram)

    errors = types.ModuleType("telegram.error")
    for name in ("BadRequest", "NetworkError", "RetryAfter", "TimedOut"):
        setattr(errors, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "telegram.error", errors)


def _load_modules(monkeypatch):
    """Import both modules fresh against the telegram stub, and leave nothing.

    Clearing ``sys.modules`` is only half of it. Importing a submodule also
    rebinds it as an attribute of its package, and ``monkeypatch.delitem``
    knows nothing about that — so without the two ``delattr`` calls below these
    tests leave ``services.match_broadcast`` pointing at a copy built against a
    fake ``telegram``. Anything that later reaches it through the package
    attribute rather than through ``sys.modules`` gets the stub build, and
    patching one of the two has no effect on the other. That is how a test in
    another file came to be patching a module nobody was calling.

    ``monkeypatch.delattr`` restores the attribute at teardown, which is the
    whole reason to route this through monkeypatch rather than doing it by hand.
    """
    import services

    _install_telegram_stub(monkeypatch)
    for name in ("miniapp_buttons", "match_broadcast"):
        monkeypatch.delitem(sys.modules, f"services.{name}", raising=False)
        monkeypatch.delattr(services, name, raising=False)
    buttons = importlib.import_module("services.miniapp_buttons")
    broadcast = importlib.import_module("services.match_broadcast")
    return buttons, broadcast


def test_group_links_require_a_named_miniapp(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "CricMasterBot")
    monkeypatch.delenv("MINIAPP_NAME", raising=False)
    buttons, broadcast = _load_modules(monkeypatch)

    assert buttons.miniapp_deep_link("home", origin_chat_id=-100123) is None
    assert buttons.miniapp_button(
        "Open Mini App", "home", is_private=False, origin_chat_id=-100123
    ) is None
    assert broadcast.play_match_keyboard(42, chat_id=-100123, is_private=False) is None


def test_group_links_open_the_named_miniapp_without_a_dm_bounce(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "@CricMasterBot")
    monkeypatch.setenv("MINIAPP_NAME", "play")
    buttons, broadcast = _load_modules(monkeypatch)

    assert buttons.miniapp_deep_link("home", origin_chat_id=-100123) == (
        "https://t.me/CricMasterBot/play?startapp=home_c-100123"
    )
    keyboard = broadcast.play_match_keyboard(42, chat_id=-100123, is_private=False)
    assert keyboard.inline_keyboard[0][0].url == (
        "https://t.me/CricMasterBot/play?startapp=cricket_42_-100123"
    )
