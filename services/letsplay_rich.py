"""Every /letsplay card as a Bot API 10.1 rich message.

``handlers/letsplay.py`` builds the whole setup flow — invitation, traits vote,
pitch, both Playing XIs, the toss — and every one of those cards was a padded
HTML string. Two of them are tables in everything but name: the Playing XI is a
numbered list of names and ratings that only lines up when every name happens to
be the same width, and the invitation is a column of ``<b>Label:</b> value``
pairs. Telegram renders both in a proportional font, so neither ever lined up.

This module is their block twin, built the way ``docs/rich-text-messages.md``
prescribes: same content, same order, and the handler's existing HTML always
passed along as the fallback, so a server below 10.1 loses nothing.

Every builder here is guarded. A card is part of a live match flow — a captain
is waiting on it to tap something — so a bug in a renderer must cost the
*rendering* and never the card: each entry point answers ``None`` on any
exception, which tells ``rich_message`` to send the HTML that was always there.
"""

import logging

from services import rich_message as R

logger = logging.getLogger(__name__)

# The pitch badges both Lets Play renderings print. They live here rather than
# in the handler because both renderers are live and must agree — a pitch that
# reads 🟩 on one card and 🧱 on the other is two cards describing one match —
# and because a service importing a handler is the wrong way round.
PITCH_EMOJI = {
    "Dry": "🟫", "Dusty": "🟤", "Hard": "🟩",
    "Flat": "⬜", "Green": "🌿", "Bouncy": "🔵", "Even": "🟨",
}


def _guarded(what):
    """Wrap a builder so a renderer bug costs the rendering, not the card."""
    def decorate(build):
        def guarded(*args, **kwargs):
            try:
                return build(*args, **kwargs)
            except Exception:
                logger.exception("Lets Play %s blocks failed to build", what)
                return None
        guarded.__name__ = build.__name__
        guarded.__doc__ = build.__doc__
        return guarded
    return decorate


def mention(info):
    """A draft's player dict as a real ``tg://user`` mention node.

    The HTML cards use ``<a href="tg://user?id=…">`` for the same thing, so the
    two renderings ping the same person — a card that names a captain without
    reaching them is the difference between a prompt and a notification.
    """
    info = info or {}
    label = str(info.get("name") or "Player")
    tg_id = info.get("tg_id")
    return R.mention(R.bold(label), tg_id) if tg_id else R.bold(label)


def _pitch_label(pitch):
    pitch = str(pitch or "Hard")
    return f"{PITCH_EMOJI.get(pitch, '🏏')} {pitch}"


# ══════════════════════════════════════════════════════════════════════
# The invitation
# ══════════════════════════════════════════════════════════════════════

@_guarded("invitation")
def invite_blocks(draft, host_preview=None):
    """The card the guest accepts or denies.

    A fixture in the Lets Play Tournament and a friendly are the same card with
    one difference that matters — the result counts — so that line is a
    ``pullquote`` rather than another row of the facts table. It is the only
    thing on this card somebody could regret not reading.
    """
    lpt = draft.get("lpt")
    heading = ("🏆 Lets Play Tournament — Fixture Invitation" if lpt
               else "🏏 Lets Play — Match Invitation")
    blocks = [R.heading(heading, size=2)]

    if lpt:
        blocks.append(R.table([
            [R.cell(R.bold("🏆 Tournament")),
             R.cell(str(lpt.get("tournament_name") or "—"))],
            [R.cell(R.bold("🗓️ Fixture")),
             R.cell(f"{lpt.get('host_team_name') or '—'}  vs  "
                    f"{lpt.get('guest_team_name') or '—'}")],
        ], bordered=True, compact=True))

    blocks.append(R.table([
        [R.cell(R.bold("👤 Host")), R.cell(mention(draft.get("host")))],
        [R.cell(R.bold("🎯 Guest")), R.cell(mention(draft.get("guest")))],
    ], bordered=True, compact=True))

    facts = [
        [R.cell(R.bold("🎮 Match type")),
         R.cell("Tournament (official)" if lpt else "Lets Play")],
        [R.cell(R.bold("⏱️ Format")), R.cell("20 Overs")],
        [R.cell(R.bold("📋 Roster")), R.cell("Own Roster")],
    ]
    if host_preview:
        facts.append([R.cell(R.bold("📊 Host XI")),
                      R.cell(f"{host_preview['ovr']} OVR  ·  "
                             f"avg {host_preview['avg']:.1f}")])
        facts.append([R.cell(R.bold("⭐ Stars")),
                      R.cell(str(host_preview["stars"]))])
    blocks.append(R.table(facts, bordered=True, striped=True, compact=True))

    if lpt:
        blocks.append(R.pullquote(
            R.bold("⚠️ This result counts towards the points table.")))
    blocks.append(R.paragraph([mention(draft.get("guest")),
                               ", do you accept? ", R.italic("(30s)")]))
    return blocks


