"""Super Over for tied Challenge League (/cipl, /c[league]) and /letsplay matches.

When a Challenge-League-style match (the over-by-over "approach" engine shared by
/cipl and /letsplay) ends level, this module takes over and plays a Super Over
that behaves like a **1-over /playmatch**: interactive, user-vs-user, ball by
ball — the bowling side picks a delivery (and length, for pacers) and the
batting side picks a shot, every ball.

The ball OUTCOME comes from the /playmatch per-ball engine
(``services.probability_engine.calculate_outcome``) — so it really is a 1-over
/playmatch, and the delivery/length/shot picks are inputs to the result rather
than decoration on top of a fixed matrix.

Special Super Over rules (per the workflow spec):
  • Each innings: max 6 LEGAL balls and max 2 wickets — whichever first.
  • Wides and no-balls add runs but are NOT legal balls (re-bowled).
  • Leg byes / byes come off a legal delivery and DO count as a legal ball.
  • The team that batted SECOND in the (main / previous super) match bats first.
  • Innings 2 is a chase: reaching the target, or losing the 2nd wicket, ends it
    immediately.
  • A Super Over the countback below cannot split is replayed — keep going
    until there is a winner. Every player a side has already USED is spent: a
    batter who came to the crease cannot bat again and a bowler who bowled
    cannot bowl again, in any later Super Over of the same tie. A nominated
    backup who never walked out was not used, so they stay available. Each
    replay therefore digs further into the squad, and a side that keeps tying
    eventually has to trust its lower order — which is the point.
  • Winner is decided by runs. Level on runs goes to a boundary countback (most
    sixes, then most fours) before another Super Over is played — see
    _decide_super_over. Runs settle ~94% of Super Overs and the countback most
    of the rest, so a replay stays the ~2% event it should be instead of
    dragging players through Super Over after Super Over.
  • Super Over score is kept separate from the main-match score.

Using the real match engine rather than a separate arcade matrix is what makes
the Super Over a fair extension of the game instead of a lottery. Measured over
20,000 simulated Super Overs (see tests/test_super_over.py):

  • Player ratings decide it. The engine's rating layers (differential curve,
    class tiers, per-ball execution duel) apply in full: a 90-rated batting side
    beats a 75-rated one ~80% of the time, where the old matrix left it a
    coin flip.
  • Traits apply in /letsplay, exactly as in the match itself — the XI dicts
    carry each player's traits and they go through the engine's trait layer.
    Challenge League XIs carry no traits, so /cipl is decided on ratings alone;
    the data does the branching, not the code.
  • Neither side gets a handicap. With no batting-first edge, equal sides split
    47%/47% (the balance being the ~6% that finish level on runs).
  • Scoring is realistic: ~11 runs an over between equal sides, against the ~17
    the boundary-inflated matrix produced.

State lives in ``context.bot_data[f"so_{mid}"]`` (in-memory, like the /wpm and
/cm lobbies). The main Match row stays ``active`` until a winner is found, so the
one-match-per-player / one-game-per-chat guards keep holding during the Super
Over, and /endmatch can still recover a stuck one.
"""

import asyncio
import html
import logging
from datetime import datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match
from services.bowling_service import get_delivery_options, AVAILABLE_SHOTS
from services.sim_match import _adapt_player, _normalize_outcome

logger = logging.getLogger(__name__)

# Engine pitch vocabulary is {"Green","Flat","Dry","Hard","Dead"}; map the
# Challenge League surfaces onto it (unknowns fall back to a neutral default in
# the engine anyway).
_PITCH_MAP = {
    "Hard": "Hard", "Flat": "Flat", "Green": "Green", "Dry": "Dry",
    "Dusty": "Dry", "Dust": "Dry", "Bouncy": "Hard", "Dead": "Dead",
    "Even": "Hard",
}

# Short delay (seconds) between showing a ball's result and the next prompt so
# the chat reads like a live broadcast rather than an instant jump.
_BALL_PAUSE = 1.4

# The Super Over is simulated as one NEUTRAL middle over: the engine's
# powerplay window is overs 0-5 and its death window is overs 16-19, and both
# inflate boundaries hard (death multiplies them by up to 2.2). Handing the
# Super Over a death over is what turned it into a 17-run slugfest; over 10 sits
# in neither window, so totals land in the realistic 8-14 band and the result
# turns on the players rather than on a phase multiplier.
SO_ENGINE_OVER = 10
SO_ENGINE_TOTAL_OVERS = 20

# The Super Over's own rules, named rather than sprinkled as literals so every
# gate (selection, the per-ball guard, the innings-end check) reads the same
# limit and none of them can drift apart.
SO_LEGAL_BALLS = 6       # an innings ends after 6 legal balls...
SO_MAX_WICKETS = 2       # ...or 2 wickets, whichever comes first
SO_BATTERS = 3           # 2 openers + 1 backup
# Wides and no-balls are re-bowled, so a pathological run of them would never
# reach 6 legal balls. Cap total deliveries so an innings always terminates.
SO_MAX_DELIVERIES = 24

# Six balls with a match on them is the most nerve-shredding over a bowler ever
# bowls, and the base engine's 1.3% wide / 0.4% no-ball made an extra a novelty:
# roughly one Super Over in twelve saw a wide, one in forty a no-ball. Doubling
# the extras weights puts a wide in about one over in seven and a no-ball — with
# the free hit that follows — in about one in twenty. Only the extras move; runs
# and wickets are untouched, so the result still turns on the players.
SO_EXTRAS_MULT = 2.0


def _so_key(mid):
    return f"so_{mid}"


def _get(context, mid):
    return context.bot_data.get(_so_key(mid))


async def _disable_message(context, chat_id, msg_id):
    """Strip the inline keyboard from a now-stale message so its buttons can't
    be tapped again (used selection messages are left in chat as a record)."""
    if not msg_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_id, reply_markup=None)
    except Exception:
        pass


def _mention(tg_id, name):
    """Clickable mention; falls back to a plain (escaped) name."""
    safe = html.escape(str(name or "Player"))
    if tg_id:
        return f'<a href="tg://user?id={int(tg_id)}">{safe}</a>'
    return safe


def _pitch_for_engine(pitch):
    return _PITCH_MAP.get((pitch or "Hard"), "Hard")


def _xi_players(xi):
    """Return ONLY the confirmed main-match Playing XI players.

    The Super Over must use the same eleven that played the main match — never
    anyone else from the squad/roster. ``state['bat_xi']`` / ``state['bowl_xi']``
    already hold exactly that XI; this just defensively drops anything malformed
    and de-dupes by roster id so no stray entry can slip into selection.
    """
    out, seen = [], set()
    for p in xi or []:
        if not isinstance(p, dict):
            continue
        rid = p.get("roster_id")
        if rid is None:
            continue
        rid = int(rid)
        if rid in seen:
            continue
        seen.add(rid)
        out.append(p)
    return out


def find_super_over_in_chat(bot_data, chat_id):
    """Return the match id of a live Super Over in ``chat_id``, or None."""
    for k, v in list(bot_data.items()):
        if (isinstance(k, str) and k.startswith("so_")
                and isinstance(v, dict) and v.get("chat_id") == chat_id):
            return v.get("mid")
    return None


async def resume_super_over(context, mid):
    """Re-render the live Super Over prompt (selection or current ball).

    The /playmatch-style recovery for a Super Over: if a prompt message was
    deleted or a send failed, re-post whichever screen is outstanding so the
    captains can carry on. Returns True if a prompt was re-sent.
    """
    so = _get(context, mid)
    if not so:
        return False
    try:
        if "inn" in so and so.get("bat_confirmed") and so.get("bowl_confirmed"):
            inn = so["inn"]
            # If we somehow stalled mid-resolution, restart the ball cleanly from
            # delivery selection (the half-resolved delivery is discarded).
            if inn.get("stage") not in ("DELIV", "LEN", "SHOT"):
                inn["stage"] = "DELIV"
                inn["pending"] = {"delivery": None, "length": None}
            inn["msg_id"] = None          # force a fresh prompt message
            await _prompt(context, mid)
        else:
            # Kill the dropped/old selection message's buttons, then re-send.
            await _disable_message(context, so["chat_id"], so.get("sel_msg_id"))
            so["sel_msg_id"] = None
            await _send_selection(context, mid)
        return True
    except Exception:
        logger.exception("resume_super_over failed (%s)", mid)
        return False


# ════════════════════════════════════════════════════════════════════
# Entry point — called from handlers.cipl_play._complete_match on a tie
# ════════════════════════════════════════════════════════════════════

