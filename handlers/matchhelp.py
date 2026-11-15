"""/matchhelp — the interactive guide to playing /letsplay and /cipl.

`/howto` covers the whole bot in four broad tabs. This is the one mode that
needs its own manual: the Approach game is a simultaneous-move contest with a
5x5 payoff matrix, three phases, eight pitches that change between innings, two
momentum tracks and a predictability rule, and none of that is discoverable by
tapping buttons and hoping.

**The pages are generated, not typed.** Every approach name, pitch par band,
phase window, momentum cap and rule threshold on these pages is read out of the
engine that will actually bowl the over — see the ``_from_engine`` helpers below.
A guide with the numbers hand-copied into it is a guide that is wrong one tuning
pass later, and a player who is told the death overs start at 16 when the engine
says 17 has been actively misled. Anything the engine cannot tell us (the shape
of the strategy advice) is prose; everything else is looked up.

Layout is a home menu of topics, each opening a page with Back / Prev / Next, so
the guide can be browsed by topic or read straight through.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)

CB = "mhelp"          # callback namespace


# ════════════════════════════════════════════════════════════════════
# Numbers pulled out of the engine, so the guide cannot go stale
# ════════════════════════════════════════════════════════════════════

def _approach_lines(approaches, blurbs):
    """"🛡 <b>Defensive</b> — survive the over." for each approach, in pick order."""
    out = []
    for key, emoji, label in approaches:
        out.append(f"{emoji} <b>{label}</b> — {blurbs.get(key, '')}")
    return "\n".join(out)


def _batting_block():
    from engine.approach_modifiers import BATTING_APPROACHES
    return _approach_lines(BATTING_APPROACHES, {
        "defensive": "protect wickets; scoring slows down.",
        "rotate": "work singles and twos; useful through the middle overs.",
        "balanced": "mix safe scoring with boundary chances.",
        "aggressive": "attack boundaries with more wicket risk.",
        "ultra": "maximum attack and wicket risk; best saved for the death.",
    })


def _bowling_block():
    from engine.approach_modifiers import BOWLING_APPROACHES
    return _approach_lines(BOWLING_APPROACHES, {
        "defensive": "limit boundaries while conceding singles.",
        "balanced": "a steady line-and-length option.",
        "mixed": "vary deliveries; more unpredictable outcomes.",
        "aggressive": "hunt wickets while offering boundary chances.",
        "variation": "cutters and slower balls to disrupt big hitters.",
    })


def _phase_block():
    """Phase windows, read from the engine's own definition."""
    from services.cipl_match import DEATH_UNITS, FORMAT_SPECS
    pp = FORMAT_SPECS["T20"]["powerplay_units"]
    return (
        f"<b>Powerplay — overs 1-{pp}</b>\n"
        "Only two fielders out. Attacking intents get their value and the "
        "bowling side's strike rate suffers. The new ball also finds the "
        "stumps: more bowled and LBW here than anywhere else.\n\n"
        f"<b>Middle — overs {pp + 1}-{20 - DEATH_UNITS}</b>\n"
        "Spin, sweepers back, a single always on. The accumulator's phase, and "
        "the worst place in the innings to try to hit spin off the square. "
        "<b>Ultra Attack is a losing pick here</b> once you price the wicket.\n\n"
        f"<b>Death — overs {21 - DEATH_UNITS}-20</b>\n"
        "Everyone swings. Ultra is worth most, Defensive is worth least, and "
        "nearly every wicket is a catch. On true surfaces the last three overs "
        "go faster still."
    )


def _pitch_block():
    """Every pitch a host can pick: what it is, its par band, who it pays and
    how it changes for the chase — all read from the engine, none of it typed.

    Listed batting-friendly → bowler-friendly (the reverse of the registry's own
    order), because the surfaces a captain is weighing at the toss are the ones
    at the top of a page, not the bottom.
    """
    from engine import ground_config, pitch_registry, pitch_state
    rows = []
    for pitch in reversed(pitch_registry.SELECTABLE):
        dyn = ground_config.get_scoring_dynamics(pitch) or {}
        par_lo, par_hi = dyn.get("par_low"), dyn.get("par_high")
        note = pitch_state.evolution_note(pitch) or ""
        par = f"par {par_lo}-{par_hi}" if par_lo else "par varies"
        prof = pitch_registry.profile(pitch)
        toss = "BOWL" if prof.toss == "bowl" else "BAT"
        rows.append(
            f"<b>{pitch}</b> — {par} · favours {prof.favours.lower()}\n"
            f"<i>2nd innings: {note}</i>\n"
            f"<i>Toss (day): {toss}</i>")
    return "\n\n".join(rows)


