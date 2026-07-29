"""/chemhelp — the Team Chemistry rulebook, as a tabbed in-chat guide.

Five sections, selectable via buttons, mirroring ``/howto``:

  🧪 Basics   — what chemistry is and what it does in a match
  🌍 Country  — the national block table and squad shapes
  ⭐ Icons    — why legends from smaller nations still work
  🃏 Cards    — special editions and the variety-over-quantity rule
  📊 Card     — how to read your own /cmuchem breakdown

Every number below is rendered from ``services.chemistry`` rather than typed
out, so the help can never drift away from the maths it explains.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services import chemistry
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Section content — built from the live constants
# ════════════════════════════════════════════════════════════════════

def _block_table():
    """The country block curve as an aligned three-row table.

    Every cell is right-aligned in a fixed width so the ``+10``/``+8`` gain row
    stays in step with the columns above it in Telegram's monospace font.
    """
    sizes = list(range(1, chemistry.MAX_PLAYERS_PER_COUNTRY + 1))
    value = chemistry.COUNTRY_BLOCK_VALUE

    def row(label, cells):
        return f"{label:<8}" + " ".join(f"{c:>3}" for c in cells)

    gains = [""] + [f"{value[n] - value[n - 1]:+d}" for n in sizes[1:]]
    return ("<code>"
            + row("players", sizes) + "\n"
            + row("chem", [value[n] for n in sizes]) + "\n"
            + row("gain", gains)
            + "</code>")


def _shape_examples():
    """A few squad shapes scored by the real calculator."""
    class _Card:
        def __init__(self, country):
            self.country = country
            self.version = "Base"

    rows = []
    for shape in ((7, 4), (6, 5), (5, 4, 2), (4, 4, 3), (7, 3, 1),
                  (7, 1, 1, 1, 1)):
        squad = [_Card(f"C{i}") for i, n in enumerate(shape) for _ in range(n)]
        total, _blocks = chemistry.country_chemistry(squad)
        label = "-".join(str(n) for n in shape)
        rows.append(f"<code>{label:<11}{total:>3}</code>")
    return "\n".join(rows)


def _role_split():
    order = ("Batsman", "Bowler", "Wicket Keeper", "All-rounder")
    return "  ".join(
        f"{chemistry.ROLE_LABEL[r]} {chemistry.ROLE_MAX_BONUS[r]}"
        for r in order)


SECTIONS = {
    "basics": {
        "emoji": "🧪",
        "title": "Basics — What Chemistry Is",
        "body": lambda: (
            "<b>🧪 Team Chemistry</b>\n\n"
            "Chemistry measures how well your XI <i>fits together</i> — not how "
            "good the cards are. Two squads with identical ratings can have very "
            "different chemistry.\n\n"
            f"Your score runs from <b>0 to {chemistry.CMUCHEM_TOTAL_MAX}</b>, and "
            "comes from four things:\n\n"
            f"🏏 <b>Roles</b> — up to {sum(chemistry.ROLE_MAX_BONUS.values())} "
            f"({_role_split()})\n"
            f"🌍 <b>Country Diversity</b> — up to {chemistry.DIVERSITY_MAX}\n"
            f"🃏 <b>Card Variety</b> — up to {chemistry.VARIETY_MAX}\n"
            f"⭐ <b>Playing XI Bonus</b> — up to {chemistry.SPECIAL_CHEMISTRY_CAP}\n\n"
            "<b>⚡ What it does</b>\n"
            f"High chemistry gives your side up to a <b>"
            f"+{chemistry.CHEMISTRY_MAX_BONUS_PCT}%</b> lift in matches. It is a "
            "tie-breaker between close squads, never a replacement for good "
            "cards — and it is <i>always a bonus</i>. Low chemistry never "
            "penalises you below your raw ratings.\n\n"
            "<b>📏 The one hard rule</b>\n"
            f"Max <b>{chemistry.MAX_PLAYERS_PER_COUNTRY} players</b> from any one "
            "country in your XI.\n\n"
            "<b>Commands</b>\n"
            "<b>/cmuchem</b> — your full chemistry breakdown\n"
            "<b>/pxi</b> — your XI, with its chemistry total"
        ),
    },
    "country": {
        "emoji": "🌍",
        "title": "Country — National Blocks",
        "body": lambda: (
            "<b>🌍 Country Chemistry</b>\n\n"
            "Players from the same country form a <b>block</b>. Bigger blocks are "
            "worth more — but not forever:\n\n"
            + _block_table() + "\n\n"
            "<b>🎯 Four is the sweet spot.</b> The 4th countryman is worth the "
            "most (+12). After that the gain drops away — a 6th or 7th countryman "
            "(+6, +4) is worth <i>less</i> than starting a fresh country (+8).\n\n"
            "One country tops out at "
            f"{chemistry.COUNTRY_BLOCK_VALUE[chemistry.MAX_PLAYERS_PER_COUNTRY]}, "
            "so no single nation can carry your XI. You always need at least two "
            "real blocks.\n\n"
            "<b>📐 Some shapes</b>\n"
            "<code>shape      chem</code>\n"
            + _shape_examples() + "\n\n"
            "Notice <b>4-4-3 beats 7-3-1</b>. Spreading across three countries is "
            "better than stacking one and hanging singles off it — a lone player "
            "from a country contributes <b>nothing</b>.\n\n"
            "<i>Balanced squads are competitive here, not punished.</i>"
        ),
    },
    "icons": {
        "emoji": "⭐",
        "title": "Icons — Legends Still Work",
        "body": lambda: (
            "<b>⭐ The Icon Rule</b>\n\n"
            "A lone player from a small cricket nation would normally be worth "
            "zero chemistry — which would make legends like Brian Lara, Viv "
            "Richards, Richard Hadlee or Mahela Jayawardene unplayable.\n\n"
            "So:\n\n"
            "<b>🌟 An Icon counts as TWO players of its country</b> when sizing "
            "its block.\n\n"
            "<code>Lara alone            0 → 8\n"
            "Lara + 1 countryman   8 → 18\n"
            "Lara + 2 countrymen  18 → 30</code>\n\n"
            "A three-man core built around an Icon is worth as much as a "
            "four-man core of anyone else.\n\n"
            "<b>⚖️ It can't be abused</b>\n"
            "An Icon still counts as only <b>one</b> player against the "
            f"{chemistry.MAX_PLAYERS_PER_COUNTRY}-per-country limit, and a block "
            "is never scored above "
            f"{chemistry.MAX_PLAYERS_PER_COUNTRY}. Dropping an Icon into a "
            "country you already have 6 or 7 of adds <b>nothing</b>.\n\n"
            "<i>Icons rescue your smallest block — they don't inflate your "
            "biggest one.</i>"
        ),
    },
    "cards": {
        "emoji": "🃏",
        "title": "Cards — Special Editions",
        "body": lambda: (
            "<b>🃏 Card Variety</b>\n\n"
            "Special editions feed two parts of your score. There are "
            f"<b>{len(chemistry.SPECIAL_TYPES)}</b> types:\n\n"
            "🌟 <b>Icon</b>\n⭐ <b>TOTY</b>\n🔥 <b>Prime</b>\n👑 <b>Legend</b>\n"
            "🎉 <b>Event</b> <i>(World Cup, IPL, Star, Gold…)</i>\n\n"
            "<b>⭐ Playing XI Bonus</b> "
            f"(up to {chemistry.SPECIAL_CHEMISTRY_CAP})\n"
            f"<code>{chemistry.SPECIAL_TYPE_POINTS} per different type "
            f"(max {chemistry.SPECIAL_TYPE_MAX_TYPES})\n"
            f"{chemistry.SPECIAL_DEPTH_POINTS} per special card "
            f"(max {chemistry.SPECIAL_DEPTH_MAX_CARDS})</code>\n\n"
            "<b>🎯 Variety beats quantity</b>\n"
            "<code>11 Icons                     8\n"
            "1 each of the 5 types       20</code>\n\n"
            "Stacking the rarest cards is <b>not</b> the way to a high score. A "
            "cheap card of a type you're missing is worth more than another copy "
            "of a type you already have.\n\n"
            "<b>🃏 Card Variety</b> "
            f"(up to {chemistry.VARIETY_MAX})\n"
            f"Full marks at <b>{chemistry.VARIETY_TARGET_TYPES} different "
            "types</b> in your XI.\n\n"
            "<i>Base cards are fine — they just don't add here.</i>"
        ),
    },
    "reading": {
        "emoji": "📊",
        "title": "Card — Reading /cmuchem",
        "body": lambda: (
            "<b>📊 Reading Your Chemistry Card</b>\n\n"
            "<code>🟦 BOWL: 20/20 + 20/20 = +12/12</code>\n\n"
            "Each role line has two halves:\n\n"
            f"<b>Left</b> — how well that role's players sit inside your XI's "
            "national blocks. Full marks once their country has a "
            "<b>3-man core</b>.\n"
            "<b>Right</b> — your Playing XI Bonus, shared by every role, because "
            "a varied card pool lifts the whole side.\n"
            "<b>Result</b> — what that role adds to your total.\n\n"
            "<b>🟧 Why ALR is halved</b>\n"
            "All-rounders already benefit from both the batting and bowling "
            "side of things, so counting them fully would count the same "
            "connection twice.\n\n"
            "<b>📈 Below the roles</b>\n"
            f"<b>Country Diversity</b> — full at "
            f"{chemistry.DIVERSITY_TARGET_COUNTRIES} countries\n"
            f"<b>Card Variety</b> — full at "
            f"{chemistry.VARIETY_TARGET_TYPES} special types\n"
            "<b>Playing XI Bonus</b> — your special-card score\n"
            "<b>Overall</b> — these simply added up\n\n"
            "<b>💯 A perfect score</b>\n"
            "<code>3-3-3-2 across four countries,\n"
            "the pair sized up by an Icon,\n"
            "one card of each of the 5 types</code>\n\n"
            "<i>Type /cmuchem to see yours.</i>"
        ),
    },
}

SECTION_ORDER = ("basics", "country", "icons", "cards", "reading")


def _build_keyboard(active_section, owner_tg):
    """Build the section-tab keyboard."""
    rows = [[]]
    for key in SECTION_ORDER:
        data = SECTIONS[key]
        label = f"{data['emoji']} {key.title()}"
        if key == active_section:
            label = "• " + label
        if len(rows[-1]) >= 3:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(
            label, callback_data=f"chemhelp_tab_{owner_tg}_{key}"))
    rows.append([InlineKeyboardButton(
        "❌ Close", callback_data=f"chemhelp_close_{owner_tg}")])
    return InlineKeyboardMarkup(rows)


def _render(section):
    data = SECTIONS[section]
    return ("🧪 <b>CHEMISTRY GUIDE</b>\n\n"
            f"Section: {data['emoji']} <b>{data['title']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            + data["body"]())


# ════════════════════════════════════════════════════════════════════
# /chemhelp
# ════════════════════════════════════════════════════════════════════

async def chemhelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    logger.info(f"/chemhelp from user {tg.id}")

    sent = await update.message.reply_text(
        _render("basics"), parse_mode="HTML",
        reply_markup=_build_keyboard("basics", tg.id),
        disable_web_page_preview=True)
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                delay_seconds=300)
    except Exception:
        pass


async def chemhelp_tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """chemhelp_tab_<owner_tg>_<section>"""
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
    try:
        await q.edit_message_text(
            _render(section), parse_mode="HTML",
            reply_markup=_build_keyboard(section, tg.id),
            disable_web_page_preview=True)
    except Exception:
        pass


async def chemhelp_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "🧪 <i>Chemistry guide closed. Type /chemhelp anytime.</i>",
            parse_mode="HTML")
    except Exception:
        pass
