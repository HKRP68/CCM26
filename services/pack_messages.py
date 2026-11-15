"""Pack shop cards: shared prices and contents for rich text and HTML."""

import html
import json
import logging

from services import rich_message as R

logger = logging.getLogger(__name__)


def prices(pack):
    return [(label, amount) for label, amount in (
        ("🪙 Coins", pack.cost_coins),
        ("🎯 Quest Points", pack.cost_quest_points),
        ("💎 Gems", pack.cost_gems),
    ) if amount]


def cost_text(pack):
    return " or ".join(f"{amount:,} {label}" for label, amount in prices(pack)) or "FREE"


def main_contents(pack):
    mode = (pack.main_filter_mode or "rating").lower()
    versions = []
    try:
        parsed = json.loads(pack.main_versions_json or "[]")
        if isinstance(parsed, list):
            versions = [str(value) for value in parsed]
    except (ValueError, TypeError):
        pass
    band = f"rated {pack.main_min_rating}–{pack.main_max_rating}"
    if mode == "version":
        band = f"{' / '.join(versions) or 'Any'} version · any rating"
    elif mode == "both":
        band = f"{' / '.join(versions) or 'Any'} version · {band}"
    return f"{pack.main_count} × main · {band}"


def bonus_contents(pack):
    return f"{pack.bonus_count} × bonus · rated {pack.bonus_min_rating}–{pack.bonus_max_rating}"


def balances(user):
    return [("🪙 Coins", user.total_coins), ("💎 Gems", user.total_gems),
            ("🎯 Quest Points", user.quest_points or 0)]


def _facts(rows, caption=None):
    return R.table([[R.cell(label), R.cell(R.bold(str(value)), align="right")]
                    for label, value in rows], compact=True, caption=caption)


def _balance_blocks(user):
    return _facts([(label, f"{amount:,}") for label, amount in balances(user)], "Your balance")


def _balance_text(user):
    return " · ".join(f"{label}: <b>{amount:,}</b>" for label, amount in balances(user))


def list_text(packs, user):
    lines = ["📦 <b>PACK STORE</b>", _balance_text(user), ""]
    for pack in packs:
        lines.extend([f"{html.escape(str(pack.emoji))} <b>{pack.slot_number}. {html.escape(pack.name)}</b>",
                      f"💸 {cost_text(pack)}", html.escape(main_contents(pack)),
                      html.escape(bonus_contents(pack)), ""])
    lines.append("Choose a pack below to see its contents and buy it.\nPurchased packs stay unopened until /openpack."
                 if packs else "No packs available right now.")
    return "\n".join(lines)


def detail_text(pack, user, bought_today):
    lines = [f"{html.escape(str(pack.emoji))} <b>{html.escape(pack.name)}</b>",
             f"💸 <b>Price:</b> {cost_text(pack)}"]
    if pack.description:
        lines.extend(["", html.escape(pack.description)])
    lines.extend(["", "🎁 <b>Inside this pack</b>", html.escape(main_contents(pack)),
                  html.escape(bonus_contents(pack))])
    if pack.daily_limit:
        lines.append(f"\n⏳ Remaining today: <b>{max(0, pack.daily_limit - bought_today)}/{pack.daily_limit}</b>")
    lines.extend(["", _balance_text(user), "", "Buy adds one unopened pack to your inventory. Use /openpack when you’re ready."])
    return "\n".join(lines)


def receipt_text(pack, user, result, inventory_count):
    labels = {"coins": "🪙 coins", "quest_points": "Quest Points", "gems": "💎 gems"}
    paid = (f"{result['cost_paid']:,} {labels.get(result['currency'], '')}"
            if result["currency"] != "free" else "FREE")
    return (f"✅ <b>Pack purchased</b>\n{html.escape(str(pack.emoji))} <b>{html.escape(pack.name)}</b>\n"
            f"💸 Paid: <b>{paid}</b>\n\n{_balance_text(user)}\n"
            f"📦 Inventory: <b>{inventory_count}/50</b> unopened packs\n\n"
            "Your pack is ready. Use <b>/openpack</b> to reveal your players.")


def _safe(builder):
    try:
        return builder()
    except Exception:
        logger.exception("Could not build rich pack card")
        return None


def list_blocks(packs, user):
    def build():
        blocks = [R.heading("📦 PACK STORE", 2), _balance_blocks(user)]
        for pack in packs:
            blocks.extend([R.heading(f"{pack.emoji} {pack.slot_number}. {pack.name}", 3),
                           R.paragraph(["💸 ", R.bold(cost_text(pack))]),
                           R.paragraph(main_contents(pack)), R.paragraph(bonus_contents(pack))])
        blocks.append(R.footer("Choose a pack below · Buy now, open later with /openpack"
                               if packs else "No packs available right now."))
        return blocks
    return _safe(build)


def detail_blocks(pack, user, bought_today):
    def build():
        blocks = [R.heading(f"{pack.emoji} {pack.name}", 2),
                  R.paragraph(["💸 Price · ", R.bold(cost_text(pack))])]
        if pack.description:
            blocks.append(R.paragraph(pack.description))
        blocks.extend([R.heading("🎁 Inside this pack", 3),
                       R.paragraph(main_contents(pack)), R.paragraph(bonus_contents(pack))])
        if pack.daily_limit:
            blocks.append(_facts([("Remaining today", f"{max(0, pack.daily_limit - bought_today)}/{pack.daily_limit}")]))
        blocks.extend([_balance_blocks(user), R.footer(
            "Buy adds one unopened pack to your inventory. Use /openpack when you’re ready.")])
        return blocks
    return _safe(build)


def receipt_blocks(pack, user, result, inventory_count):
    def build():
        label = {"coins": "🪙 coins", "quest_points": "Quest Points", "gems": "💎 gems"}.get(result["currency"], "")
        paid = "FREE" if result["currency"] == "free" else f"{result['cost_paid']:,} {label}"
        return [R.heading("✅ Pack purchased", 2), R.paragraph(R.bold(f"{pack.emoji} {pack.name}")),
                _facts([("Paid", paid), ("Unopened packs", f"{inventory_count}/50")]),
                _balance_blocks(user), R.paragraph(["Your pack is ready. Use ", R.code("/openpack"),
                                                   " to reveal your players."])]
    return _safe(build)
