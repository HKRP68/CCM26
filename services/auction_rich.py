"""Every Franchise Auction surface as a rich message, beside its HTML twin.

``services/auction_service.py`` keeps the rules and the plain-HTML renderers
(``render_board``, ``render_purses``, ``render_squad``). This module adds the
Bot API 10.1 rich-message rendering of the same things — native tables,
collapsible sections, headings — and the richer HTML the announcements are
sent in. It follows the recipe in ``docs/rich-text-messages.md``: every
``*_blocks`` builder here has an HTML rendering next to it, the two share the
logic that decides *content*, and a sender always passes the HTML as the
fallback so a refused rich message never loses the message.

Every builder returns ``(blocks, html)``. Nothing here commits or talks to
Telegram except the two ``send_*``/``edit_*`` helpers at the bottom, which the
scheduler and the handlers share.
"""

import logging
from datetime import datetime
from html import escape

from services import auction_service as A
from services import rich_message as R

logger = logging.getLogger(__name__)

# Telegram's ceiling for one text message. Long HTML lists are cut into parts
# under it rather than refused whole.
TEXT_LIMIT = 4096

# Rows per HTML quote in a long list, so one quote always fits one message and
# ``html_parts`` never has to cut inside it.
QUOTE_ROWS = 30

STATE_MARK = {"done": "✅", "live": "🔨", "next": "⏭", "queued": "⏳"}
STATE_WORD = {"done": "Done", "live": "Live", "next": "Next", "queued": "Queued"}


def _e(value):
    return escape(str(value if value is not None else ""))


def _money(season, lakh):
    return A.render_money(lakh, season.currency_label or "₹")


def _flag(lot):
    return "✈️" if lot.is_overseas else "🏠"


def _franchise(session, franchise_id):
    if not franchise_id:
        return None
    from models import AuctionFranchise
    return (session.query(AuctionFranchise)
            .filter(AuctionFranchise.id == franchise_id).first())


def _lot(session, lot_id):
    if not lot_id:
        return None
    from models import AuctionLot
    return session.query(AuctionLot).filter(AuctionLot.id == lot_id).first()


# ── Keyboards ────────────────────────────────────────────────────────

BID_CB = "au_bid_"
RTM_CB = "au_rtm_"
INFO_CB = "au_info_"

# The /ainfo menu. ``when`` decides whether a button is worth a slot: None is
# always, and the rest are asked of the season, because a menu offering
# 🔒 Retention to an auction that allows none is a button whose only answer is
# "not a thing here".
INFO_VIEWS = (
    ("rules", "📜 Rules", None),
    ("sets", "🗂 Sets", None),
    ("nextset", "⏭ Next Set", None),
    ("next", "👤 Next Player", None),
    ("squad", "👥 My Squad", None),
    ("purse", "💰 Purse", None),
    ("retention", "🔒 Retention", lambda season: A.retention_configured(season)),
    ("picks", "🆕 Picks", lambda season: A.expansion_configured(season)),
    ("sold", "✅ Sold", None),
    ("unsold", "❌ Unsold", None),
)


def bid_keyboard(season, lot):
    """Quick-bid buttons: the minimum, and one step above it.

    The exact amount rides in the callback data, so a button pressed after the
    price has moved bids a number that is no longer legal and is refused with
    "the price has moved" rather than quietly bidding the wrong thing.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if lot is not None and lot.status == A.LOT_RTM_OFFERED:
        # The holder's two answers. The top bidder's final raise is an ordinary
        # /bid, so it needs no button of its own.
        if lot.rtm_stage in (A.RTM_INTENT, A.RTM_DECISION):
            # The lot AND the stage ride in the callback data, for the same
            # reason the quick-bid buttons carry the exact price: a "yes" from
            # the *intent* prompt, pressed thirty seconds late, would otherwise
            # land as a MATCH at the decision stage — signing a player for the
            # raised number when the franchise only ever agreed to the old one.
            tag = f"{RTM_CB}{lot.id}_{lot.rtm_stage}_"
            label = ("🪪 Use RTM" if lot.rtm_stage == A.RTM_INTENT
                     else f"🪪 Match {A.render_money(A.rtm_price(season, lot), season.currency_label)}")
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(label, callback_data=f"{tag}yes"),
                InlineKeyboardButton("Pass", callback_data=f"{tag}no"),
            ]])
        return None
    if lot is None or lot.status != A.LOT_ON_BLOCK:
        return None
    minimum = A.next_min_bid(season, lot)
    step = A.increment_for(season, minimum)
    symbol = season.currency_label or "₹"
    row = [InlineKeyboardButton(f"Bid {A.render_money(minimum, symbol)}",
                                callback_data=f"{BID_CB}{lot.id}_{minimum}")]
    if minimum + step <= 1_000_000:
        row.append(InlineKeyboardButton(
            f"Bid {A.render_money(minimum + step, symbol)}",
            callback_data=f"{BID_CB}{lot.id}_{minimum + step}"))
    return InlineKeyboardMarkup([row])


def info_keyboard(season=None):
    """The /ainfo menu: every read-only view this auction has, one press each.

    ``season`` is optional so an older caller still gets the whole menu; passed,
    it drops the views this auction does not use.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [InlineKeyboardButton(label, callback_data=f"{INFO_CB}{key}")
               for key, label, when in INFO_VIEWS
               if when is None or season is None or when(season)]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


# ── The board ────────────────────────────────────────────────────────

def _purse_rows(session, season):
    header = [R.cell(R.bold("Franchise"), header=True),
              R.cell(R.bold("Purse"), header=True, align="right"),
              R.cell(R.bold("Squad"), header=True, align="center"),
              R.cell(R.bold("✈️"), header=True, align="center"),
              R.cell(R.bold("Max bid"), header=True, align="right")]
    rows = [header]
    for franchise in A.franchises(session, season.id):
        ceiling = max(0, A.max_bid_now(season, franchise))
        kept = int(franchise.retained_count or 0)
        rows.append([
            R.cell(franchise.name + (f" 🔒{kept}" if kept else "")),
            R.cell(R.bold(_money(season, franchise.purse_remaining_lakh)),
                   align="right"),
            R.cell(f"{franchise.squad_size}/{season.max_squad_size}",
                   align="center"),
            R.cell(f"{A.overseas_count(session, franchise.id)}/{season.max_overseas}",
                   align="center"),
            R.cell(_money(season, ceiling), align="right"),
        ])
    return rows


def board_blocks(session, season, lot=None, *, now=None):
    """The pinned board as a rich message: the lot, the price, every purse."""
    now = now or datetime.utcnow()
    lot = lot if lot is not None else A.current_lot(session, season)
    counts = A.pool_counts(session, season.id)
    done = counts.get(A.LOT_SOLD, 0) + counts.get(A.LOT_UNSOLD, 0)

    blocks = [R.heading(f"🏟 {season.name}", size=2),
              R.paragraph([R.bold(A.status_label(season)), "  ·  ",
                           f"📊 {done}/{counts.get('total', 0)} lots resolved"
                           + (f" · 🔒 {counts['retained']} retained"
                              if counts.get("retained") else "")])]
    if lot is None:
        blocks.append(R.paragraph(R.italic("No lot is on the block.")))
    else:
        left = A.seconds_left(lot, now)
        blocks.append(R.heading(f"🔨 Lot {lot.lot_no} · {lot.name}", size=3))
        blocks.append(R.table([
            [R.cell(R.bold("⭐ Rating")), R.cell(f"{lot.rating} OVR")],
            [R.cell(R.bold("🏏 Role")), R.cell(lot.category)],
            [R.cell(R.bold(f"{_flag(lot)} Country")), R.cell(lot.country)],
            [R.cell(R.bold("🗂 Set")), R.cell(A.set_label(lot))],
            [R.cell(R.bold("🏷 Base")),
             R.cell(_money(season, lot.base_price_lakh))],
        ], bordered=True, compact=True))
        if lot.current_bid_lakh is None:
            blocks.append(R.paragraph(["💰 No bids yet — opening at ",
                                       R.bold(_money(season, lot.base_price_lakh))]))
        else:
            bidder = _franchise(session, lot.current_bidder_id)
            blocks.append(R.paragraph([
                "💰 ", R.bold(_money(season, lot.current_bid_lakh)), " — ",
                R.bold(bidder.name if bidder else "?"),
                f"  ({lot.bid_count} bid{'s' if lot.bid_count != 1 else ''})"]))
        if lot.status != A.LOT_RTM_OFFERED or lot.rtm_stage == A.RTM_FINAL_OFFER:
            minimum = A.next_min_bid(season, lot)
            blocks.append(R.paragraph([
                "➡️ Next bid: ", R.code(f"/bid {A._bid_hint(minimum)}"),
                f" ({_money(season, minimum)})"]))
        if lot.status == A.LOT_RTM_OFFERED:
            holder = A.rtm_holder(session, lot)
            who = holder.name if holder else "the previous franchise"
            price = _money(season, A.rtm_price(season, lot))
            if lot.rtm_stage == A.RTM_INTENT:
                blocks.append(R.paragraph([
                    "🪪 ", R.bold("Right To Match"), f" — {who}, use it? ",
                    R.code("/artm yes"), " / ", R.code("/artm no")]))
            elif lot.rtm_stage == A.RTM_FINAL_OFFER:
                bidder = _franchise(session, lot.current_bidder_id)
                blocks.append(R.paragraph([
                    "🪪 ", R.bold("Right To Match"),
                    f" — {who} will use it. "
                    f"{bidder.name if bidder else '?'}: one final raise, or stand."]))
            elif lot.rtm_stage == A.RTM_DECISION:
                blocks.append(R.paragraph([
                    "🪪 ", R.bold("Right To Match"),
                    f" — {who}, match at {price}? ",
                    R.code("/artm yes"), " / ", R.code("/artm no")]))
        if season.status == A.STATUS_PAUSED:
            blocks.append(R.paragraph(["⏸ ", R.bold("Paused"),
                                       " — bidding is closed."]))
        elif left is not None:
            stage = ("" if lot.status == A.LOT_RTM_OFFERED else
                     {1: "going once", 2: "GOING TWICE"}.get(
                         A.going_stage_for(left), ""))
            parts = [f"⏳ {A.format_clock(left)} left"]
            if stage:
                parts += [" · ", R.bold(stage)]
            if lot.extensions_used and season.max_extensions:
                remaining = int(season.max_extensions) - int(lot.extensions_used)
                parts.append(f"  🛡 {lot.extensions_used} extension"
                             f"{'s' if lot.extensions_used != 1 else ''} used"
                             + (" · final" if remaining <= 0
                                else f" · {remaining} left"))
            blocks.append(R.paragraph(parts))
    blocks.append(R.divider())
    blocks.append(R.table(_purse_rows(session, season), bordered=True,
                          striped=True, compact=True,
                          caption=R.bold("💼 Purses")))
    blocks.append(R.footer(["Bid with ", R.code("/bid"), " · views: ",
                            R.code("/ainfo"), " · ", R.code("/arules"), " · ",
                            R.code("/asets"), " · ", R.code("/asquad")]))
    return blocks


