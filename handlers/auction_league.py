"""/auctionleague — the solo Auction League career, played in a DM.

Pick a franchise in a Challenge League, retain up to three, bid against nine
AI franchises in a set-by-set auction, then play the season — a round robin
and the IPL playoffs — against them. The rules and the AI live in
``services.auction_league_service``; this module is the Telegram face of it.

Commands (all DM-only):
  /auctionleague /al /rcpl   the hub — start, continue or review a career;
                             /rcpl IPL goes straight to that league's teams
  /simset [n]                simulate the rest of this set (or up to set n)
  /simtolast /alsim          simulate the rest of the auction
  /alsets  /alsquad  /alpurse  /altable  /alfixtures  /alstats
  /alplay                    play your next fixture
  /skipplayer                simulate the player on the block
  /alpause /alresume         pause and resume the auction
  /alquit /aldiscard         discard the career (needed before a new one)

Every button carries the lot's sequence number, so a stale Bid button from an
earlier lot is refused instead of bidding on whoever is on the block now. One
asyncio lock per player serialises taps, the lot timer and the commands.
"""

import asyncio
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import auction_league_service as AL
from services.auction_league_service import AuctionLeagueError, money
from services.telegram_user_service import sync_telegram_user
from utils.message_chunks import chunk_blocks

logger = logging.getLogger(__name__)

LOT_SECONDS = 20
# What starting a career costs — paid once, when you tap ✅ Start career.
ENTRY_FEE_GEMS = 100
HELP_LINE = "❓ Need help? Join @Cmugames or ask admin @lost_in_space14"
DIFFICULTIES = ("easy", "normal", "hard")
DIFF_LABEL = {"easy": "🟢 Easy", "normal": "🟡 Normal", "hard": "🔴 Hard"}


def _esc(s):
    return escape(str(s if s is not None else ""))


def _lock(context, tg_id):
    locks = context.bot_data.setdefault("al_locks", {})
    lock = locks.get(tg_id)
    if lock is None:
        lock = locks[tg_id] = asyncio.Lock()
    return lock


def _kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                 for row in rows if row])


async def _send(context, chat_id, text, rows=None):
    try:
        return await context.bot.send_message(
            chat_id, text, parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=_kb(rows) if rows else None)
    except Exception:
        logger.exception("auction league: send failed")
        return None


async def _send_long(context, chat_id, blocks, header="", rows=None):
    parts = chunk_blocks(blocks, header=header)
    sent = None
    for i, part in enumerate(parts):
        sent = await _send(context, chat_id, part, rows if i == len(parts) - 1 else None)
    return sent


async def _require_dm(update, context):
    """True in a private chat; elsewhere posts a pointer to the DM and returns False."""
    chat = update.effective_chat
    if chat is not None and getattr(chat, "type", "private") == "private":
        return True
    username = getattr(context.bot, "username", None) or ""
    markup = None
    if username:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏏 Open in DM", url=f"https://t.me/{username}")]])
    msg = update.effective_message
    if msg is not None:
        try:
            await msg.reply_text(
                "🔒 <b>Auction League</b> is a solo career — it's played in a "
                "private chat with me. Open the DM and send /auctionleague.",
                parse_mode="HTML", reply_markup=markup)
        except Exception:
            logger.exception("auction league: DM pointer failed")
    return False


# ════════════════════════════════════════════════════════════════════
# Save access
# ════════════════════════════════════════════════════════════════════

class _Career:
    """A loaded save: the row, its state and the session that holds them."""

    def __init__(self, session, row):
        self.session = session
        self.row = row
        self.state = AL.load(row) if row is not None else None

    def save(self):
        AL.store(self.row, self.state)
        self.session.commit()


def _open(tg_id, *, include_completed=False):
    session = get_session()
    row = (AL.latest_save(session, tg_id) if include_completed
           else AL.active_save(session, tg_id))
    return _Career(session, row)


# ════════════════════════════════════════════════════════════════════
# Renderers
# ════════════════════════════════════════════════════════════════════

def _role(c):
    return AL.ROLE_SHORT[c["category"]]


def _card_line(c, price=None, how=None):
    tag = " ✈️" if c.get("is_overseas") else ""
    extra = ""
    if price is not None:
        extra = f" — {money(price)}"
        if how == AL.HOW_RETAINED:
            extra += " (R)"
        elif how == AL.HOW_RTM:
            extra += " (RTM)"
        elif how == AL.HOW_AUTOFILL:
            extra += " (fill)"
    return f"{_esc(c['name'])} · {c['rating']} {_role(c)}{tag}{extra}"


def _short(state, team):
    return state["teams"][team]["short"]


def _squad_text(state, team):
    t = state["teams"][team]
    by_pid = {e["pid"]: e for e in t["squad"]}
    counts = AL.role_counts(state, team)
    lines = [f"📋 <b>{_esc(team)}</b> — {len(t['squad'])}/{AL.SQUAD_MAX} · "
             f"purse {money(t['purse'])}"]
    lines.append(" · ".join(f"{AL.ROLE_SHORT[r]} {counts[r]}" for r in AL.ROLES)
                 + (f" · ✈️ {AL.overseas_count(state, team)}/{state['overseas_cap']}"
                    if state.get("overseas_cap") else ""))
    lines.append(f"Still needed: {AL.needs_line(state, team)}")
    for role in AL.ROLES:
        cards = [c for c in AL.squad_card_dicts(state, team) if c["category"] == role]
        if not cards:
            continue
        lines.append(f"\n<b>{AL.ROLE_PLURAL[role]}</b>")
        for c in cards:
            e = by_pid[c["id"]]
            lines.append("• " + _card_line(c, e["price"], e["how"]))
    return "\n".join(lines)


def _purses_text(state):
    lines = ["💰 <b>Purses</b>", "<pre>"]
    for name in state["team_order"]:
        t = state["teams"][name]
        me = "◀" if name == state["user_team"] else ""
        lines.append(f"{t['short']:<5} {money(t['purse']):>11}  {len(t['squad']):>2}/18 "
                     f"RTM {t['rtm']} {me}")
    lines.append("</pre>")
    return "\n".join(lines)


def _sets_text(state):
    icon = {"done": "✅", "live": "🔨", "queued": "⏳"}
    lines = ["📦 <b>Auction sets</b>"]
    for n, name, size, status in AL.sets_overview(state):
        lines.append(f"{icon[status]} {n}. {_esc(name)} ({size})")
    lines.append("\n<code>/simset</code> sims the rest of the live set · "
                 "<code>/simset 5</code> sims up to set 5")
    return "\n".join(lines)