async def start_super_over(context, mid, state) -> bool:
    """Begin the Super Over for a tied match. Returns True if it started.

    ``state`` is the live cipl_approach match state (post innings 2). The team
    currently batting (``bat_team_id``) is the one that batted second in the main
    match, so per the rules it bats first in Super Over 1.
    """
    try:
        second_bat_uid = state["bat_team_id"]     # batted 2nd → bats first in SO
        first_bat_uid = state["bowl_team_id"]      # batted 1st

        teams = {
            second_bat_uid: {
                "uid": second_bat_uid,
                "tg": state.get("bat_user_tg"),
                "name": state.get("bat_team_name", "Team A"),
                "code": state.get("bat_team_code", ""),
                "emoji": state.get("bat_team_emoji", "🏏"),
                # Strictly the eleven that played the main match — nobody else.
                "xi": _xi_players(state.get("bat_xi")),
            },
            first_bat_uid: {
                "uid": first_bat_uid,
                "tg": state.get("bowl_user_tg"),
                "name": state.get("bowl_team_name", "Team B"),
                "code": state.get("bowl_team_code", ""),
                "emoji": state.get("bowl_team_emoji", "🏏"),
                "xi": _xi_players(state.get("bowl_xi")),
            },
        }
        # Need at least 2 batters + 1 bowler available on each side.
        for t in teams.values():
            if len(t["xi"]) < 2:
                logger.warning("Super Over aborted: team %s has <2 players", t["uid"])
                return False

        main = {
            "team1_uid": first_bat_uid,
            "team1_name": state.get("inn1_bat_team") or teams[first_bat_uid]["name"],
            "team1_runs": state.get("inn1_runs", 0),
            "team1_wkts": state.get("inn1_wickets", 0),
            "team2_uid": second_bat_uid,
            "team2_name": teams[second_bat_uid]["name"],
            "team2_runs": state.get("total_runs", 0),
            "team2_wkts": state.get("total_wickets", 0),
            "overs": state.get("overs"),
        }

        so = {
            "mid": mid,
            "chat_id": state["chat_id"],
            "pitch": _pitch_for_engine(state.get("pitch_type")),
            "is_letsplay": bool(state.get("is_letsplay")),
            "teams": teams,
            "main": main,
            "so_number": 1,
            "first_bat_uid": second_bat_uid,
            "results": [],                       # per-super-over scorelines
            # Who has already been USED, cumulatively across every Super Over
            # of this tie. A player is spent once they actually take the field:
            # a batter who came to the crease, a bowler who bowled. A batter who
            # was nominated as the backup but never walked out is untouched and
            # can be picked again.
            "batted": {uid: set() for uid in teams},      # rids that batted
            "used_bowlers": {uid: set() for uid in teams},  # rids that bowled
            # Keep the finished main-match state so the main scorecard image can
            # be re-rendered (tie now, winner after the Super Over).
            "main_state": state,
        }
        context.bot_data[_so_key(mid)] = so

        m = main
        await context.bot.send_message(
            so["chat_id"],
            "🏏 <b>MATCH TIED!</b>\n\n"
            f"{html.escape(m['team1_name'])}: {m['team1_runs']}/{m['team1_wkts']}\n"
            f"{html.escape(m['team2_name'])}: {m['team2_runs']}/{m['team2_wkts']}\n\n"
            "🔥 <b>SUPER OVER TIME</b>\n\n"
            f"<b>{html.escape(teams[second_bat_uid]['name'])}</b> will bat first "
            "because they batted second in the main match.",
            parse_mode="HTML")

        # Main-match scorecard image, captioned "Match Tied".
        await _send_main_scorecard(
            context, so, {"tie": True},
            caption="🏏 <b>Match Tied</b> — Super Over to follow!")

        _begin_super_over(so)
        await _send_selection(context, mid)
    except Exception:
        logger.exception("start_super_over failed for match %s", mid)
        # Roll back the partial Super Over so the caller's fallback (the normal
        # tied-result completion) runs cleanly — including its own stat persist.
        context.bot_data.pop(_so_key(mid), None)
        return False

    # Kickoff succeeded — the Super Over is committed and live. Only now persist
    # the MAIN match's player stats (the normal completion path is skipped on a
    # tie). This MUST run after kickoff so a kickoff failure → fallback path is
    # the sole place that persists; a failure HERE must not return False (that
    # would let the fallback double-count career stats for the same match).
    try:
        _persist_main_stats(mid, state)
    except Exception:
        logger.exception("Super Over: deferred main-stat persistence failed (%s)", mid)
    return True


def _spectate_markup(so):
    """InlineKeyboardMarkup carrying the MAIN match's Mini App spectate button,
    so every Super Over scorecard links back to the same match board."""
    state = so.get("main_state")
    if not state:
        return None
    try:
        from handlers.cipl_play import _miniapp_row
        rows = _miniapp_row(state)
        return InlineKeyboardMarkup(rows) if rows else None
    except Exception:
        logger.exception("Super Over: spectate button build failed (%s)", so.get("mid"))
        return None


async def _send_main_scorecard(context, so, result, caption):
    """Render + send the MAIN-match summary card image (same scores throughout;
    only the result line changes — ``{"tie": True}`` → "Match Tied", or a winner
    dict → "X won by N runs/wickets"). Carries the Mini App spectate button."""
    state = so.get("main_state")
    if not state:
        return False
    try:
        from handlers.cipl_play import _build_cipl_summary_image
        # Offload the CPU-bound Pillow render so it doesn't block the event loop.
        img = await asyncio.to_thread(_build_cipl_summary_image, state, result)
        if img:
            await context.bot.send_photo(
                so["chat_id"], photo=BytesIO(img), caption=caption,
                parse_mode="HTML", reply_markup=_spectate_markup(so))
            return True
    except Exception:
        logger.exception("Super Over: main scorecard image failed (%s)", so.get("mid"))
    return False


def _build_super_over_card(so):
    """Render a 'SUPER OVER' titled scorecard image for the deciding Super Over
    (its two innings), reusing the shared match-summary card generator."""
    innings = so.get("so_innings") or []
    if len(innings) < 2:
        return None
    try:
        from services.match_summary_card import generate_match_summary
        try:
            from services.config_service import get_config
            text_settings = get_config().get("scorecard_text_settings")
        except Exception:
            text_settings = None

        a, b = innings[0], innings[1]
        win_uid = so.get("_winner_uid")
        win_name = so["teams"][win_uid]["name"] if win_uid in so["teams"] else ""
        margin = so.get("_margin_text", "won the Super Over")
        return generate_match_summary(
            inn1_team=a["team"], inn1_runs=a["runs"], inn1_wickets=a["wickets"],
            inn1_overs=a["overs"],
            inn2_team=b["team"], inn2_runs=b["runs"], inn2_wickets=b["wickets"],
            inn2_overs=b["overs"],
            winner_name=win_name, win_margin_text=margin,
            overs_total=1,
            top_per_team={
                "inn1": {"team": a["team"], "batters": a["batters"], "bowlers": a["bowlers"]},
                "inn2": {"team": b["team"], "batters": b["batters"], "bowlers": b["bowlers"]},
            },
            match_no=so.get("mid"),
            header_left="SUPER", header_right="OVER",
            text_settings=text_settings,
        )
    except Exception:
        logger.exception("Super Over: super-over scorecard image failed")
        return None


def _persist_main_stats(mid, state):
    session = get_session()
    try:
        try:
            from services.player_stats_service import persist_player_game_stats
            persist_player_game_stats(session, state)
        except Exception:
            logger.exception("Super Over: main-match stat persistence failed (%s)", mid)
        m = session.query(Match).get(mid)
        if m:
            m.inn1_runs = state.get("inn1_runs")
            m.inn1_wickets = state.get("inn1_wickets")
            m.inn2_runs = state.get("total_runs")
            m.inn2_wickets = state.get("total_wickets")
            # Stay 'active' so concurrency guards keep holding through the SO.
            m.status = "active"
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Super Over: main Match update failed (%s)", mid)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Per-super-over setup
# ════════════════════════════════════════════════════════════════════

def _begin_super_over(so):
    """(Re)initialise selection state for the current Super Over number."""
    bat_uid = so["first_bat_uid"]
    bowl_uid = next(u for u in so["teams"] if u != bat_uid)
    so["bat_uid"] = bat_uid
    so["bowl_uid"] = bowl_uid
    so["innings_no"] = 1
    so["target"] = None
    so["score"] = {}                 # uid -> (runs, wickets) this super over
    so["so_innings"] = []            # per-innings detail for the scorecard image
    _reset_selection(so)


def _reset_selection(so):
    so["sel_batters"] = []
    so["sel_bowler"] = None
    so["bat_confirmed"] = False
    so["bowl_confirmed"] = False
    so["sel_msg_id"] = None


def _eligible_batters(so, uid):
    """Batters this team may still pick — the XI minus anyone who came to the
    crease in an earlier Super Over of this tie.

    If the restriction leaves fewer than the three a Super Over needs, the pool
    is topped back up with already-used batters — but only by as many as are
    missing, rather than throwing the rule away and re-offering the whole XI. A
    side must always be able to field a Super Over; that is no reason to let a
    spent batter come back while fresh ones are still available.
    """
    out = so["batted"][uid]
    xi = so["teams"][uid]["xi"]
    pool = [p for p in xi if int(p["roster_id"]) not in out]
    if len(pool) >= SO_BATTERS:
        return pool
    for p in xi:
        if len(pool) >= SO_BATTERS:
            break
        if int(p["roster_id"]) in out:
            pool.append(p)
    return pool


def _eligible_bowlers(so, uid):
    """Bowlers this team may still pick — the XI minus everyone who has already
    bowled a Super Over in this tie (not merely the previous one). Falls back to
    the full XI only when the whole side has bowled."""
    used = so["used_bowlers"][uid]
    pool = [p for p in so["teams"][uid]["xi"] if int(p["roster_id"]) not in used]
    return pool if pool else list(so["teams"][uid]["xi"])


def _player_by_rid(so, uid, rid):
    for p in so["teams"][uid]["xi"]:
        if int(p["roster_id"]) == int(rid):
            return p
    return None


# ════════════════════════════════════════════════════════════════════
# Player selection UI (owner-gated)
# ════════════════════════════════════════════════════════════════════

async def _send_selection(context, mid):
    so = _get(context, mid)
    if not so:
        return
    text, kb = _render_selection(so)
    sent = await context.bot.send_message(
        so["chat_id"], text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb))
    so["sel_msg_id"] = getattr(sent, "message_id", None)


def _render_selection(so):
    bat = so["teams"][so["bat_uid"]]
    bowl = so["teams"][so["bowl_uid"]]
    n = so["so_number"]
    head = f"🔥 <b>SUPER OVER {n} — PLAYER SELECTION</b>" if n > 1 \
        else "🔥 <b>SUPER OVER — PLAYER SELECTION</b>"
    target_line = f"\n🎯 Target: <b>{so['target']}</b>" if so.get("target") else ""

    lines = [
        head, "",
        f"Batting: <b>{html.escape(bat['name'])}</b>",
        f"Bowling: <b>{html.escape(bowl['name'])}</b>{target_line}", "",
        f"{_mention(bat['tg'], bat['name'])}, select <b>{SO_BATTERS} batters</b> "
        "(2 openers + 1 backup).",
        f"{_mention(bowl['tg'], bowl['name'])}, select <b>1 bowler</b>.", "",
        "🏏 Every ball is a duel: the bowler picks the delivery, the batter "
        "picks the shot, and ratings decide who wins it.",
        "",
        "<i>Only the team owner can pick for their own team.</i>",
    ]

    kb = []
    # Batting buttons (toggle, up to 3)
    chosen_b = so["sel_batters"]
    for p in _eligible_batters(so, so["bat_uid"]):
        rid = int(p["roster_id"])
        mark = "✅ " if rid in chosen_b else ""
        kb.append([InlineKeyboardButton(
            f"{mark}🏏 {p['name']}", callback_data=f"so_bat_{so['mid']}_{rid}")])
    bat_done = "✅ Batters confirmed" if so["bat_confirmed"] else \
        f"Confirm Batters ({len(chosen_b)}/{SO_BATTERS})"
    kb.append([InlineKeyboardButton(bat_done, callback_data=f"so_batok_{so['mid']}")])

    # Bowling buttons (single choice)
    for p in _eligible_bowlers(so, so["bowl_uid"]):
        rid = int(p["roster_id"])
        mark = "✅ " if so["sel_bowler"] == rid else ""
        kb.append([InlineKeyboardButton(
            f"{mark}🎯 {p['name']}", callback_data=f"so_bowl_{so['mid']}_{rid}")])
    bowl_done = "✅ Bowler confirmed" if so["bowl_confirmed"] else "Confirm Bowler"
    kb.append([InlineKeyboardButton(bowl_done, callback_data=f"so_bowlok_{so['mid']}")])
    return "\n".join(lines), kb