def board_html(session, season, lot=None, *, now=None):
    """The HTML board, plus the footer that points at the team views."""
    body = A.render_board(session, season, lot, now=now)
    return (body + "\n\n<i>Views:</i> <code>/ainfo</code> · <code>/arules</code> "
            "· <code>/asets</code> · <code>/asquad</code> · "
            "<code>/asoldlist</code>")


# ── The lot card ─────────────────────────────────────────────────────

def lot_caption(session, season, lot):
    """The caption on a new lot's player card. HTML, under the 1024 limit."""
    lines = [f"🔨 <b>LOT {lot.lot_no} · {_e(lot.name).upper()}</b>",
             "<blockquote>"
             f"⭐ <b>{lot.rating}</b> OVR · {_e(lot.category)}\n"
             f"{_flag(lot)} {_e(lot.country)}"
             + (f" · {_e(lot.version)}" if lot.version and lot.version != "Base" else "")
             + f"\n🗂 Set: <b>{_e(A.set_label(lot))}</b>"
             + (f" · re-listed ×{lot.times_unsold}" if lot.times_unsold else "")
             + "</blockquote>",
             f"💰 Base price: <b>{_money(season, lot.base_price_lakh)}</b>",
             f"➡️ Open with <code>/bid {A._bid_hint(A.next_min_bid(season, lot))}</code>"]
    holder = A.rtm_holder(session, lot) if A.rtm_configured(season) else None
    if holder is not None:
        lines.append(f"🪪 <i>{_e(holder.name)} hold a Right To Match on him.</i>")
    return "\n".join(lines)


# ── Announcements ────────────────────────────────────────────────────

def bid_burst_html(session, season, events):
    """One small message for every bid that landed inside one tick."""
    steps = []
    lot = None
    for event in events:
        detail = A._loads(event.detail_json, {})
        amount = detail.get("amount_lakh")
        franchise = _franchise(session, event.franchise_id)
        name = franchise.name if franchise else "?"
        steps.append((name, amount, event.by_admin))
        lot = lot or _lot(session, event.lot_id)
    who = lambda s: (f"<b>{_e(s[0])}</b> <code>{_money(season, s[1])}</code>"
                     + (" <i>(admin)</i>" if s[2] else ""))
    player = f" · {_e(lot.name)}" if lot is not None else ""
    if len(steps) == 1:
        return f"💸 {who(steps[0])}{player}"
    trail = " → ".join(who(s) for s in steps[-6:])
    more = f"<i>+{len(steps) - 6} earlier</i> → " if len(steps) > 6 else ""
    return f"💸 {more}{trail} <i>leads</i>{player}"


def event_html(session, season, event):
    """The richer announcement for an event, or its stored headline."""
    try:
        kind = event.kind
        lot = _lot(session, event.lot_id)
        franchise = _franchise(session, event.franchise_id)
        if kind == "lot_sold" and lot is not None:
            buyer = franchise or _franchise(session, lot.sold_to_id)
            price = lot.sold_price_lakh
            bids = int(lot.bid_count or 0)
            lines = [f"🔨 <b>SOLD!</b>",
                     f"<blockquote><b>{_e(lot.name)}</b> ({lot.rating} OVR · "
                     f"{_e(lot.category)})\n"
                     f"➡️ <b>{_e(buyer.name if buyer else '?')}</b> for "
                     f"<b>{_money(season, price)}</b>"
                     + (f" · {bids} bid{'s' if bids != 1 else ''}" if bids else "")
                     + "</blockquote>"]
            if buyer is not None:
                lines.append(f"👛 {_e(buyer.name)}: "
                             f"{_money(season, buyer.purse_remaining_lakh)} left · "
                             f"👥 {buyer.squad_size}/{season.max_squad_size}")
            return "\n".join(lines)
        if kind == "lot_unsold" and lot is not None:
            return (f"❌ <b>UNSOLD</b> — {_e(lot.name)} ({lot.rating} OVR)\n"
                    f"<i>Moves to the {_e(A.UNSOLD_SET)} set.</i>")
        if kind == "retained" and lot is not None:
            return (f"🔒 <b>RETAINED</b>\n<blockquote><b>{_e(lot.name)}</b> stays "
                    f"with <b>{_e(franchise.name if franchise else '?')}</b> for "
                    f"<b>{_money(season, lot.sold_price_lakh)}</b></blockquote>")
    except Exception:
        logger.debug("auction: rich event render failed", exc_info=True)
    return event.headline


# ── Team views ───────────────────────────────────────────────────────

def squad_view(session, season, franchise):
    """One franchise's squad: a table, and the money above it."""
    rows = A.squad(session, franchise.id)
    spent = sum(int(lot.sold_price_lakh or 0) for lot in rows)
    overseas = A.overseas_count(session, franchise.id)
    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("Player"), header=True),
              R.cell(R.bold("Role"), header=True),
              R.cell(R.bold("OVR"), header=True, align="right"),
              R.cell(R.bold("Price"), header=True, align="right")]
    table = [header]
    for i, lot in enumerate(rows, start=1):
        icon = A.acquisition_icon(lot)
        table.append([
            R.cell(str(i), align="center"),
            R.cell(f"{_flag(lot)} {lot.name}" + (f" {icon}" if icon else "")),
            R.cell(lot.category),
            R.cell(str(lot.rating), align="right"),
            R.cell(_money(season, lot.sold_price_lakh), align="right"),
        ])
    blocks = [
        R.heading(f"👥 {franchise.name}", size=2),
        R.paragraph([R.bold(f"{len(rows)}/{season.max_squad_size}"),
                     f" players (min {season.min_squad_size}) · ✈️ "
                     f"{overseas}/{season.max_overseas} overseas"]),
        R.paragraph(["💰 ", R.bold(_money(season, franchise.purse_remaining_lakh)),
                     " left of ", _money(season, franchise.purse_total_lakh),
                     " · spent ", _money(season, spent), " · 🎯 max bid ",
                     R.bold(_money(season, max(0, A.max_bid_now(season, franchise))))]),
    ]
    if rows:
        blocks.append(R.table(table, bordered=True, striped=True, compact=True))
    else:
        blocks.append(R.paragraph(R.italic("Nobody signed yet.")))
    blocks.append(R.footer("🔒 retained · 🪪 Right To Match · 🆕 expansion pick "
                           "· 🎁 auto-filled"))
    html_text = (A.render_squad(session, season, franchise)
                 + f"\n\n✈️ {overseas}/{season.max_overseas} overseas · "
                   f"🎯 max bid <b>{_money(season, max(0, A.max_bid_now(season, franchise)))}</b>")
    return blocks, html_text