def _lot_text(state, lot, note=""):
    c = AL.card(state, lot["pid"])
    me = state["user_team"]
    t = state["teams"][me]
    s = AL.current_set(state)
    pos = f"{state['lot_idx'] + 1}/{len(s['pids'])}" if s else ""
    ex = f" · ex-{_esc(_short(state, c['team']))}" if c.get("team") in state["teams"] else ""
    os_ = " · ✈️ overseas" if c.get("is_overseas") else ""
    lines = []
    if note:
        lines += [note, ""]
    lines += [
        f"📦 <b>Set {lot['set_no']}: {_esc(lot['set_name'])}</b>  <i>({pos})</i>",
        "",
        f"🏏 <b>{_esc(c['name'])}</b>",
        f"{c['category']} · <b>{c['rating']}</b> OVR (Bat {c['bat_rating']} / Bowl {c['bowl_rating']})"
        f"{os_}{ex}",
        f"Base price: {money(lot['base'])}",
    ]
    if lot.get("price") is None:
        lines.append("💰 No bids yet")
    else:
        lines.append(f"💰 <b>{money(lot['price'])}</b> — "
                     f"<b>{_esc(state['teams'][lot['leader']]['name'])}</b>")
    trail = [f"{_esc(_short(state, n))} {money(p)}" for n, p in lot.get("trail") or []][-4:]
    if len(trail) > 1:
        lines.append("📈 " + " → ".join(trail))
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"👛 Purse {money(t['purse'])} · max bid {money(AL.max_bid(state, me, c))}",
        f"👥 Squad {len(t['squad'])}/{AL.SQUAD_MAX} · needs: {AL.needs_line(state, me)}",
    ]
    if lot.get("status") == "rtm":
        lines += ["", f"🔁 <b>Right To Match!</b> {_esc(c['name'])} was yours. "
                      f"Match {money(lot['price'])} and keep him?"]
    mode = "⚡ Fast — the AI teams bid among themselves first" if state.get("fast") \
        else "🐢 Bid by bid — you answer every raise"
    lines.append(f"{mode}\n⏱️ {LOT_SECONDS}s to answer")
    return "\n".join(lines)


def _lot_rows(state, lot):
    seq = lot["seq"]
    if lot.get("status") == "rtm":
        return [[(f"✅ Use RTM {money(lot['price'])}", f"al:rtm:{seq}:1"),
                 ("❌ Let him go", f"al:rtm:{seq}:0")]]
    ok, price = AL.user_may_bid(state)
    first = []
    if ok:
        first.append((f"🔨 Bid {money(price)}", f"al:bid:{seq}"))
        jump = AL.user_jump_price(state)
        if jump:
            first.append((f"🚀 {money(jump)}", f"al:jump:{seq}:{jump}"))
    first.append(("🙅 Pass", f"al:pass:{seq}"))
    fast = "🐢 Bid by bid" if state.get("fast") else "⚡ Fast"
    return [first,
            [("⏭ Skip player", f"al:skip:{seq}"), ("⏩ Sim set", f"al:simset:{seq}"),
             ("⏭⏭ Sim to end", f"al:simall:{seq}")],
            [(fast, f"al:fast:{seq}"), ("⏸ Pause", f"al:pause:{seq}")],
            [("📋 My squad", "al:squad"), ("💰 Purses", "al:purse"), ("📦 Sets", "al:sets")]]


def _sim_summary_blocks(state, lots, by_team=False):
    lots = [l for l in lots if l]
    if by_team:
        blocks = []
        for name in state["team_order"]:
            mine = [l for l in lots if l.get("status") == "sold" and l.get("winner") == name]
            if not mine:
                continue
            lines = [f"🏏 <b>{_esc(name)}</b> (+{len(mine)})"]
            for l in mine:
                c = AL.card(state, l["pid"])
                lines.append("• " + _card_line(c, l["price"], l.get("how")))
            blocks.append("\n".join(lines))
        unsold = [l for l in lots if l.get("status") == "unsold"]
        if unsold:
            blocks.append(f"❌ <b>Unsold:</b> {len(unsold)} players")
        return blocks or ["Nothing was left to sell."]
    blocks, current = [], None
    for l in lots:
        if l.get("set_no") != current:
            current = l.get("set_no")
            blocks.append(f"\n📦 <b>Set {current}: {_esc(l.get('set_name'))}</b>")
        blocks.append(AL.lot_result_line(state, l))
    return blocks or ["Nothing was left to sell."]


def _table_text(state):
    table = state.get("table") or {}
    lines = ["📊 <b>Points table</b>", "<pre>", "#  Team   P  W  L  T Pts    NRR"]
    for i, name in enumerate(AL.standings(state), 1):
        r = table[name]
        me = "◀" if name == state["user_team"] else ""
        lines.append(f"{i:<2} {_short(state, name):<5} {r['p']:>2} {r['w']:>2} {r['l']:>2} "
                     f"{r['t']:>2} {r['pts']:>3} {AL.nrr(r):>+6.2f}{me}")
    lines.append("</pre>")
    lines.append("<i>Top 4 reach the playoffs: Q1 (1v2), Eliminator (3v4), Q2, Final.</i>")
    return "\n".join(lines)


def _fixture_line(state, fx):
    stage = "" if fx["stage"] == AL.STAGE_LEAGUE else f"<b>{AL.STAGE_LABEL[fx['stage']]}</b> · "
    h, a = _short(state, fx["home"]), _short(state, fx["away"])
    if fx["status"] == "done":
        res = fx["result"] or {}
        i1, i2 = res.get("inn1"), res.get("inn2")
        if res.get("conceded"):
            score = "conceded"
        else:
            score = (f"{_short(state, i1[0])} {i1[1]}/{i1[2]} · "
                     f"{_short(state, i2[0])} {i2[1]}/{i2[2]}")
        w = res.get("winner")
        return (f"#{fx['no']} {stage}{h} v {a} — "
                f"{('🏆 ' + _short(state, w)) if w else 'tie'} ({score})")
    return f"#{fx['no']} {stage}{h} v {a}"


def _result_digest(state, played):
    if not played:
        return []
    blocks = ["📅 <b>Around the league</b>"]
    for fx in played:
        blocks.append(_fixture_line(state, fx))
    return blocks


def _report_blocks(state):
    blocks = ["🏁 <b>Auction complete!</b> — the report card"]
    lines = ["<pre>Gr  Team  OVR  Bat  Bowl  Purse"]
    for r in AL.report_card(state):
        me = "◀" if r["team"] == state["user_team"] else ""
        lines.append(f"{r['grade']:<3} {_short(state, r['team']):<5} {r['ovr']:>4} "
                     f"{r['bat']:>4} {r['bowl']:>5} {money(r['purse']):>8}{me}")
    lines.append("</pre>")
    blocks.append("\n".join(lines))
    big = AL.biggest_buys(state)
    if big:
        lines = ["💸 <b>Biggest buys</b>"]
        for e in big:
            c = AL.card(state, e["pid"])
            lines.append(f"• {_esc(c['name'])} ({c['rating']}) → "
                         f"{_esc(_short(state, e['team']))} {money(e['price'])}")
        blocks.append("\n".join(lines))
    return blocks


def _caps_text(state):
    orange, purple = AL.cap_tables(state)
    lines = ["🟠 <b>Orange Cap</b>"]
    for pid, r in orange:
        c = AL.card(state, pid)
        owner = AL.owner_of(state, pid)
        lines.append(f"• {_esc(c['name'])} ({_esc(_short(state, owner) if owner else '?')}) — "
                     f"<b>{r['runs']}</b> runs, HS {r['hs']}")
    lines.append("\n🟣 <b>Purple Cap</b>")
    for pid, r in purple:
        c = AL.card(state, pid)
        owner = AL.owner_of(state, pid)
        econ = r["runs"] * 6 / r["balls"] if r["balls"] else 0
        lines.append(f"• {_esc(c['name'])} ({_esc(_short(state, owner) if owner else '?')}) — "
                     f"<b>{r['wkts']}</b> wkts, econ {econ:.2f}")
    if len(lines) == 2:
        return "No matches played yet."
    return "\n".join(lines)