def _predictability_block():
    from engine.approach_modifiers import REPEAT_FROM, REPEAT_CAP
    return (
        f"Pick the same thing <b>{REPEAT_FROM} overs running</b> and the "
        "opposition has it read.\n\n"
        "• A readable <b>batting</b> side scores less and loses more wickets — "
        "the field is set for it.\n"
        "• A readable <b>bowling</b> side gets hit — the batter has picked it.\n\n"
        f"It gets worse to {REPEAT_CAP} overs and then stops, and it "
        "<b>resets the moment you change</b>. The cost is for being readable, "
        "not for using an approach — one different over buys the surprise back.\n\n"
        "If <i>both</i> captains are equally predictable it cancels: neither has "
        "learned anything the other has not."
    )


def _momentum_block():
    from engine.momentum import MOMENTUM_CAP, PRESSURE_CAP
    return (
        f"<b>Momentum</b> (−{MOMENTUM_CAP:g} to +{MOMENTUM_CAP:g})\n"
        "Boundaries, big overs and 50/100 partnerships build it. Wickets, "
        "maidens and dot-ball runs tear it down. A side in flow scores faster "
        "and gets out less.\n\n"
        f"<b>Pressure</b> (chasing only, −{PRESSURE_CAP:g} to +{PRESSURE_CAP:g})\n"
        "A required rate climbing away from you, dots, and lost wickets all "
        "pile it on. Boundaries that pull the ask back — and a short target "
        "with wickets in hand — take it off.\n\n"
        "<b>They net off.</b> Momentum +1.2 against Pressure +0.8 is not a side "
        "in flow, it is a side roughly level, and the engine treats it that way."
    )


# ════════════════════════════════════════════════════════════════════
# Pages
# ════════════════════════════════════════════════════════════════════