def purses_view(session, season):
    blocks = [R.heading(f"💰 {season.name} — purses", size=2),
              R.table(_purse_rows(session, season), bordered=True,
                      striped=True, compact=True)]
    if A.rtm_configured(season):
        blocks.append(R.paragraph(
            "🪪 Right To Match left: " + ", ".join(
                f"{f.name} {A.rtm_cards_left(f)}"
                for f in A.franchises(session, season.id))))
    return blocks, A.render_purses(session, season)


# ── Before a lot opens ───────────────────────────────────────────────
#
# Everything in this section is readable by anyone, and every one of them is
# at its most useful *before* the auction starts. A franchise works out what
# its purse can reach against numbers it has to be able to see first: the
# squad caps, the reserve the reachability rule holds back, the bid ladder,
# how long a lot stays on the block, what retention already cost it. Those
# lived only on the website's setup page and behind two admin-only commands —
# which, for the people actually doing the bidding, is the same as not
# existing. `/arules`, `/aretlock` and `/apicks` are the same facts, in the
# room, before they matter rather than after.


def retention_window_text(season, now=None):
    """Retention's window in one phrase — open, closing, or long shut.

    The deadline is enforced lazily (nothing sweeps while a season is in
    setup), so a window nobody can see is one that only ever announces itself
    by refusing somebody. Printed on the rules card, on the retention card and
    in the /ainfo summary for exactly that reason.
    """
    if A.retention_locked(season):
        return "🔒 Closed"
    left = A.retention_seconds_left(season, now)
    if left is None:
        return "🔓 Open — no deadline set"
    if left > 0:
        return f"🔓 Open — closes in {A.format_clock(left)}"
    return f"🔒 Closed — the deadline passed {A.format_clock(-left)} ago"


def _rating_band(row, previous):
    """One base-price rung, read the way the ladder is: highest band first."""
    low = row["min_rating"]
    if low <= 0:
        return "everyone else"
    if previous is None:
        return f"{low}+ OVR"
    return f"{low}–{previous - 1} OVR"


def rules_view(session, season):
    """📜 Every number that decides what a franchise may do.

    One card rather than seven commands: an owner reading this has not bid yet
    and does not know which number they are missing, so the answer to "what
    are the rules" has to be all of them.
    """
    symbol = season.currency_label or "₹"
    counts = A.pool_counts(session, season.id)
    field = A.franchises(session, season.id)
    sets = A.queued_sets(session, season)
    minimums = A.role_minimums(season)

    blocks = [R.heading(f"📜 {season.name} — the rules", size=2),
              R.paragraph([R.bold(A.status_label(season)), " · ",
                           f"{len(field)} franchise{'s' if len(field) != 1 else ''}"
                           f" · {counts.get('total', 0)} lots in {len(sets)} "
                           f"set{'s' if len(sets) != 1 else ''}"])]
    lines = [f"📜 <b>{_e(season.name)} — the rules</b>",
             f"{A.status_label(season)} · {len(field)} franchises · "
             f"{counts.get('total', 0)} lots in {len(sets)} sets", ""]

    # ── Money and the squad ──
    purse = A.render_money(season.opening_purse_lakh, symbol)
    floor = A.render_money(season.min_base_price_lakh, symbol)
    money = [
        R.paragraph(["💰 Opening purse: ", R.bold(purse)]),
        R.paragraph(["👥 Squad: ", R.bold(f"{season.min_squad_size}–"
                                         f"{season.max_squad_size}"),
                     " players"]),
        R.paragraph(["✈️ Overseas: at most ", R.bold(str(season.max_overseas)),
                     f" — anyone not from {season.home_country}"]),
    ]
    body = [f"💰 Opening purse: <b>{purse}</b>",
            f"👥 Squad: <b>{season.min_squad_size}–{season.max_squad_size}</b> players",
            f"✈️ Overseas: at most <b>{season.max_overseas}</b> — anyone not "
            f"from {_e(season.home_country)}"]
    if minimums:
        need = ", ".join(f"{role} ×{n}" for role, n in sorted(minimums.items()))
        money.append(R.paragraph(["🧩 Every squad needs: ", R.bold(need)]))
        body.append(f"🧩 Every squad needs: <b>{_e(need)}</b>")
    reserve = (f"🎯 A franchise may never bid its way out of filling that "
               f"minimum: {floor} is held back for every slot it still has to "
               f"fill. That is the max bid the board prints — it is not a "
               f"suggestion, a bid above it is refused.")
    money.append(R.paragraph(R.italic(reserve)))
    body.append(f"<i>{reserve}</i>")
    blocks.append(R.details(R.bold("💰 Money and the squad"), money, is_open=True))
    lines.append("<b>💰 Money and the squad</b>")
    lines += body
    lines.append("")

    # ── The clock ──
    clock = [R.paragraph(["⏱ A lot stays on the block for ",
                          R.bold(f"{season.bid_seconds}s"),
                          " — every bid restarts it."]),
             R.paragraph(["🛡 Anti-snipe: a bid with ",
                          R.bold(f"{season.snipe_window_seconds}s"),
                          " or less left pushes the clock back to ",
                          R.bold(f"{season.snipe_extend_seconds}s"),
                          f", up to {season.max_extensions} times a lot."])]
    blocks.append(R.details(R.bold("⏱ The clock"), clock))
    lines.append("<b>⏱ The clock</b>")
    lines.append(f"⏱ A lot stays on the block for <b>{season.bid_seconds}s</b> "
                 f"— every bid restarts it.")
    lines.append(f"🛡 Anti-snipe: a bid with <b>{season.snipe_window_seconds}s</b>"
                 f" or less left pushes the clock back to "
                 f"<b>{season.snipe_extend_seconds}s</b>, up to "
                 f"{season.max_extensions} times a lot.")
    lines.append("")

    # ── The two ladders ──
    header = [R.cell(R.bold("Rating"), header=True),
              R.cell(R.bold("Base price"), header=True, align="right")]
    rows, previous, ladder_lines = [header], None, []
    for row in A.base_price_rules(season):
        band = _rating_band(row, previous)
        price = A.render_money(row["base_lakh"], symbol)
        rows.append([R.cell(band), R.cell(price, align="right")])
        ladder_lines.append(f"{band} → <b>{price}</b>")
        previous = row["min_rating"]
    blocks.append(R.details(R.bold("🏷 Base prices"),
                            [R.table(rows, bordered=True, compact=True)]))
    lines.append("<b>🏷 Base prices</b>")
    lines += ladder_lines
    lines.append("")

    header = [R.cell(R.bold("Standing price"), header=True),
              R.cell(R.bold("Least raise"), header=True, align="right")]
    rows, step_lines = [header], []
    for row in A.increment_rules(season):
        ceiling = row["upto_lakh"]
        where = (f"under {A.render_money(ceiling, symbol)}" if ceiling > 0
                 else "above that")
        step = "+" + A.render_money(row["step_lakh"], symbol)
        rows.append([R.cell(where), R.cell(step, align="right")])
        step_lines.append(f"{where} → <b>{step}</b>")
    blocks.append(R.details(R.bold("📈 Bid increments"),
                            [R.table(rows, bordered=True, compact=True),
                             R.paragraph(R.italic(
                                 "A bare /bid bids the next minimum — the "
                                 "number the bot just printed."))]))
    lines.append("<b>📈 Bid increments</b>")
    lines += step_lines
    lines.append("<i>A bare /bid bids the next minimum — the number the bot "
                 "just printed.</i>")
    lines.append("")

    # ── Retention, RTM, the expansion picks ──
    if A.retention_configured(season):
        slabs = ", ".join(A.render_money(r["price_lakh"], symbol)
                          for r in A.retention_price_rules(season))
        keep = [R.paragraph([R.bold(retention_window_text(season))]),
                R.paragraph(["Up to ", R.bold(str(season.max_retentions)),
                             " per franchise"
                             + (f", at least {season.min_retentions}"
                                if season.min_retentions else "")]),
                R.paragraph(f"Ladder: {slabs}")]
        keep_lines = [f"<b>{retention_window_text(season)}</b>",
                      f"Up to <b>{season.max_retentions}</b> per franchise"
                      + (f", at least {season.min_retentions}"
                         if season.min_retentions else ""),
                      f"Ladder: {slabs}"]
        if season.retention_max_spend_lakh is not None:
            budget = A.render_money(season.retention_max_spend_lakh, symbol)
            keep.append(R.paragraph(f"Budget: {budget}"))
            keep_lines.append(f"Budget: {budget}")
        keep.append(R.paragraph(R.italic(
            "What a franchise keeps comes off the top of its purse. /aretlock "
            "shows who has kept whom.")))
        keep_lines.append("<i>What a franchise keeps comes off the top of its "
                          "purse — /aretlock shows who has kept whom.</i>")
        blocks.append(R.details(R.bold("🔒 Retention"), keep, is_open=True))
        lines.append("<b>🔒 Retention</b>")
        lines += keep_lines
        lines.append("")

    if A.rtm_configured(season):
        extra = (f"the final bid plus "
                 f"{A.render_money(season.rtm_extra_lakh, symbol)}"
                 if season.rtm_extra_lakh else "the final bid")
        cards = f"{season.rtm_per_team} card" + ("s" if season.rtm_per_team != 1
                                                 else "")
        how = ("The holder is asked first, the top bidder then gets one last "
               "raise, and the match is against that final number.")
        blocks.append(R.details(R.bold("🪪 Right To Match"), [
            R.paragraph([R.bold(cards), " each · ",
                         f"{season.rtm_window_seconds}s to answer"]),
            R.paragraph(f"Last season's franchise may match at {extra}."),
            R.paragraph(R.italic(how)),
        ]))
        lines.append("<b>🪪 Right To Match</b>")
        lines.append(f"<b>{cards}</b> each · {season.rtm_window_seconds}s to answer")
        lines.append(f"Last season's franchise may match at {extra}.")
        lines.append(f"<i>{how}</i>")
        lines.append("")

    if A.expansion_configured(season):
        how_many = (f"{season.expansion_picks} player"
                    + ("s" if season.expansion_picks != 1 else ""))
        blocks.append(R.details(R.bold("🆕 Expansion picks"), [
            R.paragraph(["A side new this season signs ", R.bold(how_many),
                         " before the auction opens."]),
            R.paragraph(R.italic("/apicks has the order and whose turn it is.")),
        ]))
        lines.append("<b>🆕 Expansion picks</b>")
        lines.append(f"A side new this season signs <b>{how_many}</b> before "
                     f"the auction opens.")
        lines.append("<i>/apicks has the order and whose turn it is.</i>")
        lines.append("")

    if A._as_int(getattr(season, "auto_accelerated", 1), 1):
        tail = ("⚡ Anyone unsold comes back once, as the Accelerated set, "
                "before the auction finishes — and whoever is still unsold "
                "after that tops up a short squad for free.")
    else:
        tail = ("⚡ Unsold players do not come back automatically; whoever is "
                "still unsold at the end tops up a short squad for free.")
    blocks.append(R.paragraph(R.italic(tail)))
    lines.append(f"<i>{tail}</i>")
    blocks.append(R.footer(["More: ", R.code("/ainfo · /asets · /apurse · "
                                             "/asquad")]))
    lines.append("\n<i>More:</i> <code>/ainfo · /asets · /apurse · /asquad</code>")
    return blocks, "\n".join(lines)


