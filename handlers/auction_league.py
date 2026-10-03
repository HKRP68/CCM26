"""/auctionleague — the solo Auction League career, played in a DM.

Pick a franchise in a Challenge League, retain up to three, bid against nine
AI franchises in a set-by-set auction, then play the season — a round robin
and the IPL playoffs — against them. The rules and the AI live in
``services.auction_league_service``; this module is the Telegram face of it.

Commands (all DM-only):
  /auctionleague /al /rcpl   the hub — start, continue or review a career;
                             /rcpl IPL goes straight to that league's teams
  /rcpl help  /alhelp        the full guide (works in groups too)
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
            form = AL.FORM_ICON.get(AL.form_level(state, c["id"]), "")
            hurt = " 🚑" if AL.injured(state, c["id"]) else ""
            lines.append("• " + _card_line(c, e["price"], e["how"])
                         + (f" {form}" if form else "") + hurt)
    if state.get("form") or state.get("injuries"):
        lines.append("\n<i>▲ in form · ▼ out of form (±1–2 OVR next match) · 🚑 injured</i>")
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
        f"Base price: {money(lot['base'])} · 💡 fair value ≈ "
        f"{money(int(AL.fair_price(state, c['rating']) // 5 * 5))}",
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
    status = lot.get("status")
    buyer = state["teams"].get(lot.get("leader") or "", {}).get("short", "?")
    if status == "rtm_intent":
        lines += ["", f"🔁 <b>Right To Match!</b> {_esc(c['name'])} was yours — "
                      f"{_esc(buyer)} bought him for {money(lot['price'])}. Use your RTM card? "
                      f"They get one final raise, then you match it or let him go."]
    elif status == "rtm":
        raise_txt = (f"raised by {money(lot['rtm_raise'])} to {money(lot['price'])}"
                     if lot.get("rtm_raise") else f"stood at {money(lot['price'])}")
        lines += ["", f"🔁 {_esc(buyer)} {raise_txt}. <b>Match it</b> and keep him?"]
    elif status == "rtm_raise":
        holder = state["teams"].get(lot.get("rtm_team") or "", {}).get("short", "?")
        lines += ["", f"🔁 <b>{_esc(holder)} used their Right To Match!</b> You get one "
                      f"final raise — then they match it or he's yours."]
    mode = "⚡ Fast — the AI teams bid among themselves first" if state.get("fast") \
        else "🐢 Bid by bid — you answer every raise"
    lines.append(f"{mode}\n⏱️ {LOT_SECONDS}s to answer")
    return "\n".join(lines)


def _lot_rows(state, lot):
    seq = lot["seq"]
    status = lot.get("status")
    if status == "rtm_intent":
        return [[("🔁 Use RTM", f"al:rtm:{seq}:1"), ("❌ Let him go", f"al:rtm:{seq}:0")]]
    if status == "rtm":
        return [[(f"✅ Match {money(lot['price'])}", f"al:rtm:{seq}:1"),
                 ("❌ Let him go", f"al:rtm:{seq}:0")]]
    if status == "rtm_raise":
        row = [(f"⬆️ {money(p)}", f"al:rtr:{seq}:{p}") for p in AL.rtm_raise_options(state)]
        return [row, [(f"✋ Stand at {money(lot['price'])}", f"al:rtr:{seq}:0")]] if row \
            else [[(f"✋ Stand at {money(lot['price'])}", f"al:rtr:{seq}:0")]]
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
            [(fast, f"al:fast:{seq}"), ("⏸ Pause", f"al:pause:{seq}"), ("❓ Help", "al:help")],
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


FORM_ICON = {"W": "W", "L": "L", "T": "T"}
QE_MARK = {"Q": "✅Q", "E": "❌E"}


def _table_text(state):
    table = state.get("table") or {}
    marks = AL.qualification(state)
    lines = ["📊 <b>Points table</b>", "<pre>",
             "#  Team   P  W  L Pts    NRR  Form"]
    for i, name in enumerate(AL.standings(state), 1):
        r = table[name]
        form = "".join(AL.recent_form(state, name)) or "-"
        mark = QE_MARK.get(marks.get(name), "")
        me = " ◀" if name == state["user_team"] else ""
        lines.append(f"{i:<2} {_short(state, name):<5} {r['p']:>2} {r['w']:>2} {r['l']:>2} "
                     f"{r['pts']:>3} {AL.nrr(r):>+6.2f}  {form:<5} {mark}{me}")
    lines.append("</pre>")
    lines.append("<i>Form: last five, oldest first · ✅Q through · ❌E out · "
                 "Top 4 reach the playoffs: Q1 (1v2), Eliminator (3v4), Q2, Final.</i>")
    return "\n".join(lines)


def _where(state, fx):
    venue = fx.get("venue")
    pitch = fx.get("pitch")
    bits = [f"🏟️ {_esc(venue)}" if venue else "", f"🌱 {_esc(pitch)}" if pitch else ""]
    return " · ".join(b for b in bits if b)


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
    where = _where(state, fx)
    return f"#{fx['no']} {stage}{h} v {a}" + (f" · {where}" if where else "")


def _result_digest(state, played):
    if not played:
        return []
    blocks = ["📅 <b>Around the league</b>"]
    for fx in played:
        blocks.append(_fixture_line(state, fx))
    news = _injury_news(state, played)
    if news:
        blocks.append("🚑 <b>Team news</b>\n" + "\n".join(news))
    return blocks


def _injury_news(state, fixtures):
    lines = []
    for fx in fixtures:
        for n in fx.get("news") or []:
            who = _who(state, n["pid"])
            if n["kind"] == "injured":
                lines.append(f"• {who} injured — out for {n['matches']} "
                             f"match{'es' if n['matches'] > 1 else ''}")
            elif n["kind"] == "fit":
                lines.append(f"• {who} is fit again")
            elif n["kind"] == "replacement":
                lines.append(f"• {who} signed as an injury replacement")
    return lines


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


def _who(state, pid):
    c = AL.card(state, pid)
    owner = AL.owner_of(state, pid)
    return f"{_esc(c['name'])} ({_esc(_short(state, owner) if owner else '?')})"


STAT_MENU = (
    ("🟠 Orange Cap", "orange"), ("🟣 Purple Cap", "purple"), ("⭐ MVP", "mvp"),
    ("💥 Most 6s", "sixes"), ("⚡ Best SR", "sr"), ("🎯 Economy", "econ"),
    ("🏏 Top scores", "innings"), ("🔥 Best figures", "figures"),
    ("📊 Team totals", "totals"), ("👥 My squad", "mine"),
)


def _stats_rows():
    rows, row = [], []
    for label, key in STAT_MENU:
        row.append((label, f"al:st:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _econ(r):
    return r["runs"] * 6 / r["balls"] if r.get("balls") else 0.0


def _sr(r):
    return r["runs"] * 100 / r["balls"] if r.get("balls") else 0.0


def _stats_text(state, board="orange"):
    """One stats board. Every board reads ``AL.stat_board``."""
    title = dict((k, l) for l, k in STAT_MENU).get(board, "Stats")
    if not state["stats"]["bat"] and not state["stats"]["bowl"]:
        return f"{title}\n\nNo matches played yet."
    rows = AL.stat_board(state, board)
    lines = [f"<b>{title}</b>"]
    if board == "orange":
        for i, (pid, r) in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, pid)} — <b>{r['runs']}</b> runs · "
                         f"HS {r['hs']}{'*' if r.get('hs_not_out') else ''} · "
                         f"SR {_sr(r):.1f} · 50s {r.get('fifties', 0)} · 100s {r.get('hundreds', 0)}")
    elif board == "purple":
        for i, (pid, r) in enumerate(rows, 1):
            best = r.get("best") or [0, 0]
            lines.append(f"{i}. {_who(state, pid)} — <b>{r['wkts']}</b> wkts · "
                         f"econ {_econ(r):.2f} · best {best[0]}/{best[1]}")
    elif board == "mvp":
        lines.append("<i>Run 1 · four +1 · six +2 · wicket 25 · maiden 8</i>")
        for i, (pid, r) in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, pid)} — <b>{r['points']}</b> pts")
    elif board == "sixes":
        for i, (pid, r) in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, pid)} — <b>{r.get('sixes', 0)}</b> sixes · "
                         f"{r.get('fours', 0)} fours")
    elif board == "sr":
        lines.append(f"<i>Min {AL.MIN_SR_BALLS} balls faced</i>")
        for i, (pid, r) in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, pid)} — <b>{_sr(r):.1f}</b> · "
                         f"{r['runs']} off {r['balls']}")
    elif board == "econ":
        lines.append(f"<i>Min {AL.MIN_ECON_BALLS // 6} overs</i>")
        for i, (pid, r) in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, pid)} — <b>{_econ(r):.2f}</b> · "
                         f"{r['wkts']} wkts in {r['balls'] // 6}.{r['balls'] % 6} ov")
    elif board == "innings":
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, r['pid'])} — <b>{r['runs']}"
                         f"{'*' if r.get('not_out') else ''}</b> ({r['balls']}) v "
                         f"{_esc(_short(state, r['opp']) if r.get('opp') in state['teams'] else '?')}"
                         f" · #{r.get('fx')}")
    elif board == "figures":
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {_who(state, r['pid'])} — <b>{r['wkts']}/{r['runs']}</b> v "
                         f"{_esc(_short(state, r['opp']) if r.get('opp') in state['teams'] else '?')}"
                         f" · #{r.get('fx')}")
    elif board == "totals":
        def _t(r):
            return (f"{_esc(_short(state, r['team']))} <b>{r['runs']}/{r['wkts']}</b> v "
                    f"{_esc(_short(state, r['opp']))} · #{r['fx']}")
        lines.append("⬆️ Highest")
        lines += [f"• {_t(r)}" for r in rows["high"]] or ["—"]
        lines.append("⬇️ Lowest (all out or full overs)")
        lines += [f"• {_t(r)}" for r in rows["low"]] or ["—"]
    elif board == "mine":
        lines.append(f"<i>{_esc(state['user_team'])}</i>")
        for pid, b, w in sorted(rows, key=lambda x: -AL.mvp_points(state, x[0])):
            c = AL.card(state, pid)
            bat = f"{b.get('runs', 0)} r ({b.get('balls', 0)}b)" if b else "—"
            bowl = f"{w.get('wkts', 0)} w, econ {_econ(w):.1f}" if w else "—"
            lines.append(f"• {_esc(c['name'])}: 🏏 {bat} · 🎯 {bowl}")
    if len(lines) == 1:
        lines.append("Nobody qualifies yet.")
    return "\n".join(lines)


def _caps_text(state):
    """Orange and Purple Cap top fives (the season-end message)."""
    return _stats_text(state, "orange") + "\n\n" + _stats_text(state, "purple")


def _awards_text(state):
    aw = AL.season_awards(state)
    lines = ["🏆 <b>Season awards</b>"]
    labels = (("orange", "🟠 Orange Cap"), ("purple", "🟣 Purple Cap"),
              ("mvp", "⭐ Most Valuable Player"), ("sixes", "💥 Most Sixes"),
              ("emerging", "🌱 Emerging Player"))
    for key, label in labels:
        if key in aw:
            pid, head = aw[key]
            lines.append(f"{label}: <b>{_who(state, pid)}</b> — {head}")
    if aw.get("xi"):
        lines.append("\n🌟 <b>Team of the Tournament</b>")
        for i, c in enumerate(aw["xi"], 1):
            lines.append(f"{i:>2}. {_who(state, c['id'])} · {AL.ROLE_SHORT[c['category']]}")
    return "\n".join(lines)


def _hub(state, row):
    me = state["user_team"]
    lg = state["league"]["name"]
    head = (f"🏏 <b>{_esc(lg)} Auction League</b>\n"
            f"Your franchise: <b>{_esc(me)}</b> · AI: {DIFF_LABEL.get(state.get('difficulty'), '')}")
    phase = state["phase"]
    if phase == AL.PHASE_RETENTION:
        return head + f"\n\n🔒 Retention window is open.\n\n{HELP_LINE}", [
            [("🔒 Retentions", "al:rtview")],
            [("❓ Help", "al:help"), ("🗑 Discard career", "al:quit")]]
    if phase == AL.PHASE_AUCTION:
        status = "⏸ The auction is <b>paused</b>" if state.get("paused") \
            else "🔨 The auction is live"
        return (head + f"\n\n{status} — set {state['set_idx'] + 1}"
                f"/{len(state['sets'])}.\n{_purse_line(state)}\n\n{HELP_LINE}"), [
            [("▶️ Resume auction", "al:resume")],
            [("📦 Sets", "al:sets"), ("📋 My squad", "al:squad"), ("💰 Purses", "al:purse")],
            [("❓ Help", "al:help"), ("🗑 Discard career", "al:quit")]]
    if phase == AL.PHASE_SEASON:
        fx = AL.next_user_fixture(state)
        nxt = (f"Next: {_fixture_line(state, fx)}" if fx else "You're out — the rest is simulated.")
        pos = AL.standings(state).index(me) + 1
        played = any(r["p"] for r in (state.get("table") or {}).values())
        where = f" · you're <b>#{pos}</b> in the table" if played else ""
        rows = [[("▶️ Next fixture", f"al:play:{row.id}")]]
        if AL.trade_window(state) is not None:
            rows.append([("🔁 Trade window", "al:trade")])
        rows += [[("📊 Table", f"al:table:{row.id}"), ("📅 Fixtures", "al:fix"),
                  ("📈 Stats", "al:st:orange")],
                 [("📋 My squad", "al:squad"), ("❓ Help", "al:help"), ("🗑 Discard", "al:quit")]]
        return (head + f"\n\n📅 Season under way{where}\n{nxt}\n\n{HELP_LINE}"), rows
    return _season_end_text(state), [[("🆕 New career", "al:new"), ("❓ Help", "al:help")],
                                     [("📊 Final table", f"al:table:{row.id}"),
                                      ("📈 Stats", "al:st:orange"), ("🏆 Awards", "al:awards")]]


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
    for name in (f"al_lot_{tg_id}", f"al_call_{tg_id}"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()


def _arm_timer(context, tg_id, chat_id, seq):
    _cancel_timer(context, tg_id)
    jq = getattr(context, "job_queue", None)
    if not jq:
        return
    jq.run_once(_lot_timeout, LOT_SECONDS, name=f"al_lot_{tg_id}",
                data={"tg_id": tg_id, "chat_id": chat_id, "seq": seq})
    # The auctioneer: "Going once…" and "Going twice…" as the clock runs down.
    for left, call in ((CALL_ONCE_AT, 1), (CALL_TWICE_AT, 2)):
        jq.run_once(_auctioneer_call, LOT_SECONDS - left, name=f"al_call_{tg_id}",
                    data={"tg_id": tg_id, "chat_id": chat_id, "seq": seq, "call": call})


CALL_ONCE_AT = 12      # seconds left on the clock
CALL_TWICE_AT = 6
CALL_LINES = {1: "🔔 <b>Going once…</b>", 2: "🔔🔔 <b>Going twice…</b>"}


async def _auctioneer_call(context):
    data = context.job.data or {}
    tg_id = data.get("tg_id")
    async with _lock(context, tg_id):
        car = _open(tg_id)
        try:
            state = car.state
            if not state or state["phase"] != AL.PHASE_AUCTION or state.get("paused"):
                return
            lot = state.get("lot")
            mid = (state.get("ui") or {}).get("lot_msg")
            if not lot or lot["seq"] != data.get("seq") or not mid:
                return
            text = _lot_text(state, lot) + "\n" + CALL_LINES[data["call"]]
            try:
                await context.bot.edit_message_text(
                    text, chat_id=data["chat_id"], message_id=mid, parse_mode="HTML",
                    reply_markup=_kb(_lot_rows(state, lot)))
            except Exception:
                pass
        finally:
            car.session.close()


# ── Accelerated-round nominations ─────────────────────────────────────

def _noms_text(state):
    picked = state.get("noms_draft") or []
    lines = ["⚡ <b>Accelerated round — nominations</b>",
             "Every set is done. As at the IPL, only the unsold players somebody "
             f"nominates come back. The AI sides pick theirs; tap up to "
             f"{AL.ACCEL_NOMS_USER} of yours, then ✅ Done.",
             f"\nPicked: <b>{len(picked)}</b>/{AL.ACCEL_NOMS_USER}"]
    return "\n".join(lines)


def _noms_rows(state):
    picked = set(state.get("noms_draft") or [])
    rows = []
    for c in AL.nomination_choices(state, 24):
        mark = "✅ " if c["id"] in picked else ""
        rows.append([(f"{mark}{c['name']} · {c['rating']} {_role(c)} · "
                      f"{money(AL.base_price(c['rating']))}", f"al:nom:{c['id']}")])
    rows.append([("✅ Done", "al:nomok")])
    return rows


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
        if lot["status"] in ("open",) + AL.RTM_WAITING and state.get("lot") is lot:
            break
        # Decided without you (you couldn't afford or fit him).
        batch.append(AL.lot_result_line(state, lot))
        ui["reported"] = lot["seq"]
    if state.get("lot") is None:
        car.save()
        if batch:
            await _send_long(context, chat_id, batch)
        if state.get("awaiting_noms"):
            # The sets are done: nominations for the accelerated round.
            _cancel_timer(context, tg_id)
            await _send(context, chat_id, _noms_text(state), _noms_rows(state))
            return
        # Nothing left to sell: close the auction and start the season.
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
                f"{mine} for you, then the playoffs (top 4). Every match is at the hosts' "
                f"ground on a pitch drawn at random — /alfixtures shows them all.",
                [[("📅 Fixtures", "al:fix"), ("📊 Table", f"al:table:{car.row.id}")]])
    await _send_trade_window(context, chat_id, state, car.row.id)


# ── Trade window ──────────────────────────────────────────────────────

def _trade_text(state):
    t = AL.trade_window(state) or state.get("trade") or {}
    lines = ["🔁 <b>Trade window</b> — open until your first match"]
    news = t.get("news") or []
    if news:
        lines.append("\n📰 <b>Trade news</b>")
        for n in news:
            a, b = AL.card(state, n["pa"]), AL.card(state, n["pb"])
            lines.append(f"• {_esc(_short(state, n['a']))} send {_esc(a['name'])} "
                         f"({a['rating']} {_role(a)}) to {_esc(_short(state, n['b']))} for "
                         f"{_esc(b['name'])} ({b['rating']} {_role(b)})")
    offers = [o for o in t.get("offers") or [] if o["status"] == "open"]
    if offers:
        lines.append("\n📨 <b>Offers for you</b>")
        for o in offers:
            give, want = AL.card(state, o["give"]), AL.card(state, o["want"])
            lines.append(f"{o['id']}. <b>{_esc(o['team'])}</b> offer {_esc(give['name'])} "
                         f"({give['rating']} {_role(give)}) for your {_esc(want['name'])} "
                         f"({want['rating']} {_role(want)})")
    elif not news:
        lines.append("\nQuiet window — no approaches this time.")
    left = max(0, int(t.get("max", 0)) - int(t.get("done", 0)))
    lines.append(f"\nTrades you can still make: <b>{left}</b>. Swaps are one for one; the "
                 "AI only agrees to fair ones, and both squads must stay legal.")
    return "\n".join(lines)


def _trade_rows(state, save_id):
    t = AL.trade_window(state)
    if t is None:
        return [[("▶️ Next fixture", f"al:play:{save_id}")]]
    rows = []
    for o in t.get("offers") or []:
        if o["status"] == "open":
            rows.append([(f"✅ Accept {o['id']}", f"al:tro:{o['id']}:1"),
                         (f"❌ Reject {o['id']}", f"al:tro:{o['id']}:0")])
    if int(t["done"]) < int(t["max"]):
        rows.append([("🔁 Propose a trade", "al:trg")])
    rows.append([("✅ Close window", "al:trx"), ("▶️ Play first match", f"al:play:{save_id}")])
    return rows


async def _send_trade_window(context, chat_id, state, save_id):
    if AL.trade_window(state) is None:
        return
    await _send(context, chat_id, _trade_text(state), _trade_rows(state, save_id))


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
            if lot["status"] in ("rtm_intent", "rtm"):
                AL.user_rtm(state, False)
            elif lot["status"] == "rtm_raise":
                AL.user_final_raise(state, None)
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

HELP_WORDS = ("help", "guide", "rules", "?")


def help_text():
    """The full Auction League guide for /rcpl help."""
    retain = " / ".join(money(p) for p in AL.RETENTION_PRICES)
    champ, runner, playoffs = AL.REWARD_CHAMPION, AL.REWARD_RUNNER_UP, AL.REWARD_PLAYOFFS
    return (
        "🏏 <b>Auction League — help</b>\n"
        "Your solo IPL-style career: run one franchise, the AI runs the rest. "
        "Played in a private chat with me.\n\n"
        "🚀 <b>Getting started</b>\n"
        "<blockquote expandable>"
        "• <code>/rcpl</code> — pick a league, or <code>/rcpl IPL</code> to jump "
        "straight to its teams\n"
        "• Pick your franchise, then set AI retentions (on/off) and AI difficulty\n"
        f"• Entry fee: <b>{ENTRY_FEE_GEMS} 💎</b>, paid when you tap ✅ Start career\n"
        "• One career at a time — finish it or discard it before a new one"
        "</blockquote>\n"
        "🔒 <b>Retention</b>\n"
        "<blockquote expandable>"
        f"• Keep up to {AL.MAX_RETAIN} of your own players: {retain}\n"
        "• With AI retentions on, each AI side keeps up to 3 of its stars "
        "(the league's top 20%)\n"
        "• Keep 2 or fewer and you get one 🔁 Right To Match card: when a former "
        "player of yours is sold, you may match the price and keep him"
        "</blockquote>\n"
        "🔨 <b>The auction</b>\n"
        "<blockquote expandable>"
        f"• Every purse starts at <b>{money(AL.PURSE_LAKH)}</b>\n"
        "• Sets run ⭐ Marquee → rating bands by role (Batsmen 90–87, "
        "All-rounders 90–87…) → 🌱 Emerging → ⚡ Accelerated (the unsold)\n"
        f"• Bid by bid: after every AI raise it's your call — {LOT_SECONDS}s per turn, "
        "no answer counts as a pass\n"
        "• 🔨 <b>Bid</b> — the next step · 🚀 <b>Jump</b> — ₹50 L higher "
        "(₹1 Cr from ₹5 Cr) · 🙅 <b>Pass</b> — drop out\n"
        "• ⚡ <b>Fast</b> — let the AI sides settle among themselves before you're asked\n"
        "• ⏭ <b>Skip player</b> — decide this player now (your side bids on autopilot)\n"
        "• ⏩ <b>Sim set</b> / ⏭⏭ <b>Sim to end</b> — simulate the rest of the set "
        "or the whole auction, and get who went where\n"
        "• ⏸ <b>Pause</b> — stop the clock; ▶️ <b>Resume</b> when you're back\n"
        f"• AI franchises bid like real ones and spend their purses down — "
        f"stars can reach {money(AL.RECORD_PRICE)}"
        "</blockquote>\n"
        "🔁 <b>Trade window</b>\n"
        "<blockquote expandable>"
        "• Opens after the auction, closes when your first match starts\n"
        "• The AI sides trade among themselves to even the league out (trade news)\n"
        "• Now and then a side approaches you — ✅ accept or ❌ reject\n"
        f"• Propose your own one-for-one swaps (/altrade) — up to {AL.TRADE_MAX}; "
        "the AI only agrees to fair ones"
        "</blockquote>\n"
        "👥 <b>Squad rules</b>\n"
        "<blockquote expandable>"
        f"• At least {AL.ROLE_MIN[AL.ROLE_BAT]} BAT, {AL.ROLE_MIN[AL.ROLE_BOWL]} BOWL, "
        f"{AL.ROLE_MIN[AL.ROLE_WK]} WK, {AL.ROLE_MIN[AL.ROLE_AR]} AR · at most "
        f"{AL.SQUAD_MAX} players and {AL.OVERSEAS_SQUAD_CAP} overseas\n"
        "• You can never bid more than leaves enough to finish a legal squad\n"
        "• Still short at the end? You're topped up from the unsold players at "
        "base price"
        "</blockquote>\n"
        "📅 <b>The season</b>\n"
        "<blockquote expandable>"
        "• Everyone plays everyone once, then the playoffs: Qualifier 1 (1 v 2), "
        "Eliminator (3 v 4), Qualifier 2, Final\n"
        "• Every match is at the hosts' ground on a pitch drawn at random\n"
        f"• Your matches: {AL.OVERS} overs, ball by ball — Playing XI, toss, Impact "
        "Player. Other matches are simulated instantly\n"
        "• AI sides field 4 BAT · 1 WK · 3 AR · 3 BOWL, best batters at the top\n"
        "• Points table with NRR, last-five form and ✅Q / ❌E marks\n"
        "• /alstats — Orange & Purple Caps, MVP, 6s, strike rate, economy, top "
        "scores, best figures, team totals, your squad\n"
        "• Season awards and a Team of the Tournament at the end\n"
        "• 🏳️ Concede a match if you must — it counts as a loss and forfeits the "
        "season reward"
        "</blockquote>\n"
        "🏆 <b>Rewards</b>\n"
        "<blockquote expandable>"
        f"• Champions {champ[0]:,} coins + {champ[1]} 💎 · runners-up "
        f"{runner[0]:,} + {runner[1]} 💎 · playoffs {playoffs[0]:,} coins\n"
        f"• One paid season every {AL.REWARD_COOLDOWN_HOURS} hours; matches "
        "themselves are unranked practice"
        "</blockquote>\n"
        "⌨️ <b>Commands</b>\n"
        "<blockquote expandable>"
        "/rcpl [league] — start or open your career · /rcpl help — this guide\n"
        "/simset [n] — sim the live set (or up to set n) · /simtolast — sim the auction\n"
        "/skipplayer — decide the player on the block\n"
        "/alpause · /alresume — pause / resume the auction\n"
        "/alsets · /alsquad · /alpurse — sets, your squad, every purse\n"
        "/altrade — the trade window (before your first match)\n"
        "/alplay — your next match · /altable · /alfixtures · /alstats\n"
        "/alquit — discard your career"
        "</blockquote>\n\n"
        f"{HELP_LINE}"
    )


async def _send_help(context, chat_id):
    await _send(context, chat_id, help_text())


async def alhelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alhelp — the Auction League guide (works anywhere)."""
    await _send_help(context, update.effective_chat.id)


