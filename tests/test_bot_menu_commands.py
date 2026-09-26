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


def test_the_admin_bucket_alone_still_fits():
    """The admin bucket is exempt from the clamp only because it has never
    filled it on its own.

    Admin commands lead the menu, so the clamp eats player entries first and an
    admin loses nothing they cannot reach in another scope. Once the admin
    commands themselves pass the ceiling that stops being true: the tail of the
    admin list starts dropping, and nothing says so — the menu simply comes
    back short. This is the line where that begins.
    """
    admin_only = _admin_commands()
    limit = _limit()
    assert len(admin_only) <= limit, (
        f"{len(admin_only)} admin-only commands, over the {limit} Telegram "
        f"allows per scope — the tail of ADMIN_MENU_COMMANDS is now being "
        f"dropped silently. Retire some, or fold a family behind one "
        f"reference card the way /auction and /lptadmin do.")


def test_the_admin_commands_always_survive_the_clamp():
    admin = _scopes()["admin"]
    assert admin[:len(_admin_commands())] == _admin_commands(), (
        "admin commands must lead the admin menu so the clamp never drops them")


def test_no_scope_has_duplicates():
    for scope, commands in _scopes().items():
        dupes = {c for c in commands if commands.count(c) > 1}
        assert not dupes, f"duplicate entries in the {scope} menu: {sorted(dupes)}"


# Commands that deliberately reach no slash menu, and why.
#
# Both player scopes sit AT Telegram's 100-command ceiling, so publishing one
# more costs an existing command its entry — ``_clamped`` drops the tail rather
# than letting setMyCommands reject the whole call, which would silently freeze
# the menu on whatever was published last. Every command here is advertised
# in-product instead: in /help, on a reference card, from a hub's own buttons,
# or by the prompt that asks for it.
#
# This is an allowlist, not an amnesty: a command added without a menu entry
# and without a line here still fails the test below.
UNPUBLISHED_ON_PURPOSE = {
    # Franchise Auction — /auction card, the board's footer, and the RTM
    # prompt itself, which names the owner and carries buttons.
    "bid", "artm", "aboard", "apurse",
    # The auction's team views — every one is a button on /ainfo, and the
    # pinned board's footer names them.
    "ainfo", "asets", "anextset", "anextplayer", "asquad", "asoldlist",
    "aunsoldlist",
    # Every number the auction runs by. Unpublished for the same ceiling
    # reason as the views above, and named where somebody asking "what are the
    # rules" already is: the /auction card, /ainfo's own buttons and the
    # board's footer. docs/franchise-auction.md says so too.
    "arules",
    # Auction admin commands on the /adminhelp card rather than in the admin
    # bucket, which would otherwise pass the 100-command ceiling.
    "asetorder", "aretainforce", "aoffers", "aretcancel", "aaccelmode",
    "aadminremove", "aadmins",
    "aretmode", "aretslot", "aretrule", "aretdemand", "asetsexport",
    "asetsimport",
    # Dynamic retention's owner command — on /adminhelp's player section, the
    # /aretlock readout and every negotiation card, which is where an owner
    # retaining somebody already is.
    "retain",
    # Player Draft trades — the precedent the auction followed.
    "dtrade", "dtrades", "dtradecancel",
    # Challenge League tournament views — /help, the hub's buttons, and
    # whatever alias a league sets as its fixtures_command.
    "ctour", "cttable", "ctfixtures", "ctteams", "ctinjuries",
    "clsd", "teamtourstats", "mvp",
    # In-match, offered by the match's own keyboard at the moment it applies.
    "impact", "pitchstats",
    # Scorecard replay — named under every match result, in /help and in
    # /matchhelp, which is where someone whose card went missing is looking.
    "lastscorecard",
    # Team logo upload — a DM-only flow named in /help and in /howto beside
    # /teamname, and offered again by the card that says a logo was rejected.
    # Both player scopes are already at the ceiling, so publishing it would
    # cost an existing command its entry.
    "setteamlogo",
    # Team colour — same ceiling, same reason. Named in /help and /howto next
    # to /setteamlogo, and offered by /teamname to anyone without one.
    "setteamcolour",
    # Ranked ladder, rivalries and the Hall of Fame.
    # Both player scopes are at the ceiling; all five are in /help, and the
    # post-match card (ranked + rivalry lines) and /h2h point at them.
    "rank", "ranked", "rivalry", "halloffame",
    # Forward-only mode: a separate run path where this is the ONLY command
    # registered at all, so there is no menu for it to be in.
    "frwd",
}


def test_every_registered_command_is_published_somewhere():
    published = set().union(*(set(v) for v in _scopes().values()))
    primary, _every = _registered_commands()
    missing = sorted({c for c in primary
                      if c not in published and c not in UNPUBLISHED_ON_PURPOSE})
    assert not missing, (
        f"these commands have handlers but reach no slash menu: {missing}. "
        f"Add them to BOT_MENU_COMMANDS, or to ADMIN_MENU_COMMANDS if they are "
        f"admin-only (that bucket is exempt from the clamp and costs players "
        f"nothing). If leaving one unpublished is deliberate, add it to "
        f"UNPUBLISHED_ON_PURPOSE with its reason.")


def test_the_unpublished_list_does_not_outlive_its_commands():
    """A stale allowlist entry hides the next real omission behind a name that
    no longer exists, so the list has to shrink when a command does."""
    _primary, every = _registered_commands()
    stale = sorted(UNPUBLISHED_ON_PURPOSE - set(every))
    assert not stale, (
        f"UNPUBLISHED_ON_PURPOSE names commands with no handler: {stale}")


def test_nothing_is_both_published_and_excused():
    """An entry that is also in a menu means the list is being read as
    decoration rather than as the record of a decision."""
    published = set().union(*(set(v) for v in _scopes().values()))
    both = sorted(UNPUBLISHED_ON_PURPOSE & published)
    assert not both, (
        f"these are in a slash menu AND in UNPUBLISHED_ON_PURPOSE: {both}")


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