def retention_view(session, season):
    """🔒 Where retention stands, and who has kept whom.

    Open to the room, not just to admins: it is a franchise's own purse being
    spent, and every other franchise is planning against what it bought. The
    switch that *closes* the window stays with the admin — /aretlock on.
    """
    symbol = season.currency_label or "₹"
    if not A.retention_configured(season):
        text = ("🔓 <b>Retention</b> — this auction allows no retentions. "
                "Every squad is built at the auction.")
        return [R.heading("🔓 Retention", size=2),
                R.paragraph("This auction allows no retentions — every squad "
                            "is built at the auction.")], text

    window = retention_window_text(season)
    blocks = [R.heading("🔒 Retention", size=2),
              R.paragraph([R.bold(window)]),
              R.paragraph(["Up to ", R.bold(str(season.max_retentions)),
                           " per franchise"
                           + (f", at least {season.min_retentions}"
                              if season.min_retentions else "")])]
    lines = [f"🔒 <b>Retention</b> — {window}",
             f"Up to <b>{season.max_retentions}</b> per franchise"
             + (f", at least <b>{season.min_retentions}</b>"
                if season.min_retentions else "")]
    if season.retention_max_spend_lakh is not None:
        budget = A.render_money(season.retention_max_spend_lakh, symbol)
        blocks.append(R.paragraph(f"Budget: {budget}"))
        lines.append(f"Budget: {budget}")
    slabs = ", ".join(A.render_money(r["price_lakh"], symbol)
                      for r in A.retention_price_rules(season))
    blocks.append(R.paragraph(f"Ladder: {slabs}"))
    lines.append(f"Ladder: {slabs}")

    header = [R.cell(R.bold("Franchise"), header=True),
              R.cell(R.bold("Kept"), header=True, align="center"),
              R.cell(R.bold("Spent"), header=True, align="right"),
              R.cell(R.bold("Purse"), header=True, align="right")]
    rows = [header]
    lines.append("")
    keeps = [(f, A.retained(session, f.id))
             for f in A.franchises(session, season.id)]
    for franchise, kept in keeps:
        spent = A.retention_spent(session, franchise.id)
        rows.append([
            R.cell(franchise.name),
            R.cell(f"{len(kept)}/{season.max_retentions}", align="center"),
            R.cell(A.render_money(spent, symbol), align="right"),
            R.cell(R.bold(A.render_money(franchise.purse_remaining_lakh, symbol)),
                   align="right"),
        ])
        lines.append(f"<b>{_e(franchise.name)}</b> — {len(kept)}"
                     f"/{season.max_retentions} · "
                     f"{A.render_money(spent, symbol)} spent · "
                     f"{A.render_money(franchise.purse_remaining_lakh, symbol)} left")
        for lot in kept:
            lines.append(f"   🔒 {_e(lot.name)} — "
                         f"{A.render_money(lot.sold_price_lakh, symbol)}")
    blocks.append(R.table(rows, bordered=True, striped=True, compact=True))
    for franchise, kept in keeps:
        if not kept:
            continue
        blocks.append(R.details(
            R.bold(f"🔒 {franchise.name} ({len(kept)})"),
            [R.list_block([[R.bold(lot.name),
                            f" · {lot.rating} OVR · "
                            f"{A.render_money(lot.sold_price_lakh, symbol)}"]
                           for lot in kept])]))
    return blocks, "\n".join(lines)


def picks_view(session, season):
    """🆕 The expansion picks: the order, whose turn, and what is taken.

    A new side needs the running order more than the admin running it does —
    it is their turn that is coming up.
    """
    symbol = season.currency_label or "₹"
    order = A.pick_order(session, season)
    if not order:
        new_sides = A.expansion_franchises(session, season)
        if not new_sides:
            text = ("🆕 <b>Expansion picks</b> — nobody is new this season, so "
                    "there are no picks to make.")
            return [R.heading("🆕 Expansion picks", size=2),
                    R.paragraph("Nobody is new this season, so there are no "
                                "picks to make.")], text
        names = ", ".join(f.name for f in new_sides)
        text = ("🆕 <b>Expansion picks</b> — none dealt yet.\nNew this season: "
                + _e(names))
        return [R.heading("🆕 Expansion picks", size=2),
                R.paragraph("None dealt yet."),
                R.paragraph(["New this season: ", R.bold(names)])], text

    turn = A.pick_turn(session, season)
    blocks = [R.heading("🆕 Expansion picks", size=2)]
    lines = ["🆕 <b>Expansion picks</b>"]
    if turn is not None:
        blocks.append(R.paragraph(["▶️ It is ", R.bold(turn.name), "'s pick."]))
        lines.append(f"▶️ It is <b>{_e(turn.name)}</b>'s pick.")
    else:
        blocks.append(R.paragraph("✅ Every pick is used."))
        lines.append("✅ Every pick is used.")
    if A.retention_configured(season) and not A.retention_locked(season):
        # A pick made while retention is still open can be undone by somebody
        # else retaining the same player, so the room is told before it plans
        # around one.
        warn = ("Retention is still open — the picks are not final until it "
                "closes.")
        blocks.append(R.paragraph(["⚠️ ", warn]))
        lines.append(f"⚠️ {warn}")

    schedule = A.pick_schedule(session, season)
    made = sum(int(f.draft_picks_used or 0) for f in order)
    steps = []
    for i, franchise in enumerate(schedule, start=1):
        mark = "✅" if i <= made else ("▶️" if i == made + 1 else "⏳")
        steps.append(f"{mark} {i}. {franchise.name}")
    blocks.append(R.details(
        R.bold("Order — it snakes: last in a round picks first in the next"),
        [R.list_block([[step] for step in steps])], is_open=True))
    lines.append("")
    lines.append("<b>Order</b> (it snakes: last in a round picks first in "
                 "the next)")
    lines += [_e(step) for step in steps]

    lines.append("")
    for franchise in order:
        taken = A.drafted(session, franchise.id)
        left = A.picks_left(franchise)
        total = int(franchise.draft_picks_total or 0)
        purse = A.render_money(franchise.purse_remaining_lakh, symbol)
        lines.append(f"<b>{_e(franchise.name)}</b> — {left} left of {total} · "
                     f"{purse}")
        body = [[R.bold(lot.name),
                 f" · {A.render_money(lot.sold_price_lakh, symbol)}"]
                for lot in taken]
        blocks.append(R.details(
            R.bold(f"{franchise.name} — {left} left of {total} · {purse}"),
            [R.list_block(body)] if body else
            [R.paragraph(R.italic("Nobody taken yet."))]))
        for lot in taken:
            lines.append(f"   🆕 {_e(lot.name)} — "
                         f"{A.render_money(lot.sold_price_lakh, symbol)}")
    return blocks, "\n".join(lines)


