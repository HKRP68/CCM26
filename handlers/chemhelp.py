"""/chemhelp — the Team Chemistry guide, as a tabbed in-chat rulebook.

Five sections, selectable via buttons, mirroring ``/howto``:

  🧪 Overview  — what chemistry is and where the 100 comes from
  🎯 Category  — the 20-per-role cohesion score
  🌍 Bonus     — Country Diversity + Card Variety
  📊 Boosts    — what chemistry does in a match
  💡 Improve   — how to raise your score, against your own XI

Every number below is rendered from ``services.chemistry`` rather than typed
out, so the guide can never drift away from the maths it explains. The Improve
tab reads the player's own Playing XI and ranks their best available moves.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services import chemistry
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Helpers — every table is built from the live constants
# ════════════════════════════════════════════════════════════════════

def _tier_lines(tiers, maximum, plural, singular):
    """Render a tier table like '4+ countries = 10 pts'."""
    out = []
    for index, (threshold, points) in enumerate(tiers):
        label = f"{threshold}+" if index == 0 else f"{threshold}"
        colour = chemistry.chem_colour(points, maximum)
        out.append(f"{colour} <b>{label}</b> {plural} = <b>{points}</b> pts")
    out.append(f"{chemistry.CHEM_COLOURS[-1][1]} <b>1</b> {singular} = "
               f"<b>0</b> pts")
    return "\n".join(out)


def _role_boost_lines():
    grouped = {}
    for role in chemistry.ROLE_ORDER:
        grouped.setdefault(chemistry.ROLE_MAX_BONUS[role], []).append(
            chemistry.ROLE_LABEL[role])
    return "\n".join(
        f"• Max <b>+{bonus}</b> for {'/'.join(labels)}"
        for bonus, labels in sorted(grouped.items(), reverse=True))


def _colour_key():
    ranges, previous = [], chemistry.ROLE_COMPONENT_MAX
    for threshold, colour in chemistry.CHEM_COLOURS:
        top = previous
        ranges.append(f"{colour} {threshold}-{top}")
        previous = threshold - 1
    return "   ".join(ranges)


def _special_type_list():
    pretty = {"icon": "Icon", "toty": "TOTY", "prime": "Prime",
              "legend": "Legends", "star": "Stars", "ipl": "IPL",
              "wpl": "WPL", "bbl": "BBL", "psl": "PSL", "sa20": "SA20",
              "cpl": "CPL", "event": "World Cup / other drops"}
    return ", ".join(pretty.get(t, t.title())
                     for t in chemistry.SPECIAL_TYPES)


# ════════════════════════════════════════════════════════════════════
# Static sections
# ════════════════════════════════════════════════════════════════════

SECTIONS = {
    "overview": {
        "emoji": "🧪",
        "title": "Overview — The Chemistry System",
        "body": lambda: (
            "<b>🧪 Chemistry System</b>\n\n"
            "Chemistry measures how well your XI <i>fits together</i> — not how "
            "good the cards are. Squad building is strategic: two sides with "
            "identical ratings can play very differently.\n\n"
            f"✨ <b>Max Chemistry: "
            f"{chemistry.CMUCHEM_TOTAL_MAX}/{chemistry.CMUCHEM_TOTAL_MAX}</b>\n\n"
            "<b>Where it comes from</b>\n"
            f"🎯 <b>Category Chemistry</b> — "
            f"{chemistry.CATEGORY_CHEMISTRY_MAX} "
            f"({len(chemistry.ROLE_ORDER)} roles × "
            f"{chemistry.ROLE_COMPONENT_MAX})\n"
            f"🌍 <b>Playing XI Bonus</b> — {chemistry.XI_BONUS_MAX} "
            f"(Diversity {chemistry.DIVERSITY_MAX} + Variety "
            f"{chemistry.VARIETY_MAX})\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Total — {chemistry.CMUCHEM_TOTAL_MAX}</b>\n\n"
            "<b>🎨 Colour key</b>\n"
            f"{_colour_key()}\n\n"
            "<b>📏 The one hard rule</b>\n"
            f"Max <b>{chemistry.MAX_PLAYERS_PER_COUNTRY} players</b> from any "
            "one country in your XI.\n\n"
            "<b>Commands</b>\n"
            "<b>/cmuchem</b> or <b>/cmuchemistry</b> — your detailed breakdown\n"
            "<b>/pxi</b> — your XI with its chemistry total"
        ),
    },
    "category": {
        "emoji": "🎯",
        "title": "Category — Chemistry Per Role",
        "body": lambda: (
            "<b>🎯 Category Chemistry</b>\n\n"
            f"Each of your {len(chemistry.ROLE_ORDER)} roles — "
            f"{', '.join(chemistry.ROLE_LABEL[r] for r in chemistry.ROLE_ORDER)}"
            f" — is scored out of <b>{chemistry.ROLE_COMPONENT_MAX}</b>.\n\n"
            f"• Start at <b>{chemistry.ROLE_COMPONENT_MAX} points</b> per role\n"
            "• <b>Lose points</b> for each player <b>NOT</b> from that role's "
            "majority country\n"
            "• All different countries in a role = <b>0 chemistry</b> "
            f"{chemistry.CHEM_COLOURS[-1][1]}\n\n"
            "<b>Example — 4 batsmen</b>\n"
            "<code>4 same country   20/20  🟩\n"
            "3 + 1 outsider   13/20  🟨\n"
            "2 + 2 outsiders   7/20  🟧\n"
            "all 4 different   0/20  🟥</code>\n\n"
            "<b>💡 What this means</b>\n"
            "Chemistry rewards <b>unified units</b> — an all-Australian pace "
            "battery, an all-Indian top order. You do not need one country "
            "across the whole XI; you need each <i>role</i> to agree.\n\n"
            "<i>A role with only one player is automatically "
            f"{chemistry.ROLE_COMPONENT_MAX}/{chemistry.ROLE_COMPONENT_MAX}.</i>"
        ),
    },
    "bonus": {
        "emoji": "🌍",
        "title": "Bonus — Diversity & Variety",
        "body": lambda: (
            f"<b>🌍 Playing XI Bonus (0-{chemistry.XI_BONUS_MAX})</b>\n\n"
            "Two halves, both rewarding <b>spread</b> across your whole XI. "
            "Where Category Chemistry wants each role unified, this wants your "
            "squad varied — the best sides are a few unified units drawn from "
            "different places.\n\n"
            f"<b>🌍 Country Diversity (0-{chemistry.DIVERSITY_MAX})</b>\n"
            f"{_tier_lines(chemistry.DIVERSITY_TIERS, chemistry.DIVERSITY_MAX, 'countries', 'country')}\n\n"
            f"<b>🃏 Card Variety (0-{chemistry.VARIETY_MAX})</b>\n"
            f"{_tier_lines(chemistry.VARIETY_TIERS, chemistry.VARIETY_MAX, 'special types', 'special type')}\n\n"
            "<b>Special types</b>\n"
            f"<i>{_special_type_list()}</i>\n\n"
            "Each counts as a <b>different</b> type — an IPL card and a BBL "
            "card are two, not one. Base cards don't count here.\n\n"
            "<i>Four of each half = the full "
            f"{chemistry.XI_BONUS_MAX}.</i>"
        ),
    },
    "boosts": {
        "emoji": "📊",
        "title": "Boosts — Chemistry In Matches",
        "body": lambda: (
            "<b>📊 Stat Boosts (In Matches)</b>\n\n"
            "Each role turns its chemistry into a stat boost for that unit "
            "during matches:\n\n"
            f"{_role_boost_lines()}\n\n"
            "<code>(category + XI bonus) ÷ ceiling × max</code>\n\n"
            "So both halves matter: a unified unit in a varied squad boosts "
            "hardest.\n\n"
            f"<b>🔥 Clutch — ×{chemistry.CLUTCH_MULTIPLIER:g} when it counts</b>\n"
            "In the <b>last quarter of the innings</b>, and in any tight "
            f"chase, your boosts are <b>doubled</b>. A well-drilled side holds "
            "its nerve when the game is decided.\n\n"
            "<b>🤝 Partnership Bond</b>\n"
            "When the two batters at the crease are <b>countrymen</b>, they "
            "run better together — more twos, fewer dots. This moves "
            "<i>during</i> the innings as wickets fall.\n"
            f"🌟 An <b>Icon</b> part-bonds with <b>anyone</b> — a legend has "
            "partnered with everybody.\n\n"
            f"<b>🧤 Fielding Cohesion — up to +{chemistry.FIELDING_MAX_BONUS:g}</b>\n"
            "A side that plays together <b>drops fewer catches</b>.\n\n"
            "<b>🟧 Why ALR is halved</b>\n"
            "All-rounders already benefit from the batting <b>and</b> bowling "
            "boosts, so counting their category fully would pay them twice for "
            "the same cohesion.\n\n"
            "<b>⚡ Still a tie-breaker</b>\n"
            "All of it decides close matches between similar squads — it never "
            "replaces good cards, and it is <b>always a bonus</b>. Low "
            "chemistry never drops you below your raw ratings.\n\n"
            "<i>See your live boosts with /cmuchem.</i>"
        ),
    },
}

SECTION_ORDER = ("overview", "category", "bonus", "boosts", "improve")

_IMPROVE = {"emoji": "💡", "title": "Improve — Your Next Moves"}


# ════════════════════════════════════════════════════════════════════
# The Improve tab — reads the player's own XI
# ════════════════════════════════════════════════════════════════════

def _generic_improve_body():
    return (
        "<b>💡 How To Improve</b>\n\n"
        "<b>1️⃣ Unify your units</b>\n"
        "The biggest single win. Getting all four bowlers from one country "
        f"takes that role from 0 to {chemistry.ROLE_COMPONENT_MAX}. Swapping "
        "<i>one</i> outsider is usually worth more than any other move.\n\n"
        "<b>2️⃣ Spread across countries</b>\n"
        f"{chemistry.DIVERSITY_TARGET_COUNTRIES}+ countries in the XI earns "
        f"the full {chemistry.DIVERSITY_MAX}. This works <i>with</i> step 1 — "
        "four unified roles from four different countries scores both.\n\n"
        "<b>3️⃣ Collect different card types</b>\n"
        f"{chemistry.VARIETY_TARGET_TYPES}+ special types earns the full "
        f"{chemistry.VARIETY_MAX}. A cheap card of a type you're "
        "<i>missing</i> is worth more than another of one you already hold.\n\n"
        "<b>4️⃣ Mind your keeper and all-rounders</b>\n"
        "Small roles are cheap to fix — a single-player role scores full "
        "marks automatically, and a 2-player role only needs those two to "
        "match.\n\n"
        "<i>Build your XI, then run /cmuchem — it ranks your best available "
        "moves against your actual squad.</i>"
    )


def _personal_improve_body(telegram_id):
    """Improve tab against the player's real XI, or the generic guide."""
    session = None
    try:
        session = get_session()
        user = session.query(User).filter(
            User.telegram_id == telegram_id).first()
        if not user:
            return _generic_improve_body()

        from handlers.lineup import _get_ordered_roster
        roster = _get_ordered_roster(session, user.id)
        players = [player for _entry, player in roster[:11]]
        if len(players) < chemistry.XI_SIZE:
            return _generic_improve_body()

        report = chemistry.calculate_role_report(players)
        tips = chemistry.improvement_tips(players, limit=4)
        colour = chemistry.chem_colour(report["total"],
                                       chemistry.CMUCHEM_TOTAL_MAX)

        lines = [
            "<b>💡 How To Improve</b>\n",
            f"{colour} Your chemistry: <b>{report['total']}/"
            f"{report['total_max']}</b>\n",
        ]
        if not tips:
            lines.append("🏆 <b>Perfect score</b> — every role is unified, and "
                         "your diversity and variety are both maxed. Nothing "
                         "left to fix.")
        else:
            lines.append("<b>Your best moves, biggest first:</b>\n")
            lines.extend(f"• {text}" for _points, text in tips)
            lines.append("\n<i>Swapping one player in a broken role is usually "
                         "the cheapest big win.</i>")
        return "\n".join(lines)
    except Exception:
        logger.exception("chemhelp: personal improve tab failed")
        return _generic_improve_body()
    finally:
        if session is not None:
            session.close()


def _build_keyboard(active_section, owner_tg):
    """Build the section-tab keyboard."""
    rows = [[]]
    for key in SECTION_ORDER:
        data = SECTIONS.get(key, _IMPROVE)
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


def _render(section, telegram_id):
    data = SECTIONS.get(section, _IMPROVE)
    body = (_personal_improve_body(telegram_id) if section == "improve"
            else data["body"]())
    return ("🧪 <b>CHEMISTRY GUIDE</b>\n\n"
            f"Section: {data['emoji']} <b>{data['title']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            + body)


# ════════════════════════════════════════════════════════════════════
# /chemhelp
# ════════════════════════════════════════════════════════════════════

async def chemhelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    logger.info(f"/chemhelp from user {tg.id}")

    sent = await update.message.reply_text(
        _render("overview", tg.id), parse_mode="HTML",
        reply_markup=_build_keyboard("overview", tg.id),
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
    if section not in SECTION_ORDER:
        await q.answer("Unknown")
        return

    await q.answer()
    try:
        await q.edit_message_text(
            _render(section, tg.id), parse_mode="HTML",
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
