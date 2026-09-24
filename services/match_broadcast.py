"""Chat-side helpers for Mini-App matches: the post-toss launch message,
the live scorecard broadcast, and the "Play Match" button.

Launch scheme:
  • Private chats → a Telegram Web App button opening
        https://<host>/cricket?match_id=<id>&chat_id=<chat>
  • Group chats   → a deep link with startapp=cricket_<matchId>_<chatId>
    (Web App keyboard buttons aren't allowed in groups, so a t.me deep link
    is used; the Mini App reads start_param and routes to the match.)

Backward-compatible: the Mini App still understands the older lm_/sc_ forms.

The two cards this module posts into the chat — "Match Ready" and the live
scorecard — each have a Bot API 10.1 block rendering beside the HTML one. A
scorecard is columns (batsman, runs, balls; bowler, wickets, runs), which a
proportional font cannot align, so the blocks version makes them a native
table; ``services/rich_message.py`` falls back to the HTML whenever the rich
send is refused. See ``docs/rich-text-messages.md``.
"""

import asyncio
import logging
import os
import random
import re

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from services import rich_message as R

logger = logging.getLogger(__name__)

# The mentions handed to the Match Ready card are already HTML anchors. A block
# tree cannot carry a tag, so they are stripped back to the visible name.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# The anchor those mentions use, so the block rendering can rebuild it as a
# real mention node instead of flattening a ping into plain text.
_TG_MENTION_RE = re.compile(
    r'<a\s+href="tg://user\?id=(\d+)"\s*>(.*?)</a>', re.I | re.S)


def _plain(value):
    """An HTML fragment as the text a reader sees, or ``—`` when it is empty."""
    return _HTML_TAG_RE.sub("", str(value or "")).strip() or "—"


def _mention_node(value):
    """An HTML mention as a rich-message node, keeping the link where there is one.

    Callers build these mentions as ``<a href="tg://user?id=…">`` strings, which
    a block tree cannot carry. Reading the id back out means the card still
    pings the captain it names, rather than degrading to a plain name.
    """
    match = _TG_MENTION_RE.search(str(value or ""))
    if match:
        return R.mention(_HTML_TAG_RE.sub("", match.group(2)).strip() or "—",
                         match.group(1))
    return _plain(value)


def _retry_after_seconds(exc):
    """Flood-control wait from a RetryAfter, as float seconds.

    ``RetryAfter.retry_after`` is a number in python-telegram-bot < 22.2 but can
    be a ``datetime.timedelta`` in newer versions (opt-in via PTB_TIMEDELTA).
    Since this repo only pins ``>=21.3``, normalise either form so adding jitter
    never raises ``TypeError`` and abandons the flood wait.
    """
    ra = getattr(exc, "retry_after", 1)
    if hasattr(ra, "total_seconds"):
        ra = ra.total_seconds()
    try:
        return float(ra)
    except (TypeError, ValueError):
        return 1.0


def _webapp_host():
    """Base https host for the Mini App, from WEBAPP_URL (strip trailing path)."""
    url = (os.getenv("WEBAPP_URL", "") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        return None
    # WEBAPP_URL may already point at /webapp; reduce to scheme+host.
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def _cricket_webapp_url(match_id, chat_id):
    """Direct Mini App URL with match + chat in the query string."""
    host = _webapp_host()
    if not host:
        return None
    return f"{host}/cricket?match_id={match_id}&chat_id={chat_id}"


def _cricket_deep_link(match_id, chat_id):
    """Group deep link: startapp=cricket_<matchId>_<chatId>."""
    from services.miniapp_buttons import miniapp_deep_link
    return miniapp_deep_link(f"cricket_{match_id}_{chat_id}")


def play_match_url(match_id, chat_id=None):
    """Public group-style launch URL for opening/spectating a live match.

    Uses the cricket_<matchId>_<chatId> deep-link scheme when the originating
    chat id is known, so Telegram users land on the same live board as the
    original /wpm or /cm match.
    """
    return _launch_url(match_id, chat_id)


def _launch_url(match_id, chat_id=None):
    """Group-style deep link (kept for the scorecard/result broadcasts, which
    always post to a group chat). Uses the new cricket_ scheme when chat_id is
    known, else falls back to the match-only form."""
    if chat_id is not None:
        link = _cricket_deep_link(match_id, chat_id)
        if link:
            return link
    return _cricket_deep_link(match_id, 0)


def play_match_keyboard(match_id, chat_id=None, is_private=False, label=None):
    """'Play Match' button.

    • Private chat → Telegram Web App button → /cricket?match_id&chat_id
      (opens the Mini App in place; user id resolves from Telegram initData).
    • Group chat → t.me deep link with startapp=cricket_<matchId>_<chatId>.

    The Mini App detects each user's role (batsman / bowler / spectator) from
    the live state, so one button routes everyone correctly.
    """
    if is_private:
        wa_url = _cricket_webapp_url(match_id, chat_id if chat_id is not None else 0)
        if wa_url:
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(label or "🎮 Play Match",
                                     web_app=WebAppInfo(url=wa_url))
            ]])
        # Fall through to deep link if no host configured
    url = _launch_url(match_id, chat_id)
    if not url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label or "🎮 Play Match (Mini App)", url=url)
    ]])


