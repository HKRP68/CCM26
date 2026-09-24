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

INFO_VIEWS = (
    ("sets", "🗂 Sets"),
    ("nextset", "⏭ Next Set"),
    ("next", "👤 Next Player"),
    ("squad", "👥 My Squad"),
    ("sold", "✅ Sold"),
    ("unsold", "❌ Unsold"),
    ("purse", "💰 Purse"),
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


def info_keyboard():
    """The /ainfo menu: every read-only view, one press each."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [InlineKeyboardButton(label, callback_data=f"{INFO_CB}{key}")
               for key, label in INFO_VIEWS]
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
                            R.code("/ainfo"), " · ", R.code("/asets"), " · ",
                            R.code("/asquad")]))
    return blocks


def board_html(session, season, lot=None, *, now=None):
    """The HTML board, plus the footer that points at the team views."""
    body = A.render_board(session, season, lot, now=now)
    return (body + "\n\n<i>Views:</i> <code>/ainfo</code> · <code>/asets</code> "
            "· <code>/asquad</code> · <code>/asoldlist</code>")


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
                     " left · spent ", _money(season, spent), " · 🎯 max bid ",
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


def sets_view(session, season):
    """Every set in running order, and the unsold pile as one of its own."""
    entries = A.list_sets(session, season)
    unsold = A.unsold(session, season.id)
    header = [R.cell(R.bold(""), header=True, align="center"),
              R.cell(R.bold("Set"), header=True),
              R.cell(R.bold("Left"), header=True, align="right"),
              R.cell(R.bold("Sold"), header=True, align="right"),
              R.cell(R.bold("Unsold"), header=True, align="right")]
    table = [header]
    lines = [f"🗂 <b>{_e(season.name)} — sets</b>", ""]
    for entry in entries:
        mark = STATE_MARK[entry["state"]]
        table.append([R.cell(mark, align="center"),
                      R.cell(R.bold(entry["name"]) if entry["state"] in ("live", "next")
                             else entry["name"]),
                      R.cell(str(entry["queued"]), align="right"),
                      R.cell(str(entry["sold"]), align="right"),
                      R.cell(str(entry["unsold"]), align="right")])
        lines.append(f"{mark} <b>{_e(entry['name'])}</b> — "
                     f"{STATE_WORD[entry['state']]} · {entry['queued']} left · "
                     f"{entry['sold']} sold · {entry['unsold']} unsold")
    blocks = [R.heading(f"🗂 {season.name} — sets", size=2)]
    if len(table) > 1:
        blocks.append(R.table(table, bordered=True, striped=True, compact=True))
    else:
        blocks.append(R.paragraph(R.italic("The pool is empty.")))
        lines.append("<i>The pool is empty.</i>")
    blocks.append(R.paragraph([
        "⚡ ", R.bold(A.UNSOLD_SET), f": {len(unsold)} waiting"
        + (" — they return once, automatically, when the main pool is done."
           if unsold and A._as_int(getattr(season, 'auto_accelerated', 1), 1)
           and not A._as_int(getattr(season, 'accelerated_done', 0), 0) else ".")]))
    lines.append("")
    lines.append(f"⚡ <b>{_e(A.UNSOLD_SET)}</b>: {len(unsold)} waiting")
    blocks.append(R.footer("✅ done · 🔨 live · ⏭ next · ⏳ queued — "
                           "/anextset shows the next set's players"))
    return blocks, "\n".join(lines)


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
    blocks = [R.heading(f"⏭ Next set: {entry['name']}", size=2),
              R.paragraph(f"{len(rows)} player{'s' if len(rows) != 1 else ''}, "
                          f"in the order they come up."),
              _lot_table(season, rows[:40])]
    lines = [f"⏭ <b>Next set: {_e(entry['name'])}</b> — {len(rows)} players", ""]
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


def info_menu(session, season, franchise=None):
    """The /ainfo card: where the auction stands, and a button per view."""
    counts = A.pool_counts(session, season.id)
    done = counts.get(A.LOT_SOLD, 0) + counts.get(A.LOT_UNSOLD, 0)
    live = A.current_lot(session, season)
    nxt = A.next_set(session, season)
    blocks = [R.heading(f"📋 {season.name}", size=2),
              R.paragraph([R.bold(A.status_label(season)),
                           f" · {done}/{counts.get('total', 0)} lots resolved"])]
    lines = [f"📋 <b>{_e(season.name)}</b> — {A.status_label(season)} · "
             f"{done}/{counts.get('total', 0)} lots resolved"]
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
        blocks.append(R.paragraph([
            "👛 ", R.bold(franchise.name), ": ",
            _money(season, franchise.purse_remaining_lakh), " · 👥 ",
            f"{franchise.squad_size}/{season.max_squad_size}"]))
        lines.append(f"👛 <b>{_e(franchise.name)}</b>: "
                     f"{_money(season, franchise.purse_remaining_lakh)} · 👥 "
                     f"{franchise.squad_size}/{season.max_squad_size}")
    commands = ("/asets · /anextset · /anextplayer · /asquad · /asoldlist · "
                "/aunsoldlist · /apurse")
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
        ("/aretlock [on]", "Retention state, and close the window"),
    )),
    ("🪪 Right To Match", (
        ("/artmset <cards> [seconds] [premium]", "RTM rules — /artmset off turns it off"),
        ("/artmcards <team> <cards>", "One franchise's own card count"),
        ("/artmforce yes|no|stand", "Answer an open RTM for a franchise"),
        ("/artmundo <player>", "Undo a match — money and card back"),
    )),
    ("🆕 Expansion picks", (
        ("/apick <team> | <player> | [price]", "A new side signs a player"),
        ("/apicks · /apickset · /apickskip · /apickundo", "The pick order and its fixes"),
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
    ("/asets · /anextset · /anextplayer", "Sets, the next set, the next players"),
    ("/asquad [team] · /apurse [team]", "Your squad, every purse"),
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
    who = A.owner_ping(franchise) if franchise else "?"
    status = {A.OFFER_ACCEPTED: "✅ <b>Accepted</b>",
              A.OFFER_DECLINED: "❌ <b>Declined</b>",
              A.OFFER_CANCELLED: "🚫 <b>Withdrawn</b>"}.get(offer.status)
    lines = ["🔒 <b>Retention offer</b>",
             f"<blockquote><b>{_e(offer.player_name)}</b> → "
             f"<b>{_e(franchise.name if franchise else '?')}</b>\n"
             f"💰 {_money(season, price)} · retention "
             f"{kept + 1}/{season.max_retentions}</blockquote>"]
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


async def edit(bot, chat_id, message_id, blocks, *, reply_markup=None):
    """Edit a message into ``blocks``. True, False, or None (not attempted).

    "Not modified" is a success here, as it is for the HTML board: two ticks
    can render the same thing.
    """
    post = getattr(bot, "_post", None)
    if not (R.rich_text_enabled() and blocks and post):
        return None
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
        return False
