"""/howto — 4-section interactive tutorial.

Sections (selectable via buttons):
  🏏 Game     — playing matches, vsbot, toss, traits
  👥 Squad    — roster, claim, daily, gspin, captain, lineup
  💰 Economy  — coins, gems, market, traits, trades
  ⭐ General  — quests, achievements, leaderboard, profile, settings
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Section content
# ════════════════════════════════════════════════════════════════════

SECTIONS = {
    "game": {
        "emoji": "🏏",
        "title": "Game — Playing Matches",
        "body": (
            "<b>🏏 Playing Matches</b>\n\n"
            "<b>/playmatch</b> (or <b>/pm</b>) — challenge another player\n"
            "<b>/vsbot N</b> — play vs a bot team for N overs (1-20)\n"
            "<b>/sim N</b> — instantly simulate a full match vs a Sim XI; get the "
            "scorecard + commentary in seconds (no tapping)\n\n"
            "<b>The flow:</b>\n"
            "1️⃣ Select overs (1-20)\n"
            "2️⃣ Animated coin toss decides who calls\n"
            "3️⃣ Toss winner picks bat or bowl (pitch hint helps you choose!)\n"
            "4️⃣ Pick openers + opening bowler\n"
            "5️⃣ Ball-by-ball: bowler picks delivery → batsman picks shot\n\n"
            "<b>📍 Pitches matter</b>\n"
            "• <b>Flat / Hard</b> — batters' paradise\n"
            "• <b>Green</b> — seam friendly early\n"
            "• <b>Dry / Dusty</b> — spinners thrive late\n"
            "Pitches <b>wear down</b> over the match — 2nd innings plays differently!\n\n"
            "<b>⭐ Form matters</b>\n"
            "Each player's last 5 matches affect their effective rating (±2.5 OVR). Hot players hit harder; cold players struggle.\n\n"
            "<b>💎 Traits</b>\n"
            "Equipped traits activate during matches under certain conditions (death overs, chasing, vs spin, etc). Watch for the 💎 line in scorecards!\n\n"
            "<b>🤖 If you go AFK</b>\n"
            "Match heartbeat re-renders buttons after 90s of silence. After 5 min idle, the bot auto-decides defaults to keep the game flowing — no more forfeits.\n\n"
            "<b>/resume</b> or <b>/r</b> — instantly recover any stuck match\n"
            "<b>/endmatch</b> or <b>/em</b> — forfeit current match"
        ),
    },
    "squad": {
        "emoji": "👥",
        "title": "Squad — Roster & Players",
        "body": (
            "<b>👥 Building Your Squad</b>\n\n"
            "<b>/debut</b> (or <b>/d</b>) — start your career, get a balanced starter XI\n"
            "<b>/myroster</b> (or <b>/mr</b>) — view your full squad\n\n"
            "<b>📥 Getting players</b>\n"
            "<b>/claim</b> (or <b>/c</b>) — random pull every 8 hours\n"
            "<b>/daily</b> (or <b>/dl</b>) — daily login bonus + free player\n"
            "<b>/gspin</b> (or <b>/gs</b>) — spin the wheel for coins, gems, or rare players\n"
            "<b>/playermarket</b> (or <b>/market</b>) — 5 random 87+ rated players at 5% off, refreshed every 24h\n"
            "<b>/buypl &lt;name&gt;</b> (or <b>/buy</b>) — buy any player at full price\n"
            "<b>/buypack</b> (or <b>/packs</b>) — buy player packs (Bronze, Silver, Star, Legend, Ultimate)\n"
            "<b>/openpack</b> (or <b>/open</b>) — open packs from your inventory with reveal animation\n\n"
            "<b>🔍 Finding players</b>\n"
            "<b>/searchpl &lt;name&gt;</b> (or <b>/search</b>) — search by name (15 per page)\n"
            "<b>/searchovr &lt;rating&gt;</b> (or <b>/so</b>) — search by rating\n"
            "<b>/playerinfo &lt;name&gt;</b> (or <b>/info</b>) — full player details + traits\n\n"
            "<b>🏏 Building your XI</b>\n"
            "<b>/playingxi</b> (or <b>/pxi</b>) — view top 11 / required composition\n"
            "<b>/swapplayers A B</b> (or <b>/swap</b>) — reorder players\n"
            "<b>/setcaptain N</b> (or <b>/cap</b>) — pick your captain\n"
            "<b>/teamname &lt;name&gt;</b> — set your team name\n\n"
            "<b>Required XI composition:</b>\n"
            "• 3-5 Batsmen\n"
            "• 3-5 Bowlers\n"
            "• 1-2 Wicket Keepers\n"
            "• 1-3 All-rounders\n\n"
            "<b>📤 Releasing</b>\n"
            "<b>/releasepl &lt;name&gt;</b> (or <b>/rel</b>) — release for sell value\n"
            "<b>/releasemultiple A-B</b> (or <b>/rm</b>) — release a position range"
        ),
    },
    "economy": {
        "emoji": "💰",
        "title": "Economy — Coins, Gems & Trading",
        "body": (
            "<b>💰 Currencies</b>\n"
            "🪙 <b>Coins</b> — primary currency. Earned from matches, daily, releasing players.\n"
            "💎 <b>Gems</b> — premium currency. Earned from achievements, win streaks, gspin.\n"
            "⭐ <b>Quest Points</b> — earned from completing quests; track engagement progress.\n\n"
            "<b>💵 Earning</b>\n"
            "• <b>Win a match:</b> overs × 300 coins + overs gems\n"
            "• <b>Lose a match:</b> overs × 150 coins (still rewarded for playing)\n"
            "• <b>/daily:</b> daily login bonus, increases with streak\n"
            "• <b>/gspin:</b> spin wheel — 58% coin rewards, 13% gems, 5% rare players\n"
            "• <b>Releasing players:</b> sell value scales with rating\n"
            "• <b>Achievements unlock:</b> bonus coins/gems on each badge\n\n"
            "<b>🛒 Spending</b>\n"
            "<b>/buypl</b> — buy a player at market price\n"
            "<b>/playermarket</b> — daily 87+ market at 5% off\n"
            "<b>/traitshop</b> — buy traits with gems (150-1500💎 by level)\n"
            "<b>/trade</b> — peer-to-peer player trades\n\n"
            "<b>💎 Traits (gem economy)</b>\n"
            "<b>/traits</b> — view your active player-traits\n"
            "<b>/traitshop</b> — buy new traits (random)\n"
            "<b>/traitapply</b> — equip a trait on a player\n"
            "<b>/traitupgrade</b> — upgrade trait level (L1→L5)\n"
            "<b>/traitreplace</b> — swap an equipped trait for an unowned one\n\n"
            "<b>🔄 Trading</b>\n"
            "<b>/trade @user</b> — start a trade negotiation (player ↔ player)\n"
            "Trades complete = both achievements unlock + bot logs it\n\n"
            "<b>💼 Check balance</b>\n"
            "<b>/purse</b> (or <b>/p</b>) — current coins + gems"
        ),
    },
    "general": {
        "emoji": "⭐",
        "title": "General — Progression & Profile",
        "body": (
            "<b>⭐ Progression Systems</b>\n\n"
            "<b>🎯 Quests</b>\n"
            "<b>/myquest</b> (or <b>/mq</b>) — view daily + monthly quests\n"
            "• Daily quests reset every 24h, give 5 quest points each\n"
            "• Monthly quests reset every 30 days, give 10 quest points each\n"
            "• Many give bonus coins/gems on top\n"
            "• Use \"CLAIM ALL\" when 2+ are ready\n\n"
            "<b>🏆 Achievements</b>\n"
            "<b>/achievements</b> (or <b>/ach</b>, <b>/badges</b>) — view all 37 achievements\n"
            "• Auto-unlock when conditions are met (no claiming needed)\n"
            "• Each unlock awards bonus coins or gems\n"
            "• Categories: Match, Streak, Batting, Bowling, Collection, Traits, Economy, Engagement, Social\n"
            "• Top 3 most-recent badges shown on your profile\n\n"
            "<b>👤 Profile & Stats</b>\n"
            "<b>/myprofile</b> (or <b>/me</b>) — your team page\n"
            "<b>/stats</b> (or <b>/st</b>) — career stats\n"
            "<b>/leaderboard</b> (or <b>/lb</b>, <b>/top</b>) — global rankings\n\n"
            "<b>🔥 Win Streaks</b>\n"
            "Consecutive wins build a streak. Best streak persists in your profile and unlocks streak achievements (3/5/10 wins).\n\n"
            "<b>💬 Live Commentary</b>\n"
            "When you score / take a wicket / play a dot, randomized commentary lines appear in the scorecard for flavor.\n\n"
            "<b>🆘 Stuck?</b>\n"
            "<b>/resume</b> or <b>/r</b> — recover any stuck match instantly\n"
            "<b>/lastmatch</b> or <b>/lm</b> — see your last completed match\n"
            "<b>/howto</b> — show this help (you're here!)"
        ),
    },
    "tours": {
        "emoji": "🏆",
        "title": "Tours — Multi-match Challenges",
        "body": (
            "<b>🏆 Tours</b>\n\n"
            "A tour is a multi-match series between you and another player. "
            "First to win more matches wins the tour. Ties get marked as drawn.\n\n"
            "<b>📨 Creating a tour</b>\n"
            "<b>/cmtours @user</b> — challenge another user (group chats only)\n"
            "Then pick:\n"
            "  • Match count: 3, 4, 5, or 6\n"
            "  • Overs per match: 5, 8, 10, 15, or 20\n"
            "Invite expires in <b>30 seconds</b> if not accepted.\n\n"
            "<b>📋 Playing a tour</b>\n"
            "<b>/mytours</b> (or <b>/tours</b>) — view your active tour\n"
            "Tap <b>▶️ Play Match N</b> to start the next match.\n"
            "The match runs exactly like <code>/playmatch</code> — both users participate.\n\n"
            "<b>🏟️ Each match has its own venue</b>\n"
            "Stadium and pitch type are randomized per match — adapt your strategy!\n\n"
            "<b>📈 Tour Stats button</b>\n"
            "Shows top run-scorers and top wicket-takers across the tour.\n\n"
            "<b>⏰ Tour lifetime</b>\n"
            "Tour expires after <code>matches × 2 days</code>. Unfinished matches mark "
            "the tour complete with the current score deciding the winner.\n\n"
            "<b>One tour at a time</b>\n"
            "You can't create a new tour while you're in another."
        ),
    },
}


def _build_keyboard(active_section, owner_tg):
    """Build the section-tab keyboard."""
    rows = [[]]
    for key, data in SECTIONS.items():
        label = f"{data['emoji']} {key.title()}"
        if key == active_section:
            label = "• " + label
        if len(rows[-1]) >= 3:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(
            label, callback_data=f"howto_tab_{owner_tg}_{key}"))
    rows.append([InlineKeyboardButton("❌ Close", callback_data=f"howto_close_{owner_tg}")])
    return InlineKeyboardMarkup(rows)


# ════════════════════════════════════════════════════════════════════
# /howto
# ════════════════════════════════════════════════════════════════════

async def howto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user

    section = "game"  # default
    data = SECTIONS[section]
    text = (
        "📖 <b>HOW TO PLAY</b>\n\n"
        "Pick a section to learn about that area.\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        + data["body"]
    )
    kb = _build_keyboard(section, tg.id)

    sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb,
                                            disable_web_page_preview=True)
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=300)
    except Exception:
        pass


async def howto_tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """howto_tab_<owner_tg>_<section>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        section = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if section not in SECTIONS:
        await q.answer("Unknown")
        return

    await q.answer()
    data = SECTIONS[section]
    text = (
        "📖 <b>HOW TO PLAY</b>\n\n"
        f"Section: {data['emoji']} <b>{data['title']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        + data["body"]
    )
    kb = _build_keyboard(section, tg.id)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:
        pass


async def howto_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await q.edit_message_text("📖 <i>Tutorial closed. Type /howto anytime.</i>",
                                   parse_mode="HTML")
    except Exception:
        pass
