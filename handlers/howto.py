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

from config import trait_sell_value, trait_trade_fee, TRAIT_MAX_LEVEL
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)

# Read the resale/swap price lists off ``config`` rather than typing them out —
# a tuning pass on the trait economy re-prices this tutorial with it instead of
# leaving a stale table on the page nobody remembers to edit.
_TRAIT_LEVELS = range(1, TRAIT_MAX_LEVEL + 1)
TRAIT_SELL_TABLE = " / ".join(f"{trait_sell_value(l):,}" for l in _TRAIT_LEVELS)
TRAIT_FEE_TABLE = " / ".join(f"{trait_trade_fee(l):,}" for l in _TRAIT_LEVELS)


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
            "<b>/claim</b> (or <b>/c</b>) — random pull every hour (faster on paid tiers)\n"
            "<b>/daily</b> (or <b>/dl</b>) — daily login bonus + free player\n"
            "<b>/gspin</b> (or <b>/gs</b>) — spin the wheel for coins, gems, or rare players\n"
            "<b>/playermarket</b> (or <b>/market</b>) — 5 random 87+ rated players, refreshed every 24h (Platinum 5% / Diamond 10% off)\n"
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
            "<b>/setbo</b> (or <b>/sbo</b>) — view your batting order; "
            "<b>/sbo 2 11</b> swaps two slots, <b>/sbo 2 13</b> brings a bench "
            "player in, <b>/sbo auto</b> rebuilds it by batting rating\n"
            "   <i>Saved once, it is used in every match — /playmatch, /vsbot, "
            "/letsplay, /wpm, /wsp, /sim, tours and Quick Match. Slots 1 &amp; 2 "
            "open and a wicket brings in the next player down, so you are never "
            "asked to pick openers again.</i>\n"
            "<b>/setcaptain N</b> (or <b>/cap</b>) — pick your captain\n"
            "<b>/teamname &lt;name&gt;</b> — set your team name\n\n"
            "<b>Required XI composition:</b>\n"
            "• 3-5 Batsmen\n"
            "• 3-5 Bowlers\n"
            "• 1-2 Wicket Keepers\n"
            "• 1-3 All-rounders\n\n"
            "<b>📤 Releasing</b>\n"
            "<b>/releasepl &lt;name&gt;</b> (or <b>/rel</b>) — release for sell value\n"
            "<b>/releasemultiple A-B</b> (or <b>/rm</b>) — release a position range\n\n"
            "<b>🎖 Your Career Player</b>\n"
            "<b>/cmucareer</b> (or <b>/career</b>) — create the one card that is "
            "<i>you</i>. Pick your country, your initials, your batting and "
            "bowling style and your face; the name is generated for you and "
            "belongs to nobody else.\n"
            "• Always an <b>All-rounder</b>, always starts at <b>78</b> "
            "batting, bowling and OVR\n"
            "• Ten attributes — Technique, Power, Timing, Footwork, Composure, "
            "Pace, Accuracy, Swing, Stamina, Variation — each upgraded with "
            "gems in the Mini App. A point costs its own new value (78 → 79 is "
            "79 💎) and caps at 99\n"
            "• Batting Power is the average of your five batting attributes, "
            "Bowling Specs the average of your five bowling ones, and OVR the "
            "average of those two — so every upgrade counts in real matches\n"
            "• It has its own <b>weekly quests</b> — <b>5 fresh ones every "
            "Monday</b>, dealt 2 batting, 2 bowling and 1 all-round from a pool "
            "of nearly 70, so your card is never the same two weeks running and "
            "rarely the same as anybody else's. Each pays quest points and "
            "gems, with the tougher ones paying more\n"
            "• Clear <b>all five</b> in a week to build your streak; keep it up "
            "week after week for the <b>gem jackpot</b>\n"
            "• <b>It can never be sold or traded.</b> One per account, free"
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
            "<b>/playermarket</b> — daily 87+ market (Platinum 5% / Diamond 10% off)\n"
            "<b>/traitshop</b> — buy traits with gems (150-1500💎 by level)\n"
            "<b>/trade</b> — peer-to-peer player trades\n\n"
            "<b>💎 Traits (gem economy)</b>\n"
            "<b>/traits</b> — view your active player-traits\n"
            "<b>/traitshop</b> — buy new traits (random)\n"
            "<b>/traitapply</b> — equip a trait on a player\n"
            "<b>/traitupgrade</b> — upgrade trait level (L1→L5)\n"
            "<b>/traitreplace</b> — swap an equipped trait for an unowned one\n"
            "<b>/removetrait</b> — unequip a trait (returns to inventory, free)\n"
            "<b>/selltrait</b> — sell an inventory trait for gems\n"
            "<b>/tradetrait @user</b> — swap a trait with another captain\n"
            "• Sold, traded or replaced a player? Their traits come back to "
            "your inventory at the same level\n\n"
            "<b>💠 Trait resale &amp; swaps</b>\n"
            "A trait's value is what it cost to build — 150💎 for Lv.1 plus every "
            "upgrade on top (Lv.5 = 3,050💎).\n"
            f"• <b>/selltrait</b> pays just under what the trait cost you: "
            f"{TRAIT_SELL_TABLE}💎 for Lv.1→5\n"
            "• <b>/tradetrait</b> swaps two traits of the <b>same level</b>, and "
            f"each side pays {TRAIT_FEE_TABLE}💎 for Lv.1→5\n"
            "• Inventory only, both commands — a trait on a player isn't for "
            "sale. /removetrait first (free, keeps the level)\n\n"
            "<b>🔄 Trading</b>\n"
            "<b>/trade @user</b> — start a trade negotiation (player ↔ player)\n"
            "• <b>Trade fee:</b> 1% of the card's buy value, charged to "
            "<b>both</b> captains when the trade completes\n"
            "Trades complete = both achievements unlock + bot logs it\n\n"
            "<b>🔒 While a match is on</b>\n"
            "Your squad is frozen from the toss until the result: no XI swaps, "
            "no batting-order changes, no captain change, no trait apply/"
            "upgrade/replace/remove, and no buying, selling or trading players. "
            "Browsing, packs and rewards still work.\n\n"
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