# ════════════════════════════════════════════════════════════════════
# Shared coin-toss UI (heads/tails call → animated flip → result)
# ════════════════════════════════════════════════════════════════════

# Keep the flip short and snappy — a long animation (many frames + a long sleep
# per frame) means several sequential Telegram edit round-trips before the
# result lands, which players read as "the toss is stuck". Three quick frames is
# enough to feel like a coin flip while showing the winner fast.
COIN_TOSS_FRAMES = [
    "🪙 <b>TOSS</b>\n\n     ⬆️\n   ╱  🪙  ╲\n\n<i>The coin is in the air…</i>",
    "🪙 <b>TOSS</b>\n\n     🌀 🪙 🌀\n\n<i>Tumbling end over end…</i>",
    "🪙 <b>TOSS</b>\n\n          ⬇️\n        🪙\n\n<i>Coming down now!</i>",
]

# Seconds to hold each animation frame before editing to the next one.
COIN_TOSS_FRAME_DELAY = 0.25


def coin_call_keyboard(heads_cb, tails_cb, prompt_owner=None):
    """Heads/Tails call buttons. heads_cb/tails_cb are full callback_data."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪙 Heads", callback_data=heads_cb),
        InlineKeyboardButton("🌑 Tails", callback_data=tails_cb),
    ]])


async def run_coin_toss(edit_fn, call_side):
    """Animate a coin toss and return (coin_side, won).

    edit_fn: async callable taking the HTML string to display each frame.
    call_side: 'heads' or 'tails' — what the calling captain chose.

    Animation-frame edits are best-effort: a dropped frame is cosmetic, so we
    swallow ordinary errors. Flood control (RetryAfter) is the exception — if we
    keep firing edits into a throttled chat the *result* reveal that follows is
    the one that gets dropped, leaving the toss frozen mid-flip. Honour the
    requested wait so the burst drains before the caller reveals the winner.
    """
    for fr in COIN_TOSS_FRAMES:
        try:
            await edit_fn(fr)
        except RetryAfter as e:
            await asyncio.sleep(_retry_after_seconds(e) + 0.5)
        except Exception:
            pass
        await asyncio.sleep(COIN_TOSS_FRAME_DELAY)
    coin = random.choice(["heads", "tails"])
    return coin, (coin == call_side)


async def reveal_toss_result(edit_fn, attempts=4):
    """Run the final winner-reveal edit, retrying so it reliably lands.

    The reveal is the one edit that MUST render: if it silently fails the toss
    looks frozen on a mid-flip animation frame ("Tumbling end over end…") and
    players are stuck with no Bat/Bowl buttons. Telegram flood control or a
    transient network blip on this single edit is exactly what causes that, so
    retry a few times (honouring RetryAfter) and report whether it rendered.

    edit_fn: async callable performing the reveal edit (takes no arguments).
    Returns True if an edit succeeded, False if every attempt failed — callers
    use that to recover the toss instead of leaving it permanently stuck.
    """
    for i in range(attempts):
        try:
            await edit_fn()
            return True
        except RetryAfter as e:
            await asyncio.sleep(_retry_after_seconds(e) + 0.5)
        except BadRequest as e:
            # NB: BadRequest subclasses NetworkError in PTB, so it must be caught
            # before the NetworkError clause below.
            if "not modified" in str(e).lower():
                # A previous attempt's edit landed even though the client never
                # saw the ack — the reveal is already on screen, so this is a
                # success, not a failure that would strand the Bat/Bowl buttons.
                return True
            logger.warning("toss reveal BadRequest: %s", e)
            await asyncio.sleep(0.5 * (i + 1))
        except (TimedOut, NetworkError):
            # The edit may actually have reached Telegram before the client gave
            # up; the next attempt re-sends the identical reveal and finds it
            # already applied — handled by the "not modified" branch above.
            await asyncio.sleep(0.5 * (i + 1))
        except Exception:
            logger.exception("toss reveal edit failed")
            await asyncio.sleep(0.5 * (i + 1))
    return False


async def send_match_ready_message(context, chat_id, match, bat_team, bowl_team,
                                   bat_mention, bowl_mention, rules_note=None,
                                   toss_note=None, traits_note=None):
    """Post the 'Match Ready' card with all details + the Play Match button.

    ``traits_note`` carries the result of the pre-toss Trait Vote
    (services.trait_vote_service) when the mode runs one, so the answer both
    captains gave is on the card they open the match from — nobody has to
    remember it, or scroll back for it, when a trait does or does not fire.
    """
    # A private chat with the bot uses a positive user-id chat_id; groups are
    # negative. Web App buttons only work in private chats, so pick the right
    # button type for the chat we're posting into.
    is_private = isinstance(chat_id, int) and chat_id > 0
    kb = play_match_keyboard(match.id, chat_id=chat_id, is_private=is_private)
    text = (
        "🏏 <b>MATCH READY!</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏟️ <b>Venue:</b> {match.stadium or 'Neutral'}\n"
        f"🌤️ <b>Pitch:</b> {match.pitch_type or 'Balanced'}\n"
        f"⏱️ <b>Overs:</b> {match.overs}\n"
        + (f"🪙 <b>Toss:</b> {toss_note}\n" if toss_note else "")
        + (f"🎯 <b>Rules:</b> {rules_note}\n" if rules_note else "")
        + (f"{traits_note}\n" if traits_note else "") +
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏏 <b>Batting first:</b> {bat_team}\n   {bat_mention}\n"
        f"🎳 <b>Bowling first:</b> {bowl_team}\n   {bowl_mention}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Tap <b>Play Match</b> to open the game board.\n"
        "• Batting side → pick openers & play shots\n"
        "• Bowling side → pick bowler & deliver\n"
        "• Everyone else → spectate live 👁"
    )
    if kb is None:
        text += ("\n\n⚠️ <i>Mini App link unavailable — set BOT_USERNAME"
                 " (and MINIAPP_NAME) to enable.</i>")
    blocks = _match_ready_blocks(match, bat_team, bowl_team, bat_mention,
                                 bowl_mention, rules_note, toss_note,
                                 traits_note, has_button=kb is not None)
    try:
        await R.send_rich_message(context.bot, chat_id, blocks, text,
                                  reply_markup=kb)
    except Exception:
        logger.exception("send_match_ready_message failed")


def _match_ready_blocks(match, bat_team, bowl_team, bat_mention, bowl_mention,
                        rules_note, toss_note, traits_note, *, has_button):
    """The Match Ready card as blocks — the twin of the HTML above.

    The mentions arrive already rendered as HTML anchors (every caller builds
    them that way), so they are stripped back to their visible text here: a
    block tree cannot carry a tag, and a name that reads ``<a href=…>`` on the
    card is worse than one that does not ping.
    """
    try:
        facts = [
            [R.cell(R.bold("🏟️ Venue")), R.cell(match.stadium or "Neutral")],
            [R.cell(R.bold("🌤️ Pitch")), R.cell(match.pitch_type or "Balanced")],
            [R.cell(R.bold("⏱️ Overs")), R.cell(str(match.overs))],
        ]
        if toss_note:
            facts.append([R.cell(R.bold("🪙 Toss")), R.cell(_plain(toss_note))])
        if rules_note:
            facts.append([R.cell(R.bold("🎯 Rules")), R.cell(_plain(rules_note))])
        if traits_note:
            facts.append([R.cell(R.bold("💎 Traits")),
                          R.cell(_plain(traits_note))])

        blocks = [
            R.heading("🏏 MATCH READY!", size=2),
            R.table(facts, bordered=True, compact=True),
            R.table([
                [R.cell("🏏"), R.cell(R.bold(bat_team)),
                 R.cell(_mention_node(bat_mention), align="right"),
                 R.cell(R.italic("bats first"), align="right")],
                [R.cell("🎳"), R.cell(R.bold(bowl_team)),
                 R.cell(_mention_node(bowl_mention), align="right"),
                 R.cell(R.italic("bowls first"), align="right")],
            ], bordered=True, striped=True, compact=True),
            R.paragraph(["Tap ", R.bold("Play Match"),
                         " to open the game board."]),
            R.list_block([
                "Batting side → pick openers & play shots",
                "Bowling side → pick bowler & deliver",
                "Everyone else → spectate live 👁",
            ]),
        ]
        if not has_button:
            blocks.append(R.footer(R.italic(
                "⚠️ Mini App link unavailable — set BOT_USERNAME (and "
                "MINIAPP_NAME) to enable.")))
        return blocks
    except Exception:
        logger.exception("match ready blocks failed to build")
        return None


def build_live_scorecard_text(state, waiting_for_mention=None):
    """Build the improved LIVE SCORECARD broadcast text for the chat."""
    bat_team = state.get("bat_team_name", "Batting")
    runs = state.get("total_runs", 0)
    wkts = state.get("total_wickets", 0)
    over = max(0, state.get("current_over", 1) - 1)
    ball = state.get("current_ball", 0)
    overs_limit = state.get("overs", 0)

    order = state.get("batting_order", [])
    si = state.get("striker_idx", 0)
    nsi = state.get("non_striker_idx", 1)
    bat_stats = state.get("bat_stats", {})

    def _bat_line(idx, on_strike):
        if idx is None or idx < 0 or idx >= len(order):
            return None
        p = order[idx]
        st = bat_stats.get(p["roster_id"], {})
        arrow = "👉 " if on_strike else "• "
        return f"{arrow}{p['name']} : {st.get('runs', 0)} ({st.get('balls', 0)}b)"

    bowler = state.get("current_bowler") or {}
    bws = state.get("bowl_stats", {}).get(bowler.get("roster_id"), {}) if bowler else {}
    b_overs_done = bws.get("overs_done", 0)
    b_this = bws.get("this_over_balls", 0)
    b_ov = f"{b_overs_done}.{b_this}" if b_this else f"{b_overs_done}"

    lines = [
        "🏏 <b>LIVE SCORECARD</b>",
        "══════════════════════════════",
        f"• Batting: {bat_team}",
        f"• Score: {runs}/{wkts} in {over}.{ball}/{overs_limit} overs",
        "══════════════════════════════",
        "🪓 <b>Batsmen:</b>",
    ]
    s_line = _bat_line(si, True)
    n_line = _bat_line(nsi, False)
    if s_line:
        lines.append(s_line)
    if n_line:
        lines.append(n_line)
    lines.append("")
    lines.append("🎳 <b>Bowler:</b>")
    if bowler:
        lines.append(f"• {bowler.get('name', '?')} : "
                     f"{bws.get('wickets', 0)}-{bws.get('runs', 0)} ({b_ov} ov)")
    else:
        lines.append("• —")
    if state.get("innings") == 2 and state.get("target"):
        from services.match_engine import chase_requirements
        chase = chase_requirements(state)
        if chase:
            lines.append(
                f"🎯 Need {chase['runs_required']} runs from "
                f"{chase['balls_remaining']} balls"
            )
    lines.append("══════════════════════════════")
    if waiting_for_mention:
        lines.append(f"🎳 <b>Waiting for {waiting_for_mention} to deliver…</b>")

    return "\n".join(lines)


def build_live_scorecard_blocks(state, waiting_for_mention=None):
    """The broadcast scorecard as blocks — the twin of the text above.

    Same four sections in the same order (who is batting and on what score, the
    pair at the crease, the bowler, and the chase when there is one), with the
    two player sections as native tables: their numbers are columns, and the
    text version can only pad them and hope.
    """
    try:
        bat_team = state.get("bat_team_name", "Batting")
        runs = state.get("total_runs", 0)
        wkts = state.get("total_wickets", 0)
        over = max(0, state.get("current_over", 1) - 1)
        ball = state.get("current_ball", 0)
        overs_limit = state.get("overs", 0)

        blocks = [R.heading("🏏 LIVE SCORECARD", size=3),
                  R.table([
                      [R.cell(R.bold("Batting")), R.cell(bat_team)],
                      [R.cell(R.bold("Score")),
                       R.cell(R.bold(f"{runs}/{wkts}"))],
                      [R.cell(R.bold("Overs")),
                       R.cell(f"{over}.{ball}/{overs_limit}")],
                  ], bordered=True, compact=True)]

        order = state.get("batting_order", [])
        bat_stats = state.get("bat_stats", {})
        rows = [[R.cell(R.bold("BATSMAN"), header=True),
                 R.cell(R.bold("R"), header=True, align="right"),
                 R.cell(R.bold("B"), header=True, align="right")]]
        for idx, on_strike in ((state.get("striker_idx", 0), True),
                               (state.get("non_striker_idx", 1), False)):
            if idx is None or idx < 0 or idx >= len(order):
                continue
            player = order[idx]
            st = bat_stats.get(player["roster_id"], {})
            name = f"{'👉 ' if on_strike else ''}{player['name']}"
            rows.append([R.cell(R.bold(name) if on_strike else name),
                         R.cell(R.bold(str(st.get("runs", 0))), align="right"),
                         R.cell(str(st.get("balls", 0)), align="right")])
        if len(rows) > 1:
            blocks.append(R.table(rows, bordered=True, compact=True,
                                  caption=R.bold("🪓 Batsmen")))

        bowler = state.get("current_bowler") or {}
        if bowler:
            bws = state.get("bowl_stats", {}).get(bowler.get("roster_id"), {})
            done = bws.get("overs_done", 0)
            this_over = bws.get("this_over_balls", 0)
            blocks.append(R.table([
                [R.cell(R.bold("BOWLER"), header=True),
                 R.cell(R.bold("W-R"), header=True, align="right"),
                 R.cell(R.bold("OV"), header=True, align="right")],
                [R.cell(bowler.get("name", "?")),
                 R.cell(R.bold(f"{bws.get('wickets', 0)}-{bws.get('runs', 0)}"),
                        align="right"),
                 R.cell(f"{done}.{this_over}" if this_over else str(done),
                        align="right")],
            ], bordered=True, compact=True, caption=R.bold("🎳 Bowler")))

        if state.get("innings") == 2 and state.get("target"):
            from services.match_engine import chase_requirements
            chase = chase_requirements(state)
            if chase:
                blocks.append(R.pullquote(
                    ["🎯 Need ", R.bold(str(chase["runs_required"])), " from ",
                     R.bold(str(chase["balls_remaining"])), " balls"],
                    caption="To win"))
        if waiting_for_mention:
            blocks.append(R.footer(
                ["🎳 ", R.bold(f"Waiting for {waiting_for_mention} to deliver…")]))
        return blocks
    except Exception:
        logger.exception("live scorecard blocks failed to build")
        return None


async def broadcast_scorecard(context, match_id, state, waiting_for_mention=None):
    """Send the live scorecard + Play Match button to the match chat."""
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    text = build_live_scorecard_text(state, waiting_for_mention)
    blocks = build_live_scorecard_blocks(state, waiting_for_mention)
    kb = play_match_keyboard(match_id)
    try:
        await R.send_rich_message(context.bot, chat_id, blocks, text,
                                  reply_markup=kb)
    except Exception:
        logger.exception("broadcast_scorecard failed")
