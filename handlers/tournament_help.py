"""/tourhelp — every tournament command on one card.

Tournament commands are spread over the Challenge League Tournament views
(``/ctour``…), the Lets Play family (``/lpt…``), the stat boards, the draft and
the admin tools (``/tpoints``, ``/tsim``…). This card lists them all, grouped
the way a player meets them: play, follow, stats, draft. Bot admins also get
the admin sections.

The card is data (:data:`PLAYER_SECTIONS`, :data:`ADMIN_SECTIONS`), and
``tests/test_tournament_help.py`` checks every command named here is really
registered in ``bot.py`` — so the card can't quietly drift as commands change.
"""

import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from services.admin_ids import is_admin

logger = logging.getLogger(__name__)

# (section title, [(command, aliases, what it does), ...])
PLAYER_SECTIONS = (
    ("🏏 Play your match", (
        ("/lptour", "/lptplay",
         "Lets Play: reply to your opponent (or /lptour @user) to play your fixture"),
        ("/playapp", "/appmode",
         "Move the live match to the Mini App — each captain picks privately"),
        ("/continue", "/unpause",
         "Carry on a saved tournament match from the ball it stopped on"),
        ("/thome", "/homeground",
         "Team owners: set your home ground — /thome Mumbai | Wankhede Stadium"),
        ("/clearmatches", "/clearmatch",
         "Clear a stuck match in this chat — a tournament match is saved first"),
    )),
    ("🏆 Challenge League Tournament (CIPL)", (
        ("/ctour", "/ctournament", "Tournament hub — table, fixtures, teams"),
        ("/cttable", "/ctpoints", "Points table (P W L T · Pts · NRR)"),
        ("/ctfixtures", "/ctfix",
         "This round's fixtures and the results so far"),
        ("/ctteams", "", "The participating teams and who owns them"),
        ("/ctinjuries", "/ctinjury", "Who is injured, and for how many matches"),
        ("/clsd", "/clschedule /ctsd",
         "One team's schedule: standing, form, next matches, results"),
        ("/teamtourstats", "/tts /myteamstats",
         "A team's tournament by the numbers"),
        ("/mvp", "/tourmvp /ctmvp",
         "Most Valuable Player — batting + bowling + wins + POTM"),
    )),
    ("🎮 Lets Play Tournament", (
        ("/lpt", "/lptournament", "Tournament hub — table, fixtures, teams"),
        ("/lptable", "/lptpoints", "Points table (P W L T · Pts · NRR)"),
        ("/lptfixtures", "/lptfix",
         "This round's fixtures, with your own next matches on top"),
        ("/lptteams", "", "Who is in the tournament"),
        ("/lptstats", "", "Leaderboards — runs, wickets, sixes, average, economy"),
    )),
    ("📊 Stats", (
        ("/tournamentstats", "", "Tournament stat leaderboards"),
        ("/tbracket", "/bracket", "The playoff bracket as a picture"),
        ("/tteam", "/tott",
         "Team of the Tournament — the best XI so far, as cards"),
        ("/statstour", "",
         "A player's This Season and Total Season stats"),
        ("/mytours", "/tours", "Your tournaments"),
    )),
    ("📝 Tournament Draft", (
        ("/pick", "/pk /dpick", "Make your pick when you're on the clock"),
        ("/dboard", "/draftboard", "The live draft board"),
        ("/dsquad", "/myteam", "A team's drafted squad"),
        ("/dqueue", "/dq", "Your auto-pick wishlist"),
        ("/dsearch", "/dfind /dpool", "Who is still available"),
        ("/dtrade", "/dswap", "Trade players once the draft is done"),
        ("/dtrades", "/dtradelog", "The trade log"),
    )),
)

ADMIN_SECTIONS = (
    ("🔒 Admin · Lets Play setup", (
        ("/lptadmin", "", "The full Lets Play admin reference"),
        ("/lptnew", "", "Create a tournament: name | single/double | playoffs | max"),
        ("/lptadd", "/lptaddteam", "Enter a player by Telegram id"),
        ("/lptremove", "/lptremoveteam", "Take a player out"),
        ("/lptrename", "", "Rename a player's team"),
        ("/lptsync", "", "Fill in names for players who have since /debut'd"),
        ("/lptschedule", "", "Generate the round-robin fixture list"),
        ("/lptknockout", "",
         "Seed the playoffs now (they also seed themselves after the league)"),
        ("/lptstart", "", "Start it"),
        ("/lptpause", "", "Pause it"),
        ("/lptresume", "/lptunpause", "Resume it"),
        ("/lptcomplete", "", "Mark it completed"),
        ("/lptcancel", "", "Cancel it"),
        ("/lptreset", "", "Wipe every result, keeping teams and schedule"),
        ("/lptlist", "", "List every Lets Play tournament"),
        ("/lptuse", "", "Switch the active tournament"),
        ("/lptdelete", "", "Delete a tournament"),
    )),
    ("🔒 Admin · Running tournament (CIPL & Lets Play)", (
        ("/tsim", "/tsimulate",
         "Simulate an unplayed match: /tsim 7 1 · /tsim 7 2 · /tsim 7 random · "
         "/tsim round"),
        ("/tdeadline", "/tdeadlines",
         "Round deadlines: /tdeadline 48h · /tdeadline +12h · /tdeadline off "
         "(alerts only — nothing is auto-simulated)"),
        ("/tprize", "/tprizes",
         "Prizes paid when the final is decided: /tprize champion 5000 50"),
        ("/tstadiums", "/tstadium",
         "Grounds from Stadium Data: /tstadiums add|remove|all"),
        ("/ttiebreak", "/ttb",
         "Teams level on points: /ttiebreak h2h (head-to-head) or nrr"),
        ("/tsetchat", "/tchat",
         "Post recaps, deadline alerts and the ceremony in this group"),
        ("/tpoints", "/tpts", "Dock or award points: /tpoints MI | -2 | reason"),
        ("/tpointsclear", "/tptsclear", "Clear a team's points adjustment"),
        ("/taddmatch", "/addmatch",
         "Record a fixture from a replied scorecard file"),
        ("/tfixsync", "/tfixheal", "Un-stick fixtures still showing as live"),
        ("/tratingrule", "/tratingrules",
         "Every XI must field N players rated X or lower"),
        ("/tseasons", "/tseason", "Link past seasons into Total Season Stats"),
        ("/remindmatch", "/nudge",
         "Nudge two teams to play their pending fixture"),
    )),
    ("🔒 Admin · Tournament Draft", (
        ("/dadmin", "", "Every draft admin command"),
    )),
)