async def _refresh_selection(context, mid, q):
    so = _get(context, mid)
    if not so or not so.get("sel_msg_id"):
        return
    text, kb = _render_selection(so)
    try:
        await q.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass


def _parse(q, prefix):
    """Return (mid, rest) from callback data 'prefix_{mid}[_{rest}]' or (None, None)."""
    try:
        data = q.data
        body = data[len(prefix):]
        parts = body.split("_", 1)
        mid = int(parts[0])
        rest = parts[1] if len(parts) > 1 else None
        return mid, rest
    except Exception:
        return None, None


async def so_bat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_bat_{mid}_{rid} — batting owner toggles a batter (max 3)."""
    q = update.callback_query
    mid, rest = _parse(q, "so_bat_")
    so = _get(context, mid) if mid is not None else None
    if not so or rest is None:
        await q.answer("This Super Over is no longer active.", show_alert=True)
        return
    bat = so["teams"][so["bat_uid"]]
    if q.from_user.id != bat["tg"]:
        await q.answer(f"❌ Only {bat['name']} can select these players.", show_alert=True)
        return
    if so["bat_confirmed"]:
        await q.answer("Batters already confirmed.", show_alert=True)
        return
    rid = int(rest)
    sel = so["sel_batters"]
    if rid in sel:
        sel.remove(rid)
        await q.answer()
        await _refresh_selection(context, mid, q)
        return
    # Reject taps on stale buttons for players who aren't currently eligible
    # (e.g. a batter dismissed in an earlier Super Over of this same tie).
    eligible = {int(p["roster_id"]) for p in _eligible_batters(so, so["bat_uid"])}
    if rid not in eligible:
        await q.answer("That player can't bat in this Super Over.", show_alert=True)
        return
    if len(sel) >= SO_BATTERS:
        await q.answer(f"You already picked {SO_BATTERS} — tap a selected one to remove it.",
                       show_alert=True)
        return
    sel.append(rid)
    await q.answer()
    await _refresh_selection(context, mid, q)


async def so_batok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_batok_{mid} — batting owner confirms exactly 3 batters."""
    q = update.callback_query
    mid, _ = _parse(q, "so_batok_")
    so = _get(context, mid) if mid is not None else None
    if not so:
        await q.answer("This Super Over is no longer active.", show_alert=True)
        return
    bat = so["teams"][so["bat_uid"]]
    if q.from_user.id != bat["tg"]:
        await q.answer(f"❌ Only {bat['name']} can confirm batters.", show_alert=True)
        return
    eligible = {int(p["roster_id"]) for p in _eligible_batters(so, so["bat_uid"])}
    need = min(SO_BATTERS, len(eligible))
    if len(so["sel_batters"]) != need:
        await q.answer(f"Select {need} batters first.", show_alert=True)
        return
    # Defensive: every confirmed batter must still be eligible (a stale tap could
    # have slipped one in before validation existed on its message).
    if any(int(r) not in eligible for r in so["sel_batters"]):
        so["sel_batters"] = []
        await q.answer("Selection had an ineligible player — pick again.",
                       show_alert=True)
        await _refresh_selection(context, mid, q)
        return
    so["bat_confirmed"] = True
    await q.answer("Batters confirmed!")
    await _refresh_selection(context, mid, q)
    await _maybe_start_innings(context, mid)


async def so_bowl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_bowl_{mid}_{rid} — bowling owner picks the single bowler."""
    q = update.callback_query
    mid, rest = _parse(q, "so_bowl_")
    so = _get(context, mid) if mid is not None else None
    if not so or rest is None:
        await q.answer("This Super Over is no longer active.", show_alert=True)
        return
    bowl = so["teams"][so["bowl_uid"]]
    if q.from_user.id != bowl["tg"]:
        await q.answer(f"❌ Only {bowl['name']} can select the bowler.", show_alert=True)
        return
    if so["bowl_confirmed"]:
        await q.answer("Bowler already confirmed.", show_alert=True)
        return
    rid = int(rest)
    # Validate against the CURRENT eligible bowlers (this team's XI minus the
    # previous Super Over's bowler). Without this a stale button from an earlier
    # selection message could reuse a restricted bowler or a player from the
    # other XI — the latter would make _player_by_rid() return None at innings
    # start. so_bowl_ is a shared prefix, so any captain's old button can arrive.
    eligible = {int(p["roster_id"]) for p in _eligible_bowlers(so, so["bowl_uid"])}
    if rid not in eligible:
        await q.answer("That bowler can't bowl this Super Over.", show_alert=True)
        return
    so["sel_bowler"] = rid
    await q.answer()
    await _refresh_selection(context, mid, q)


async def so_bowlok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_bowlok_{mid} — bowling owner confirms the bowler."""
    q = update.callback_query
    mid, _ = _parse(q, "so_bowlok_")
    so = _get(context, mid) if mid is not None else None
    if not so:
        await q.answer("This Super Over is no longer active.", show_alert=True)
        return
    bowl = so["teams"][so["bowl_uid"]]
    if q.from_user.id != bowl["tg"]:
        await q.answer(f"❌ Only {bowl['name']} can confirm the bowler.", show_alert=True)
        return
    if not so["sel_bowler"]:
        await q.answer("Pick a bowler first.", show_alert=True)
        return
    # Defensive: the chosen bowler must still be eligible for this Super Over.
    eligible = {int(p["roster_id"]) for p in _eligible_bowlers(so, so["bowl_uid"])}
    if int(so["sel_bowler"]) not in eligible:
        so["sel_bowler"] = None
        await q.answer("That bowler isn't eligible — pick again.", show_alert=True)
        await _refresh_selection(context, mid, q)
        return
    so["bowl_confirmed"] = True
    await q.answer("Bowler confirmed!")
    await _refresh_selection(context, mid, q)
    await _maybe_start_innings(context, mid)


async def _maybe_start_innings(context, mid):
    so = _get(context, mid)
    if not so or not (so["bat_confirmed"] and so["bowl_confirmed"]):
        return
    await _start_innings(context, mid)


# ════════════════════════════════════════════════════════════════════
# Innings — interactive ball-by-ball
# ════════════════════════════════════════════════════════════════════

async def _start_innings(context, mid):
    so = _get(context, mid)
    if not so:
        return
    # Selection is locked in — remove the buttons from the selection message so a
    # stale tap can't change the side after the innings has begun.
    await _disable_message(context, so["chat_id"], so.get("sel_msg_id"))
    so["sel_msg_id"] = None
    bat_uid, bowl_uid = so["bat_uid"], so["bowl_uid"]
    # Sanitise the confirmed picks: de-dupe and drop anything not in this XI, so
    # a duplicated pick can't put the same player at both ends (where a wicket
    # would leave a dismissed batter still at the crease).
    trio = []
    for rid in so["sel_batters"]:
        rid = int(rid)
        if rid not in trio and _player_by_rid(so, bat_uid, rid):
            trio.append(rid)
    if not trio:
        logger.warning("Super Over: no valid batters confirmed (%s)", mid)
        _reset_selection(so)
        await _send_selection(context, mid)
        return
    bowler_rid = so["sel_bowler"]
    so["inn"] = {
        "trio": trio,
        "striker": trio[0],
        "non_striker": trio[1] if len(trio) > 1 else trio[0],
        "backup": trio[2] if len(trio) > 2 else None,
        "backup_used": False,
        "runs": 0, "wickets": 0, "legal": 0, "deliveries": 0,
        "bowler_rid": bowler_rid,
        "bowl_runs": 0, "bowl_wkts": 0,
        "bat": {int(r): {"r": 0, "b": 0, "4": 0, "6": 0, "out": False, "how": ""}
                for r in trio},
        "streak": {int(r): {"boundaries": 0} for r in trio},
        "recent": [],
        "stage": "DELIV",
        "pending": {"delivery": None, "length": None},
        "msg_id": None,
        # Crowd/situation colour for the last ball bowled (see _crowd_line).
        "hype": "",
        # Everyone who has actually walked out this innings. The opening pair
        # start here; the backup joins if a wicket brings them in. This — not
        # the nominated trio — is what a replay treats as "already used".
        "at_crease": {int(trio[0])} | ({int(trio[1])} if len(trio) > 1 else set()),
        # The previous delivery + length, for the engine's spam layer.
        "last_ball": None,
        "repeat": 0,
    }
    await _prompt(context, mid)


def _name(so, uid, rid):
    p = _player_by_rid(so, uid, rid)
    return p["name"] if p else "Player"


_BALL_EMOJI = {"0": "⚪", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣",
               "4": "4️⃣", "6": "6️⃣", "W": "🔴"}


def _ball_symbol(sym):
    """Pretty per-ball marker for the Recent strip."""
    return _BALL_EMOJI.get(sym, f"<code>{sym}</code>")