# ══════════════════════════════════════════════════════════════════════
# The pitch
# ══════════════════════════════════════════════════════════════════════

@_guarded("pitch prompt")
def pitch_prompt_blocks(draft):
    """"Host, pick the pitch" — the question above the pitch buttons."""
    return [R.heading("🌱 Pitch Selection", size=3),
            R.paragraph([mention(draft.get("host")),
                         ", choose the pitch for this match:"])]


@_guarded("pitch locked")
def pitch_locked_blocks(pitch):
    """What the pitch prompt becomes once the host has picked."""
    return [R.paragraph(["🌱 Pitch locked: ", R.bold(_pitch_label(pitch))])]


# ══════════════════════════════════════════════════════════════════════
# Both Playing XIs
# ══════════════════════════════════════════════════════════════════════
#
# The card players actually read, and the one a proportional font ruined: two
# numbered 1-11 line-ups, each rating in brackets, each bench underneath. As a
# table the client aligns the columns, so a long name stops shunting the
# ratings out of line — and the bench becomes a real ``details`` rather than a
# quoted list, which is what it was always standing in for.


def _xi_table(rows, caption):
    """One side's batting order as a numbered table."""
    cells = [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("ROLE"), header=True, align="center"),
              R.cell(R.bold("RATING"), header=True, align="right")]]
    for position, (name, rating, role, badge) in enumerate(rows, 1):
        cells.append([
            R.cell(str(position), align="center"),
            R.cell([name, f"  {badge}"] if badge else name),
            R.cell(role or "—", align="center"),
            R.cell(R.bold(rating), align="right"),
        ])
    return R.table(cells, bordered=True, striped=True, compact=True,
                   caption=caption)


def _bench_details(rows, start=12):
    """The bench, collapsed, numbered from 12 the way ``/change`` reads it."""
    if not rows:
        return R.paragraph(R.italic("🪑 Bench: none (exactly 11 players)"))
    cells = [[R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("PLAYER"), header=True),
              R.cell(R.bold("ROLE"), header=True, align="center"),
              R.cell(R.bold("RATING"), header=True, align="right")]]
    for offset, (name, rating, role, _badge) in enumerate(rows):
        cells.append([
            R.cell(str(start + offset), align="center"),
            R.cell(name),
            R.cell(role or "—", align="center"),
            R.cell(rating, align="right"),
        ])
    return R.details(R.bold(f"🪑 Bench ({len(rows)})"),
                     [R.table(cells, bordered=True, compact=True)])


def _side_blocks(caption, xi_rows, bench_rows):
    """One side of the XI card: the line-up, then its bench behind a tap."""
    blocks = [_xi_table(xi_rows, caption)]
    if bench_rows is not None:
        blocks.append(_bench_details(bench_rows))
    return blocks


@_guarded("playing XI")
def playing_xi_blocks(*, vs_bot, pitch, host_label, guest_label,
                      host_xi, guest_xi, host_bench=None, guest_bench=None,
                      trait_status=None, fairness=None, start_prompt=None):
    """The 'both XIs locked' card, as blocks.

    Takes already-flattened rows rather than ORM pairs on purpose: the handler
    reads those inside its open session (the objects are detached the moment it
    commits), and a renderer that touched the database would be one more thing
    that can strand a draft mid-setup.

    ``xi``/``bench`` rows are ``(name, rating, role, trait_badge)`` tuples;
    ``fairness`` is the Team Overall block described by :func:`fairness_blocks`.
    """
    title = ("🤖 Lets Play vs Bot — Playing XI" if vs_bot
             else "🧾 Lets Play — Playing XI")
    blocks = [R.heading(title, size=2),
              R.paragraph([R.bold("🌱 Pitch: "), _pitch_label(pitch),
                           "  ·  20 overs"])]
    if trait_status:
        blocks.append(R.paragraph(trait_status))
    blocks.append(R.paragraph(R.italic(
        "Batting order = the one you saved with /sbo (or by rating, high → "
        "low, if you never set one). Tweak it for this match below.")))

    blocks += _side_blocks(R.bold(f"👤 {host_label} (Host)"),
                           host_xi, host_bench)
    blocks += _side_blocks(R.bold(f"🎯 {guest_label}"), guest_xi, guest_bench)
    blocks += list(fairness or [])
    blocks.append(R.footer([
        "✏️ Reorder with ", R.code("/change <a> <b>"),
        " (both 1–11), or swap in a bench player — ", R.code("/change 2 13"),
        ". ", start_prompt or ""]))
    return blocks