def all_commands(include_admin=True):
    """Every primary command and alias named on the card, without the slash."""
    sections = PLAYER_SECTIONS + (ADMIN_SECTIONS if include_admin else ())
    names = []
    for _title, rows in sections:
        for cmd, aliases, _what in rows:
            names.append(cmd.lstrip("/"))
            names += [a.lstrip("/") for a in aliases.split()]
    return names


def _active_cipl_command():
    """Every running CIPL tournament's own start command (``["tipl", "tpsl"]``).

    Several may run at once, one per league; an empty list when none is."""
    try:
        from database import get_session
        from services import tournament_service
        session = get_session()
        try:
            cmds = [tournament_service.start_command(session, t)
                    for t in tournament_service.get_active_tournaments(session)]
            return [c for c in cmds if c]
        finally:
            session.close()
    except Exception:
        logger.exception("Could not read the active tournament command")
        return []


def _running_commands_block():
    """Each running CIPL tournament with its own commands, or "" if none."""
    try:
        from database import get_session
        from services import tournament_service
        session = get_session()
        try:
            tours = tournament_service.get_active_tournaments(session)
            return tournament_service.running_commands_text(session, tours) if tours else ""
        finally:
            session.close()
    except Exception:
        logger.exception("Could not list the running tournaments' commands")
        return ""


def _line(cmd, aliases, what):
    alias = f" <i>({html.escape(aliases, quote=False)})</i>" if aliases else ""
    return f"<code>{html.escape(cmd, quote=False)}</code>{alias} — {html.escape(what, quote=False)}"


def render_help(admin=False, cipl_command=""):
    """The card as a list of HTML blocks, ready for ``chunk_blocks``."""
    from utils.message_chunks import expandable_quotes

    # One command or several (one per running league tournament).
    raw = [cipl_command] if isinstance(cipl_command, str) else list(cipl_command or ())
    cipls = []
    for c in raw:
        c = (c or "").strip()
        if c:
            cipls.append(c if c.startswith("/") else "/" + c)
    blocks = ["📖 <b>Tournament Help</b> — every tournament command\n"
              "<i>Tap a section to expand it.</i>"]
    sections = PLAYER_SECTIONS + (ADMIN_SECTIONS if admin else ())
    for i, (title, rows) in enumerate(sections):
        lines = []
        if i == 0:
            for cipl in cipls:
                lines.append(f"<code>{html.escape(cipl)}</code> — CIPL: reply to "
                             "your opponent to play your tournament fixture")
            if cipls:
                lines.append("Each running tournament also has its own table, fixtures, "
                             "teams, stats and MVP commands — listed at the end of this card")
            else:
                lines.append("<b>CIPL:</b> your league's tournament command — "
                             "reply to your opponent to play your fixture "
                             "(/ctour shows which one)")
        lines += [_line(*row) for row in rows]
        blocks.append(f"<b>{html.escape(title, quote=False)}</b>")
        blocks += expandable_quotes(lines)
    blocks.append(
        "<b>How the schedule works</b>\n"
        "• Fixtures open one round at a time — Round 2 starts once every "
        "Round 1 match is played or simulated.\n"
        "• When the league stage ends, the playoffs are set automatically.")
    if admin:
        blocks.append("<i>Admin website: Tournament Panel → Schedule has the "
                      "same tools (record result, 🎲 simulate).</i>")
    else:
        blocks.append("<i>Every other command: /commands</i>")
    return blocks


async def tourhelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tourhelp — every tournament command, admin sections for admins."""
    from utils.message_chunks import chunk_blocks
    msg = update.effective_message
    if msg is None:
        return
    user = update.effective_user
    admin = bool(user and is_admin(user.id))
    blocks = render_help(admin=admin, cipl_command=_active_cipl_command())
    running = _running_commands_block()
    if running:
        blocks.append(running)
    for part in chunk_blocks(blocks):
        await msg.reply_text(part, parse_mode="HTML",
                             disable_web_page_preview=True)