def _pressure_bar(so, inn):
    """A 6-block gauge of how hard the chase (or the defence) has become.

    Innings 2 reads the required run rate against a Super Over's realistic
    ceiling; innings 1 reads the balls already gone. It is cosmetic, but it is
    what makes a 3-off-2 finish *look* like 3 off 2.
    """
    left = max(0, SO_LEGAL_BALLS - inn["legal"])
    if so["innings_no"] == 2 and so.get("target"):
        need = max(0, so["target"] - inn["runs"])
        if left <= 0 or need <= 0:
            filled = 6 if need > 0 else 0
        else:
            # 2 runs a ball is comfortable, 4+ a ball is desperation.
            filled = max(0, min(6, round((need / left) / 4 * 6)))
    else:
        filled = min(6, inn["legal"])
    return "▓" * filled + "░" * (6 - filled)


def _equation_line(so, inn):
    """The one line everybody actually stares at during a Super Over."""
    need = max(0, so["target"] - inn["runs"])
    left = max(0, SO_LEGAL_BALLS - inn["legal"])
    if need <= 0:
        return "✅ <b>Target reached!</b>"
    if left <= 0:
        return "⌛ <b>No deliveries left.</b>"
    if left == 1:
        return f"🚨 <b>{need} needed off the LAST BALL!</b>"
    return f"⚡ Need <b>{need}</b> off <b>{left}</b> balls"


def _batter_label(so, inn, rid):
    """A batter's name for the two-batter line, badged 🆕 from the moment they
    walk out until they have faced a ball — so the replacement is visible on
    every screen, not just the one message announcing them."""
    name = html.escape(_name(so, so["bat_uid"], rid))
    if int(rid) == inn.get("last_in") and inn["bat"].get(int(rid), {}).get("b", 0) == 0:
        return f"🆕 {name}"
    return name


def _score_card(so, note=None):
    inn = so["inn"]
    bat = so["teams"][so["bat_uid"]]
    bowl = so["teams"][so["bowl_uid"]]
    n = so["so_number"]
    title = f"🔥 <b>SUPER OVER {n} — INNINGS {so['innings_no']}</b>" if n > 1 \
        else f"🔥 <b>SUPER OVER — INNINGS {so['innings_no']}</b>"

    s_rid, ns_rid = inn["striker"], inn["non_striker"]
    sb, nb = inn["bat"][int(s_rid)], inn["bat"][int(ns_rid)]
    balls = inn["legal"]
    over_disp = f"{balls // 6}.{balls % 6}"

    lines = [title, "",
             f"Batting: <b>{html.escape(bat['name'])}</b>",
             f"Bowling: <b>{html.escape(bowl['name'])}</b>"]
    if so["innings_no"] == 2:
        lines.append(f"🎯 Target: <b>{so['target']}</b>")
        lines.append(_equation_line(so, inn))
    lines += [
        "",
        f"Score: <b>{inn['runs']}/{inn['wickets']}</b> ({over_disp}/1)",
        f"🌡 Pressure: {_pressure_bar(so, inn)}",
        "",
        f"🏏 {_batter_label(so, inn, s_rid)} <b>{sb['r']}</b>({sb['b']})  •  "
        f"{_batter_label(so, inn, ns_rid)} {nb['r']}({nb['b']})",
        f"🎯 {html.escape(_name(so, so['bowl_uid'], inn['bowler_rid']))} "
        f"{inn['bowl_wkts']}-{inn['bowl_runs']}",
    ]
    if inn["recent"]:
        lines.append("Recent: " + " ".join(_ball_symbol(s) for s in inn["recent"]))
    if inn.get("hype"):
        lines += ["", inn["hype"]]
    if note:
        lines += ["", note]
    return "\n".join(lines)


