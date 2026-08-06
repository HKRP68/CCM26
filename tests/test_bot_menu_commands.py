"""Every registered command reaches Telegram's slash menu, in some scope.

Two ways this used to rot: a new command got a handler but never reached the
menu (nobody discovers it), or the single list grew past Telegram's 100-command
ceiling and setMyCommands started failing outright — which silently freezes the
menu on whatever was published last.

The ceiling is per *scope*, not per bot, so the menu is split three ways:
private chats, group chats, and each admin's own DM. These tests pin that every
command lands in at least one of those, and that no bucket busts the ceiling.

The file parses bot.py rather than importing it: importing pulls in ~120 handler
modules and needs a bot token, which is far too much for a lint-shaped check.
"""

import ast
import re
from pathlib import Path

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"
SRC = BOT_PY.read_text()
_TREE = ast.parse(SRC)


def _literal(name):
    """Evaluate a module-level literal constant out of bot.py.

    Walks the parsed module rather than pattern-matching the text, so a
    reformatted constant can't quietly break these checks.
    """
    for node in _TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        # frozenset({...}) — unwrap the call and evaluate its argument.
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"):
            return ast.literal_eval(value.args[0])
        return ast.literal_eval(value)
    raise AssertionError(f"could not find {name} in bot.py")


def _menu_commands():
    return [name for name, _description in _literal("BOT_MENU_COMMANDS")]


def _admin_commands():
    return [name for name, _description in _literal("ADMIN_MENU_COMMANDS")]


def _limit():
    return _literal("MENU_LIMIT")


def _dm_only():
    """The DM-only set, read from the service bot.py builds its constant from.

    Importing services.dm_only costs a couple of stdlib modules and telegram's
    keyboard classes — nothing like importing bot.py, and it keeps this file
    from re-listing a set that lives somewhere else.
    """
    from services.dm_only import DM_ONLY_COMMANDS
    return set(DM_ONLY_COMMANDS)


def _scopes():
    """Mirror bot._menu_for_scope so the buckets can be checked without importing."""
    menu = _menu_commands()
    group_only = _literal("GROUP_ONLY_COMMANDS")
    hidden_in_groups = set(_literal("PRIVATE_ONLY_COMMANDS")) | _dm_only()
    private = [c for c in menu if c not in group_only]
    return {
        "private": private,
        "group": [c for c in menu if c not in hidden_in_groups],
        "admin": _admin_commands() + private,
    }


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


def test_every_scope_fits_telegrams_limit():
    limit = _limit()
    assert limit <= 100, "Telegram's setMyCommands ceiling is 100"
    for scope, commands in _scopes().items():
        # The admin bucket is allowed to overflow: bot._menu_for_scope puts the
        # admin commands first precisely so the clamp drops player commands the
        # admin still sees everywhere else.
        if scope == "admin":
            continue
        assert len(commands) <= limit, (
            f"the {scope} menu has {len(commands)} commands, over the {limit} "
            f"Telegram allows per scope — setMyCommands would reject it whole")


def test_the_admin_commands_always_survive_the_clamp():
    admin = _scopes()["admin"]
    assert admin[:len(_admin_commands())] == _admin_commands(), (
        "admin commands must lead the admin menu so the clamp never drops them")


def test_no_scope_has_duplicates():
    for scope, commands in _scopes().items():
        dupes = {c for c in commands if commands.count(c) > 1}
        assert not dupes, f"duplicate entries in the {scope} menu: {sorted(dupes)}"


def test_every_registered_command_is_published_somewhere():
    published = set().union(*(set(v) for v in _scopes().values()))
    primary, _every = _registered_commands()
    missing = sorted({c for c in primary if c not in published})
    assert not missing, (
        f"these commands have handlers but reach no slash menu: {missing}. "
        f"Add them to BOT_MENU_COMMANDS, or to ADMIN_MENU_COMMANDS if they are "
        f"admin-only.")


def test_menu_entries_all_have_a_handler():
    menu = _menu_commands() + _admin_commands()
    _primary, every = _registered_commands()
    # The Challenge League commands are served by a regex MessageHandler rather
    # than a CommandHandler, so they have no literal registration to match.
    league = {"challengeipl", "challengebbl", "challengeint"}
    orphans = [c for c in menu if c not in every and c not in league]
    assert not orphans, f"menu advertises commands with no handler: {orphans}"


def test_group_only_commands_are_kept_out_of_private_menus():
    """Their handlers refuse in a DM, so listing them only promises an error."""
    private = _scopes()["private"]
    for command in _literal("GROUP_ONLY_COMMANDS"):
        assert command not in private, (
            f"/{command} refuses to run in a private chat, so it must not be "
            f"advertised there")


def test_private_only_commands_are_kept_out_of_group_menus():
    group = _scopes()["group"]
    for command in _literal("PRIVATE_ONLY_COMMANDS"):
        assert command not in group, (
            f"/{command} is a private-chat command and must not be advertised "
            f"in groups")


def test_dm_only_commands_are_kept_out_of_group_menus():
    """In a group they answer with a redirect, not with what the menu promises."""
    group = _scopes()["group"]
    for command in _dm_only():
        assert command not in group, (
            f"/{command} answers in DM only, so the group menu must not offer it")


def test_every_dm_only_command_is_still_offered_in_dms():
    """Hiding one from groups must never hide it everywhere."""
    private = set(_scopes()["private"])
    missing = sorted(_dm_only() - private)
    assert not missing, (
        f"these DM-only commands reach no private menu either: {missing}")


def test_group_and_private_together_cover_the_whole_player_menu():
    scopes = _scopes()
    covered = set(scopes["private"]) | set(scopes["group"])
    missing = sorted(set(_menu_commands()) - covered)
    assert not missing, f"player commands reaching no chat type: {missing}"


def test_career_player_command_is_published():
    assert "cmucareer" in _menu_commands()


def test_batting_order_command_is_published():
    assert "setbo" in _menu_commands()


def test_wsp_modes_stay_turned_off():
    """/wsp and /wspbot are deliberately disabled.

    The handler modules are kept on disk so past watch-mode matches stay
    viewable, which makes it easy to switch the commands back on by accident —
    this fails if anything re-registers or re-advertises them.
    """
    _primary, every = _registered_commands()
    for command in ("wsp", "wspbot"):
        assert command not in every, f"/{command} was re-registered"
        assert command not in _menu_commands(), f"/{command} was re-advertised"
