"""The Challenge League's setup cards as Bot API 10.1 rich messages.

``handlers/challenge.py`` already renders the two cards a captain *interacts*
with — the XI picker and the XI it confirms — as blocks. The cards around them
were still padded HTML: the team picker, the pitch menu, the "challenge
created" recap and the pitch confirmation, each a column of names and labels
drawn with ``═════`` rules and hoped-for alignment.

They are tables. A fixture is two sides and two teams; the pitch menu is a
surface and what it does to a match; the pitch confirmation is a report with
named conditions. Drawing them as tables means the client aligns them, so they
stop depending on how long a franchise happens to be called.

Same contract as every other block twin in the bot (``docs/rich-text-messages.md``):
the handler's existing HTML rides along as the fallback, and a builder that
raises answers ``None`` rather than costing the chat its card — a captain is
waiting on each of these to tap something.
"""

import logging
import re
from html import unescape

from services import rich_message as R

logger = logging.getLogger(__name__)


def _guarded(what):
    """Wrap a builder so a renderer bug costs the rendering, not the card."""
    def decorate(build):
        def guarded(*args, **kwargs):
            try:
                return build(*args, **kwargs)
            except Exception:
                logger.exception("Challenge League %s blocks failed to build",
                                 what)
                return None
        guarded.__name__ = build.__name__
        guarded.__doc__ = build.__doc__
        return guarded
    return decorate


def plain(text):
    """Plain text from a snippet a handler formatted as HTML.

    Several of these cards are assembled from lines other services already
    rendered — a pitch report, a team-selection tick. A block carries its own
    formatting, so the tags come off rather than being printed as literal angle
    brackets.
    """
    if not text:
        return None
    return unescape(re.sub(r"<[^>]+>", "", str(text))).strip() or None


def _who(info, fallback="Player"):
    """A draft's player dict as a ``tg://user`` mention, like the HTML cards."""
    info = info or {}
    label = str(info.get("name") or fallback)
    tg_id = info.get("tg_id")
    return R.mention(R.bold(label), tg_id) if tg_id else R.bold(label)


# ══════════════════════════════════════════════════════════════════════
# Picking a team
# ══════════════════════════════════════════════════════════════════════

@_guarded("team picker")
def team_picker_blocks(*, title, player, league_name, status_lines=()):
    """"Pick your team" — the prompt above the team buttons.

    ``status_lines`` are the already-rendered "✅ X selected Y" ticks; they read
    as a list because that is what they are — a running record of who has
    chosen, which the second captain checks before choosing themselves.
    """
    blocks = [R.heading(f"🏆 {title}", size=3)]
    ticks = [plain(line) for line in (status_lines or [])]
    ticks = [t for t in ticks if t]
    if ticks:
        blocks.append(R.list_block(ticks))
    blocks.append(R.paragraph(
        [_who(player), f", pick your {league_name or 'league'} team."]))
    return blocks


# ══════════════════════════════════════════════════════════════════════
# Picking a pitch
# ══════════════════════════════════════════════════════════════════════

@_guarded("pitch prompt")
def pitch_prompt_blocks(*, title, host, host_team, target_team, pitches):
    """The surface menu: every pitch and what it does to a match.

    ``pitches`` is ``[(name, description), …]``. A table rather than a bulleted
    line each, because the captain is comparing them — that is the whole reason
    the descriptions are on the card at all.
    """
    cells = [[R.cell(R.bold("PITCH"), header=True),
              R.cell(R.bold("WHAT IT DOES"), header=True)]]
    for name, description in pitches:
        cells.append([R.cell(R.bold(name)), R.cell(description or "—")])
    return [
        R.heading(f"🏆 {title}", size=3),
        R.pullquote(R.bold(f"🟢 {host_team}  🆚  {target_team}")),
        R.table(cells, bordered=True, striped=True, compact=True,
                caption=R.bold("🌱 Choose the pitch")),
        R.paragraph([_who(host, "Host"),
                     ", pick the surface you want to play on."]),
    ]


@_guarded("pitch confirmation")
def pitch_locked_blocks(*, title, host_team, target_team, pitch,
                        description=None, report=None, locked_note=None):
    """The card the pitch menu becomes once the surface is settled.

    ``report`` is the Pitch Report another service rendered as HTML; it goes in
    a ``blockquote`` because it is a description of the ground rather than
    another fact about the fixture.
    """
    blocks = [R.heading(f"🏆 {title}", size=3),
              R.pullquote(R.bold(f"🟢 {host_team}  🆚  {target_team}"))]
    facts = [[R.cell(R.bold("🌱 Pitch")), R.cell(R.bold(str(pitch)))]]
    if description:
        facts.append([R.cell(R.bold("Plays like")), R.cell(description)])
    blocks.append(R.table(facts, bordered=True, compact=True))
    body = plain(report)
    if body:
        blocks.append(R.blockquote(
            [R.paragraph(line) for line in body.splitlines() if line.strip()]))
    note = plain(locked_note)
    if note:
        blocks.append(R.footer(R.italic(note)))
    return blocks


# ══════════════════════════════════════════════════════════════════════
# The fixture, once both teams are in
# ══════════════════════════════════════════════════════════════════════

@_guarded("challenge created")
def created_blocks(*, title, host, target, host_team, target_team,
                   host_code=None, target_code=None, host_emoji="", 
                   target_emoji="", series=None, bot_xi=None):
    """The "the battle is set" recap, with the Playing XI buttons under it.

    The two sides go in one table so the captains, their franchises and the
    short codes line up in columns — which is the only way a reader can tell at
    a glance which of the four names belongs to which side.
    """
    blocks = [R.heading(f"🏏 {title}", size=2)]
    if series:
        blocks.append(R.paragraph(R.italic(series)))

    def team_cell(emoji, name, code):
        label = f"{emoji} {name}".strip()
        return R.cell([R.bold(label), f"  ({code})"] if code else R.bold(label))

    blocks.append(R.table([
        [R.cell(R.bold("👑 Host")), R.cell(_who(host, "User 1")),
         team_cell(host_emoji, host_team, host_code)],
        [R.cell(R.bold("⚔️ Guest")), R.cell(_who(target, "User 2")),
         team_cell(target_emoji, target_team, target_code)],
    ], bordered=True, striped=True, compact=True))
    blocks.append(R.pullquote(R.bold("🔥 The battle is set!")))

    lines = [plain(line) for line in (bot_xi or [])]
    lines = [line for line in lines if line]
    if lines:
        blocks.append(R.details(R.bold("🤖 The bot's Playing XI"),
                                [R.list_block(lines)]))
    blocks.append(R.footer(
        "🎽 Both captains — tap your team below to pick your Playing XI."))
    return blocks