def fairness_blocks(*, host_label, guest_label, host_ovr, guest_ovr,
                    gap_limit, vs_bot=False, boost=None, boost_note=None):
    """The Team Overall verdict — whether this match counts, and why.

    Its own builder because it is the one part of the XI card a captain may act
    on: a gap too wide costs both sides their career stats, and the answer is to
    even the XIs up with ``/change`` before the toss rather than after it. A
    verdict that has to be hunted for inside a paragraph gets missed, so it is a
    ``pullquote``.
    """
    if vs_bot:
        blocks = [R.table([
            [R.cell(R.bold("📊 Team Overall")),
             R.cell(f"You {host_ovr}"), R.cell(f"Bot {guest_ovr}",
                                               align="right")]],
            bordered=True, compact=True)]
        if boost:
            blocks.append(R.paragraph(boost))
        blocks.append(R.pullquote(
            R.bold("🎯 Practice match — unranked."),
            caption="No career stats, no coins or gems, no Win/Loss and no "
                    "streak. Just cricket."))
        return blocks

    gap = abs(int(host_ovr) - int(guest_ovr))
    blocks = [R.table([
        [R.cell(R.bold("📊 Team Overall")),
         R.cell(f"{host_label} {host_ovr}"),
         R.cell(f"{guest_label} {guest_ovr}", align="right")]],
        bordered=True, compact=True)]
    if gap >= gap_limit:
        blocks.append(R.pullquote(
            R.bold("⚠️ This match WON'T count."),
            caption=(f"The Team Overall gap is {gap}, over the limit of "
                     f"{gap_limit}. No career stats, no Win/Loss or streak, "
                     f"and no coins or gems — it keeps things fair. Play on "
                     f"for fun, or even the XIs up with /change.")))
    else:
        blocks.append(R.paragraph(["✅ ", R.bold("Stats will count"),
                                   f"  ·  {gap} apart, limit {gap_limit}"]))
    if boost:
        blocks.append(R.paragraph(boost))
    if boost_note:
        blocks.append(R.footer(R.italic(boost_note)))
    return blocks


# ══════════════════════════════════════════════════════════════════════
# The toss
# ══════════════════════════════════════════════════════════════════════

@_guarded("toss call")
def toss_call_blocks(caller, vs_bot=False):
    """"Heads or tails?" — the prompt above the two coin buttons.

    ``caller`` is the draft's player dict for whoever calls it: the guest in an
    ordinary match, and the human host against the bot.
    """
    return [R.heading("🪙 The Toss", size=3),
            R.paragraph([mention(caller), ", call it — ", R.bold("Heads"),
                         " or ", R.bold("Tails"), "?"]),
            R.footer(R.italic(
                "You call it against the bot." if vs_bot else
                "The guest calls it; the winner elects to bat or bowl."))]


@_guarded("toss result")
def toss_result_blocks(coin, call, caller_label, winner):
    """The coin has landed and the winner has a choice to make."""
    return [R.pullquote(R.bold(f"🪙 {str(coin).upper()}"),
                        caption=f"{caller_label} called {str(call).upper()}"),
            R.paragraph(["🏆 ", mention(winner),
                         " won the toss. ", R.bold("Bat"), " or ",
                         R.bold("Bowl"), " first?"])]


@_guarded("toss elected")
def toss_elected_blocks(winner, decision, *, bot_won=False):
    """What the toss card becomes once the winner has elected."""
    choice = "BAT" if decision == "bat" else "BOWL"
    who = R.bold("🤖 Bot") if bot_won else mention(winner)
    return [R.paragraph(["✅ ", who, " elected to ", R.bold(choice),
                         " first."]),
            R.footer(R.italic("The match begins below — play over by over!"))]


# ══════════════════════════════════════════════════════════════════════
# /change — what it takes, when somebody gets it wrong
# ══════════════════════════════════════════════════════════════════════

@_guarded("change usage")
def change_usage_blocks(problem):
    """The ``/change`` instructions, under whatever went wrong this time.

    Two commands share one syntax — reorder a batting slot, or bring a bench
    player in — and which one you get depends entirely on the second number.
    That is the thing people get wrong, so the two readings are two list items
    rather than one run-on sentence.
    """
    return [
        R.paragraph(R.bold(f"❌ {problem}")),
        R.paragraph(["Usage: ", R.code("/change <a> <b>")]),
        R.list_block([
            ["Reorder batting: both 1–11 — swaps those two slots. e.g. ",
             R.code("/change 3 1")],
            ["Bring in a sub: an XI slot (1–11) for a bench player (12+). "
             "e.g. ", R.code("/change 2 13")],
        ]),
    ]