# ── Sets: paged, with a set openable in place ────────────────────────
#
# A pool is routinely dozens of sets of dozens of players, which is several
# messages' worth of text — so the card is paged, and a set is opened rather
# than always printed. Three things carry that, in order of how little they
# cost the reader:
#
#   • every set on the page is a ``details`` collapsible (an expandable
#     blockquote in the HTML twin), so its first few players are one tap away
#     with no round trip at all;
#   • a button per set opens that set in full, itself paged, by editing the
#     message rather than posting another one;
#   • the page buttons walk the sets.
#
# Sets are addressed in callback data by ``set_no``, never by name: names carry
# emoji, spaces and commas, Telegram caps callback data at 64 bytes, and
# ``list_sets`` maps the number back. A number that no longer exists is refused
# rather than silently opening whatever now sits in that place.

SETS_CB = "au_sets_"
# Sets per page of the list.
SETS_PAGE_SIZE = 6
# Players per page of one opened set.
LOT_PAGE_SIZE = 20
# Players shown inside a set's collapsible on the list page. Deliberately small:
# this is the "is this the set I meant?" glance, and the button beside it is the
# whole thing.
INLINE_LOTS = 8


def _pages(total, per_page):
    return max(1, (total + per_page - 1) // per_page)


def _clamp_page(page, total_pages):
    return min(max(A._as_int(page, 1) or 1, 1), total_pages)


def _set_lot_table(session, season, rows):
    """One opened set's players, and where each of them ended up.

    Its own table rather than ``_lot_table``: a set holds queued, sold and
    unsold players at once, and the two shapes ``_lot_table`` offers each drop
    the half the other shows.
    """
    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("Player"), header=True),
              R.cell(R.bold("OVR"), header=True, align="right"),
              R.cell(R.bold("Role"), header=True),
              R.cell(R.bold("Base"), header=True, align="right"),
              R.cell(R.bold("Result"), header=True)]
    table = [header]
    for lot in rows:
        table.append([R.cell(str(lot.lot_no), align="center"),
                      R.cell(f"{_flag(lot)} {lot.name}"),
                      R.cell(str(lot.rating), align="right"),
                      R.cell(lot.category),
                      R.cell(_money(season, lot.base_price_lakh), align="right"),
                      R.cell(_lot_result(session, season, lot))])
    return R.table(table, bordered=True, striped=True, compact=True)


def _lot_result(session, season, lot):
    """Where a lot stands, in the few words a table cell has room for."""
    if lot.status == A.LOT_SOLD:
        buyer = _franchise(session, lot.sold_to_id)
        return (f"{buyer.name if buyer else '?'}"
                f"{A.acquisition_mark(lot)} "
                f"{_money(season, lot.sold_price_lakh)}")
    return {A.LOT_QUEUED: "⏳ waiting", A.LOT_ON_BLOCK: "🔨 on the block",
            A.LOT_UNSOLD: "❌ unsold",
            A.LOT_WITHDRAWN: "🚫 withdrawn"}.get(lot.status, lot.status)


def sets_view(session, season, *, page=1, expand=None, lot_page=1):
    """The sets card: one page of sets, or one set opened in full.

    ``expand`` is a ``set_no``. Returns ``(blocks, html, keyboard)`` — the third
    element is what makes it pageable, and ``_view`` already forwards it.
    Returns None for ``expand`` naming a set that is no longer there, so the
    caller can say the sets have moved instead of showing the wrong one.
    """
    entries = A.list_sets(session, season)
    groups = _grouped(A.lots(session, season.id))
    total_pages = _pages(len(entries), SETS_PAGE_SIZE)

    if expand is not None:
        chosen = next((e for e in entries
                       if e["set_no"] == A._as_int(expand, 0)), None)
        if chosen is None:
            return None
        return _set_detail_view(session, season, chosen, groups,
                                lot_page=lot_page, total_pages=total_pages)

    page = _clamp_page(page, total_pages)
    start = (page - 1) * SETS_PAGE_SIZE
    shown = entries[start:start + SETS_PAGE_SIZE]

    blocks = [R.heading(f"🗂 {season.name} — sets", size=2)]
    lines = [f"🗂 <b>{_e(season.name)} — sets</b>"]
    if not entries:
        blocks.append(R.paragraph(R.italic("The pool is empty.")))
        lines.append("<i>The pool is empty.</i>")
    else:
        count = f"{len(entries)} set{'s' if len(entries) != 1 else ''}"
        blocks.append(R.paragraph(
            f"{count}, in the order they run · page {page}/{total_pages}"))
        lines.append(f"<i>{count}, in the order they run — page "
                     f"{page}/{total_pages}</i>")

    for entry in shown:
        mark = STATE_MARK[entry["state"]]
        rows = groups.get(entry["name"], [])
        summary = (f"#{entry['set_no']} {mark} {entry['name']} — "
                   f"{entry['queued']} left · {entry['sold']} sold · "
                   f"{entry['unsold']} unsold")
        body = [_set_lot_table(session, season, rows[:INLINE_LOTS])] if rows else []
        if len(rows) > INLINE_LOTS:
            body.append(R.paragraph(R.italic(
                f"…and {len(rows) - INLINE_LOTS} more — press "
                f"#{entry['set_no']} below for the whole set.")))
        blocks.append(R.details(
            R.bold(summary), body,
            # The set being auctioned right now is the one the room is actually
            # asking about, so it starts open.
            is_open=entry["state"] in ("live", "next")))

        lines.append(f"\n#{entry['set_no']} {mark} <b>{_e(entry['name'])}</b> — "
                     f"{STATE_WORD[entry['state']]} · {entry['queued']} left · "
                     f"{entry['sold']} sold · {entry['unsold']} unsold")
        if rows:
            preview = [_lot_line(season, lot) for lot in rows[:INLINE_LOTS]]
            if len(rows) > INLINE_LOTS:
                preview.append(f"<i>…and {len(rows) - INLINE_LOTS} more</i>")
            lines.append("<blockquote expandable>" + "\n".join(preview)
                         + "</blockquote>")

    unsold = A.unsold(session, season.id)
    tail = ("⚡ " + A.UNSOLD_SET + f": {len(unsold)} waiting")
    if (unsold and A._as_int(getattr(season, "auto_accelerated", 1), 1)
            and not A._as_int(getattr(season, "accelerated_done", 0), 0)):
        tail += " — they return once, automatically, when the main pool is done."
    blocks.append(R.paragraph(tail))
    lines.append(f"\n⚡ <b>{_e(A.UNSOLD_SET)}</b>: {len(unsold)} waiting")

    footer = ("✅ done · 🔨 live · ⏭ next · ⏳ queued — tap a set to see its "
              "first few, or press its number for the whole set")
    blocks.append(R.footer(footer))
    lines.append(f"\n<i>{footer}</i>")
    return blocks, "\n".join(lines), sets_keyboard(shown, page=page,
                                                  total_pages=total_pages)