def _hub(state, row):
    me = state["user_team"]
    lg = state["league"]["name"]
    head = (f"🏏 <b>{_esc(lg)} Auction League</b>\n"
            f"Your franchise: <b>{_esc(me)}</b> · AI: {DIFF_LABEL.get(state.get('difficulty'), '')}")
    phase = state["phase"]
    if phase == AL.PHASE_RETENTION:
        return head + f"\n\n🔒 Retention window is open.\n\n{HELP_LINE}", [
            [("🔒 Retentions", "al:rtview")], [("🗑 Discard career", "al:quit")]]
    if phase == AL.PHASE_AUCTION:
        status = "⏸ The auction is <b>paused</b>" if state.get("paused") \
            else "🔨 The auction is live"
        return (head + f"\n\n{status} — set {state['set_idx'] + 1}"
                f"/{len(state['sets'])}.\n{_purse_line(state)}\n\n{HELP_LINE}"), [
            [("▶️ Resume auction", "al:resume")],
            [("📦 Sets", "al:sets"), ("📋 My squad", "al:squad"), ("💰 Purses", "al:purse")],
            [("🗑 Discard career", "al:quit")]]
    if phase == AL.PHASE_SEASON:
        fx = AL.next_user_fixture(state)
        nxt = (f"Next: {_fixture_line(state, fx)}" if fx else "You're out — the rest is simulated.")
        pos = AL.standings(state).index(me) + 1
        played = any(r["p"] for r in (state.get("table") or {}).values())
        where = f" · you're <b>#{pos}</b> in the table" if played else ""
        return (head + f"\n\n📅 Season under way{where}\n{nxt}\n\n{HELP_LINE}"), [
            [("▶️ Next fixture", f"al:play:{row.id}")],
            [("📊 Table", f"al:table:{row.id}"), ("📅 Fixtures", "al:fix"), ("🟠 Caps", "al:caps")],
            [("📋 My squad", "al:squad"), ("🗑 Discard", "al:quit")]]
    return _season_end_text(state), [[("🆕 New career", "al:new")],
                                     [("📊 Final table", f"al:table:{row.id}"),
                                      ("🟠 Caps", "al:caps")]]


def _purse_line(state):
    t = AL.user_team(state)
    return f"👛 {money(t['purse'])} · squad {len(t['squad'])}/{AL.SQUAD_MAX} · needs: " \
           f"{AL.needs_line(state, state['user_team'])}"


def _season_end_text(state):
    finish = AL.season_finish(state)
    me = state["user_team"]
    verdict = {"champion": "🏆 <b>CHAMPIONS!</b> You won the title!",
               "runner_up": "🥈 Runners-up — so close.",
               "playoffs": "🎯 You made the playoffs.",
               "league": "📉 Knocked out in the league stage."}[finish]
    return (f"🏁 <b>{_esc(state['league']['name'])} Auction League — season over</b>\n\n"
            f"🏆 Champions: <b>{_esc(state.get('champion'))}</b>\n"
            f"🥈 Runners-up: {_esc(state.get('runner_up'))}\n\n"
            f"{_esc(me)}: {verdict}")


# ════════════════════════════════════════════════════════════════════
# The auction loop
# ════════════════════════════════════════════════════════════════════

def _cancel_timer(context, tg_id):
    jq = getattr(context, "job_queue", None)
    if not jq:
        return
    for job in jq.get_jobs_by_name(f"al_lot_{tg_id}"):
        job.schedule_removal()


def _arm_timer(context, tg_id, chat_id, seq):
    _cancel_timer(context, tg_id)
    jq = getattr(context, "job_queue", None)
    if not jq:
        return
    jq.run_once(_lot_timeout, LOT_SECONDS, name=f"al_lot_{tg_id}",
                data={"tg_id": tg_id, "chat_id": chat_id, "seq": seq})


async def _close_card(context, state, chat_id, line):
    """Turn the last lot card into its result line (buttons gone)."""
    ui = state.setdefault("ui", {})
    mid = ui.pop("lot_msg", None)
    if not mid:
        return False
    try:
        await context.bot.edit_message_text(line, chat_id=chat_id, message_id=mid,
                                            parse_mode="HTML")
        return True
    except Exception:
        return False


async def _advance(context, car, chat_id, tg_id, note=""):
    """Run the auction forward until you have a decision to make.

    Lots you cannot bid on are decided on the spot and reported in one batch.
    Saves the career before posting the next card.
    """
    state = car.state
    ui = state.setdefault("ui", {})
    batch = []
    if state.get("last_lot") and ui.get("reported") != state["last_lot"]["seq"]:
        line = AL.lot_result_line(state, state["last_lot"])
        ui["reported"] = state["last_lot"]["seq"]
        if not await _close_card(context, state, chat_id, line):
            batch.append(line)
    guard = 0
    while guard < 500:
        guard += 1
        lot = state.get("lot")
        if lot is None:
            lot = AL.open_next_lot(state)
            if lot is None:
                break
        if lot["status"] in ("open", "rtm") and state.get("lot") is lot:
            break
        # Decided without you (you couldn't afford or fit him).
        batch.append(AL.lot_result_line(state, lot))
        ui["reported"] = lot["seq"]
    if state.get("lot") is None:
        # Nothing left to sell: close the auction and start the season.
        car.save()
        if batch:
            await _send_long(context, chat_id, batch)
        await _finish_auction(context, car, chat_id)
        return
    lot = state["lot"]
    car.save()
    if batch:
        await _send_long(context, chat_id, batch, header="🔨 <b>Meanwhile…</b>")
    sent = await _send(context, chat_id, _lot_text(state, lot, note), _lot_rows(state, lot))
    if sent is not None:
        ui["lot_msg"] = sent.message_id
        car.save()
    _arm_timer(context, tg_id, chat_id, lot["seq"])


async def _finish_auction(context, car, chat_id):
    state = car.state
    _cancel_timer(context, car.row.user_tg_id)
    filled = AL.finish_auction(state)
    car.save()
    blocks = _report_blocks(state)
    if filled:
        lines = ["🧩 <b>Squads topped up</b> (unsold players at base price, to meet the rules)"]
        for team, pid, price in filled:
            lines.append(f"• {_esc(_short(state, team))}: "
                         f"{_card_line(AL.card(state, pid), price)}")
        blocks.append("\n".join(lines))
    await _send_long(context, chat_id, blocks)
    await _send(context, chat_id, _squad_text(state, state["user_team"]))
    mine = sum(1 for f in state["fixtures"] if state["user_team"] in (f["home"], f["away"]))
    await _send(context, chat_id,
                f"📅 <b>The season is set</b> — {len(state['fixtures'])} league matches, "
                f"{mine} for you, then the playoffs (top 4).",
                [[("▶️ Play your first fixture", f"al:play:{car.row.id}")],
                 [("📅 Fixtures", "al:fix"), ("📊 Table", f"al:table:{car.row.id}")]])


