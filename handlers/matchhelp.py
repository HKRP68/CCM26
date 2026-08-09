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
        "defensive": "block it out. Almost no wickets, almost no runs. A tool "
                     "for seeing off a spell or protecting the last pair.",
        "rotate": "ones and twos, no risks. The middle-overs workhorse — it "
                  "keeps the board moving and the strike turning over.",
        "balanced": "respect the good ball, punish the bad one. The honest "
                    "contest, and never a mistake.",
        "aggressive": "look for the boundary from ball one. Real runs, real "
                      "risk.",
        "ultra": "swing at everything. The most runs on the board and the most "
                 "wickets in the book — a death-overs weapon that will cost you "
                 "the match in the 11th.",
    })


def _bowling_block():
    from engine.approach_modifiers import BOWLING_APPROACHES
    return _approach_lines(BOWLING_APPROACHES, {
        "defensive": "dry it up. Kills the boundary, hands over singles. "
                     "Vulnerable to a batter happy to take those singles.",
        "balanced": "line and length. No weakness, no edge.",
        "mixed": "change it up ball to ball. Adds volatility and squeezes the "
                 "six; a settled batter reads the pattern.",
        "aggressive": "attack the stumps and the body. Buys wickets by offering "
                      "pace — which is exactly what a slogger wants.",
        "variation": "slower balls, cutters, wrong'uns. Punishes the swinger, "
                     "wasted on someone content to nudge it around.",
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
    """Every pitch, its par band and how it changes for the chase — from config."""
    from engine import ground_config, pitch_state
    order = ("Flat", "Hard", "Even", "Bouncy", "Dry", "Green", "Dusty")
    rows = []
    for pitch in order:
        dyn = ground_config.get_scoring_dynamics(pitch) or {}
        par_lo, par_hi = dyn.get("par_low"), dyn.get("par_high")
        note = pitch_state.evolution_note(pitch) or ""
        par = f"par {par_lo}-{par_hi}" if par_lo else "par varies"
        rows.append(f"<b>{pitch}</b> — {par}\n<i>2nd innings: {note}</i>")
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
        "<b>/letsplay</b> (or <b>/lp</b>) — your own collected squad, traits and "
        "all, against another player's.\n"
        "<b>/cipl</b> — Challenge League. Drafted franchise squads; no traits, "
        "so it is decided on ratings and captaincy alone.\n\n"
        "<b>Practice against the AI</b>\n"
        "<b>/lpbot</b> — LetsPlay vs the bot captain\n"
        "<b>/ciplbot</b> — CIPL vs the bot captain\n"
        "Bot matches are unranked. The bot's picks stay hidden, because a bot "
        "whose plans are published is a bot you can write down and beat.\n\n"
        "<b>Getting going</b>\n"
        "1️⃣ Send the command (reply to someone to challenge them)\n"
        "2️⃣ Pick overs and accept\n"
        "3️⃣ Toss — the winner bats or bowls\n"
        "4️⃣ Pick your XI and batting order\n"
        "5️⃣ Play the over loop below\n\n"
        "Stuck? <b>/rcl</b> resumes a Challenge League match, <b>/r</b> any other."
    )),
    ("over", "🔁", "How an over works", lambda: (
        "Every over is a <b>simultaneous guess</b>. Neither captain sees the "
        "other's pick until the over is bowled — that is the whole game.\n\n"
        "1️⃣ The bowling captain picks a <b>bowler</b>\n"
        "2️⃣ …then a <b>bowling plan</b>, hidden\n"
        "3️⃣ The batting captain picks a <b>batting intent</b>, hidden\n"
        "4️⃣ Both are revealed and the over is bowled ball by ball\n\n"
        "The two picks together decide the shape of the over. Neither one alone "
        "does — the same intent can be your best or worst choice depending on "
        "what you were up against.\n\n"
        "<b>Bowler limits:</b> 4 overs each in a T20, and nobody bowls two in a "
        "row.\n\n"
        "<b>If you go quiet</b> the bot picks a sensible default for you after a "
        "few minutes, so a match never dies waiting."
    )),
    ("batting", "🏏", "Batting intents", lambda: (
        _batting_block() + "\n\n"
        "<b>Risk is genuinely priced.</b> Ultra scores the most and loses the "
        "most. Measured through the engine, at the death Ultra is the best pick "
        "on the board; in the middle overs it is the worst, because the wicket "
        "it costs you is worth more than the runs it buys.\n\n"
        "There is no approach that is right everywhere. If there were, there "
        "would be no game."
    )),
    ("bowling", "🎯", "Bowling plans", lambda: (
        _bowling_block() + "\n\n"
        "<b>Every plan has a weakness</b>, and that is the point:\n"
        "• Defensive shuts the boundary but concedes the single\n"
        "• Aggressive buys wickets by offering pace to hit\n"
        "• Variation punishes the swinger, wasted on the nudger\n"
        "• Mixed spikes on the surprise ball, readable if you settle\n"
        "• Balanced has no weakness and no edge\n\n"
        "Match the plan to the intent you think is coming — not to the one you "
        "would pick yourself."
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
    )),
    ("momentum", "📈", "Momentum & the chase", _momentum_block),
    ("squad", "💎", "Squad, order & traits", lambda: (
        "<b>Your XI</b>\n"
        "<b>/lineup</b> — set your XI\n"
        "<b>/battingorder</b> (or <b>/bo</b>) — save a batting order the match "
        "reuses, so you are not rebuilding it every game\n\n"
        "<b>Traits — /letsplay only</b>\n"
        "Challenge League squads carry no traits at all; CIPL is pure ratings "
        "and captaincy. In LetsPlay, equipped traits fire on their conditions — "
        "death overs, chasing, against spin, on a hot streak — and the scorecard "
        "shows a 💎 line when one does. See <b>/traitlist</b>.\n\n"
        "<b>Chemistry</b> counts too: a pair that has batted four overs together "
        "runs better between the wickets. <b>/chemhelp</b> has the detail.\n\n"
        "<b>Form</b> — a player's last five matches move their effective rating."
    )),
    ("after", "🏆", "After the match", lambda: (
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
        "<b>1. Vary.</b> The single biggest edge. Four overs of the same pick "
        "and the opposition reads you; one different over resets it.\n\n"
        "<b>2. Price the wicket, not the runs.</b> A wicket costs roughly 18 "
        "runs of what is left. Ultra in the 11th over wins the over and loses "
        "the innings.\n\n"
        "<b>3. Pick for the phase.</b> Rotate through the middle, save Ultra "
        "for the last four, and never bat Defensive at the death — it is "
        "actively penalised.\n\n"
        "<b>4. Read the pitch twice.</b> What it does in the first innings is "
        "not what it does in the chase. A dry track is a lottery at the death; "
        "a green one has flattened out by over 9.\n\n"
        "<b>5. Bowl your best at the danger.</b> Bowlers get four overs and "
        "cannot bowl consecutively — spend them on the batters who are set, not "
        "on the ones who just walked in.\n\n"
        "<b>6. Watch the momentum line.</b> It is on the over summary. A side "
        "in flow is harder to bowl at; break it with a wicket or a maiden and "
        "the whole innings turns.\n\n"
        "<b>7. Read your own analysis file.</b> The duel grid tells you which "
        "picks are actually earning."
    )),
]

PAGE_INDEX = {key: i for i, (key, _, _, _) in enumerate(PAGES)}
ALIASES = {"approach": "duel", "approaches": "duel", "pitch": "pitches",
           "phase": "phases", "bat": "batting", "bowl": "bowling",
           "help": "start", "tip": "tips", "trait": "squad"}

HEADER = "🏏 <b>MATCH GUIDE</b> — /letsplay &amp; /cipl"


def _page_text(index):
    _key, emoji, title, body = PAGES[index]
    # Titles are plain text — escape them, because Telegram rejects the whole
    # message on malformed HTML and a bare "&" in a title is easy to miss.
    return (f"{HEADER}\n"
            f"{emoji} <b>{html.escape(title)}</b>  "
            f"<i>({index + 1}/{len(PAGES)})</i>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n" + body())


def _keyboard(index, owner_tg):
    """Topic grid, then Prev/Next, then Close — so it reads by topic or straight
    through."""
    rows, row = [], []
    for i, (key, emoji, _title, _body) in enumerate(PAGES):
        label = f"{emoji}{'•' if i == index else ''}"
        row.append(InlineKeyboardButton(
            label, callback_data=f"{CB}_go_{owner_tg}_{key}"))
        if len(row) == 6:
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
