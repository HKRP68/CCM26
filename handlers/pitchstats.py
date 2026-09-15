"""/pitchstats — what the surfaces actually did, in matches people played.

``tools/pitch_calibration`` answers "what does the engine produce when it plays
itself", and ``/matchhelp`` shows what each pitch is *designed* to do. Neither
answers the question a captain standing at the toss actually has: on this
surface, in real matches, how many runs is an over worth, how often does a
wicket fall, is it better to bat or chase, and which approach is earning.

Counts ``/letsplay`` and Challenge League (``/cipl``, ``/c<league>``) only,
tournament fixtures included — see ``services.pitch_stats``. Practice matches
against the AI captain are excluded.

Three views, all sharing the same mode / tournament filters:

  🏟 Overview   every surface, one block each, against its designed par band
  📍 A pitch    phase splits, the toss record, and how the two innings went
  🎯 Approaches every intent and plan rated per six balls, plus the pairings

The par-band comparison is the point of the overview. A surface whose measured
average sits outside the band it was designed for is either mis-tuned or being
played in a way the design did not expect, and until now nothing in the bot
could tell you which surfaces those were.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from engine import pitch_registry
from engine.approach_modifiers import BATTING_APPROACHES, BOWLING_APPROACHES
from services import pitch_stats
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)

CB = "pst"            # callback namespace
SEP = "|"             # not "_": pitch names and mode keys both contain none,
                      # but a separator that cannot appear in them is cheaper to
                      # reason about than one that happens not to today.

VIEW_HOME = "home"
VIEW_PITCH = "pitch"
VIEW_APPROACH = "app"

MODE_ALL = "all"
SCOPE_ALL = "all"
SCOPE_TOUR = "tour"

# One marker per pitch family, so a surface added to the registry gets an icon
# without a second table to keep in step.
_FAMILY_ICON = {"turn": "🟫", "seam": "🟩", "bounce": "🟦",
                "true": "⬜️", "road": "🟨"}

_BAT_LABEL = {k: (e, l) for k, e, l in BATTING_APPROACHES}
_BOWL_LABEL = {k: (e, l) for k, e, l in BOWLING_APPROACHES}

_PHASE_ORDER = ("all",) + pitch_stats.PHASES
_PHASE_BUTTON = {"all": "All", "powerplay": "PP", "middle": "Mid", "death": "Death"}


# ════════════════════════════════════════════════════════════════════
# Formatting helpers
# ════════════════════════════════════════════════════════════════════

def _esc(text):
    """Escape for Telegram's HTML mode.

    ``quote=False`` on purpose: nothing here is an attribute value, and the
    default would turn every apostrophe in a pitch's description into a literal
    "&#x27;" on the card.
    """
    return html.escape(str(text), quote=False)


def _icon(pitch):
    return _FAMILY_ICON.get(pitch_registry.profile(pitch).family, "🏟")


def _num(value, places=2, dash="—"):
    return dash if value is None else f"{value:.{places}f}"


def _pct(value, dash="—"):
    return dash if value is None else f"{value:.0f}%"


def _verdict_tag(verdict):
    """How the measured average sits against the designed par band."""
    return {"in": "✅ in band",
            "under": "🔻 under band",
            "over": "🔺 over band"}.get(verdict, "")


def _scope_line(mode, scope, matches, balls):
    mode_text = ("Lets Play + Challenge League" if mode == MODE_ALL
                 else pitch_stats.MODE_LABELS.get(mode, mode))
    scope_text = "tournament matches" if scope == SCOPE_TOUR else "all matches"
    overs = balls / 6.0
    return (f"<i>{_esc(mode_text)} · {scope_text} · "
            f"{matches:,} match{'es' if matches != 1 else ''}, "
            f"{overs:,.0f} overs</i>")


def _empty_text(mode, scope):
    return (
        "🏟 <b>PITCH STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Nothing recorded yet for this filter.</b>\n\n"
        "These stats are built from finished matches, and only from the two "
        "modes that play the full Approach game between two people:\n"
        "• <b>/letsplay</b> (and Lets Play Tournament fixtures)\n"
        "• <b>Challenge League</b> — /cipl and /c&lt;league&gt;, tournaments included\n\n"
        "<i>Practice matches against the AI captain are unranked and are not "
        "counted. A match that was abandoned or force-ended never counts either."
        "</i>\n\n"
        "Play a few and check back. <b>/matchhelp pitches</b> has what each "
        "surface is <i>designed</i> to do in the meantime."
    )


# ════════════════════════════════════════════════════════════════════
# Views
# ════════════════════════════════════════════════════════════════════

def _home_text(session, mode, scope):
    real_mode = None if mode == MODE_ALL else mode
    tour_only = scope == SCOPE_TOUR
    rows = pitch_stats.overview(session, real_mode, tour_only)
    if not rows:
        return _empty_text(mode, scope)

    matches, balls = pitch_stats.totals(session, real_mode, tour_only)
    out = ["🏟 <b>PITCH STATS</b> — what the surfaces actually did",
           _scope_line(mode, scope, matches, balls),
           "━━━━━━━━━━━━━━━━━━━"]
    for row in rows:
        pitch = row["pitch"]
        band = row["par_band"]
        band_text = f" · par {band[0]}-{band[1]}" if band else ""
        avg = row["avg_first"]
        avg_text = "—" if avg is None else f"{avg:.0f}"
        out.append(
            f"\n{_icon(pitch)} <b>{_esc(pitch)}</b> · "
            f"{row['matches']} match{'es' if row['matches'] != 1 else ''}"
            f"\n   <b>{_num(row['rpo'])}</b> runs/over · "
            f"<b>{_num(row['wpo'])}</b> wkts/over"
            f"\n   1st inns avg <b>{avg_text}</b>{band_text} "
            f"{_verdict_tag(row['par_verdict'])}"
            f"\n   Bat 1st <b>{_pct(row['bat_first_win_pct'])}</b> · "
            f"Bat 2nd <b>{_pct(row['bat_second_win_pct'])}</b>"
            + (f" <i>({row['ties']} tie{'s' if row['ties'] != 1 else ''})</i>"
               if row["ties"] else ""))
    out.append(
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "<i>Every rate is per six balls, so a chase won in 17.2 counts for what "
        "it was. Tap a surface for phase splits, the toss record and its "
        "approach table.</i>")
    return "\n".join(out)


def _pitch_text(session, pitch, mode, scope):
    real_mode = None if mode == MODE_ALL else mode
    tour_only = scope == SCOPE_TOUR
    detail = pitch_stats.pitch_detail(session, pitch, real_mode, tour_only)
    profile = pitch_registry.profile(pitch)
    if not detail:
        return (f"{_icon(pitch)} <b>{_esc(profile.name)}</b>\n"
                f"<i>{_esc(profile.blurb)}</i>\n"
                "━━━━━━━━━━━━━━━━━━━\n\n"
                "No matches recorded on this surface for the current filter yet.")

    band = detail["par_band"]
    band_text = f"{band[0]}-{band[1]}" if band else "—"
    avg = detail["avg_first"]
    out = [
        f"{_icon(pitch)} <b>{_esc(profile.name)}</b> — "
        f"<i>{_esc(profile.blurb)}</i>",
        f"<i>Designed par {band_text} · favours {profile.favours.lower()}</i>",
        "━━━━━━━━━━━━━━━━━━━",
        f"\n📊 <b>{detail['matches']}</b> matches · "
        f"<b>{detail['innings']}</b> innings · "
        f"{detail['balls'] / 6.0:,.0f} overs",
        f"Runs/over <b>{_num(detail['rpo'])}</b> · "
        f"Wickets/over <b>{_num(detail['wpo'])}</b>",
    ]
    if avg is not None:
        out.append(f"1st innings avg <b>{avg:.0f}</b> · "
                   f"high {detail['high_first']} · low {detail['low_first']}  "
                   f"{_verdict_tag(detail['par_verdict'])}")

    out.append("\n⏱ <b>By phase</b>")
    for phase in pitch_stats.PHASES:
        row = detail["phases"][phase]
        label = pitch_stats.PHASE_LABELS[phase]
        if not row["balls"]:
            out.append(f"{label:<10} <i>no data</i>")
            continue
        out.append(f"{label:<10} <b>{_num(row['rpo'])}</b> rpo · "
                   f"<b>{_num(row['wpo'])}</b> wkts/over")

    toss = detail["toss"]
    out.append("\n🪙 <b>Toss</b>")
    out.append(f"The book says <b>{toss['book_call'].upper()}</b> first here.")
    if toss["recorded"]:
        out.append(f"Winners chose bat {toss['chose_bat']} · "
                   f"bowl {toss['chose_bowl']}")
        if toss["followed"] or toss["against"]:
            out.append(
                f"Followed it: {toss['followed']} "
                f"(won {_pct(toss['followed_win_pct'])}) · "
                f"went against: {toss['against']} "
                f"(won {_pct(toss['against_win_pct'])})")
        out.append(f"Toss winner won the match {_pct(toss['toss_winner_win_pct'])}"
                   " of the time")
    else:
        out.append("<i>No toss decisions recorded yet.</i>")

    out.append("\n🏏 <b>Result</b>")
    decided = detail["decided"]
    if decided:
        first_wins = round((detail["bat_first_win_pct"] or 0) / 100 * decided)
        out.append(f"Batting first  <b>{_pct(detail['bat_first_win_pct'])}</b> "
                   f"({first_wins}/{decided})")
        out.append(f"Batting second <b>{_pct(detail['bat_second_win_pct'])}</b> "
                   f"({decided - first_wins}/{decided})")
    else:
        out.append("<i>No decided matches yet.</i>")
    if detail["ties"]:
        out.append(f"<i>{detail['ties']} tie"
                   f"{'s' if detail['ties'] != 1 else ''} "
                   "(decided by Super Over or countback) excluded from the split.</i>")

    out.append(f"\n<i>{_esc(profile.character)}</i>")
    return "\n".join(out)


def _approach_text(session, pitch, mode, phase):
    real_mode = None if mode == MODE_ALL else mode
    real_phase = None if phase == "all" else phase
    batting, bowling, pairs = pitch_stats.approach_table(
        session, pitch=pitch, mode=real_mode, phase=real_phase)

    where = _esc(pitch) if pitch else "every surface"
    when = "all phases" if phase == "all" else pitch_stats.PHASE_LABELS[phase]
    head = [f"🎯 <b>APPROACHES</b> — {where}",
            f"<i>{when} · rated per six balls</i>",
            "━━━━━━━━━━━━━━━━━━━"]
    if not batting:
        head.append("\nNo overs recorded for this filter yet.")
        return "\n".join(head)

    total_overs = sum(r["overs"] for r in batting)
    head.append(f"<i>{total_overs:,} overs of real match data</i>")

    head.append("\n🏏 <b>Batting intent</b>  <i>(runs/over · wkts/over · overs)</i>")
    for row in batting:
        emoji, label = _BAT_LABEL.get(row["approach"], ("•", row["approach"]))
        head.append(f"{emoji} <b>{_esc(label)}</b> — "
                    f"{_num(row['rpo'])} · {_num(row['wpo'])} · {row['overs']}")

    head.append("\n🎳 <b>Bowling plan</b>  <i>(economy · wkts/over · overs)</i>")
    for row in bowling:
        emoji, label = _BOWL_LABEL.get(row["approach"], ("•", row["approach"]))
        head.append(f"{emoji} <b>{_esc(label)}</b> — "
                    f"{_num(row['rpo'])} · {_num(row['wpo'])} · {row['overs']}")

    # Pairings are the only view that can say a pick is good *against a
    # specific reply*, which is the whole point of a simultaneous-move game.
    judged = [p for p in pairs if p["overs"] >= MIN_PAIR_OVERS]
    if judged:
        head.append(f"\n⚔️ <b>Pairings</b> <i>({MIN_PAIR_OVERS}+ overs)</i>")
        # Both picks make the cell, so these are named for the over they
        # produced rather than credited to one captain: an intent of Defensive
        # is the cheapest over on the board no matter what was bowled at it.
        head.append("Most productive over: " + _pair_line(judged[0]))
        head.append("Cheapest over: "
                    + _pair_line(min(judged, key=lambda p: p["rpo"])))
        head.append("Most wickets: "
                    + _pair_line(max(judged, key=lambda p: p["wpo"] or 0)))
    else:
        head.append(f"\n<i>No pairing has reached {MIN_PAIR_OVERS} overs yet — "
                    "the head-to-head grid needs a bigger sample to mean "
                    "anything.</i>")
    head.append("\n<i>This is what players actually did, not what the engine "
                "says they should. /matchhelp duel explains the picks.</i>")
    return "\n".join(head)


# A pairing is 1 of 25 cells; below this many overs the run rate is noise, and
# printing "Ultra vs Variation: 24.0 rpo" off two overs is worse than silence.
MIN_PAIR_OVERS = 8


def _pair_line(pair):
    bat, bowl = pair["pair"]
    b_emoji, b_label = _BAT_LABEL.get(bat, ("•", bat))
    w_emoji, w_label = _BOWL_LABEL.get(bowl, ("•", bowl))
    return (f"{b_emoji} {_esc(b_label)} vs {w_emoji} {_esc(w_label)}"
            f" — <b>{_num(pair['rpo'])}</b> rpo · {_num(pair['wpo'])} wkts/over "
            f"<i>({pair['overs']} ov)</i>")


# ════════════════════════════════════════════════════════════════════
# Keyboard
# ════════════════════════════════════════════════════════════════════

def _cb(view, owner, mode, scope, arg=""):
    return SEP.join((CB, view, str(owner), mode, scope, arg))


def _keyboard(session, view, owner, mode, scope, arg=""):
    rows = []

    # Surfaces that have data, so the keyboard never offers an empty page.
    played = pitch_stats.played_pitches(
        session, None if mode == MODE_ALL else mode, scope == SCOPE_TOUR)
    row = []
    for pitch in played:
        mark = "•" if view == VIEW_PITCH and arg == pitch else ""
        # Named, not icon-only: two pairs of surfaces share a family icon
        # (Dusty/Dry are both turners, Even/Hard both true tracks), and a row of
        # seven squares where two pairs look identical is a guessing game.
        row.append(InlineKeyboardButton(
            f"{_icon(pitch)} {pitch}{mark}",
            callback_data=_cb(VIEW_PITCH, owner, mode, scope, pitch)))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Approach view: a phase filter, carrying whichever pitch is in view.
    if view == VIEW_APPROACH:
        phase_row = []
        for phase in _PHASE_ORDER:
            mark = "•" if arg.endswith(f":{phase}") else ""
            pitch = arg.split(":", 1)[0]
            phase_row.append(InlineKeyboardButton(
                f"{_PHASE_BUTTON[phase]}{mark}",
                callback_data=_cb(VIEW_APPROACH, owner, mode, scope,
                                  f"{pitch}:{phase}")))
        rows.append(phase_row)

    nav = [
        InlineKeyboardButton("🏟 Overview" + ("•" if view == VIEW_HOME else ""),
                             callback_data=_cb(VIEW_HOME, owner, mode, scope)),
        InlineKeyboardButton("🎯 Approaches" + ("•" if view == VIEW_APPROACH else ""),
                             callback_data=_cb(
                                 VIEW_APPROACH, owner, mode, scope,
                                 f"{arg if view == VIEW_PITCH else ''}:all")),
    ]
    rows.append(nav)

    mode_row = []
    for key, label in ((MODE_ALL, "Both"),
                       (pitch_stats.MODE_LETSPLAY, "Lets Play"),
                       (pitch_stats.MODE_CHALLENGE, "League")):
        mark = "•" if mode == key else ""
        mode_row.append(InlineKeyboardButton(
            f"{label}{mark}", callback_data=_cb(view, owner, key, scope, arg)))
    rows.append(mode_row)

    other_scope = SCOPE_ALL if scope == SCOPE_TOUR else SCOPE_TOUR
    rows.append([
        InlineKeyboardButton(
            "🏆 Tournaments only" if other_scope == SCOPE_TOUR else "📋 All matches",
            callback_data=_cb(view, owner, mode, other_scope, arg)),
        InlineKeyboardButton("❌ Close", callback_data=_cb("close", owner, mode,
                                                          scope)),
    ])
    return InlineKeyboardMarkup(rows)


def _render(session, view, owner, mode, scope, arg=""):
    """``(text, keyboard)`` for one view."""
    if view == VIEW_PITCH and arg:
        text = _pitch_text(session, arg, mode, scope)
    elif view == VIEW_APPROACH:
        pitch, _, phase = arg.partition(":")
        text = _approach_text(session, pitch or None, mode, phase or "all")
    else:
        view = VIEW_HOME
        text = _home_text(session, mode, scope)
    return text, _keyboard(session, view, owner, mode, scope, arg)


# ════════════════════════════════════════════════════════════════════
# Handlers
# ════════════════════════════════════════════════════════════════════

_ARG_ALIASES = {"approach": VIEW_APPROACH, "approaches": VIEW_APPROACH,
                "duel": VIEW_APPROACH, "app": VIEW_APPROACH}


async def pitchstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pitchstats [pitch|approaches] [lp|cl] — real pitch numbers."""
    tg = update.effective_user
    view, arg, mode = VIEW_HOME, "", MODE_ALL
    for raw in (context.args or []):
        token = str(raw).strip().lower()
        if token in ("lp", "letsplay"):
            mode = pitch_stats.MODE_LETSPLAY
        elif token in ("cl", "cipl", "league", "challenge"):
            mode = pitch_stats.MODE_CHALLENGE
        elif token in _ARG_ALIASES:
            view, arg = VIEW_APPROACH, ":all"
        elif pitch_registry.is_known(token.title()):
            view, arg = VIEW_PITCH, token.title()

    session = get_session()
    try:
        text, keyboard = _render(session, view, tg.id, mode, SCOPE_ALL, arg)
    except Exception:
        logger.exception("/pitchstats failed")
        await update.message.reply_text("⚠️ Could not load pitch stats.")
        return
    finally:
        session.close()

    sent = await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True)
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                delay_seconds=600)
    except Exception:
        pass


async def pitchstats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``pst|<view>|<owner>|<mode>|<scope>|<arg>``"""
    q = update.callback_query
    try:
        _, view, owner, mode, scope, arg = q.data.split(SEP, 5)
        owner_tg = int(owner)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != owner_tg:
        await q.answer("Open your own with /pitchstats", show_alert=True)
        return

    if view == "close":
        await q.answer("Closed")
        try:
            await q.edit_message_text(
                "🏟 <i>Pitch stats closed. /pitchstats anytime.</i>",
                parse_mode="HTML")
        except Exception:
            pass
        return

    await q.answer()
    session = get_session()
    try:
        text, keyboard = _render(session, view, owner_tg, mode, scope, arg)
    except Exception:
        logger.exception("/pitchstats callback failed (%s)", q.data)
        await q.answer("⚠️ Could not load that view.", show_alert=True)
        return
    finally:
        session.close()
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard,
                                  disable_web_page_preview=True)
    except Exception:
        # Telegram rejects an edit that changes nothing — tapping the view you
        # are already on is not an error worth logging.
        pass