async def _lot_timeout(context):
    data = context.job.data or {}
    tg_id, chat_id, seq = data.get("tg_id"), data.get("chat_id"), data.get("seq")
    async with _lock(context, tg_id):
        car = _open(tg_id)
        try:
            state = car.state
            if not state or state["phase"] != AL.PHASE_AUCTION or state.get("paused"):
                return
            lot = state.get("lot")
            if not lot or lot["seq"] != seq:
                return
            if lot["status"] == "rtm":
                AL.user_rtm(state, False)
            else:
                AL.user_pass(state)
            await _advance(context, car, chat_id, tg_id, note="⏰ Time's up — you passed.")
        except Exception:
            logger.exception("auction league: lot timeout failed")
        finally:
            car.session.close()


# ════════════════════════════════════════════════════════════════════
# Commands
# ════════════════════════════════════════════════════════════════════

async def auction_league_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/auctionleague — the hub."""
    if not await _require_dm(update, context):
        return
    tg = update.effective_user
    session = get_session()
    try:
        if not sync_telegram_user(session, tg):
            await update.effective_message.reply_text("❌ Use /debut first.")
            return
        session.commit()
    finally:
        session.close()
    chat_id = update.effective_chat.id
    args = [a for a in (context.args or []) if a]
    wanted = " ".join(args).strip()
    car = _open(tg.id, include_completed=True)
    try:
        active = car.row is not None and car.state["phase"] in AL.ACTIVE_STATUSES
        if active and wanted:
            # One career at a time: a new one needs the old one discarded.
            await _send_one_career_notice(context, chat_id, car.state)
            return
        if active or (car.row is not None and not wanted):
            text, rows = _hub(car.state, car.row)
            await _send(context, chat_id, text, rows)
            return
        if wanted and wanted.lower() != "new":
            league_id = _find_league_id(wanted)
            if league_id is None:
                await _send(context, chat_id,
                            f"❌ No league called <b>{_esc(wanted)}</b>. Pick one:")
                await _start_new(chat_id, context)
                return
            await _send(context, chat_id, INTRO.rsplit("\n\n", 1)[0])
            await _send_team_picker(context, chat_id, league_id)
            return
        await _start_new(chat_id, context)
    finally:
        car.session.close()


async def _send_one_career_notice(context, chat_id, state):
    await _send(context, chat_id,
                f"⚠️ You already have an Auction League career in progress "
                f"(<b>{_esc(state['user_team'])}</b>, {_esc(state['league']['name'])}).\n"
                f"Finish it, or discard it, before starting a new one.\n\n{HELP_LINE}",
                [[("▶️ Continue", "al:hub"), ("🗑 Discard career", "al:quit")]])


def _find_league_id(wanted):
    """The active league matching ``wanted`` by name, short code or command."""
    from handlers.challenge import normalize_challenge_league
    from models import ChallengeLeague
    key = normalize_challenge_league(wanted)
    session = get_session()
    try:
        leagues = (session.query(ChallengeLeague)
                   .filter(ChallengeLeague.is_active.is_(True)).all())
        for lg in leagues:
            names = {normalize_challenge_league(lg.name),
                     normalize_challenge_league(lg.short_code or ""),
                     normalize_challenge_league((lg.command or "").lstrip("/"))}
            if key and key in names:
                return lg.id
        # "/rcpl ipl" for a league whose command is /cipl
        for lg in leagues:
            cmd = normalize_challenge_league((lg.command or "").lstrip("/"))
            if key and cmd == "c" + key:
                return lg.id
        return None
    finally:
        session.close()


INTRO = (
    "🏏 <b>Auction League</b> — your solo franchise career\n\n"
    "1️⃣ Pick a franchise — the other teams are run by the AI\n"
    "2️⃣ Retain up to 3 players: ₹18 Cr / ₹14 Cr / ₹10 Cr\n"
    "3️⃣ Auction from a ₹120 Cr purse — marquee set first, then rating bands "
    "by role. Teams that keep 2 or fewer get a 🔁 Right To Match card.\n"
    "4️⃣ Squad rules: at least 4 BAT, 4 BOWL, 1 WK, 2 AR · max 18"
    " · max 8 overseas\n"
    "5️⃣ Season: everyone plays everyone once, then the IPL playoffs — "
    "your matches ball by ball, 20 overs\n\n"
    "🏆 Champions earn 25,000 coins + 15 💎 (runners-up 10,000 + 5 💎, "
    "playoffs 4,000). Matches themselves are unranked practice.\n"
    f"🎟 Entry fee: <b>{ENTRY_FEE_GEMS} 💎</b>, paid when you start the career.\n\n"
    "💡 Tip: <code>/rcpl IPL</code> jumps straight to a league's teams.\n"
    f"{HELP_LINE}\n\n"
    "Choose your league:"
)


async def _start_new(chat_id, context):
    session = get_session()
    try:
        from models import ChallengeLeague
        leagues = (session.query(ChallengeLeague)
                   .filter(ChallengeLeague.is_active.is_(True))
                   .order_by(ChallengeLeague.sort_order, ChallengeLeague.name).all())
        if not leagues:
            await _send(context, chat_id, "❌ No leagues are set up yet.")
            return
        rows = [[(f"🏆 {lg.name}", f"al:lg:{lg.id}")] for lg in leagues[:12]]
        await _send(context, chat_id, INTRO, rows)
    finally:
        session.close()


def _league_key(session, league):
    """The league's key, derived exactly as handlers.challenge._challenge_leagues does."""
    from handlers.challenge import normalize_challenge_league
    return normalize_challenge_league(league.short_code or league.name)


async def _cb_league(q, context, league_id):
    err = await _send_team_picker(context, q.message.chat_id, league_id)
    await q.answer(err or None, show_alert=bool(err))