PAGES = [
    ("start", "🏁", "The two modes", lambda: (
        "<b>Choose a mode</b>\n"
        "• /letsplay — use your collected squad and equipped traits.\n"
        "• /cipl — play with Challenge League franchise squads.\n"
        "• /lpbot or /ciplbot — unranked practice against the AI.\n\n"
        "<b>Start playing</b>\n"
        "Challenge someone, agree on the format, choose your XI and complete the toss. "
        "Each over needs a bowler, a bowling plan and a batting intent.\n\n"
        "<b>Useful commands</b>\n"
        "/rcl or /r — resume a stuck match\n"
        "/pause · /saved · /continue — save and return later\n"
        "/playapp · /playchat — switch where you play\n"
        "/matchguide — full guide as a file\n\n"
        "Choose a topic below."
    )),
    ("over", "🔁", "How an over works", lambda: (
        "1. Bowling captain selects a <b>bowler</b>.\n"
        "2. Bowling captain chooses a <b>bowling plan</b>.\n"
        "3. Batting captain chooses a <b>batting intent</b>.\n"
        "4. The engine resolves the over from both picks.\n\n"
        "Plans stay hidden while captains choose. Mini App plans and the AI's picks remain private.\n\n"
        "In a full T20, a bowler can bowl up to 4 overs and cannot bowl consecutive overs.\n"
        "Open <b>Manage match</b> inside the live message for the scorecard, Impact Player and batting controls."
    )),
    ("batting", "🏏", "Batting intents", lambda: (
        _batting_block() + "\n\n"
        "Higher attack trades more wickets for more scoring. Pick for the phase, pitch and chase."
    )),
    ("bowling", "🎯", "Bowling plans", lambda: (
        _bowling_block() + "\n\n"
        "Choose a plan for the intent you expect. Change it before repeated picks become predictable."
    )),
    ("duel", "⚔️", "Approach vs approach", lambda: (
        "<b>Read the game.</b> Each intent is built to beat particular plans. "
        "Guess right — blind — and you get +8% runs for the over. Walk into a "
        "plan your intent has no answer to and the bowler gets +10% wicket "
        "chance. Both directions are priced, so the mind game is not a free "
        "gift to the batting side.\n\n"
        "<b>Special combinations.</b> Eight named match-ups play differently — "
        "The Chess Match, The Shootout, The Gladiator Over, The Blockathon and "
        "the rest. Some change the over you are in; some set up the next one.\n\n"
        "<b>Best replies move.</b> There is no single right answer:\n"
        "• vs Defensive bowling → Ultra\n"
        "• vs Mixed → Rotate Strike\n"
        "• vs Aggressive → Aggressive\n"
        "• vs Variation → Balanced\n\n"
        "Which is why the next page matters most."
    )),
    ("repeat", "🔍", "Don't be predictable", _predictability_block),
    ("phases", "⏱", "The three phases", _phase_block),
    ("pitches", "📍", "Pitches", lambda: (
        _pitch_block() + "\n\n"
        "<i>The pitch does not reset at the interval — it carries its wear into "
        "the chase. Read the 2nd-innings line before you decide at the toss.</i>"
        "\n\n<b>/pitchstats</b> shows what each surface has <i>actually</i> done "
        "in real matches — runs and wickets per over, the bat-first vs chasing "
        "split, and which approaches are earning on it."
    )),
    ("momentum", "📈", "Momentum & the chase", _momentum_block),
    ("squad", "💎", "Squad, order & traits", lambda: (
        "<b>Your XI</b>\n"
        "<b>/lineup</b> — set your XI\n"
        "<b>/battingorder</b> (or <b>/bo</b>) — save a batting order the match "
        "reuses, so you are not rebuilding it every game\n\n"
        "<b>During a live innings</b>\n"
        "<b>/managebatting</b> — retire either batter, choose who comes in, or "
        "move available batsmen up/down. <b>/retire</b> and <b>/liveorder</b> open "
        "the same controls. In the Mini App, tap <b>Manage batting</b>.\n"
        "Retired players keep their score, can return, and cost no wicket. "
        "Changes apply only to this innings, between overs or before the next delivery.\n\n"
        "<b>Traits — /letsplay only</b>\n"
        "Challenge League squads carry no traits at all; CIPL is pure ratings "
        "and captaincy. In LetsPlay, equipped traits fire on their conditions — "
        "death overs, chasing, against spin, on a hot streak — and the scorecard "
        "shows a 💎 line when one does. See <b>/traitlist</b>.\n"
        "Both captains are asked, before the toss, whether traits play at all: "
        "yes from both plays with them, no from both plays without, and one of "
        "each defers to the lower-rated XI. Nothing is unequipped either way.\n\n"
        "<b>Chemistry</b> counts too: a pair that has batted four overs together "
        "runs better between the wickets. <b>/chemhelp</b> has the detail.\n\n"
        "<b>Form</b> — a player's last five matches move their effective rating."
    )),
    ("after", "🏆", "After the match", lambda: (
        "<b>📋 The scorecards</b>\n"
        "Batting and bowling cards go out at the end of every innings, and a "
        "summary card at the result. They are archived as they are sent, so a "
        "card that never arrived — or one buried a hundred messages up — comes "
        "back with <b>/lastscorecard</b> (or <b>/lsc</b>), which replays the "
        "last match played in this chat. <b>/lastscorecard 1234</b> pulls up a "
        "specific match by number.\n\n"
        "<b>📊 The match analysis file</b>\n"
        "Every finished match sends a full HTML report into the chat. Open it in "
        "your browser for:\n"
        "• phase-by-phase splits against the pitch's par\n"
        "• runs per over, and both innings' worms\n"
        "• full scorecards\n"
        "• momentum, pressure and the live win probability\n"
        "• the turning points\n"
        "• <b>the approach duel</b> — every over's two picks, the balls, and a "
        "5×5 grid of what each intent returned against each plan\n\n"
        "That grid is the fastest way to get better: it shows which of your "
        "intents actually worked against which plan.\n\n"
        "<b>🔥 Super Over</b> — a tie goes to one over each, then countback.\n\n"
        "<b>Rewards</b> — coins, gems, quest progress and career stats. Bot "
        "matches are unranked and pay nothing."
    )),
    ("tips", "🧠", "Playing well", lambda: (
        "• <b>Vary your picks.</b> Repetition lets the opposition read you.\n"
        "• <b>Respect the phase.</b> Protect the middle overs; plan your attack at the death.\n"
        "• <b>Read the pitch.</b> Its behaviour can change in the chase.\n"
        "• <b>Manage wicket risk.</b> Fast scoring means little if you run out of batters.\n"
        "• <b>Save key bowlers.</b> Keep overs available for dangerous batters.\n"
        "• <b>Watch momentum.</b> A wicket or quiet over can turn the game.\n"
        "• <b>Review your analysis.</b> Check which intent worked against each plan."
    )),
]