async def _prompt(context, mid):
    """Render the current score card + the right pick keyboard for the stage."""
    so = _get(context, mid)
    inn = so["inn"]
    bat = so["teams"][so["bat_uid"]]
    bowl = so["teams"][so["bowl_uid"]]
    bowler = _player_by_rid(so, so["bowl_uid"], inn["bowler_rid"])
    opts = get_delivery_options(bowler.get("bowl_style", ""), bowler.get("bowl_hand", "Right"))

    stage = inn["stage"]
    kb = []
    if stage == "DELIV":
        choices = opts["deliveries"] if opts["is_spinner"] else opts["variations"]
        inn["_choices"] = choices
        note = f"🎳 {_mention(bowl['tg'], bowl['name'])} — choose your delivery:"
        for i, c in enumerate(choices):
            kb.append([InlineKeyboardButton(c, callback_data=f"so_dv_{mid}_{i}")])
    elif stage == "LEN":
        choices = opts["lengths"]
        inn["_choices"] = choices
        note = (f"🎳 {_mention(bowl['tg'], bowl['name'])} — "
                f"<b>{html.escape(inn['pending']['delivery'])}</b> — choose length:")
        for i, c in enumerate(choices):
            kb.append([InlineKeyboardButton(c, callback_data=f"so_ln_{mid}_{i}")])
    else:  # SHOT
        inn["_choices"] = AVAILABLE_SHOTS
        deliv = inn["pending"]["delivery"]
        if inn["pending"]["length"]:
            deliv = f"{deliv} {inn['pending']['length']}"
        note = (f"🏏 {_mention(bat['tg'], bat['name'])} — facing "
                f"<b>{html.escape(deliv)}</b> — play your shot:")
        row = []
        for i, c in enumerate(AVAILABLE_SHOTS):
            row.append(InlineKeyboardButton(c, callback_data=f"so_sh_{mid}_{i}"))
            if len(row) == 2:
                kb.append(row); row = []
        if row:
            kb.append(row)

    text = _score_card(so, note=note)
    markup = InlineKeyboardMarkup(kb)
    if inn.get("msg_id"):
        try:
            await context.bot.edit_message_text(
                text, chat_id=so["chat_id"], message_id=inn["msg_id"],
                parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    sent = await context.bot.send_message(
        so["chat_id"], text, parse_mode="HTML", reply_markup=markup)
    inn["msg_id"] = getattr(sent, "message_id", None)


async def so_deliv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_dv_{mid}_{idx} — bowling owner picks a delivery/variation."""
    q = update.callback_query
    mid, rest = _parse(q, "so_dv_")
    so = _get(context, mid) if mid is not None else None
    if not so or "inn" not in so or rest is None:
        await q.answer("Not active.", show_alert=True)
        return
    inn = so["inn"]
    if inn["stage"] != "DELIV":
        await q.answer("Not your turn.", show_alert=True)
        return
    bowl = so["teams"][so["bowl_uid"]]
    if q.from_user.id != bowl["tg"]:
        await q.answer("Only the bowling side picks the delivery.", show_alert=True)
        return
    idx = int(rest)
    choices = inn.get("_choices") or []
    if not (0 <= idx < len(choices)):
        await q.answer("Invalid.", show_alert=True)
        return
    inn["pending"]["delivery"] = choices[idx]
    bowler = _player_by_rid(so, so["bowl_uid"], inn["bowler_rid"])
    spin = get_delivery_options(bowler.get("bowl_style", ""),
                                bowler.get("bowl_hand", "Right"))["is_spinner"]
    inn["stage"] = "SHOT" if spin else "LEN"
    await q.answer()
    await _prompt(context, mid)


async def so_len_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_ln_{mid}_{idx} — bowling owner picks a length (pacers only)."""
    q = update.callback_query
    mid, rest = _parse(q, "so_ln_")
    so = _get(context, mid) if mid is not None else None
    if not so or "inn" not in so or rest is None:
        await q.answer("Not active.", show_alert=True)
        return
    inn = so["inn"]
    if inn["stage"] != "LEN":
        await q.answer("Not your turn.", show_alert=True)
        return
    bowl = so["teams"][so["bowl_uid"]]
    if q.from_user.id != bowl["tg"]:
        await q.answer("Only the bowling side picks the length.", show_alert=True)
        return
    idx = int(rest)
    choices = inn.get("_choices") or []
    if not (0 <= idx < len(choices)):
        await q.answer("Invalid.", show_alert=True)
        return
    inn["pending"]["length"] = choices[idx]
    inn["stage"] = "SHOT"
    await q.answer()
    await _prompt(context, mid)


async def so_shot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """so_sh_{mid}_{idx} — batting owner picks a shot, then the ball resolves."""
    q = update.callback_query
    mid, rest = _parse(q, "so_sh_")
    so = _get(context, mid) if mid is not None else None
    if not so or "inn" not in so or rest is None:
        await q.answer("Not active.", show_alert=True)
        return
    inn = so["inn"]
    if inn["stage"] != "SHOT":
        await q.answer("Not your turn.", show_alert=True)
        return
    # Hard stop at the Super Over's limits. The innings-end path below already
    # closes the innings the moment the 2nd wicket falls or the 6th legal ball
    # is bowled, but a stale button from a message that was still on screen must
    # never be able to bat on past them.
    if inn["wickets"] >= SO_MAX_WICKETS or inn["legal"] >= SO_LEGAL_BALLS:
        await q.answer("That innings is over.", show_alert=True)
        return
    bat = so["teams"][so["bat_uid"]]
    if q.from_user.id != bat["tg"]:
        await q.answer("Only the batting side picks the shot.", show_alert=True)
        return
    idx = int(rest)
    if not (0 <= idx < len(AVAILABLE_SHOTS)):
        await q.answer("Invalid.", show_alert=True)
        return
    # Lock this delivery synchronously (before any await) so a double-tap or a
    # second concurrent shot callback — the bot runs updates concurrently — can't
    # pass the SHOT check and resolve the same ball twice. _resolve_ball advances
    # the stage to DELIV (next ball) or ends the innings.
    inn["stage"] = "RESOLVING"
    await q.answer()
    shot = AVAILABLE_SHOTS[idx]
    await _resolve_ball(context, mid, shot)


_COMMENTARY_KEYS = {0: "dot", 1: "one", 2: "two", 3: "three", 4: "four",
                    6: "six"}


def _ball_commentary(oc, batter_name, bowler_name):
    """A commentary line for this delivery.

    The /playmatch engine returns outcome weights, not prose, so the line comes
    from the shared commentary bank — the same wording players see in a normal
    match. Uses the built-in fallback bank rather than the admin-configured
    rows, so a Super Over ball never pays for a database round trip mid-over.
    """
    otype = oc.get("type")
    runs = int(oc.get("runs", 0))
    if otype == "wicket":
        how = (oc.get("wicket_type") or "").lower()
        key = {"bowled": "wicket_bowled", "lbw": "wicket_lbw",
               "stumped": "wicket_stumped", "run out": "wicket_runOut",
               "caught behind": "wicket_caught_keeper"}.get(how,
                                                            "wicket_caught_fielder")
    elif otype == "extra":
        key = {"Wide": "wide", "No Ball": "no_ball"}.get(
            oc.get("extra_type"), "extras")
    else:
        key = _COMMENTARY_KEYS.get(runs, "general")
    try:
        from services.commentary_service import (
            _fallback_commentary, DEFAULT_FIELDER, DEFAULT_KEEPER)
        line = _fallback_commentary(key, batter_name, bowler_name,
                                    DEFAULT_FIELDER, DEFAULT_KEEPER, runs)
        if line:
            return line
    except Exception:
        logger.exception("Super Over: commentary lookup failed")
    if otype == "wicket":
        return f"OUT! {batter_name} departs — {oc.get('wicket_type', 'gone')}."
    if otype == "extra":
        return f"{oc.get('extra_type', 'Extra')} from {bowler_name}."
    return f"{runs} run(s)."


def _crowd_line(so, inn, otype, runs, out):
    """One line of crowd/situation colour to sit under the score.

    The Super Over is six balls long; without this the chat reads like a table
    of numbers. It keys off what just happened AND the state of the chase, so a
    six with two needed off one lands differently from a six in a stroll.
    """
    if out:
        return "🔴 <i>Stunned silence, then a roar. That is a huge wicket.</i>"
    if otype == "run" and runs == 6:
        return "🚀 <i>INTO THE STANDS! The whole ground is on its feet!</i>"
    if otype == "run" and runs == 4:
        return "💨 <i>Races away to the fence — the pressure just flipped.</i>"
    if otype == "run" and runs == 0:
        return "😤 <i>Dot ball. You can hear the tension from here.</i>"
    if otype == "extra":
        return "😬 <i>Nerves from the bowler — free runs in a Super Over!</i>"
    if so["innings_no"] == 2 and so.get("target"):
        need = max(0, so["target"] - inn["runs"])
        left = max(0, SO_LEGAL_BALLS - inn["legal"])
        if left == 1 and need <= 6:
            return f"🚨 <i>It all comes down to the final ball — {need} to win.</i>"
        if need <= 2 and left >= 2:
            return "😅 <i>Almost home now.</i>"
    return "🏟 <i>The crowd holds its breath…</i>"


# ── Ball resolution (the main match engine, unchanged rules) ───────────

def _required_rate(target, runs, legal):
    """Runs per over still needed — feeds the traits that key off a chase
    (Clutch, Finisher, Death, Yorker), so they fire in a Super Over too."""
    if not target:
        return 0.0
    balls_left = max(0, SO_LEGAL_BALLS - legal)
    if balls_left <= 0:
        return 0.0
    return max(0.0, (target - runs) * 6.0 / balls_left)


def _super_over_ball(batter, bowler, pitch, *, batter_runs=0, balls_faced=0,
                     bat_traits=None, bowl_traits=None, free_hit=False,
                     legal=0, target=None, runs=0, shot=None,
                     variation=None, length=None, delivery_repeat=0):
    """Resolve ONE Super Over delivery — a single ball of /playmatch.

    Pure (no Telegram, no state mutation) so the balance can be simulated and
    tested directly.

    This is the /playmatch per-ball engine, which is what the Super Over spec
    asks for and what makes the captains' choices real:

      • the bowler's delivery/length and the batter's shot are inputs to the
        outcome, not decoration — picking the wrong shot for the ball costs you;
      • ratings drive it hard (the engine's rating-differential layer plus the
        per-ball execution duel), so the better player wins more Super Overs;
      • traits are applied natively for /letsplay, whose XI dicts carry them.
        Challenge League XIs have no traits, so /cipl resolves on ratings alone
        — no branching needed, the data decides.

    Two knobs are turned up for the Super Over specifically:

      • ``SO_EXTRAS_MULT`` — nerves. Wides and no-balls are twice as likely as
        in a normal over, so a free hit is a live possibility rather than a
        curiosity;
      • ``delivery_repeat`` — bowling the same ball again and again leaks extras
        and invites the wicket (engine layer, already used by /playmatch). It
        costs the bowler to be predictable, which is the point of letting them
        choose at all.

    Returns the normalised outcome dict
    (``type``/``runs``/``extra_type``/``wicket_type``/``traits_activated``).
    """
    from services.probability_engine import calculate_outcome

    from services.bowling_service import is_spinner
    bowl_style = (bowler.get("bowl_style") or bowler.get("bowling_type")
                  or "Medium Pacer")
    trait_ctx = {
        "over": SO_ENGINE_OVER, "total_overs": SO_ENGINE_TOTAL_OVERS,
        "innings": 1,
        "rrr": _required_rate(target, runs, legal),
        "bat_balls_faced": balls_faced,
        "bat_runs": batter_runs,
        "balls_left": max(0, SO_LEGAL_BALLS - legal),
        "runs_needed": max(0, (target or 0) - runs) if target else 0,
        # A Super Over is one over of one spell, so the bowler's ball count is
        # the balls already bowled — Golden Arm fires, Spell Builder cannot.
        "bowler_balls": legal,
        "is_spin": bool(is_spinner(bowl_style)),
        "bat_rating": int(batter.get("batting_rating")
                          or batter.get("bat_rating") or 50),
        "bowl_rating": int(bowler.get("bowling_rating")
                           or bowler.get("bowl_rating") or 40),
        "bat_hand": batter.get("batting_hand") or batter.get("bat_hand"),
        "bowl_hand": bowler.get("bowl_hand") or bowler.get("bowling_hand"),
        "striker_id": batter.get("roster_id"), "bowler_id": bowler.get("roster_id"),
        "pitch": pitch,
    }
    oc = calculate_outcome(
        bowler.get("bowl_style") or bowler.get("bowling_type") or "Medium Pacer",
        bowler.get("bowl_hand") or bowler.get("bowling_hand") or "Right",
        variation, length, pitch,
        SO_ENGINE_OVER, SO_ENGINE_TOTAL_OVERS, shot,
        int(batter.get("batting_rating") or batter.get("bat_rating") or 50),
        int(bowler.get("bowling_rating") or bowler.get("bowl_rating") or 40),
        striker_traits=bat_traits or [], bowler_traits=bowl_traits or [],
        trait_ctx=trait_ctx,
        free_hit=free_hit,
        balls_faced=balls_faced, batter_runs=batter_runs,
        fielding_quality=bowler.get("fielding_rating"),
        bat_hand=batter.get("batting_hand") or batter.get("bat_hand") or "Right",
        delivery_repeat=delivery_repeat,
        extras_mult=SO_EXTRAS_MULT,
    )
    activated = oc.get("traits_activated") or []
    oc = _normalize_outcome(oc)
    oc["traits_activated"] = activated
    return oc


async def _resolve_ball(context, mid, shot):
    so = _get(context, mid)
    inn = so["inn"]
    s_rid = int(inn["striker"])
    bowler_raw = _player_by_rid(so, so["bowl_uid"], inn["bowler_rid"])
    striker_raw = _player_by_rid(so, so["bat_uid"], s_rid)

    batter = _adapt_player(striker_raw)
    bowler = _adapt_player(bowler_raw)
    streak = inn["streak"].setdefault(s_rid, {"boundaries": 0})

    # A no-ball earlier in the over grants a free hit on this delivery: only a
    # run out can dismiss the batter.
    free_hit = bool(inn.get("free_hit", False))

    # How many times in a row this exact delivery has been bowled. Six identical
    # balls is the easy Super Over to bowl and the dull one to watch, so the
    # engine's spam layer charges for it in extras and wickets.
    this_ball = (inn["pending"]["delivery"], inn["pending"]["length"])
    if this_ball == inn.get("last_ball"):
        inn["repeat"] = inn.get("repeat", 0) + 1
    else:
        inn["repeat"] = 1
    inn["last_ball"] = this_ball

    oc = _super_over_ball(
        batter, bowler, so["pitch"],
        batter_runs=inn["bat"][s_rid]["r"], balls_faced=inn["bat"][s_rid]["b"],
        bat_traits=striker_raw.get("traits"), bowl_traits=bowler_raw.get("traits"),
        free_hit=free_hit, legal=inn["legal"], target=so.get("target"),
        runs=inn["runs"], shot=shot,
        variation=inn["pending"]["delivery"], length=inn["pending"]["length"],
        delivery_repeat=inn["repeat"])

    deliv = inn["pending"]["delivery"] or "delivery"
    if inn["pending"]["length"]:
        deliv = f"{deliv} {inn['pending']['length']}"
    legal = True            # does this ball count toward the 6?
    rotate = False          # do the batters cross on this delivery?
    sym = "•"
    runs = int(oc.get("runs", 0))
    otype = oc.get("type")
    extra_type = oc.get("extra_type")
    # An engine that flags the dismissal without typing the ball a wicket (a run
    # out taken off a no-ball, say) still costs the batting side a wicket, so
    # read both and let either one stand.
    out = bool(oc.get("batter_out")) or otype == "wicket"
    wicket_counted = False

    def _credit_batter(n):
        """Runs off the bat — to the batter's tally, with the boundary counts
        the level-scores countback is decided on."""
        inn["bat"][s_rid]["r"] += n
        if n == 4:
            inn["bat"][s_rid]["4"] += 1
            streak["boundaries"] += 1
        elif n == 6:
            inn["bat"][s_rid]["6"] += 1
            streak["boundaries"] += 1

    if otype == "extra" and extra_type in ("Wide", "No Ball"):
        legal = False
        # A wide or no-ball is ONE run to the batting side plus anything run off
        # it — the engine reports only the latter (nothing at all, for a wide).
        # Without the penalty a wide was a free re-bowl that cost the bowling
        # side nothing, which in a six-ball innings is worth a match.
        conceded = 1 + runs
        inn["runs"] += conceded
        inn["bowl_runs"] += conceded
        if extra_type == "No Ball":
            inn["free_hit"] = True   # next legal ball is a free hit
            if runs:
                _credit_batter(runs)  # runs off a no-ball are the batter's
                rotate = runs % 2 == 1
            sym = "Nb" + (str(runs) if runs else "")
        else:
            sym = "Wd" + (str(runs) if runs else "")
    elif otype == "extra" and not out:
        # Leg Bye / Byes — legal delivery, runs are extras (not to the batter,
        # not against the bowler).
        inn["runs"] += runs
        inn["bat"][s_rid]["b"] += 1
        rotate = runs % 2 == 1
        # The two engines spell it "Leg Bye" and "LegByes"; accept both.
        is_leg_bye = str(extra_type or "").replace(" ", "").lower().startswith("legbye")
        sym = ("Lb" if is_leg_bye else "B") + (str(runs) if runs else "")
    elif out:
        inn["bat"][s_rid]["b"] += 1
        _credit_batter(runs)                 # completed runs before a run out
        inn["bat"][s_rid]["out"] = True
        wtype = oc.get("wicket_type") or "Out"
        inn["bat"][s_rid]["how"] = wtype
        inn["runs"] += runs
        inn["bowl_runs"] += runs
        inn["wickets"] += 1
        wicket_counted = True
        # A run out is not the bowler's wicket (matters on free-hit run-outs).
        if wtype != "Run Out":
            inn["bowl_wkts"] += 1
        # Batters who crossed before the run out swap ends, so the dismissed one
        # is not always the striker — the crease is sorted out below.
        rotate = wtype == "Run Out" and runs % 2 == 1
        sym = "W"
    else:  # run
        inn["bat"][s_rid]["b"] += 1
        _credit_batter(runs)
        inn["runs"] += runs
        inn["bowl_runs"] += runs
        rotate = runs % 2 == 1
        sym = str(runs)

    if out and not wicket_counted:
        # A dismissal off a wide/no-ball: the delivery was scored above, but the
        # wicket itself still has to land on the card and count toward the two.
        inn["bat"][s_rid]["out"] = True
        inn["bat"][s_rid]["how"] = oc.get("wicket_type") or "Run Out"
        inn["wickets"] += 1
        sym += "+W"

    if legal:
        inn["legal"] += 1
        inn["free_hit"] = False   # a legal delivery consumes the free hit
    inn["deliveries"] += 1
    inn["recent"].append(sym)

    commentary = _ball_commentary(
        oc, _name(so, so["bat_uid"], s_rid),
        _name(so, so["bowl_uid"], inn["bowler_rid"]))
    free_hit_tag = "🆓 <b>FREE HIT</b>\n" if free_hit else ""
    no_ball_tag = ("\n🆓 <i>Free hit coming up — only a run out can dismiss!</i>"
                   if (otype == "extra" and extra_type == "No Ball") else "")
    # Traits that fired on this ball (letsplay only — /cipl XIs carry none).
    trait_tag = ""
    fired = oc.get("traits_activated") or []
    if fired:
        trait_tag = "✨ <i>" + html.escape(" · ".join(str(t) for t in fired)) + "</i>\n"
    note = (f"{free_hit_tag}⚡ <b>{html.escape(deliv)}</b> → "
            f"<i>{html.escape(_name(so, so['bat_uid'], s_rid))} plays the {html.escape(shot)}</i>\n"
            f"{trait_tag}{html.escape(commentary)}{no_ball_tag}")
    inn["hype"] = _crowd_line(so, inn, otype, runs, out)

    # Batters cross on odd runs — off the bat, off byes/leg byes, and off a
    # no-ball alike.
    if rotate:
        inn["striker"], inn["non_striker"] = inn["non_striker"], inn["striker"]

    # Wicket → bring in the backup batter (if any) at the dismissed batter's end.
    innings_ended_reason = None
    new_batter = None
    if out:
        if inn["wickets"] >= SO_MAX_WICKETS:
            innings_ended_reason = "2 down"
        elif inn["backup"] is not None and not inn["backup_used"]:
            inn["backup_used"] = True
            # Replace at whichever end the dismissed batter is standing: a run
            # out with the batters crossed leaves them at the non-striker's end,
            # and leaving an out batter there is exactly how a side ends up
            # batting on with a dismissed player.
            new_batter = inn["backup"]
            inn["at_crease"].add(int(new_batter))
            if int(inn["non_striker"]) == s_rid:
                inn["non_striker"] = new_batter
            else:
                inn["striker"] = new_batter
        else:
            innings_ended_reason = "no batters left"

    # Say out loud that the next batter is walking out, and keep saying it. The
    # name silently changing on the two-batter line is easy to miss in a busy
    # chat, and the ball message is overwritten by the next delivery's prompt a
    # second later — so the walk-out went with it and read as no replacement
    # arriving at all. inn["hype"] is part of the score card, so it survives
    # into the next prompt, and the 🆕 badge stays on until they face a ball.
    if new_batter is not None:
        inn["hype"] += (
            f"\n🚶 <b>{html.escape(_name(so, so['bat_uid'], new_batter))}</b>"
            " walks out to the middle.")
        inn["last_in"] = int(new_batter)
    elif out and innings_ended_reason:
        note += "\n\n🏁 <b>Innings over</b> — no batters left."

    # Invariant, whatever the engine returned: the innings cannot go on past the
    # wicket limit or the sixth legal ball.
    if not innings_ended_reason and (inn["wickets"] >= SO_MAX_WICKETS
                                     or inn["legal"] >= SO_LEGAL_BALLS):
        innings_ended_reason = "innings complete"

    # Show the ball result.
    so["inn"]["pending"] = {"delivery": None, "length": None}
    await _show_ball(context, mid, note)

    # ── End-of-innings / chase checks ──
    if so["innings_no"] == 2:
        if inn["runs"] >= so["target"]:
            await asyncio.sleep(_BALL_PAUSE)
            await _end_innings(context, mid, chase_won=True)
            return
    # Safety valve against a pathological run of wides/no-balls never reaching
    # 6 legal balls — cap total deliveries so an innings always terminates.
    if (innings_ended_reason or inn["legal"] >= SO_LEGAL_BALLS
            or inn["deliveries"] >= SO_MAX_DELIVERIES):
        await asyncio.sleep(_BALL_PAUSE)
        await _end_innings(context, mid)
        return

    # Next ball.
    inn["stage"] = "DELIV"
    await asyncio.sleep(_BALL_PAUSE)
    await _prompt(context, mid)


async def _show_ball(context, mid, note):
    so = _get(context, mid)
    inn = so["inn"]
    text = _score_card(so, note=note)
    if inn.get("msg_id"):
        try:
            await context.bot.edit_message_text(
                text, chat_id=so["chat_id"], message_id=inn["msg_id"],
                parse_mode="HTML")
            return
        except Exception:
            pass
    sent = await context.bot.send_message(so["chat_id"], text, parse_mode="HTML")
    inn["msg_id"] = getattr(sent, "message_id", None)


# ════════════════════════════════════════════════════════════════════
# Innings / Super Over transitions
# ════════════════════════════════════════════════════════════════════

def _capture_innings_detail(so, inn, bat_uid, bowl_uid):
    """Snapshot the just-finished Super Over innings — used by both the scorecard
    image and the Mini App scorecard innings."""
    batters = []
    for rid in inn["trio"]:
        st = inn["bat"].get(int(rid), {})
        # Runs count even with no legal ball faced (a six off a no-ball), and the
        # countback is read back off these rows — so never drop a scoring batter.
        if st.get("b", 0) > 0 or st.get("r", 0) > 0 or st.get("out"):
            batters.append({
                "name": _name(so, bat_uid, rid), "runs": st.get("r", 0),
                "balls": st.get("b", 0), "fours": st.get("4", 0),
                "sixes": st.get("6", 0), "out": st.get("out", False),
                "how_out": st.get("how", "") or ("out" if st.get("out") else "not out"),
            })
    legal = inn["legal"]
    bowlers = [{"name": _name(so, bowl_uid, inn["bowler_rid"]),
                "wickets": inn["bowl_wkts"], "runs": inn["bowl_runs"],
                "overs": f"{legal // 6}.{legal % 6}", "balls": legal}]
    return {
        "team": so["teams"][bat_uid]["name"],
        "team_uid": bat_uid, "opp_uid": bowl_uid,
        "runs": inn["runs"], "wickets": inn["wickets"],
        "overs": f"{legal // 6}.{legal % 6}",
        "batters": batters, "bowlers": bowlers,
    }


def _super_over_summary(so, winner_name, margin_text):
    """Compact Super Over summary for the Mini App result screen: winner +
    each Super Over innings' total (labelled Innings 1 & 2)."""
    innings = []
    for i, d in enumerate(so.get("so_innings") or [], start=1):
        innings.append({
            "label": f"Super Over Innings {i}",
            "team": d["team"], "runs": d["runs"], "wickets": d["wickets"],
        })
    return {"winner": winner_name, "marginText": margin_text, "innings": innings}


def _super_over_mvp(so, winner_uid=None):
    """Best performer of the DECIDING Super Over (batting or bowling).

    Six balls a side is too short for the main-match Player of the Match maths,
    so this is a compact impact score: a batter earns their runs plus a bonus
    per boundary, a bowler earns a lot per wicket minus what they leaked. Ties
    go to the winning side. Returns ``(name, line)`` or ``(None, None)``.
    """
    best = None
    for d in so.get("so_innings") or []:
        team_uid = d.get("team_uid")
        opp_uid = d.get("opp_uid")
        winners_side = (winner_uid is not None and team_uid == winner_uid)
        for b in d.get("batters") or []:
            impact = (b.get("runs", 0) + b.get("fours", 0)
                      + 2 * b.get("sixes", 0))
            line = (f"{b.get('runs', 0)} ({b.get('balls', 0)}b) "
                    f"for {d.get('team', '')}")
            cand = (impact, 1 if winners_side else 0, b.get("name"), line)
            if best is None or cand[:2] > best[:2]:
                best = cand
        # The bowler's side is the one that did NOT bat in this innings.
        bowler_won = (winner_uid is not None and opp_uid == winner_uid)
        for w in d.get("bowlers") or []:
            impact = 15 * w.get("wickets", 0) + max(0, 15 - w.get("runs", 0))
            line = (f"{w.get('wickets', 0)}-{w.get('runs', 0)} "
                    f"in the Super Over")
            cand = (impact, 1 if bowler_won else 0, w.get("name"), line)
            if best is None or cand[:2] > best[:2]:
                best = cand
    if not best or not best[2]:
        return None, None
    return best[2], best[3]


def _super_over_miniapp_innings(so):
    """Build the Super Over's innings in the Mini App scorecard format so they
    show after the two main-match innings (Super Over Innings 1 & 2)."""
    out = []
    for i, d in enumerate(so.get("so_innings") or [], start=1):
        bowl_team = so["teams"].get(d.get("opp_uid"), {}).get("name", "")
        batting = [{
            "name": b["name"], "runs": b["runs"], "balls": b["balls"],
            "fours": b.get("fours", 0), "sixes": b.get("sixes", 0),
            "out": b["out"], "how_out": b.get("how_out", ""),
            "sr": round(b["runs"] * 100 / b["balls"], 1) if b["balls"] else 0,
        } for b in d["batters"]]
        bowling = [{
            "name": w["name"], "overs": w["overs"], "runs": w["runs"],
            "wickets": w["wickets"], "maidens": 0,
            "econ": round(w["runs"] / (w["balls"] / 6), 2) if w.get("balls") else 0,
        } for w in d["bowlers"]]
        out.append({
            "number": 2 + i,                       # after the two main innings
            "label": f"Super Over - Innings {i}",
            "super_over": True,
            "bat_team": d["team"], "bowl_team": bowl_team,
            "runs": d["runs"], "wickets": d["wickets"], "overs": d["overs"],
            "batting": batting, "bowling": bowling,
        })
    return out


def _innings_totals(so, uid):
    """(runs, wickets, sixes, fours) this team made in the CURRENT Super Over."""
    runs, wkts = so["score"].get(uid, (0, 0))
    sixes = fours = 0
    for d in so.get("so_innings") or []:
        if d.get("team_uid") != uid:
            continue
        for b in d.get("batters") or []:
            sixes += int(b.get("sixes", 0) or 0)
            fours += int(b.get("fours", 0) or 0)
    return runs, wkts, sixes, fours


def _decide_super_over(so, first_uid, second_uid, chase_won=False):
    """Decide the Super Over. Returns ``(winner_uid, loser_uid, how)``.

    Runs settle it about 94% of the time. A level score would otherwise mean
    replaying the whole Super Over, and with six-ball innings that lands often
    enough (~6%) to become a slog, so a level score first goes to cricket's own
    boundary countback:

        1. most sixes        2. most fours

    Both reward the side that played the better over and neither favours batting
    first or second. Wickets are deliberately NOT a rung: rewarding the side
    that lost fewer would push players toward blocking out a Super Over.

    Measured over 20,000 simulated Super Overs between equal sides, this leaves
    a replay in ~2% of them — rare enough to stay an event, common enough to
    still happen. ``winner_uid`` is None in exactly that case.
    """
    f_runs, _f_wkts, f_six, f_four = _innings_totals(so, first_uid)
    s_runs, _s_wkts, s_six, s_four = _innings_totals(so, second_uid)

    if chase_won or s_runs > f_runs:
        return second_uid, first_uid, "runs"
    if f_runs > s_runs:
        return first_uid, second_uid, "runs"
    if f_six != s_six:
        return ((first_uid, second_uid, "sixes") if f_six > s_six
                else (second_uid, first_uid, "sixes"))
    if f_four != s_four:
        return ((first_uid, second_uid, "fours") if f_four > s_four
                else (second_uid, first_uid, "fours"))
    return None, None, "tie"


async def _end_innings(context, mid, chase_won=False):
    so = _get(context, mid)
    inn = so["inn"]
    bat_uid = so["bat_uid"]
    so["score"][bat_uid] = (inn["runs"], inn["wickets"])
    so.setdefault("so_innings", []).append(
        _capture_innings_detail(so, inn, bat_uid, so["bowl_uid"]))
    # Bank the players this Super Over actually used, so a replay cannot reuse
    # them: everyone who came to the crease, and the bowler who bowled.
    so["batted"][bat_uid] |= {int(r) for r in inn.get("at_crease", ())}
    so["used_bowlers"][so["bowl_uid"]].add(int(inn["bowler_rid"]))

    if so["innings_no"] == 1:
        so["target"] = inn["runs"] + 1
        bat = so["teams"][bat_uid]
        nxt = so["teams"][so["bowl_uid"]]
        await context.bot.send_message(
            so["chat_id"],
            "🔥 <b>SUPER OVER — INNINGS 1 COMPLETE</b>\n\n"
            f"{html.escape(bat['name'])}: <b>{inn['runs']}/{inn['wickets']}</b>\n\n"
            f"🎯 Target for <b>{html.escape(nxt['name'])}</b>: <b>{so['target']}</b> run(s)\n\n"
            "🔁 <b>ROLE SWAP</b> — select your players for the chase.",
            parse_mode="HTML")
        # Swap roles for innings 2.
        so["bat_uid"], so["bowl_uid"] = so["bowl_uid"], so["bat_uid"]
        so["innings_no"] = 2
        _reset_selection(so)
        await _send_selection(context, mid)
        return

    # Innings 2 done → decide the Super Over.
    first_uid = so["first_bat_uid"]
    second_uid = bat_uid                     # batted second this super over
    winner_uid, loser_uid, how = _decide_super_over(
        so, first_uid, second_uid, chase_won=chase_won)
    if winner_uid is None:
        await _super_over_tied(context, mid)
        return
    await _finalize(context, mid, winner_uid=winner_uid, loser_uid=loser_uid,
                    decided_by=how)


async def _super_over_tied(context, mid):
    so = _get(context, mid)
    a_uid = so["first_bat_uid"]
    b_uid = next(u for u in so["teams"] if u != a_uid)
    a, b = so["teams"][a_uid], so["teams"][b_uid]
    sa, sb = so["score"][a_uid], so["score"][b_uid]
    so["results"].append({
        "n": so["so_number"],
        a_uid: sa, b_uid: sb,
    })
    n = so["so_number"]
    # Next super over: team that batted SECOND this super over bats first.
    next_first = b_uid
    so["first_bat_uid"] = next_first
    so["so_number"] = n + 1
    nf = so["teams"][next_first]
    await context.bot.send_message(
        so["chat_id"],
        "🔥 <b>SUPER OVER TIED!</b>\n\n"
        f"{html.escape(a['name'])}: {sa[0]}/{sa[1]}\n"
        f"{html.escape(b['name'])}: {sb[0]}/{sb[1]}\n\n"
        "Level on runs, sixes <i>and</i> fours — nothing can separate you.\n\n"
        f"Starting <b>Super Over {n + 1}</b>…\n"
        f"<b>{html.escape(nf['name'])}</b> will bat first (they batted second "
        "in the previous Super Over).\n\n"
        f"{_spent_players_text(so)}",
        parse_mode="HTML")
    _begin_super_over(so)
    await _send_selection(context, mid)


def _spent_players_text(so):
    """Who each side has burned so far, and so cannot pick again.

    Named out loud before the next selection screen, because the restriction is
    the whole shape of a replay: a captain needs to know their opener and their
    death bowler are gone before they start tapping, not after the buttons come
    back short.
    """
    lines = ["🚫 <b>Already used — cannot play again</b>"]
    for uid, t in so["teams"].items():
        spent = []
        for p in t["xi"]:
            rid = int(p["roster_id"])
            roles = []
            if rid in so["batted"][uid]:
                roles.append("bat")
            if rid in so["used_bowlers"][uid]:
                roles.append("bowl")
            if roles:
                spent.append(f"{html.escape(p['name'])} ({'/'.join(roles)})")
        lines.append(f"   <b>{html.escape(t['name'])}</b>: "
                     + (", ".join(spent) if spent else "nobody"))
    lines.append("<i>A nominated backup who never walked out is still available.</i>")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# Finalisation
# ════════════════════════════════════════════════════════════════════

# Phrased to read correctly straight after a team name, in the chat message,
# the image caption and on the summary card itself.
_COUNTBACK_TEXT = {
    "sixes": "won on sixes hit (scores level)",
    "fours": "won on fours hit (scores and sixes level)",
}


async def _finalize(context, mid, winner_uid, loser_uid, decided_by="runs"):
    so = _get(context, mid)
    # Append the deciding super over to the results log.
    so["results"].append({
        "n": so["so_number"],
        winner_uid: so["score"].get(winner_uid),
        loser_uid: so["score"].get(loser_uid),
    })
    win = so["teams"][winner_uid]
    lose = so["teams"][loser_uid]

    # Super Over winning margin (runs if the winner batted first and defended,
    # wickets if they chased it down). A countback win has no run margin to
    # quote, so it says what actually separated the sides instead.
    w_runs, w_wkts = so["score"].get(winner_uid, (0, 0))
    l_runs, _l_wkts = so["score"].get(loser_uid, (0, 0))
    if decided_by in _COUNTBACK_TEXT:
        margin_type, margin = decided_by, 0
        margin_text = _COUNTBACK_TEXT[decided_by]
    elif winner_uid == so["first_bat_uid"]:
        margin_type, margin = "runs", max(0, w_runs - l_runs)
        margin_text = f"won by {margin} {margin_type}"
    else:
        margin_type, margin = "wickets", max(1, 2 - w_wkts)
        margin_text = f"won by {margin} {margin_type}"
    # How the match result reads wherever a full sentence is needed.
    result_phrase = (f"{margin_text} — and the match"
                     if decided_by in _COUNTBACK_TEXT
                     else f"won the match by {margin} {margin_type}")
    so["_winner_uid"] = winner_uid
    so["_margin_text"] = margin_text

    # Reward + finalise the Match row.
    prize = None
    session = get_session()
    try:
        m = session.query(Match).get(mid)
        if m:
            m.status = "completed"
            m.completed_at = datetime.utcnow()
            m.winner_id = winner_uid
            m.loser_id = loser_uid
            m.margin_type = "super over"
            m.margin_value = 0
        try:
            from services.match_rewards import award_match_rewards_core
            overs = so["main"].get("overs") or 20
            # A /letsplay or /wpm mismatch flagged for anti stat-farming counts
            # for nothing — no coins/gems and no W/L record or streak for the
            # Super-Over-decided match (award returns zeros).
            count_result = not bool((so.get("main_state") or {}).get("stats_disabled"))
            w_coins, w_gems, l_coins, l_gems = award_match_rewards_core(
                session, winner_uid, loser_uid, overs, is_vsbot=False,
                count_result=count_result)
            prize = {"wc": w_coins, "wg": w_gems, "lc": l_coins, "lg": l_gems}
        except Exception:
            logger.exception("Super Over reward award failed (%s)", mid)
        # Snapshot the final scorecard / Arena board while the live MatchState
        # still exists (cleanup_state below removes it) so the Super-Over-decided
        # match stays viewable in the Mini App — same as the normal completion.
        try:
            from services.match_webapp_service import save_final_scorecard
            save_final_scorecard(
                session, mid,
                result_text=f"{win['name']} {result_phrase} (Super Over)",
                extra_innings=_super_over_miniapp_innings(so),
                super_over=_super_over_summary(so, win["name"], margin_text))
        except Exception:
            logger.exception("Super Over final scorecard snapshot failed (%s)", mid)
        # Record the tournament result if the main match was an official tournament
        # match — the Super Over winner is the tournament winner.
        try:
            main_state = so.get("main") or {}
            if main_state.get("tournament_id"):
                from services import tournament_service
                tournament_service.record_tournament_match(
                    session, main_state, winner_user_id=winner_uid,
                    result_text=f"{win['name']} won (Super Over)")
        except Exception:
            logger.exception("tournament Super Over recording failed (%s)", mid)
        # Record the CL Tour series result — a Super Over decides a tied tour
        # match, so the winner here is the series winner. The CL Tour tags live on
        # the saved live state under "main_state" ("main" above is only the
        # scoreline dict, which carries no cl_tour_match_id).
        try:
            cl_state = so.get("main_state") or {}
            if cl_state.get("cl_tour_match_id"):
                from services.cl_tour_service import record_cl_match_result
                # Savepoint-isolate so a flush failure can't roll back the Super
                # Over finalization committed just below.
                with session.begin_nested():
                    record_cl_match_result(
                        session, cl_state["cl_tour_match_id"], winner_uid)
        except Exception:
            logger.exception("CL tour Super Over recording failed (%s)", mid)
        # Player-of-the-Match career credit — taken from the MAIN match (same as
        # the reward text) with the Super Over winner as the winning team, so
        # /statscl's POTM(s) count reflects Super-Over-decided matches too.
        try:
            from handlers.cipl_play import _cipl_potm_select, _record_cipl_potm
            main_state = so.get("main_state") or {}
            best = _cipl_potm_select(main_state, win["name"])
            if best and best.get("player_id"):
                _record_cipl_potm(session, main_state, best)
        except Exception:
            logger.exception("Super Over POTM career credit failed (%s)", mid)
        # ── Match-end quest tracking ──
        # A tied match never reaches the ordinary finalize — it returns early to
        # start the Super Over — so this is the only place the tie-breaker's
        # match can credit quests. Both sides get the main match's events off
        # the same shared tracker, and the winner also gets 'super_over_won'.
        try:
            from services.quest_service import (
                track_user_match_quests, safe_track, match_counts_for_quests)
            from models import User as _QUser
            main_state = so.get("main_state") or {}
            for uid in (main_state.get("inn1_bat_team_id"),
                        main_state.get("inn1_bowl_team_id")):
                if not uid:
                    continue
                qu = session.query(_QUser).get(uid)
                track_user_match_quests(session, main_state, qu,
                                        uid == winner_uid, False, winner_uid)
            if match_counts_for_quests(main_state):
                safe_track(session, winner_uid, "super_over_won", 1)
        except Exception:
            logger.exception("Super Over quest tracking failed (%s)", mid)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Super Over finalisation failed (%s)", mid)
    finally:
        session.close()

    # Winner announcement.
    mvp_name, mvp_line = _super_over_mvp(so, winner_uid)
    mvp_block = ""
    if mvp_name:
        mvp_block = (f"\n\n⭐ <b>Super Over Star:</b> {html.escape(str(mvp_name))}"
                     f"\n   <i>{html.escape(str(mvp_line))}</i>")
    countback_block = ""
    if decided_by in _COUNTBACK_TEXT:
        w_six, w_four = _innings_totals(so, winner_uid)[2:]
        l_six, l_four = _innings_totals(so, loser_uid)[2:]
        countback_block = (
            f"\n\n⚖️ <b>Scores level — decided on countback</b>\n"
            f"   {html.escape(win['name'])}: {w_six}×6️⃣ {w_four}×4️⃣\n"
            f"   {html.escape(lose['name'])}: {l_six}×6️⃣ {l_four}×4️⃣")
    await context.bot.send_message(
        so["chat_id"],
        "🏆 <b>SUPER OVER RESULT</b>\n\n"
        f"{_so_scoreline(so, winner_uid)}\n"
        f"{_so_scoreline(so, loser_uid)}\n\n"
        f"🎉 {_mention(win['tg'], win['name'])} (<b>{html.escape(win['name'])}</b>) "
        "win the Super Over!\n\n"
        f"🏆 Match Winner: <b>{html.escape(win['name'])}</b>"
        f"{countback_block}"
        f"{mvp_block}",
        parse_mode="HTML")

    # Super Over scorecard image (titled "SUPER OVER").
    try:
        # Offload the CPU-bound Pillow render so it doesn't block the event loop.
        so_img = await asyncio.to_thread(_build_super_over_card, so)
        if so_img:
            await context.bot.send_photo(
                so["chat_id"], photo=BytesIO(so_img),
                caption=f"🔥 <b>Super Over</b> — {html.escape(win['name'])} {margin_text}",
                parse_mode="HTML", reply_markup=_spectate_markup(so))
    except Exception:
        logger.exception("Super Over: sending super-over card failed (%s)", mid)

    # Main scorecard image — same main-match scores, result now reads
    # "X won the match by N runs/wickets" instead of "Match Tied".
    sent_main = await _send_main_scorecard(
        context, so,
        {"tie": False, "winner": win["name"], "margin": margin,
         "margin_type": margin_type,
         # A countback win has no run/wicket margin, so hand the card the
         # finished phrase rather than letting it print "won by 0 sixes".
         "margin_text": margin_text if decided_by in _COUNTBACK_TEXT else None},
        caption=f"🏆 <b>{html.escape(win['name'])} {html.escape(result_phrase)}</b>")
    if not sent_main:
        # Image unavailable — fall back to the text combined scorecard (scores
        # only; prize/POTM live in the reward message below).
        await context.bot.send_message(
            so["chat_id"], _final_scorecard(so, win, None), parse_mode="HTML")

    # Final match-reward message — ALWAYS sent (independent of whether the images
    # rendered) so the coins/gems from award_match_rewards_core and the Player of
    # the Match are never lost. Carries the spectate button like the cards.
    await context.bot.send_message(
        so["chat_id"], _final_reward_text(so, win, lose, prize, margin_text),
        parse_mode="HTML", reply_markup=_spectate_markup(so),
        disable_web_page_preview=True)

    # Clean up the live match state and the Super Over working state.
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(context, mid)
    except Exception:
        logger.exception("Super Over cleanup failed (%s)", mid)
    context.bot_data.pop(_so_key(mid), None)


def _so_scoreline(so, uid):
    t = so["teams"][uid]
    sc = so["score"].get(uid, (0, 0))
    return f"{html.escape(t['name'])}: <b>{sc[0]}/{sc[1]}</b>"


def _final_reward_text(so, win, lose, prize, margin_text):
    """Final reward message — result + Player of the Match + coins/gems. Sent
    after the Super Over winner is decided, regardless of image rendering, so
    the rewards (award_match_rewards_core) and POTM are never shown silently."""
    lines = [
        "🏆 <b>MATCH RESULT</b>",
        f"{_mention(win['tg'], win['name'])} (<b>{html.escape(win['name'])}</b>) "
        f"won the match {html.escape(margin_text)} <i>(Super Over)</i>.",
    ]
    # Player of the Match — taken from the MAIN match performance (like /cipl).
    try:
        from handlers.cipl_play import _cipl_calc_potm
        potm_name, potm_stats, potm_team = _cipl_calc_potm(
            so.get("main_state"), win["name"])
    except Exception:
        logger.exception("Super Over: POTM calc failed (%s)", so.get("mid"))
        potm_name, potm_stats, potm_team = None, None, None
    if potm_name:
        team_suffix = f" ({html.escape(potm_team)})" if potm_team else ""
        lines += ["", f"🌟 <b>Player of the Match:</b> {html.escape(potm_name)}{team_suffix}"]
        if potm_stats:
            lines.append(f"   <i>{html.escape(str(potm_stats))}</i>")
    if prize:
        lines += [
            "", "💰 <b>Prizes</b>",
            f"🏆 {html.escape(win['name'])}: +{prize['wc']:,} coins, +{prize['wg']} 💎",
            f"🤝 {html.escape(lose['name'])}: +{prize['lc']:,} coins, +{prize['lg']} 💎",
        ]
    return "\n".join(lines)


def _final_scorecard(so, win, prize):
    m = so["main"]
    lines = [
        "🏏 <b>MATCH RESULT</b>", "",
        f"{html.escape(m['team1_name'])}: {m['team1_runs']}/{m['team1_wkts']} "
        f"({m['overs']} ov)",
        f"{html.escape(m['team2_name'])}: {m['team2_runs']}/{m['team2_wkts']} "
        f"({m['overs']} ov)",
        "", "<i>Main match tied.</i>", "",
        "🔥 <b>SUPER OVER</b>",
    ]
    for r in so["results"]:
        uids = [k for k in r if k != "n"]
        lines.append(f"\n<b>Super Over {r['n']}:</b>")
        for uid in uids:
            sc = r.get(uid) or (0, 0)
            lines.append(f"  {html.escape(so['teams'][uid]['name'])}: {sc[0]}/{sc[1]}")
    lines += ["", f"🏆 <b>Winner: {html.escape(win['name'])}</b>",
              f"Result: {html.escape(win['name'])} won by Super Over."]
    if prize:
        lines += ["",
                  f"💰 +{prize['wc']:,} coins, +{prize['wg']} 💎 to the winner."]
    return "\n".join(lines)