async def _send_team_picker(context, chat_id, league_id):
    """Post a league's franchises to pick from. Returns an error text, or None."""
    session = get_session()
    try:
        from models import ChallengeLeague
        league = session.get(ChallengeLeague, int(league_id))
        if league is None:
            return "That league no longer exists."
        teams = AL.league_teams(session, league)
        playable = [t for t in teams if len(t["players"]) >= 11]
        if len(teams) < 4:
            await _send(context, chat_id, "❌ This league needs at least four teams.")
            return "This league needs at least four teams."
        context.user_data["al_setup"] = {"league_id": league.id, "ai_retain": True,
                                         "difficulty": "normal"}
        rows, row = [], []
        for i, t in enumerate(teams):
            row.append((t["name"], f"al:tm:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        note = "" if len(playable) == len(teams) else \
            "\n<i>(Some squads are small — the auction will fill them.)</i>"
        await _send(context, chat_id,
                    f"🏆 <b>{_esc(league.name)}</b> — pick your franchise:{note}", rows)
        return None
    finally:
        session.close()


def _setup_text(setup, team):
    return (f"⚙️ <b>Career setup</b>\n\n"
            f"Franchise: <b>{_esc(team)}</b>\n"
            f"AI retentions: <b>{'On' if setup['ai_retain'] else 'Off'}</b> "
            f"<i>(AI keeps up to 3 of its stars — the league's top 20%)</i>\n"
            f"AI captaincy: <b>{DIFF_LABEL[setup['difficulty']]}</b>\n\n"
            f"🎟 Entry fee: <b>{ENTRY_FEE_GEMS} 💎</b> — paid when you tap ✅ Start career "
            f"(not refunded if you discard it).\n{HELP_LINE}")


def _setup_rows(setup):
    return [[(f"🔁 AI retentions: {'On' if setup['ai_retain'] else 'Off'}", "al:sret")],
            [(f"🎚 Difficulty: {DIFF_LABEL[setup['difficulty']]}", "al:sdiff")],
            [(f"✅ Start career ({ENTRY_FEE_GEMS} 💎)", "al:sgo")]]


async def _cb_team(q, context, idx):
    setup = context.user_data.get("al_setup")
    if not setup:
        await q.answer("Start again with /auctionleague.", show_alert=True)
        return
    session = get_session()
    try:
        from models import ChallengeLeague
        league = session.get(ChallengeLeague, int(setup["league_id"]))
        teams = AL.league_teams(session, league) if league else []
        if not (0 <= int(idx) < len(teams)):
            await q.answer("That team is gone — start again.", show_alert=True)
            return
        setup["team"] = teams[int(idx)]["name"]
    finally:
        session.close()
    await q.answer()
    try:
        await q.edit_message_text(_setup_text(setup, setup["team"]), parse_mode="HTML",
                                  reply_markup=_kb(_setup_rows(setup)))
    except Exception:
        await _send(context, q.message.chat_id, _setup_text(setup, setup["team"]),
                    _setup_rows(setup))


async def _cb_setup_toggle(q, context, what):
    setup = context.user_data.get("al_setup")
    if not setup or not setup.get("team"):
        await q.answer("Start again with /auctionleague.", show_alert=True)
        return
    if what == "sret":
        setup["ai_retain"] = not setup["ai_retain"]
    else:
        i = DIFFICULTIES.index(setup["difficulty"])
        setup["difficulty"] = DIFFICULTIES[(i + 1) % len(DIFFICULTIES)]
    await q.answer()
    try:
        await q.edit_message_text(_setup_text(setup, setup["team"]), parse_mode="HTML",
                                  reply_markup=_kb(_setup_rows(setup)))
    except Exception:
        pass


async def _cb_setup_go(q, context):
    setup = context.user_data.get("al_setup")
    tg_id = q.from_user.id
    if not setup or not setup.get("team"):
        await q.answer("Start again with /auctionleague.", show_alert=True)
        return
    session = get_session()
    try:
        from models import ChallengeLeague
        league = session.get(ChallengeLeague, int(setup["league_id"]))
        if league is None:
            await q.answer("That league no longer exists.", show_alert=True)
            return
        if AL.active_save(session, tg_id) is not None:
            await q.answer("Finish or discard your current career first.", show_alert=True)
            return
        from models import User
        user = session.query(User).filter(User.telegram_id == tg_id).first()
        if user is None:
            await q.answer("Use /debut first.", show_alert=True)
            return
        gems = int(user.total_gems or 0)
        if gems < ENTRY_FEE_GEMS:
            await q.answer(f"🎟 Entry fee is {ENTRY_FEE_GEMS} 💎 — you have {gems}.",
                           show_alert=True)
            return
        teams = AL.league_teams(session, league)
        state = AL.new_state(AL.league_dict(league, _league_key(session, league)), teams,
                             setup["team"], ai_retain=setup["ai_retain"],
                             difficulty=setup["difficulty"])
        state["pending_retain"] = []
        state["entry_fee_gems"] = ENTRY_FEE_GEMS
        # The fee and the new save commit together: no charge without a career.
        user.total_gems = gems - ENTRY_FEE_GEMS
        try:
            from services.activity_service import log_activity
            log_activity(session, user.id, "auction_league_entry",
                         f"Auction League entry fee: -{ENTRY_FEE_GEMS} gems",
                         gems_change=-ENTRY_FEE_GEMS)
        except Exception:
            logger.exception("auction league: could not log the entry fee")
        AL.create_save(session, tg_id, state)
        session.commit()
        context.user_data.pop("al_setup", None)
        await q.answer(f"Career started! −{ENTRY_FEE_GEMS} 💎")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _send_retention(context, q.message.chat_id, state)
    except AuctionLeagueError as exc:
        await q.answer(str(exc), show_alert=True)
    finally:
        session.close()


# ── Retention ────────────────────────────────────────────────────────

def _retention_text(state):
    picked = state.get("pending_retain") or []
    lines = [f"🔒 <b>Retention — {_esc(state['user_team'])}</b>",
             "Tap up to 3 players to keep. Slot prices: ₹18 Cr · ₹14 Cr · ₹10 Cr.",
             ""]
    for i, pid in enumerate(picked):
        lines.append(f"{i + 1}. {_card_line(AL.card(state, pid), AL.RETENTION_PRICES[i])}")
    cost = AL.retention_cost(len(picked))
    lines += ["", f"Total: <b>{money(cost)}</b> · purse after: "
                  f"<b>{money(AL.PURSE_LAKH - cost)}</b>",
              f"🔁 Right To Match cards: {1 if len(picked) < AL.MAX_RETAIN else 0}"]
    return "\n".join(lines)


def _retention_rows(state):
    picked = set(state.get("pending_retain") or [])
    rows = []
    for c in AL.original_squad(state, state["user_team"])[:24]:
        mark = "✅ " if c["id"] in picked else ""
        rows.append([(f"{mark}{c['name']} · {c['rating']} {_role(c)}", f"al:rt:{c['id']}")])
    rows.append([("🔒 Confirm retentions", "al:rtok")])
    return rows


async def _send_retention(context, chat_id, state):
    await _send(context, chat_id, _retention_text(state), _retention_rows(state))


async def _cb_retain_toggle(q, context, car, pid):
    state = car.state
    if state["phase"] != AL.PHASE_RETENTION:
        await q.answer("Retentions are already locked.", show_alert=True)
        return
    picked = list(state.get("pending_retain") or [])
    pid = int(pid)
    if pid in picked:
        picked.remove(pid)
    elif len(picked) >= AL.MAX_RETAIN:
        await q.answer("You can retain at most 3 players.", show_alert=True)
        return
    else:
        picked.append(pid)
    state["pending_retain"] = picked
    car.save()
    await q.answer()
    try:
        await q.edit_message_text(_retention_text(state), parse_mode="HTML",
                                  reply_markup=_kb(_retention_rows(state)))
    except Exception:
        pass


async def _cb_retain_ok(q, context, car):
    state = car.state
    try:
        made = AL.apply_retentions(state, state.pop("pending_retain", []) or [])
    except AuctionLeagueError as exc:
        await q.answer(str(exc), show_alert=True)
        return
    car.save()
    await q.answer("Retentions locked!")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    blocks = ["🔒 <b>Retentions</b>"]
    for name in state["team_order"]:
        pids = made.get(name) or []
        t = state["teams"][name]
        kept = ", ".join(f"{_esc(AL.card(state, p)['name'])} ({AL.card(state, p)['rating']})"
                         for p in pids) or "—"
        pers = AL.PERSONALITIES.get(t["personality"], {}).get("label", "You")
        blocks.append(f"<b>{_esc(t['short'])}</b> [{pers}] {kept} · "
                      f"purse {money(t['purse'])}{' · 🔁 RTM' if t['rtm'] else ''}")
    blocks.append(f"\n📦 {len(state['sets'])} sets, "
                  f"{sum(len(s['pids']) for s in state['sets'])} players go under the hammer.")
    await _send_long(context, q.message.chat_id, blocks,
                     rows=[[("🔨 Start the auction", "al:resume")], [("📦 Sets", "al:sets")]])


# ── Lot actions ──────────────────────────────────────────────────────

async def _cb_lot(q, context, car, action, seq, arg=None):
    state = car.state
    tg_id = q.from_user.id
    chat_id = q.message.chat_id
    lot = state.get("lot")
    if state["phase"] != AL.PHASE_AUCTION or not lot or lot["seq"] != int(seq):
        await q.answer("That lot is already closed.", show_alert=False)
        return
    if state.get("paused"):
        await q.answer("⏸ The auction is paused — tap ▶️ Resume first.", show_alert=True)
        return
    try:
        if action == "bid":
            AL.user_bid(state)
            await q.answer("Bid placed!")
        elif action == "jump":
            AL.user_bid(state, to_price=int(arg))
            await q.answer(f"🚀 Jumped to {money(int(arg))}!")
        elif action == "pass":
            AL.user_pass(state)
            await q.answer("Passed")
        elif action == "rtm":
            AL.user_rtm(state, arg == "1")
            await q.answer("RTM used!" if arg == "1" else "Let go")
        else:
            return
    except AuctionLeagueError as exc:
        await q.answer(str(exc), show_alert=True)
        return
    _cancel_timer(context, tg_id)
    if state.get("lot") is lot:
        # Still on the block — the AI countered. Refresh the same card.
        car.save()
        try:
            await q.edit_message_text(_lot_text(state, lot), parse_mode="HTML",
                                      reply_markup=_kb(_lot_rows(state, lot)))
            state.setdefault("ui", {})["lot_msg"] = q.message.message_id
            car.save()
        except Exception:
            await _advance(context, car, chat_id, tg_id)
            return
        _arm_timer(context, tg_id, chat_id, lot["seq"])
        return
    await _advance(context, car, chat_id, tg_id)


async def _skip_player(context, car, chat_id, tg_id):
    """/skipplayer — the player on the block is decided at once (autopilot)."""
    state = car.state
    _cancel_timer(context, tg_id)
    state.pop("paused", None)
    lot = AL.simulate_lot(state)
    if lot is None:
        await _advance(context, car, chat_id, tg_id)
        return
    # _advance turns the old card into this lot's result line, then moves on.
    await _advance(context, car, chat_id, tg_id, note="⏭ <i>Player skipped — simulated.</i>")


async def _pause(context, car, chat_id, tg_id, message=None):
    state = car.state
    _cancel_timer(context, tg_id)
    state["paused"] = True
    mid = state.setdefault("ui", {}).pop("lot_msg", None)
    car.save()
    text = ("⏸ <b>Auction paused.</b> Nothing moves and no clock runs until you "
            "resume.\n\n/alresume or tap below to carry on.")
    rows = [[("▶️ Resume auction", "al:resume")], [("📋 My squad", "al:squad"),
                                                   ("💰 Purses", "al:purse")]]
    if mid:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=mid,
                                                parse_mode="HTML", reply_markup=_kb(rows))
            return
        except Exception:
            pass
    await _send(context, chat_id, text, rows)


async def _do_sim(context, car, chat_id, tg_id, *, upto=None, all_=False):
    state = car.state
    _cancel_timer(context, tg_id)
    state.pop("paused", None)
    lot = state.get("lot")
    if lot:
        await _close_card(context, state, chat_id, "⏩ <i>Simulated…</i>")
    if all_:
        lots = AL.simulate_to_last(state)
        state.setdefault("ui", {})["reported"] = state["last_lot"]["seq"] if state.get("last_lot") else 0
        car.save()
        await _send_long(context, chat_id, _sim_summary_blocks(state, lots, by_team=True),
                         header="⏭ <b>Auction simulated to the end</b> — who went where")
        await _finish_auction(context, car, chat_id)
        return
    lots = AL.simulate_set(state, upto=upto)
    if state.get("last_lot"):
        state.setdefault("ui", {})["reported"] = state["last_lot"]["seq"]
    car.save()
    await _send_long(context, chat_id, _sim_summary_blocks(state, lots),
                     header="⏩ <b>Set simulated</b>")
    await _advance(context, car, chat_id, tg_id)


async def simset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/simset [n] — simulate the live set, or every set up to n."""
    if not await _require_dm(update, context):
        return
    upto = None
    if context.args:
        try:
            upto = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /simset or /simset 5")
            return
    await _locked_auction_cmd(update, context, upto=upto, all_=False)


async def simtolast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/simtolast — simulate the rest of the auction."""
    if not await _require_dm(update, context):
        return
    await _locked_auction_cmd(update, context, all_=True)


async def skipplayer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/skipplayer — simulate the player on the block."""
    if not await _require_dm(update, context):
        return
    await _locked_auction_action(update, context, "skip")


async def alpause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alpause — pause the auction."""
    if not await _require_dm(update, context):
        return
    await _locked_auction_action(update, context, "pause")


async def alresume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alresume — resume a paused auction."""
    if not await _require_dm(update, context):
        return
    await _locked_auction_action(update, context, "resume")


async def _locked_auction_action(update, context, action):
    tg_id = update.effective_user.id
    chat_id = update.effective_chat.id
    async with _lock(context, tg_id):
        car = _open(tg_id)
        try:
            if car.row is None or car.state["phase"] != AL.PHASE_AUCTION:
                await update.effective_message.reply_text(
                    "❌ No auction is running. /auctionleague to see your career.")
                return
            if action == "skip":
                await _skip_player(context, car, chat_id, tg_id)
            elif action == "pause":
                if car.state.get("paused"):
                    await update.effective_message.reply_text("⏸ Already paused — /alresume.")
                    return
                await _pause(context, car, chat_id, tg_id)
            else:
                car.state.pop("paused", None)
                car.state.setdefault("ui", {}).pop("lot_msg", None)
                await _advance(context, car, chat_id, tg_id, note="▶️ <i>Auction resumed.</i>")
        except AuctionLeagueError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
        finally:
            car.session.close()


async def _locked_auction_cmd(update, context, *, upto=None, all_=False):
    tg_id = update.effective_user.id
    chat_id = update.effective_chat.id
    async with _lock(context, tg_id):
        car = _open(tg_id)
        try:
            if car.row is None or car.state["phase"] != AL.PHASE_AUCTION:
                await update.effective_message.reply_text(
                    "❌ No auction is running. /auctionleague to see your career.")
                return
            try:
                await _do_sim(context, car, chat_id, tg_id, upto=upto, all_=all_)
            except AuctionLeagueError as exc:
                await update.effective_message.reply_text(f"❌ {exc}")
        finally:
            car.session.close()


# ── Season ───────────────────────────────────────────────────────────

async def _season_step(context, car, chat_id, tg_id):
    """Sim the AI games up to your next fixture, then offer it."""
    state = car.state
    played = AL.sim_until_user(state)
    car.save()
    if played:
        await _send_long(context, chat_id, _result_digest(state, played))
    if state["phase"] == AL.PHASE_COMPLETED:
        await _season_over(context, car, chat_id)
        return
    fx = AL.next_user_fixture(state)
    if fx is None:
        await _send(context, chat_id, "⏳ Waiting on the other results…")
        return
    me = state["user_team"]
    opp = fx["away"] if fx["home"] == me else fx["home"]
    where = "🏟 Home — you pick the pitch" if fx["home"] == me else "✈️ Away — the hosts set the pitch"
    pos = AL.standings(state)
    played = any(r["p"] for r in (state.get("table") or {}).values())

    def _rank(team):
        return f" (#{pos.index(team) + 1})" if played else ""

    text = (f"🏏 <b>{AL.STAGE_LABEL[fx['stage']]} · Match #{fx['no']}</b>\n"
            f"<b>{_esc(me)}</b>{_rank(me)} vs <b>{_esc(opp)}</b>{_rank(opp)}\n"
            f"{where}\n\n"
            f"Opponent strength: {AL.team_strength(state, opp)['ovr']} OVR · "
            f"yours {AL.team_strength(state, me)['ovr']} OVR")
    rows = [[("▶️ Play match", f"al:go:{fx['no']}")]]
    if fx["stage"] == AL.STAGE_LEAGUE:
        rows.append([("🏳️ Concede", f"al:conc:{fx['no']}"), ("📊 Table", f"al:table:{car.row.id}")])
    await _send(context, chat_id, text, rows)


async def _season_over(context, car, chat_id):
    state = car.state
    session = car.session
    from models import User
    user = session.query(User).filter(User.telegram_id == car.row.user_tg_id).first()
    coins = gems = 0
    note = ""
    if user is not None and not car.row.reward_paid:
        coins, gems, note = AL.pay_season_reward(session, car.row, state, user)
        session.commit()
    text = _season_end_text(state)
    if coins or gems:
        text += f"\n\n💰 Reward: +{coins:,} coins, +{gems} 💎"
    elif note:
        text += f"\n\n<i>No reward: {_esc(note)}.</i>"
    await _send(context, chat_id, text)
    await _send(context, chat_id, _caps_text(state),
                [[("📊 Final table", f"al:table:{car.row.id}"), ("🆕 New career", "al:new")]])


async def _launch_fixture(q, context, car, no):
    state = car.state
    fx = AL.fixture_by_no(state, no)
    if fx is None or fx["status"] != "pending" or fx is not AL.next_user_fixture(state):
        await q.answer("That fixture isn't up next.", show_alert=True)
        return
    # Anything scheduled before it is played first.
    if AL.pending_fixtures(state)[0] is not fx:
        AL.sim_until_user(state)
        car.save()
    me = state["user_team"]
    opp = fx["away"] if fx["home"] == me else fx["home"]
    session = car.session
    from handlers.challenge import launch_auction_league_match
    from handlers.vsbot import _get_or_create_bot_user
    from handlers.botlevel import remember_level
    from models import ChallengeLeague
    host = sync_telegram_user(session, q.from_user)
    bot_user = _get_or_create_bot_user(session)
    session.commit()
    league = session.get(ChallengeLeague, state["league"]["id"]) if state["league"].get("id") else None
    remember_level(context.bot_data, q.from_user.id, state.get("difficulty") or "normal")
    tag = {"save_id": car.row.id, "fixture_no": fx["no"], "user_id": host.id,
           "user_team": me, "opp_team": opp}
    stage = AL.STAGE_LABEL[fx["stage"]]
    ok, err = await launch_auction_league_match(
        context, chat_id=q.message.chat_id, host=host, bot_user=bot_user,
        league_record=league, league_key=state["league"].get("key"),
        league_name=f"{state['league']['name']} Auction League · {stage}",
        host_team=me, target_team=opp,
        host_squad=AL.squad_card_dicts(state, me),
        target_squad=AL.squad_card_dicts(state, opp),
        tag=tag, home_team=fx["home"],
        team_codes={n: state["teams"][n]["short"] for n in (me, opp)},
        session=session)
    if not ok:
        await q.answer(err or "Couldn't start the match.", show_alert=True)
        return
    await q.answer("Match on!")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Read-only commands
# ════════════════════════════════════════════════════════════════════

async def _view(update, context, render, *, include_completed=True, phases=None):
    if not await _require_dm(update, context):
        return
    car = _open(update.effective_user.id, include_completed=include_completed)
    try:
        if car.row is None:
            await update.effective_message.reply_text(
                "❌ No Auction League career yet — /auctionleague to start one.")
            return
        if phases and car.state["phase"] not in phases:
            await update.effective_message.reply_text("❌ Not available at this stage.")
            return
        text = render(car.state)
        for part in chunk_blocks(text.split("\n\n")):
            await update.effective_message.reply_text(part, parse_mode="HTML")
    finally:
        car.session.close()


def _fixtures_text(state):
    me = state["user_team"]
    mine = [f for f in state["fixtures"] if me in (f["home"], f["away"])]
    lines = [f"📅 <b>{_esc(me)} fixtures</b>"] + [_fixture_line(state, f) for f in mine]
    po = [f for f in state["fixtures"] if f["stage"] != AL.STAGE_LEAGUE]
    if po:
        lines += ["", "🏆 <b>Playoffs</b>"] + [_fixture_line(state, f) for f in po]
    return "\n".join(lines)


async def alsets_handler(update, context):
    await _view(update, context, _sets_text, phases=(AL.PHASE_AUCTION,))


async def alsquad_handler(update, context):
    await _view(update, context, lambda s: _squad_text(s, s["user_team"]))


async def alpurse_handler(update, context):
    await _view(update, context, _purses_text)


async def altable_handler(update, context):
    await _view(update, context, _table_text,
                phases=(AL.PHASE_SEASON, AL.PHASE_COMPLETED))


async def alfixtures_handler(update, context):
    await _view(update, context, _fixtures_text,
                phases=(AL.PHASE_SEASON, AL.PHASE_COMPLETED))


async def alstats_handler(update, context):
    await _view(update, context, _caps_text,
                phases=(AL.PHASE_SEASON, AL.PHASE_COMPLETED))


async def alplay_handler(update, context):
    """/alplay — sim up to your next fixture and offer it."""
    if not await _require_dm(update, context):
        return
    tg_id = update.effective_user.id
    async with _lock(context, tg_id):
        car = _open(tg_id)
        try:
            if car.row is None or car.state["phase"] != AL.PHASE_SEASON:
                await update.effective_message.reply_text(
                    "❌ Your season hasn't started. /auctionleague to see your career.")
                return
            await _season_step(context, car, update.effective_chat.id, tg_id)
        finally:
            car.session.close()


async def alquit_handler(update, context):
    if not await _require_dm(update, context):
        return
    await _send(context, update.effective_chat.id,
                "🗑 Discard your Auction League career? This can't be undone, and the "
                f"{ENTRY_FEE_GEMS} 💎 entry fee is not refunded.",
                [[("Yes, discard", "al:quitok"), ("No", "al:hub")]])


# ════════════════════════════════════════════════════════════════════
# The one callback router
# ════════════════════════════════════════════════════════════════════

async def auction_league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    tg_id = q.from_user.id
    chat_id = q.message.chat_id if q.message else tg_id

    # Setup steps need no save.
    if action == "new":
        car = _open(tg_id)
        try:
            if car.row is not None:
                await q.answer()
                await _send_one_career_notice(context, chat_id, car.state)
                return
        finally:
            car.session.close()
        await q.answer()
        await _start_new(chat_id, context)
        return
    if action == "lg" and args:
        await _cb_league(q, context, args[0])
        return
    if action == "tm" and args:
        await _cb_team(q, context, args[0])
        return
    if action in ("sret", "sdiff"):
        await _cb_setup_toggle(q, context, action)
        return
    if action == "sgo":
        async with _lock(context, tg_id):
            await _cb_setup_go(q, context)
        return

    async with _lock(context, tg_id):
        car = _open(tg_id, include_completed=action in ("table", "caps", "fix", "squad", "hub"))
        try:
            if car.row is None:
                await q.answer("No active career — /auctionleague to start one.", show_alert=True)
                return
            # Buttons that name a save (from a match result) must be this one.
            if action in ("play", "table") and args and str(car.row.id) != args[0]:
                await q.answer("That career is over — /auctionleague", show_alert=True)
                return
            state = car.state
            if action == "rt" and args:
                await _cb_retain_toggle(q, context, car, args[0])
            elif action == "rtok":
                await _cb_retain_ok(q, context, car)
            elif action == "rtview":
                await q.answer()
                await _send_retention(context, chat_id, state)
            elif action == "resume":
                if state["phase"] != AL.PHASE_AUCTION:
                    await q.answer("The auction isn't running.", show_alert=True)
                    return
                await q.answer("Resumed" if state.get("paused") else None)
                try:
                    await q.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                was_paused = bool(state.pop("paused", None))
                state.setdefault("ui", {}).pop("lot_msg", None)
                await _advance(context, car, chat_id, tg_id,
                               note="▶️ <i>Auction resumed.</i>" if was_paused else "")
            elif action in ("bid", "pass") and args:
                await _cb_lot(q, context, car, action, args[0])
            elif action == "jump" and len(args) >= 2:
                await _cb_lot(q, context, car, "jump", args[0], args[1])
            elif action == "rtm" and len(args) >= 2:
                await _cb_lot(q, context, car, "rtm", args[0], args[1])
            elif action in ("skip", "fast", "pause") and args:
                lot = state.get("lot")
                if state["phase"] != AL.PHASE_AUCTION or not lot or lot["seq"] != int(args[0]):
                    await q.answer("That lot is already closed.")
                    return
                if action == "skip":
                    await q.answer("Simulating this player…")
                    await _skip_player(context, car, chat_id, tg_id)
                elif action == "pause":
                    await q.answer("Paused")
                    await _pause(context, car, chat_id, tg_id)
                else:
                    state["fast"] = not state.get("fast")
                    car.save()
                    await q.answer("⚡ Fast bidding on" if state["fast"]
                                   else "🐢 Bid by bid")
                    try:
                        await q.edit_message_text(_lot_text(state, lot), parse_mode="HTML",
                                                  reply_markup=_kb(_lot_rows(state, lot)))
                    except Exception:
                        pass
            elif action in ("simset", "simall") and args:
                lot = state.get("lot")
                if state["phase"] != AL.PHASE_AUCTION or (lot and lot["seq"] != int(args[0])):
                    await q.answer("That lot is already closed.")
                    return
                await q.answer("Simulating…")
                await _do_sim(context, car, chat_id, tg_id, all_=(action == "simall"))
            elif action == "squad":
                await q.answer()
                await _send(context, chat_id, _squad_text(state, state["user_team"]))
            elif action == "purse":
                await q.answer()
                await _send(context, chat_id, _purses_text(state))
            elif action == "sets":
                await q.answer()
                await _send(context, chat_id, _sets_text(state))
            elif action == "table":
                await q.answer()
                if not state.get("table"):
                    await _send(context, chat_id, "The season hasn't started yet.")
                else:
                    await _send(context, chat_id, _table_text(state))
            elif action == "fix":
                await q.answer()
                await _send(context, chat_id, _fixtures_text(state))
            elif action == "caps":
                await q.answer()
                await _send(context, chat_id, _caps_text(state))
            elif action == "play":
                if state["phase"] != AL.PHASE_SEASON:
                    await q.answer("Your season isn't running.", show_alert=True)
                    return
                await q.answer()
                await _season_step(context, car, chat_id, tg_id)
            elif action == "go" and args:
                await _launch_fixture(q, context, car, int(args[0]))
            elif action == "conc" and args:
                await q.answer()
                await _send(context, chat_id,
                            "🏳️ Concede this match? It counts as a loss, and a "
                            "conceded match forfeits the season reward.",
                            [[("Yes, concede", f"al:concok:{args[0]}"), ("No", "al:hub")]])
            elif action == "concok" and args:
                fx = AL.fixture_by_no(state, int(args[0]))
                if fx is None or fx is not AL.next_user_fixture(state):
                    await q.answer("That fixture isn't up next.", show_alert=True)
                    return
                AL.concede_fixture(state, fx)
                car.save()
                await q.answer("Conceded")
                await _season_step(context, car, chat_id, tg_id)
            elif action == "quit":
                await q.answer()
                await _send(context, chat_id,
                            "🗑 Discard your Auction League career? This can't be undone, "
                            f"and the {ENTRY_FEE_GEMS} 💎 entry fee is not refunded.",
                            [[("Yes, discard", "al:quitok"), ("No", "al:hub")]])
            elif action == "quitok":
                _cancel_timer(context, tg_id)
                state["phase"] = AL.PHASE_ABANDONED
                car.save()
                await q.answer("Career discarded")
                await _send(context, chat_id,
                            "🗑 Career discarded. <code>/rcpl IPL</code> (or /auctionleague) "
                            "to start a new one.")
            elif action == "hub":
                await q.answer()
                text, rows = _hub(state, car.row)
                await _send(context, chat_id, text, rows)
            else:
                await q.answer()
        except Exception:
            logger.exception("auction league: callback %s failed", q.data)
            try:
                await q.answer("⚠️ Something went wrong — try /auctionleague.", show_alert=True)
            except Exception:
                pass
        finally:
            car.session.close()


def register(app):
    """Wire the commands and the callback into the application."""
    from telegram.ext import CallbackQueryHandler, CommandHandler
    app.add_handler(CommandHandler(["auctionleague", "al", "rcpl"], auction_league_handler))
    app.add_handler(CommandHandler("simset", simset_handler))
    app.add_handler(CommandHandler(["simtolast", "alsim"], simtolast_handler))
    app.add_handler(CommandHandler("alsets", alsets_handler))
    app.add_handler(CommandHandler("alsquad", alsquad_handler))
    app.add_handler(CommandHandler("alpurse", alpurse_handler))
    app.add_handler(CommandHandler("altable", altable_handler))
    app.add_handler(CommandHandler("alfixtures", alfixtures_handler))
    app.add_handler(CommandHandler("alstats", alstats_handler))
    app.add_handler(CommandHandler("alplay", alplay_handler))
    app.add_handler(CommandHandler(["alquit", "aldiscard"], alquit_handler))
    app.add_handler(CommandHandler("skipplayer", skipplayer_handler))
    app.add_handler(CommandHandler("alpause", alpause_handler))
    app.add_handler(CommandHandler("alresume", alresume_handler))
    app.add_handler(CallbackQueryHandler(auction_league_callback, pattern=r"^al:"))