def _set_detail_view(session, season, entry, groups, *, lot_page, total_pages):
    """One set, in full, a page of players at a time."""
    rows = groups.get(entry["name"], [])
    lot_pages = _pages(len(rows), LOT_PAGE_SIZE)
    lot_page = _clamp_page(lot_page, lot_pages)
    start = (lot_page - 1) * LOT_PAGE_SIZE
    shown = rows[start:start + LOT_PAGE_SIZE]
    mark = STATE_MARK[entry["state"]]
    # Which page of the list this set sits on, so "All sets" goes back to it
    # rather than to the first page.
    back_page = ((entry["set_no"] - 1) // SETS_PAGE_SIZE) + 1

    title = f"🗂 #{entry['set_no']} {entry['name']}"
    stand = (f"{mark} {STATE_WORD[entry['state']]} · {entry['queued']} waiting · "
             f"{entry['sold']} sold · {entry['unsold']} unsold")
    blocks = [R.heading(title, size=2), R.paragraph(stand)]
    lines = [f"{mark} <b>{_e(title)}</b>", f"<i>{_e(stand)}</i>"]

    if not rows:
        blocks.append(R.paragraph(R.italic("This set has no players.")))
        lines.append("<i>This set has no players.</i>")
    else:
        span = (f"Players {start + 1}–{start + len(shown)} of {len(rows)}"
                + (f" · page {lot_page}/{lot_pages}" if lot_pages > 1 else ""))
        blocks.append(R.paragraph(span))
        blocks.append(_set_lot_table(session, season, shown))
        lines.append(f"<i>{span}</i>")
        for chunk in range(0, len(shown), QUOTE_ROWS):
            lines.append("<blockquote expandable>"
                         + "\n".join(_lot_line(season, lot)
                                     for lot in shown[chunk:chunk + QUOTE_ROWS])
                         + "</blockquote>")

    return (blocks, "\n".join(lines),
            sets_keyboard([], page=back_page, total_pages=total_pages,
                          expand=entry["set_no"], lot_page=lot_page,
                          lot_pages=lot_pages))


def sets_keyboard(entries, *, page, total_pages, expand=None, lot_page=1,
                  lot_pages=1):
    """Page buttons for the sets card, and one button per set to open it."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def nav(label, data):
        return InlineKeyboardButton(label, callback_data=data)

    rows = []
    if expand is None:
        # Three per row keeps the labels readable on a phone.
        opens = [nav(f"#{e['set_no']} {_short(e['name'])}",
                     f"{SETS_CB}x_{e['set_no']}_1")
                 for e in entries]
        rows += [opens[i:i + 3] for i in range(0, len(opens), 3)]
        if total_pages > 1:
            bar = []
            if page > 1:
                bar.append(nav("⬅️ Prev", f"{SETS_CB}p_{page - 1}"))
            bar.append(nav(f"Page {page}/{total_pages}", f"{SETS_CB}noop"))
            if page < total_pages:
                bar.append(nav("Next ➡️", f"{SETS_CB}p_{page + 1}"))
            rows.append(bar)
        rows.append([nav("🔄 Refresh", f"{SETS_CB}p_{page}")])
        return InlineKeyboardMarkup(rows)

    if lot_pages > 1:
        bar = []
        if lot_page > 1:
            bar.append(nav("⬅️", f"{SETS_CB}x_{expand}_{lot_page - 1}"))
        bar.append(nav(f"Page {lot_page}/{lot_pages}", f"{SETS_CB}noop"))
        if lot_page < lot_pages:
            bar.append(nav("➡️", f"{SETS_CB}x_{expand}_{lot_page + 1}"))
        rows.append(bar)
    rows.append([nav("⬅️ All sets", f"{SETS_CB}p_{page}"),
                 nav("🔄 Refresh", f"{SETS_CB}x_{expand}_{lot_page}")])
    return InlineKeyboardMarkup(rows)


def _short(name, limit=14):
    """A set name cut to fit a button beside its number.

    Cuts by code point, so an emoji built from several of them can lose its tail.
    That is a cosmetic risk on a label only — the button carries the set's number,
    never its name, so nothing is resolved from what this returns.
    """
    name = str(name or "").strip()
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


def _lot_table(season, rows, *, sold=False, session=None):
    header = [R.cell(R.bold("#"), header=True, align="center"),
              R.cell(R.bold("Player"), header=True),
              R.cell(R.bold("OVR"), header=True, align="right")]
    if sold:
        header += [R.cell(R.bold("Team"), header=True),
                   R.cell(R.bold("Price"), header=True, align="right")]
    else:
        header += [R.cell(R.bold("Role"), header=True),
                   R.cell(R.bold("Base"), header=True, align="right")]
    table = [header]
    for lot in rows:
        row = [R.cell(str(lot.lot_no), align="center"),
               R.cell(f"{_flag(lot)} {lot.name}"),
               R.cell(str(lot.rating), align="right")]
        if sold:
            buyer = _franchise(session, lot.sold_to_id)
            icon = A.acquisition_icon(lot)
            row += [R.cell((buyer.name if buyer else "?") + (f" {icon}" if icon else "")),
                    R.cell(_money(season, lot.sold_price_lakh), align="right")]
        else:
            row += [R.cell(lot.category),
                    R.cell(_money(season, lot.base_price_lakh), align="right")]
        table.append(row)
    return R.table(table, bordered=True, striped=True, compact=True)


def _lot_line(season, lot):
    return (f"{_flag(lot)} <b>{_e(lot.name)}</b> · {lot.rating} · "
            f"{_e(lot.category)} · base {_money(season, lot.base_price_lakh)}")


def next_set_view(session, season):
    entry = A.next_set(session, season)
    if entry is None:
        text = "⏭ No other set is waiting — this is the last one."
        return [R.paragraph(text)], text
    rows = A.queued_lots(session, season, set_name=entry["name"])
    blocks = [R.heading(f"⏭ Next set #{entry['set_no']}: {entry['name']}", size=2),
              R.paragraph(f"{len(rows)} player{'s' if len(rows) != 1 else ''}, "
                          f"in the order they come up."),
              _lot_table(season, rows[:40])]
    lines = [f"⏭ <b>Next set #{entry['set_no']}: {_e(entry['name'])}</b> — "
             f"{len(rows)} players", ""]
    lines += [_lot_line(season, lot) for lot in rows[:40]]
    if len(rows) > 40:
        more = f"…and {len(rows) - 40} more"
        blocks.append(R.paragraph(R.italic(more)))
        lines.append(f"<i>{more}</i>")
    return blocks, "\n".join(lines)


def next_players_view(session, season, count=5):
    live = A.current_lot(session, season)
    rows = A.queued_lots(session, season, limit=count)
    blocks = [R.heading("👤 Next up", size=2)]
    lines = ["👤 <b>Next up</b>"]
    if live is not None:
        blocks.append(R.paragraph(["🔨 On the block: ", R.bold(live.name),
                                   f" ({A.set_label(live)})"]))
        lines.append(f"🔨 On the block: <b>{_e(live.name)}</b> "
                     f"({_e(A.set_label(live))})")
    if not rows:
        blocks.append(R.paragraph(R.italic("Nobody else is waiting.")))
        lines.append("<i>Nobody else is waiting.</i>")
        return blocks, "\n".join(lines)
    blocks.append(R.list_block(
        [[R.bold(lot.name), f" · {lot.rating} OVR · {lot.category} · "
          f"{A.set_label(lot)} · base {_money(season, lot.base_price_lakh)}"]
         for lot in rows], ordered=True))
    lines.append("")
    lines += [f"{i}. {_lot_line(season, lot)} · <i>{_e(A.set_label(lot))}</i>"
              for i, lot in enumerate(rows, start=1)]
    return blocks, "\n".join(lines)


def _grouped(rows):
    groups = {}
    for lot in rows:
        groups.setdefault(A.set_label(lot), []).append(lot)
    return groups


def sold_view(session, season):
    rows = A.sold_lots(session, season.id)
    total = sum(int(lot.sold_price_lakh or 0) for lot in rows)
    blocks = [R.heading(f"✅ Sold — {len(rows)}", size=2),
              R.paragraph(["Spent across the room: ", R.bold(_money(season, total))])]
    lines = [f"✅ <b>Sold — {len(rows)}</b> · {_money(season, total)} spent"]
    if not rows:
        blocks.append(R.paragraph(R.italic("Nobody has been sold yet.")))
        lines.append("<i>Nobody has been sold yet.</i>")
        return blocks, "\n".join(lines)
    groups = _grouped(rows)
    for index, (name, group) in enumerate(groups.items()):
        blocks.append(R.details(R.bold(f"🗂 {name} ({len(group)})"),
                                [_lot_table(season, group, sold=True,
                                            session=session)],
                                is_open=index == len(groups) - 1))
        body = []
        for lot in group:
            buyer = _franchise(session, lot.sold_to_id)
            body.append(f"{_flag(lot)} {_e(lot.name)} → "
                        f"<b>{_e(buyer.name if buyer else '?')}</b> "
                        f"{_money(season, lot.sold_price_lakh)}"
                        f"{A.acquisition_mark(lot)}")
        for start in range(0, len(body), QUOTE_ROWS):
            more = " (cont.)" if start else ""
            lines.append(f"\n🗂 <b>{_e(name)}</b> ({len(group)}){more}")
            lines.append("<blockquote expandable>"
                         + "\n".join(body[start:start + QUOTE_ROWS])
                         + "</blockquote>")
    return blocks, "\n".join(lines)


def unsold_view(session, season):
    rows = A.unsold(session, season.id)
    blocks = [R.heading(f"❌ {A.UNSOLD_SET} — {len(rows)}", size=2)]
    lines = [f"❌ <b>{_e(A.UNSOLD_SET)} — {len(rows)}</b>"]
    if not rows:
        blocks.append(R.paragraph(R.italic("Nobody is unsold right now.")))
        lines.append("<i>Nobody is unsold right now.</i>")
        return blocks, "\n".join(lines)
    if (A._as_int(getattr(season, "auto_accelerated", 1), 1)
            and not A._as_int(getattr(season, "accelerated_done", 0), 0)):
        note = "They come back once, as the ⚡ Accelerated set, when the main pool is done."
    else:
        note = "Anyone still unsold at the end tops up a short squad, free."
    blocks.append(R.paragraph(R.italic(note)))
    lines.append(f"<i>{note}</i>")
    for name, group in _grouped(rows).items():
        blocks.append(R.details(R.bold(f"from {name} ({len(group)})"),
                                [_lot_table(season, group)]))
        body = [_lot_line(season, lot) for lot in group]
        for start in range(0, len(body), QUOTE_ROWS):
            more = " (cont.)" if start else ""
            lines.append(f"\n<b>from {_e(name)}</b> ({len(group)}){more}")
            lines.append("<blockquote expandable>"
                         + "\n".join(body[start:start + QUOTE_ROWS])
                         + "</blockquote>")
    return blocks, "\n".join(lines)


def _setup_lines(session, season, counts):
    """What /ainfo should say about an auction that has not started.

    "0/120 lots resolved" is true and useless before the first lot opens. What
    a franchise wants at that point is whether the pool is built, whether
    retention is still open, whether any picks are outstanding, and how long
    it has to do something about any of it. Returns ``(blocks, lines)``.
    """
    sets = A.queued_sets(session, season)
    field = A.franchises(session, season.id)
    queued = counts.get(A.LOT_QUEUED, 0)
    blocks, lines = [], []

    if queued:
        pool = (f"{queued} player{'s' if queued != 1 else ''} in "
                f"{len(sets)} set{'s' if len(sets) != 1 else ''}")
    else:
        pool = "the pool is not built yet"
    blocks.append(R.paragraph(["🗂 Pool: ", R.bold(pool), " · 👥 ",
                               R.bold(f"{len(field)}"), " franchises"]))
    lines.append(f"🗂 Pool: <b>{pool}</b> · 👥 <b>{len(field)}</b> franchises")

    if A.retention_configured(season):
        window = retention_window_text(season)
        kept = counts.get("retained", 0)
        blocks.append(R.paragraph(["🔒 Retention: ", R.bold(window),
                                   f" · {kept} kept so far"]))
        lines.append(f"🔒 Retention: <b>{window}</b> · {kept} kept so far")
    if A.expansion_configured(season):
        turn = A.pick_turn(session, season)
        where = (f"{turn.name} to pick" if turn is not None
                 else "every pick used")
        blocks.append(R.paragraph(["🆕 Expansion picks: ", R.bold(where)]))
        lines.append(f"🆕 Expansion picks: <b>{_e(where)}</b>")

    return blocks, lines


def info_menu(session, season, franchise=None):
    """The /ainfo card: where the auction stands, and a button per view."""
    counts = A.pool_counts(session, season.id)
    done = counts.get(A.LOT_SOLD, 0) + counts.get(A.LOT_UNSOLD, 0)
    live = A.current_lot(session, season)
    nxt = A.next_set(session, season)
    setup = season.status == A.STATUS_SETUP
    progress = ("not started yet" if setup
                else f"{done}/{counts.get('total', 0)} lots resolved")
    blocks = [R.heading(f"📋 {season.name}", size=2),
              R.paragraph([R.bold(A.status_label(season)), f" · {progress}"])]
    lines = [f"📋 <b>{_e(season.name)}</b> — {A.status_label(season)} · "
             f"{progress}"]
    if setup:
        extra_blocks, extra_lines = _setup_lines(session, season, counts)
        blocks += extra_blocks
        lines += extra_lines
    if live is not None:
        blocks.append(R.paragraph(["🔨 On the block: ", R.bold(live.name),
                                   f" · {A.set_label(live)}"]))
        lines.append(f"🔨 On the block: <b>{_e(live.name)}</b> · "
                     f"{_e(A.set_label(live))}")
    if nxt is not None:
        blocks.append(R.paragraph(["⏭ Next set: ", R.bold(nxt["name"]),
                                   f" ({nxt['queued']})"]))
        lines.append(f"⏭ Next set: <b>{_e(nxt['name'])}</b> ({nxt['queued']})")
    if franchise is not None:
        # Before a lot opens the interesting number is not what is left but
        # what has already gone: a purse that reads ₹73 Cr against an opening
        # ₹100 Cr is retention's bill, and an owner should not have to work
        # that out from two cards.
        spent = max(0, int(franchise.purse_total_lakh or 0)
                    - int(franchise.purse_remaining_lakh or 0))
        of_total = (f" of {_money(season, franchise.purse_total_lakh)}"
                    if setup and spent else "")
        blocks.append(R.paragraph([
            "👛 ", R.bold(franchise.name), ": ",
            _money(season, franchise.purse_remaining_lakh), of_total, " · 👥 ",
            f"{franchise.squad_size}/{season.max_squad_size}"]))
        lines.append(f"👛 <b>{_e(franchise.name)}</b>: "
                     f"{_money(season, franchise.purse_remaining_lakh)}"
                     f"{of_total} · 👥 "
                     f"{franchise.squad_size}/{season.max_squad_size}")
    if setup:
        note = ("Bidding has not opened. 📜 /arules has every number it will "
                "run by.")
        blocks.append(R.paragraph(R.italic(note)))
        lines.append(f"<i>{note}</i>")
    commands = ("/arules · /asets · /anextset · /anextplayer · /asquad · "
                "/apurse · /asoldlist · /aunsoldlist")
    blocks.append(R.footer(["Or type: ", R.code(commands)]))
    lines.append(f"\n<i>Or type:</i> <code>{commands}</code>")
    return blocks, "\n".join(lines)


def call_html(session, season, message=None):
    """/acall: every owner and co-owner, tagged, grouped by franchise.

    HTML on purpose — a mention inside a plain ``sendMessage`` is the form
    Telegram is known to notify from — and cut into parts under the limit.
    """
    from services.draft_scheduler import mention
    head = f"📣 <b>Calling every franchise — {_e(season.name)}</b>"
    if message:
        head += f"\n<blockquote>{_e(message)}</blockquote>"
    lines = []
    for franchise in A.franchises(session, season.id):
        people = [mention(session, tg_id,
                          (franchise.owner_name if tg_id == franchise.owner_tg_id
                           else "Co-owner") or "Owner")
                  for tg_id in A.owner_ids(franchise)]
        lines.append(f"🏛 <b>{_e(franchise.name)}</b>: "
                     + (", ".join(people) if people else "<i>no owner set</i>"))
    return split_html(head, lines)


def split_html(head, lines, limit=TEXT_LIMIT - 96, joiner="\n"):
    """``head`` then ``lines``, cut into messages that each fit Telegram."""
    parts, current = [], head
    for line in lines:
        candidate = f"{current}{joiner}{line}" if current else line
        if len(candidate) > limit and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# ── Admin help ───────────────────────────────────────────────────────

ADMIN_SECTIONS = (
    ("⚙️ Setting up", (
        ("/anew <name>", "Create an auction and bind it to this group"),
        ("/abind <name>", "Bind an existing auction to this group"),
        ("/atimer <seconds>", "Seconds per lot (default 30)"),
        ("/asnipe <window> <extend> <max>", "Anti-snipe rule, e.g. /asnipe 10 10 5"),
        ("/aco <team> | <id>", "Add a co-owner who may bid"),
        ("/aaccelmode on|off", "The automatic ⚡ Accelerated round"),
    )),
    ("🗂 Pool & sets", (
        ("/apool <min>-<max> [| set] [| all]", "Add every card in a rating range as a set"),
        ("/anextset <set | 80-85>", "Make a set or rating range come next"),
        ("/asetorder A, B, C", "Order the whole queue by set"),
        ("/asets", "Every set and where it stands"),
        ("/awithdraw <player>", "Pull a player out of the auction"),
    )),
    ("▶️ Running it", (
        ("/astart · /apause · /aresume", "Start, pause and resume the clock"),
        ("/anext", "Put the next lot on the block"),
        ("/aextend [seconds]", "Add time to the lot on the block"),
        ("/asold", "Sell at the standing bid"),
        ("/aunsold", "Pass the lot (refused while a bid stands)"),
        ("/aundobid", "Void the standing bid and fall back"),
        ("/aaccel [go]", "Re-list everything unsold now"),
    )),
    ("💰 Money", (
        ("/agrant <team> | <amount>", "Correct a purse — | 5 adds ₹5 Cr, | -2 takes ₹2 Cr"),
    )),
    ("🔒 Retention", (
        ("/aretain <team> | <player> | [price]", "Offer a retention — the team must press Accept"),
        ("/aretainforce <team> | <player> | [price]", "Retain at once, no acceptance"),
        ("/aoffers", "Retention offers still waiting"),
        ("/aretcancel <player>", "Withdraw a waiting offer"),
        ("/aunretain <player>", "Release a retained player into the pool"),
        ("/aretlock on", "Close the window — bare /aretlock is the room's readout"),
    )),
    ("🪪 Right To Match", (
        ("/artmset <cards> [seconds] [premium]", "RTM rules — /artmset off turns it off"),
        ("/artmcards <team> <cards>", "One franchise's own card count"),
        ("/artmforce yes|no|stand", "Answer an open RTM for a franchise"),
        ("/artmundo <player>", "Undo a match — money and card back"),
    )),
    ("🆕 Expansion picks", (
        ("/apick <team> | <player> | [price]", "A new side signs a player"),
        ("/apickset · /apickskip · /apickundo", "Deal picks, skip a turn, undo one"),
    )),
    ("🏛 Teams", (
        ("/acall [message]", "Tag every owner and co-owner"),
        ("/aremoveteam <team>", "Remove a team — players back in the pool, purse shared"),
    )),
    ("🏁 The end", (
        ("/apublish", "Publish squads as a Challenge League"),
        ("/aclone <name>", "Start next season from this one"),
        ("/acancel", "Cancel the auction"),
    )),
)

BOT_ADMIN_SECTION = ("👮 Auction admins — bot admins only", (
    ("/aadminadd <id | @user | reply>", "Let someone run auctions"),
    ("/aadminremove <id | @user | reply>", "Take it away again"),
    ("/aadmins", "Everyone who may run auctions"),
))

PLAYER_SECTION = ("👥 For owners & everyone", (
    ("/bid [amount]", "Bid — bare /bid is the next minimum"),
    ("/artm yes|no", "Answer a Right To Match"),
    ("/ainfo", "Buttons for every view below"),
    ("/arules", "Purse, caps, base prices, bid steps, the clock — before a lot opens"),
    ("/asets · /anextset · /anextplayer", "Sets, the next set, the next players"),
    ("/asquad [team] · /apurse [team]", "Your squad, every purse"),
    ("/aretlock · /apicks", "Who kept whom, and the expansion pick order"),
    ("/asoldlist · /aunsoldlist", "Everyone sold, everyone unsold"),
    ("/aboard", "The live board with quick-bid buttons"),
))


def admin_help(*, bot_admin=False):
    """The /adminhelp card: every auction admin command, section by section."""
    sections = list(ADMIN_SECTIONS)
    if bot_admin:
        sections.append(BOT_ADMIN_SECTION)
    sections.append(PLAYER_SECTION)

    blocks = [R.heading("🔨 Franchise Auction — admin help", size=1),
              R.paragraph(["Amounts are in ", R.bold("crore"),
                           " unless you say lakh: ", R.code("/bid 15"),
                           " is ₹15 Cr, ", R.code("/bid 75L"), " is ₹75 lakh."])]
    lines = ["🔨 <b>Franchise Auction — admin help</b>",
             "<i>Amounts are in crore unless you say lakh: "
             "<code>/bid 15</code> is ₹15 Cr, <code>/bid 75L</code> is ₹75 lakh.</i>"]
    for index, (title, rows) in enumerate(sections):
        table = [[R.cell(R.bold("Command"), header=True),
                  R.cell(R.bold("What it does"), header=True)]]
        table += [[R.cell(R.code(cmd)), R.cell(what)] for cmd, what in rows]
        blocks.append(R.details(R.bold(title),
                                [R.table(table, bordered=True, compact=True)],
                                is_open=index == 0))
        body = "\n".join(f"<code>{_e(cmd)}</code> — {_e(what)}"
                         for cmd, what in rows)
        lines.append(f"\n<b>{_e(title)}</b>")
        lines.append(f"<blockquote expandable>{body}</blockquote>")
    blocks.append(R.pre("/apool 85-90 | Marquee\n"
                        "/anextset Marquee\n"
                        "/aretain Mumbai | Virat Kohli | 18\n"
                        "/aremoveteam Delhi | confirm", language="text"))
    blocks.append(R.footer("Auction admins can use every command here, and no "
                           "other admin command in the bot."))
    lines.append("\n<b>Examples</b>\n<pre><code class=\"language-text\">"
                 "/apool 85-90 | Marquee\n/anextset Marquee\n"
                 "/aretain Mumbai | Virat Kohli | 18\n"
                 "/aremoveteam Delhi | confirm</code></pre>")
    return blocks, "\n".join(lines)


def retention_offer_card(session, season, offer):
    """The card an owner presses Accept on. HTML — it carries the buttons."""
    franchise = _franchise(session, offer.franchise_id)
    price = A.offer_price(season, franchise, offer) if franchise else offer.price_lakh
    kept = int(franchise.retained_count or 0) if franchise else 0
    # Once accepted, this retention is already counted in ``kept``.
    slab = kept if offer.status == A.OFFER_ACCEPTED else kept + 1
    who = A.owner_ping(franchise) if franchise else "?"
    status = {A.OFFER_ACCEPTED: "✅ <b>Accepted</b>",
              A.OFFER_DECLINED: "❌ <b>Declined</b>",
              A.OFFER_CANCELLED: "🚫 <b>Withdrawn</b>"}.get(offer.status)
    lines = ["🔒 <b>Retention offer</b>",
             f"<blockquote><b>{_e(offer.player_name)}</b> → "
             f"<b>{_e(franchise.name if franchise else '?')}</b>\n"
             f"💰 {_money(season, price)} · retention "
             f"{slab}/{season.max_retentions}</blockquote>"]
    if status:
        lines.append(status)
    else:
        lines.append(f"{who}, only your franchise can accept this.")
        if franchise is not None:
            lines.append(f"👛 Purse now: {_money(season, franchise.purse_remaining_lakh)}")
    return "\n".join(lines)


def retention_offer_keyboard(offer):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if offer.status != A.OFFER_PENDING:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept retention",
                             callback_data=f"au_ret_{offer.id}_yes"),
        InlineKeyboardButton("❌ Decline", callback_data=f"au_ret_{offer.id}_no"),
    ]])


# ── Sending ──────────────────────────────────────────────────────────

def html_parts(html_text, limit=TEXT_LIMIT - 96):
    """``html_text`` cut into messages that each fit Telegram.

    Cut at blank lines first, which is where every renderer here puts its
    section breaks — a ``<blockquote>`` never contains one, so no tag is ever
    cut in half. Only a single section longer than a whole message falls back
    to cutting at line breaks.
    """
    if len(html_text) <= limit:
        return [html_text]
    parts = []
    for chunk in split_html("", html_text.split("\n\n"), limit=limit,
                            joiner="\n\n"):
        chunk = chunk.strip("\n")
        if len(chunk) <= limit:
            parts.append(chunk)
        else:
            parts.extend(p.strip("\n") for p in
                         split_html("", chunk.split("\n"), limit=limit))
    return [p for p in parts if p]


async def send(bot, chat_id, blocks, html_text, *, reply_markup=None,
               reply_to_message_id=None):
    """A rich message, or the HTML when rich is off, refused or unavailable.

    Wider than ``rich_message.send_rich_message`` in two ways: any failure of
    the rich attempt — not only a ``TelegramError`` — falls through to HTML,
    because an auction announcement must never be lost to a renderer bug; and
    an HTML fallback too long for one message goes out in parts (the buttons
    ride on the last, the quote on the first).
    """
    post = getattr(bot, "_post", None)
    if R.rich_text_enabled() and blocks and post:
        payload = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        try:
            from telegram import Message
            result = await post("sendRichMessage", payload)
            return Message.de_json(result, bot)
        except Exception as exc:
            from telegram.error import TelegramError
            if isinstance(exc, TelegramError):
                R._note_failure("sendRichMessage", exc)
            else:
                logger.warning("auction rich send failed; sending HTML",
                               exc_info=True)
    parts = html_parts(html_text)
    sent = None
    for index, part in enumerate(parts):
        kwargs = {}
        if index == 0 and reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        sent = await bot.send_message(
            chat_id=chat_id, text=part, parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup if index == len(parts) - 1 else None,
            **kwargs)
    return sent


async def edit(bot, chat_id, message_id, blocks, *, reply_markup=None,
               html_text=None):
    """Edit a message into ``blocks``. True, False, or None (not attempted).

    "Not modified" is a success here, as it is for the HTML board: two ticks
    can render the same thing.

    Pass ``html_text`` for a message somebody is *driving* — a paged card whose
    buttons edit it in place. Without it a chat where rich text is off, refused
    or unavailable gets no edit at all, which is a button that does nothing; with
    it the edit falls back to HTML the same way :func:`send` does. The board
    leaves it out on purpose: it has its own HTML edit path and re-renders on the
    next tick anyway.
    """
    post = getattr(bot, "_post", None)
    if R.rich_text_enabled() and blocks and post:
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "rich_message": {"blocks": blocks}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await post("editMessageText", payload)
            return True
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return True
            from telegram.error import TelegramError
            if isinstance(exc, TelegramError):
                R._note_failure("editMessageText", exc)
            else:
                logger.warning("auction rich edit failed: %s", exc)
            if html_text is None:
                return False
    if html_text is None:
        return None
    try:
        # Only the first part: an edit replaces one message, so a card driven by
        # buttons has to fit one. html_parts caps it rather than losing the tail
        # to Telegram's length limit.
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=html_parts(html_text)[0], parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=reply_markup)
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return True
        logger.warning("auction HTML edit failed: %s", exc)
        return False
