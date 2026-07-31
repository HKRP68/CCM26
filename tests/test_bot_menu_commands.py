"""BOT_MENU_COMMANDS must stay in step with the commands main() registers.

Two ways this list rots: a new command gets a handler but never reaches the
slash menu (nobody discovers it), or the list grows past Telegram's 100-command
ceiling and setMyCommands starts failing outright — which silently freezes the
menu on whatever was published last.
"""

import re
from pathlib import Path

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"
SRC = BOT_PY.read_text()

# Registered but deliberately unpublished: admin/owner-gated commands, the
# /testwpm diagnostic, the two Unscramble lobby controls the lobby message
# already spells out, and /tradetrait plus /cmucareer — the menu is full at
# Telegram's 100-command ceiling, so those are documented in /help and /howto
# instead. Publishing them means demoting something else, which is a deliberate
# call, not a default.
UNPUBLISHED = {
    "grant", "setcardid", "clearmatches", "removematch",
    "frwd_grp", "frwd_prvt", "tourallow", "tourblock", "tourallowlist",
    "testwpm", "eu", "cu", "tradetrait", "cmucareer",
}


def _menu_commands():
    body = re.search(r"BOT_MENU_COMMANDS = \((.*?)\n\)\n", SRC, re.S).group(1)
    return re.findall(r'\("([a-z0-9_]+)",', body)


def _registered_commands():
    """(primary_names, all_names) from every CommandHandler(...) in bot.py."""
    raw = re.findall(r'CommandHandler\(\s*(\[[^\]]*\]|"[a-z0-9_]+")', SRC)
    primary, every = [], []
    for entry in raw:
        names = (re.findall(r'"([a-z0-9_]+)"', entry) if entry.startswith("[")
                 else [entry.strip('"')])
        primary.append(names[0])
        every.extend(names)
    return primary, every


def test_menu_fits_telegrams_limit():
    menu = _menu_commands()
    limit = int(re.search(r"MENU_LIMIT = (\d+)", SRC).group(1))
    assert limit <= 100, "Telegram's setMyCommands ceiling is 100"
    assert len(menu) <= limit, (
        f"{len(menu)} menu commands exceeds the {limit} Telegram allows — "
        f"setMyCommands would reject the whole list")


def test_menu_has_no_duplicates():
    menu = _menu_commands()
    dupes = {c for c in menu if menu.count(c) > 1}
    assert not dupes, f"duplicate menu entries: {sorted(dupes)}"


def test_every_registered_command_is_published_or_explicitly_exempt():
    menu = set(_menu_commands())
    primary, _every = _registered_commands()
    missing = sorted({c for c in primary if c not in menu and c not in UNPUBLISHED})
    assert not missing, (
        f"these commands have handlers but no slash-menu entry: {missing}. "
        f"Add them to BOT_MENU_COMMANDS, or to UNPUBLISHED here if they are "
        f"admin-only/deliberately hidden.")


def test_menu_entries_all_have_a_handler():
    menu = _menu_commands()
    _primary, every = _registered_commands()
    # The Challenge League commands are served by a regex MessageHandler rather
    # than a CommandHandler, so they have no literal registration to match.
    league = {"challengeipl", "challengebbl", "challengeint"}
    orphans = [c for c in menu if c not in every and c not in league]
    assert not orphans, f"menu advertises commands with no handler: {orphans}"


def test_batting_order_command_is_published():
    assert "setbo" in _menu_commands()


def test_wsp_modes_stay_turned_off():
    """/wsp and /wspbot are deliberately disabled.

    The handler modules are kept on disk so past watch-mode matches stay
    viewable, which makes it easy to switch the commands back on by accident —
    this fails if anything re-registers or re-advertises them.
    """
    menu = _menu_commands()
    _primary, every = _registered_commands()
    for cmd in ("wsp", "wspbot", "wspb"):
        assert cmd not in every, f"/{cmd} was re-registered as a command"
        assert cmd not in menu, f"/{cmd} was re-added to the slash menu"
    assert "CallbackQueryHandler(wsp" not in SRC, \
        "a /wsp or /wspbot callback handler was re-registered"
