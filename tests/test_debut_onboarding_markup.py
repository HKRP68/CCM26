import sys
import types
from types import SimpleNamespace

# Captured before any test stubs out ``config``, so the stub below can mirror
# the live squad cap instead of pinning a literal that a rebalance would strand.
import config as _real_config
_REAL_MAX_ROSTER = _real_config.MAX_ROSTER


def _load_debut_with_stubs(monkeypatch):
    """Load handlers.debut with tiny dependency stubs for this helper test."""

    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, url=None, web_app=None, callback_data=None):
            self.text = text
            self.url = url
            self.web_app = web_app
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class Update:
        pass

    class WebAppInfo:
        def __init__(self, url):
            self.url = url

    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.Update = Update
    telegram.WebAppInfo = WebAppInfo
    monkeypatch.setitem(sys.modules, "telegram", telegram)

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext)

    database = types.ModuleType("database")
    database.get_session = lambda: None
    monkeypatch.setitem(sys.modules, "database", database)

    models = types.ModuleType("models")
    models.User = type("User", (), {})
    models.UserRoster = type("UserRoster", (), {})
    models.UserStats = type("UserStats", (), {})
    monkeypatch.setitem(sys.modules, "models", models)

    player_service = types.ModuleType("services.player_service")
    player_service.get_players_for_debut = lambda session: []
    monkeypatch.setitem(sys.modules, "services.player_service", player_service)

    config = types.ModuleType("config")
    config.DEBUT_COINS = 0
    config.DEBUT_GEMS = 0
    # The debut message renders "N/<cap> players", so the stub has to carry the
    # squad cap too. Mirror the real value rather than pinning a literal.
    config.MAX_ROSTER = _REAL_MAX_ROSTER
    monkeypatch.setitem(sys.modules, "config", config)

    activity_service = types.ModuleType("services.activity_service")
    activity_service.log_activity = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "services.activity_service", activity_service)

    monkeypatch.delitem(sys.modules, "services.miniapp_buttons", raising=False)
    monkeypatch.delitem(sys.modules, "handlers.debut", raising=False)

    from handlers.debut import _build_post_debut_onboarding_markup

    return _build_post_debut_onboarding_markup


def _load_debut_text_builders(monkeypatch):
    """Same stubs, but hand back the two text builders instead."""
    _load_debut_with_stubs(monkeypatch)
    from handlers.debut import (
        _build_debut_command_list, _build_post_debut_onboarding_text,
    )
    return _build_debut_command_list, _build_post_debut_onboarding_text


def _only_button(markup):
    return markup.inline_keyboard[0][0]


def test_post_debut_onboarding_private_uses_native_webapp(monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "https://mini.example/app")
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    build_markup = _load_debut_with_stubs(monkeypatch)

    markup = build_markup(SimpleNamespace(type="private", id=123))

    button = _only_button(markup)
    assert button.web_app.url == "https://mini.example/app#home"
    assert button.url is None


def test_post_debut_onboarding_group_uses_deep_link_not_webapp(monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "https://mini.example/app")
    monkeypatch.setenv("BOT_USERNAME", "@CricMasterBot")
    monkeypatch.setenv("MINIAPP_NAME", "play")
    build_markup = _load_debut_with_stubs(monkeypatch)

    markup = build_markup(SimpleNamespace(type="supergroup", id=-100987654321))

    button = _only_button(markup)
    assert button.web_app is None
    assert button.url == (
        "https://t.me/CricMasterBot/play?startapp=home_c-100987654321"
    )


def test_post_debut_onboarding_group_without_deep_link_config_has_no_markup(monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "https://mini.example/app")
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    monkeypatch.delenv("MINIAPP_NAME", raising=False)
    build_markup = _load_debut_with_stubs(monkeypatch)

    assert build_markup(SimpleNamespace(type="group", id=-123)) is None


# ── Rookie mode: /debut must not advertise commands it has just locked ──

def test_debut_command_list_is_unchanged_when_the_bot_is_open(monkeypatch):
    build_list, _build_guide = _load_debut_text_builders(monkeypatch)

    text = build_list(rookie_mode=False)

    for command in ("/claim", "/myroster", "/daily", "/gspin"):
        assert command in text


def test_debut_command_list_drops_locked_commands_in_rookie_mode(monkeypatch):
    """A brand-new account can run none of these until it has a membership."""
    build_list, _build_guide = _load_debut_text_builders(monkeypatch)

    text = build_list(rookie_mode=True)

    for command in ("/claim", "/daily", "/gspin", "/myroster"):
        assert command not in text
    assert "/membership" in text


def test_post_debut_guide_points_at_the_membership_in_rookie_mode(monkeypatch):
    _build_list, build_guide = _load_debut_text_builders(monkeypatch)

    open_text = build_guide(11, rookie_mode=False)
    locked_text = build_guide(11, rookie_mode=True)

    assert "/claim" in open_text          # the normal starter roadmap
    assert "/claim" not in locked_text
    assert "/membership" in locked_text