async def auction_league_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/auctionleague — the hub; ``/rcpl help`` for the guide."""
    args = [a for a in (context.args or []) if a]
    if args and args[0].lower() in HELP_WORDS:
        # The guide works anywhere — it only explains. The mode itself is DM-only.
        await _send_help(context, update.effective_chat.id)
        return
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
    "💡 Tip: <code>/rcpl IPL</code> jumps straight to a league's teams · "
    "<code>/rcpl help</code> for the full guide.\n"
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
        rows.append([("❓ How it works", "al:help")])
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
            await q.answer("Done" if arg == "1" else "Let go")
        elif action == "rtr":
            AL.user_final_raise(state, int(arg) or None)
            await q.answer("Final raise in" if int(arg) else "You stand")
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
    AL.fixture_pitch(state, fx)
    side = "🏠 Home" if fx["home"] == me else ("✈️ Away" if fx["stage"] == AL.STAGE_LEAGUE
                                              else "⚖️ Neutral venue")
    where = f"{side} · {_where(state, fx)}"
    pos = AL.standings(state)
    played = any(r["p"] for r in (state.get("table") or {}).values())

    def _rank(team):
        return f" (#{pos.index(team) + 1})" if played else ""

    text = (f"🏏 <b>{AL.STAGE_LABEL[fx['stage']]} · Match #{fx['no']}</b>\n"
            f"<b>{_esc(me)}</b>{_rank(me)} vs <b>{_esc(opp)}</b>{_rank(opp)}\n"
            f"{where}\n\n"
            f"Opponent strength: {AL.team_strength(state, opp)['ovr']} OVR · "
            f"yours {AL.team_strength(state, me)['ovr']} OVR")
    mine_news = _injury_news(state, [f for f in state["fixtures"] if f["status"] == "done"
                                     and me in (f["home"], f["away"])][-1:])
    if mine_news:
        text += "\n\n🚑 <b>Team news</b>\n" + "\n".join(mine_news)
    out_now = [_esc(AL.card(state, p)["name"]) for p in (state.get("injuries") or {})
               if AL.owner_of(state, p) == me]
    if out_now:
        text += f"\n🚑 Unavailable for you: {', '.join(out_now)}"
    tip = AL.PITCH_TIPS.get(fx.get("pitch") or "")
    if tip:
        text += f"\n🌱 {_esc(fx['pitch'])}: {tip}"
    form = "".join(AL.recent_form(state, opp))
    if form:
        text += f"\n{_esc(_short(state, opp))} form: {form}"
    if AL.trade_window(state) is not None:
        text += "\n\n🔁 The trade window closes when this match starts."
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
    await _send(context, chat_id, _awards_text(state))
    await _send(context, chat_id, text,
                [[("📊 Final table", f"al:table:{car.row.id}"), ("📈 Stats", "al:st:orange")],
                 [("🆕 New career", "al:new")]])


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
    pitch = AL.fixture_pitch(state, fx)
    if AL.trade_window(state) is not None:
        # The window shuts as the first ball of your season approaches.
        AL.close_trade_window(state)
    car.save()
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
        host_squad=AL.match_cards(state, me),
        target_squad=AL.match_cards(state, opp),
        tag=tag, home_team=fx["home"],
        team_codes={n: state["teams"][n]["short"] for n in (me, opp)},
        session=session, pitch=pitch, venue=fx.get("venue"))
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
    """/alstats — the tournament stats hub (Orange Cap first)."""
    if not await _require_dm(update, context):
        return
    car = _open(update.effective_user.id, include_completed=True)
    try:
        if car.row is None or car.state["phase"] not in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
            await update.effective_message.reply_text(
                "❌ Stats start once your season does. /auctionleague to see your career.")
            return
        await _send(context, update.effective_chat.id,
                    _stats_text(car.state, "orange"), _stats_rows())
    finally:
        car.session.close()


async def altrade_handler(update, context):
    """/altrade — the pre-season trade window."""
    if not await _require_dm(update, context):
        return
    car = _open(update.effective_user.id)
    try:
        if car.row is None or AL.trade_window(car.state) is None:
            await update.effective_message.reply_text(
                "🔁 The trade window is closed. It opens after the auction and "
                "closes when your first match starts.")
            return
        await _send_trade_window(context, update.effective_chat.id, car.state, car.row.id)
    finally:
        car.session.close()


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

async def _cb_trade(q, context, car, action, args):
    """The trade window: offers, proposals, closing it."""
    state = car.state
    chat_id = q.message.chat_id
    if AL.trade_window(state) is None:
        await q.answer("The trade window is closed.", show_alert=True)
        return
    me = state["user_team"]
    if action == "trade":
        await q.answer()
        await _send_trade_window(context, chat_id, state, car.row.id)
    elif action == "trx":
        AL.close_trade_window(state)
        car.save()
        await q.answer("Window closed")
        await _send(context, chat_id, "🔒 Trade window closed — squads are locked for the season.",
                    [[("▶️ Play first match", f"al:play:{car.row.id}")]])
    elif action == "tro" and len(args) >= 2:
        try:
            offer = AL.answer_offer(state, int(args[0]), args[1] == "1")
        except AuctionLeagueError as exc:
            car.save()
            await q.answer(str(exc), show_alert=True)
            return
        car.save()
        if offer["status"] == "accepted":
            give, want = AL.card(state, offer["give"]), AL.card(state, offer["want"])
            await q.answer("Trade done!")
            await _send(context, chat_id, f"✅ <b>Trade done</b> — {_esc(give['name'])} joins "
                        f"you from {_esc(offer['team'])}; {_esc(want['name'])} goes the other way.")
        else:
            await q.answer("Offer rejected")
        try:
            await q.edit_message_text(_trade_text(state), parse_mode="HTML",
                                      reply_markup=_kb(_trade_rows(state, car.row.id)))
        except Exception:
            await _send_trade_window(context, chat_id, state, car.row.id)
    elif action == "trg" and not args:
        await q.answer()
        rows = [[(f"{c['name']} · {c['rating']} {_role(c)}", f"al:trg:{c['id']}")]
                for c in AL.squad_card_dicts(state, me)]
        await _send(context, chat_id, "🔁 Which of <b>your</b> players do you offer?", rows)
    elif action == "trg":
        await q.answer()
        pid = int(args[0])
        rows, row = [], []
        for i, name in enumerate(state["team_order"]):
            if name == me:
                continue
            row.append((state["teams"][name]["short"], f"al:trt:{pid}:{i}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await _send(context, chat_id, f"🔁 Offer <b>{_esc(AL.card(state, pid)['name'])}</b> to "
                    "which franchise?", rows)
    elif action == "trt" and len(args) >= 2:
        await q.answer()
        pid, team = int(args[0]), state["team_order"][int(args[1])]
        rows = [[(f"{c['name']} · {c['rating']} {_role(c)}",
                  f"al:trp:{pid}:{args[1]}:{c['id']}")]
                for c in AL.squad_card_dicts(state, team)]
        await _send(context, chat_id, f"🔁 Which <b>{_esc(team)}</b> player do you want for "
                    f"{_esc(AL.card(state, pid)['name'])}?", rows)
    elif action == "trp" and len(args) >= 3:
        pid, team, theirs = int(args[0]), state["team_order"][int(args[1])], int(args[2])
        try:
            ok, reason = AL.propose_trade(state, pid, team, theirs)
        except AuctionLeagueError as exc:
            await q.answer(str(exc), show_alert=True)
            return
        car.save()
        await q.answer("Accepted!" if ok else "Rejected")
        a, b = AL.card(state, pid), AL.card(state, theirs)
        head = (f"✅ <b>Trade done</b> — {_esc(b['name'])} joins you; {_esc(a['name'])} "
                f"goes to {_esc(team)}." if ok else f"❌ {_esc(reason)}")
        await _send(context, chat_id, head + "\n\n" + _trade_text(state),
                    _trade_rows(state, car.row.id))
    else:
        await q.answer()


async def auction_league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    tg_id = q.from_user.id
    chat_id = q.message.chat_id if q.message else tg_id

    # Setup steps (and the guide) need no save.
    if action == "help":
        await q.answer()
        await _send_help(context, chat_id)
        return
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
        car = _open(tg_id, include_completed=action in (
            "table", "caps", "fix", "squad", "hub", "st", "awards"))
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
            elif action in ("rtm", "rtr") and len(args) >= 2:
                await _cb_lot(q, context, car, action, args[0], args[1])
            elif action in ("nom", "nomok"):
                if not state.get("awaiting_noms"):
                    await q.answer("Nominations are closed.")
                    return
                if action == "nom" and args:
                    picked = list(state.get("noms_draft") or [])
                    pid = int(args[0])
                    if pid in picked:
                        picked.remove(pid)
                    elif len(picked) >= AL.ACCEL_NOMS_USER:
                        await q.answer(f"At most {AL.ACCEL_NOMS_USER}.", show_alert=True)
                        return
                    else:
                        picked.append(pid)
                    state["noms_draft"] = picked
                    car.save()
                    await q.answer()
                    try:
                        await q.edit_message_text(_noms_text(state), parse_mode="HTML",
                                                  reply_markup=_kb(_noms_rows(state)))
                    except Exception:
                        pass
                else:
                    AL.submit_nominations(state, state.pop("noms_draft", []) or [])
                    await q.answer("Nominations in")
                    try:
                        await q.edit_message_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    await _advance(context, car, chat_id, tg_id,
                                   note="⚡ <i>The accelerated round begins.</i>")
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
            elif action == "st":
                board = args[0] if args and args[0] in AL.STAT_BOARDS else "orange"
                await q.answer()
                if state["phase"] not in (AL.PHASE_SEASON, AL.PHASE_COMPLETED):
                    await _send(context, chat_id, "Stats start once your season does.")
                    return
                text = _stats_text(state, board)
                try:
                    await q.edit_message_text(text, parse_mode="HTML",
                                              reply_markup=_kb(_stats_rows()))
                except Exception:
                    await _send(context, chat_id, text, _stats_rows())
            elif action == "awards":
                await q.answer()
                if state["phase"] != AL.PHASE_COMPLETED:
                    await _send(context, chat_id, "The awards come at the end of the season.")
                    return
                await _send(context, chat_id, _awards_text(state))
            elif action in ("trade", "trg", "trt", "trp", "tro", "trx"):
                await _cb_trade(q, context, car, action, args)
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
                AL.close_trade_window(state)
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
    app.add_handler(CommandHandler("altrade", altrade_handler))
    app.add_handler(CommandHandler(["alhelp", "rcplhelp"], alhelp_handler))
    app.add_handler(CommandHandler("alresume", alresume_handler))
    app.add_handler(CallbackQueryHandler(auction_league_callback, pattern=r"^al:"))