PAGE_INDEX = {key: i for i, (key, _, _, _) in enumerate(PAGES)}
ALIASES = {"approach": "duel", "approaches": "duel", "pitch": "pitches",
           "phase": "phases", "bat": "batting", "bowl": "bowling",
           "help": "start", "tip": "tips", "trait": "squad"}

HEADER = "🏏 <b>MATCH GUIDE</b> — /letsplay &amp; /cipl"
TOPIC_LABELS = {
    "start": "Start here", "over": "Over flow", "batting": "Batting", "bowling": "Bowling",
    "duel": "Match-ups", "repeat": "Repetition", "phases": "Phases", "pitches": "Pitches",
    "momentum": "Momentum", "squad": "Squad & traits", "after": "After match", "tips": "Quick tips",
}


def _page_text(index):
    _key, emoji, title, body = PAGES[index]
    # Titles are plain text — escape them, because Telegram rejects the whole
    # message on malformed HTML and a bare "&" in a title is easy to miss.
    return (f"{HEADER}\n"
            f"{emoji} <b>{html.escape(title)}</b>  "
            f"<i>({index + 1}/{len(PAGES)})</i>\n"
            "\n\n" + body())


def _keyboard(index, owner_tg):
    """Topic grid, then Prev/Next, then Close — so it reads by topic or straight
    through."""
    rows, row = [], []
    for i, (key, emoji, _title, _body) in enumerate(PAGES):
        label = f"{'✓ ' if i == index else ''}{emoji} {TOPIC_LABELS.get(key, _title)}"
        row.append(InlineKeyboardButton(
            label, callback_data=f"{CB}_go_{owner_tg}_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Back", callback_data=f"{CB}_go_{owner_tg}_{PAGES[index - 1][0]}"))
    if index < len(PAGES) - 1:
        nav.append(InlineKeyboardButton(
            f"{PAGES[index + 1][1]} Next ▶️",
            callback_data=f"{CB}_go_{owner_tg}_{PAGES[index + 1][0]}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close",
                                      callback_data=f"{CB}_close_{owner_tg}")])
    return InlineKeyboardMarkup(rows)


async def matchhelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/matchhelp [topic] — open the guide, optionally straight to a topic."""
    tg = update.effective_user
    index = 0
    if context.args:
        wanted = str(context.args[0]).strip().lower()
        wanted = ALIASES.get(wanted, wanted)
        index = PAGE_INDEX.get(wanted, 0)
    sent = await update.message.reply_text(
        _page_text(index), parse_mode="HTML", reply_markup=_keyboard(index, tg.id),
        disable_web_page_preview=True)
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                delay_seconds=600)
    except Exception:
        pass


async def matchhelp_go_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """mhelp_go_<owner_tg>_<page_key>"""
    q = update.callback_query
    tg = q.from_user
    try:
        _, _, owner, key = q.data.split("_", 3)
        owner_tg = int(owner)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Open your own with /matchhelp", show_alert=True)
        return
    index = PAGE_INDEX.get(key)
    if index is None:
        await q.answer("Unknown page")
        return
    await q.answer()
    try:
        await q.edit_message_text(_page_text(index), parse_mode="HTML",
                                  reply_markup=_keyboard(index, tg.id),
                                  disable_web_page_preview=True)
    except Exception:
        # Telegram rejects an edit that changes nothing — tapping the page you
        # are already on is not an error worth logging.
        pass


async def matchhelp_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Closed")
    try:
        await q.edit_message_text(
            "🏏 <i>Match guide closed. /matchhelp anytime.</i>", parse_mode="HTML")
    except Exception:
        pass
