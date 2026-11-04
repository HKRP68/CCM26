"""Frozen prize shares, atomic account credits and per-recipient receipts."""

import logging
from collections import defaultdict
from datetime import datetime
from html import escape

logger = logging.getLogger(__name__)


def payout_rows(session, tournament_id):
    from models import TournamentPrizePayout
    return (session.query(TournamentPrizePayout)
            .filter_by(tournament_id=int(tournament_id))
            .order_by(TournamentPrizePayout.id).all())


def payout_summary(session, tournament_id):
    result = {"paid": 0, "pending": 0, "failed": 0, "reversed": 0, "cancelled": 0, "coins": 0, "gems": 0}
    for status in ("paid", "pending", "failed"):
        result[f"{status}_coins"] = result[f"{status}_gems"] = 0
    for row in payout_rows(session, tournament_id):
        if row.status in ("reversed", "cancelled"):
            result[row.status] += 1
            continue
        status = row.status if row.status in ("paid", "pending", "failed") else "pending"
        result[status] += 1
        result["coins"] += row.coins
        result["gems"] += row.gems
        result[f"{status}_coins"] += row.coins
        result[f"{status}_gems"] += row.gems
    return result


def _recipients(session, tour, honour):
    from models import TournamentTeam, User
    from services.tournament_prize_splits import recipients
    team = session.get(TournamentTeam, honour.tournament_team_id) if honour.tournament_team_id else None
    if team is None:
        team = (session.query(TournamentTeam)
                .filter_by(tournament_id=tour.id, name=honour.team_name).first())
    if team:
        honour.tournament_team_id = team.id
        shares = recipients(session, tour, team)
        if shares:
            return ("team", team.id), shares
        # An actual team without managers must not pay its match controller.
        return ("team", team.id), [{"telegram_id": 0, "weight": 1}]
    user = session.get(User, honour.user_id) if honour.user_id else None
    return ("honour", honour.id), [{"telegram_id": user.telegram_id if user else 0,
                                     "weight": 1}]


def _new_row(honour, tg, coins, gems, bps, **extra):
    from models import TournamentPrizePayout
    return TournamentPrizePayout(
        honour_id=honour.id, tournament_id=honour.tournament_id,
        revision=honour.prize_revision or 0,
        tournament_name=honour.tournament_name, award=honour.award,
        team_name=honour.team_name, recipient_tg_id=tg,
        coins=coins, gems=gems, share_bps=bps, **extra)


def _from_remaining(amount, remaining):
    from services.tournament_prize_splits import split_amount
    result = [0] * len(remaining)
    if not amount:
        return result
    active = [i for i, units in enumerate(remaining) if units > 0]
    allocated = split_amount(amount, [{"weight": remaining[i]} for i in active])
    for i, units in zip(active, allocated):
        result[i] = units
    return result


def _pool_targets(amount, shares, existing, currency):
    """Carry rounding across late prizes while protecting every reserved share."""
    from services.tournament_prize_splits import split_amount
    allocated = {share["telegram_id"]: 0 for share in shares}
    for row in existing:
        if row.recipient_tg_id in allocated:
            allocated[row.recipient_tg_id] += getattr(row, currency)
    total = amount + sum(allocated.values())
    ideal = split_amount(total, shares)
    deficits = [max(0, ideal[i] - allocated[share["telegram_id"]])
                for i, share in enumerate(shares)]
    # Apportionment with custom weights can lower a target by one as the pool
    # grows. Reserved/paid units cannot be reclaimed; distribute the new amount
    # among positive deficits instead, retaining exact conservation.
    return _from_remaining(amount, deficits)


def plan_honours(session, tour, honours):
    """Freeze entitlement once; allocate a team's combined awards exactly."""
    from models import TournamentPrizePayout, User
    from services.tournament_awards import prizes
    from services.tournament_prize_splits import split_amount
    session.flush()
    table = prizes(tour)
    groups = defaultdict(list)
    shares_by_group = {}
    for honour in honours:
        if not (honour.team_name or honour.player_name):
            continue
        if (honour.prize_revision and honour.prize_snapshot_coins == 0
                and not honour.prize_snapshot_gems):
            # A reviewed zero-value correction cancels the award's cash prize.
            continue
        existing = (session.query(TournamentPrizePayout)
                    .filter_by(honour_id=honour.id, revision=honour.prize_revision or 0)
                    .populate_existing().all())
        if not existing and honour.prize_snapshot_coins is None and (
                honour.prize_coins or honour.prize_gems):
            # Pre-ledger honours and balances committed in one transaction.
            # Backfill their receipt without creating currency a second time.
            user = session.get(User, honour.user_id) if honour.user_id else None
            honour.prize_snapshot_coins = honour.prize_coins or 0
            honour.prize_snapshot_gems = honour.prize_gems or 0
            session.add(_new_row(
                honour, user.telegram_id if user else 0,
                honour.prize_coins or 0, honour.prize_gems or 0, 10000,
                user_id=user.id if user else None, status="paid",
                paid_at=honour.awarded_at))
            continue
        placeholder = (len(existing) == 1 and existing[0].recipient_tg_id == 0
                       and existing[0].status == "pending")
        if existing and not placeholder:
            continue
        if not existing and honour.prize_snapshot_coins is None and not table.get(honour.award):
            continue
        key, shares = _recipients(session, tour, honour)
        if existing:
            # The only re-plannable row has no recipient assigned yet. Known
            # Telegram IDs and their shares remain frozen even without accounts.
            if placeholder and shares[0]["telegram_id"]:
                session.delete(existing[0])
            else:
                continue
        if honour.prize_snapshot_coins is None:
            prize = table.get(honour.award) or {}
            coins, gems = int(prize.get("coins") or 0), int(prize.get("gems") or 0)
            if not (coins or gems):
                continue
            honour.prize_snapshot_coins, honour.prize_snapshot_gems = coins, gems
        groups[key].append(honour)
        shares_by_group[key] = shares

    for key, awards in groups.items():
        shares = shares_by_group[key]
        session.flush()
        if key[0] == "team":
            from models import TournamentHonour
            reserved = (session.query(TournamentPrizePayout)
                        .join(TournamentHonour, TournamentPrizePayout.honour_id == TournamentHonour.id)
                        .filter(TournamentHonour.tournament_id == tour.id,
                                TournamentHonour.tournament_team_id == key[1],
                                TournamentPrizePayout.revision == TournamentHonour.prize_revision,
                                TournamentPrizePayout.status.in_(("paid", "pending", "failed"))).all())
        else:
            reserved = []
        coins_left = _pool_targets(sum(h.prize_snapshot_coins or 0 for h in awards), shares, reserved, "coins")
        gems_left = _pool_targets(sum(h.prize_snapshot_gems or 0 for h in awards), shares, reserved, "gems")
        bps = split_amount(10000, shares)
        for honour in awards:
            # Allocate from the remaining pool targets. This preserves the
            # aggregate 50/50 result even when several award amounts are odd.
            coins = _from_remaining(honour.prize_snapshot_coins or 0, coins_left)
            gems = _from_remaining(honour.prize_snapshot_gems or 0, gems_left)
            for i, share in enumerate(shares):
                tg = share["telegram_id"]
                session.add(_new_row(
                    honour, tg, coins[i], gems[i], bps[i], status="pending",
                    failure_reason=None if tg else "Assign a team owner to release this prize."))
                coins_left[i] -= coins[i]
                gems_left[i] -= gems[i]
    session.flush()


def _lock_payout(session, row):
    from models import TournamentPrizePayout
    session.flush()
    if session.get_bind().dialect.name == "sqlite":
        session.query(TournamentPrizePayout).filter_by(id=row.id).update({
            TournamentPrizePayout.updated_at: TournamentPrizePayout.updated_at,
        }, synchronize_session=False)
    return (session.query(TournamentPrizePayout).filter_by(id=row.id)
            .populate_existing().with_for_update().one())


def _settle_rows(session, rows):
    from models import User
    from sqlalchemy import func
    from services.activity_service import log_activity
    from services.tournament_awards import AWARD_LABEL
    for original in sorted(rows, key=lambda r: (r.recipient_tg_id, r.id)):
        row = _lock_payout(session, original)
        if row.status not in ("pending", "failed"):
            continue
        if not row.recipient_tg_id:
            row.status = "pending"
            continue
        try:
            with session.begin_nested():
                user = session.query(User).filter_by(telegram_id=row.recipient_tg_id).first()
                if not user and (row.coins or row.gems):
                    row.status = "pending"
                    row.failure_reason = "Recipient must register with /debut."
                    continue
                if user:
                    session.query(User).filter_by(id=user.id).update({
                        User.total_coins: func.coalesce(User.total_coins, 0) + row.coins,
                        User.total_gems: func.coalesce(User.total_gems, 0) + row.gems,
                    }, synchronize_session="fetch")
                    session.refresh(user, attribute_names=["total_coins", "total_gems"])
                    row.user_id = user.id
                    row.balance_coins_after, row.balance_gems_after = user.total_coins, user.total_gems
                    if row.coins or row.gems:
                        log_activity(
                            session, user.id, "tournament_prize",
                            f"Receipt #{row.id} · {AWARD_LABEL.get(row.award, row.award)} — "
                            f"{row.tournament_name}: +{row.coins} coins, +{row.gems} gems. "
                            f"Balance: {user.total_coins} coins, {user.total_gems} gems",
                            coins_change=row.coins, gems_change=row.gems)
                row.status, row.failure_reason, row.paid_at = "paid", None, datetime.utcnow()
                session.flush()
        except Exception as exc:
            # A failed share rolls back its account change and activity entry;
            # other recipients can still be paid and this receipt can retry.
            logger.exception("Tournament prize receipt %s failed", row.id)
            row.status = "failed"
            row.failure_reason = f"{type(exc).__name__}: {str(exc)}"[:500]
            session.flush()


def _sync_honours(session, rows):
    from models import TournamentHonour, TournamentPrizePayout
    from sqlalchemy import func
    for hid in sorted({row.honour_id for row in rows if row.honour_id}):
        honour = (session.query(TournamentHonour).filter_by(id=hid)
                  .populate_existing().with_for_update().first())
        if not honour:
            continue
        coins, gems = (session.query(func.coalesce(func.sum(TournamentPrizePayout.coins), 0),
                                     func.coalesce(func.sum(TournamentPrizePayout.gems), 0))
                       .filter_by(honour_id=hid, revision=honour.prize_revision or 0,
                                  status="paid").one())
        honour.prize_coins, honour.prize_gems = coins, gems
        linked = [row for row in rows if row.honour_id == hid
                  and row.revision == (honour.prize_revision or 0)]
        if len(linked) == 1 and linked[0].user_id:
            honour.user_id = linked[0].user_id
    session.flush()


def pay_honours(session, tour, honours):
    honours = list(honours)
    plan_honours(session, tour, honours)
    rows = payout_rows(session, tour.id)
    _settle_rows(session, rows)
    _sync_honours(session, rows)


def retry_payouts(session, tour):
    """Retry pending/failed shares; this function never commits the caller."""
    from services.tournament_awards import _lock_tournament, honours_for
    tour = _lock_tournament(session, tour)
    pay_honours(session, tour, honours_for(session, tour.id, refresh=True))
    return payout_summary(session, tour.id)


def retry_for_recipient(session, telegram_id):
    """Release frozen pending shares after registration, including old events."""
    from models import TournamentPrizePayout
    rows = (session.query(TournamentPrizePayout)
            .filter(TournamentPrizePayout.recipient_tg_id == int(telegram_id),
                    TournamentPrizePayout.status.in_(("pending", "failed")))
            .order_by(TournamentPrizePayout.id).all())
    _settle_rows(session, rows)
    _sync_honours(session, rows)
    return sum(row.status == "paid" for row in rows)


def honour_payment_text(session, tour, honour):
    from models import TournamentPrizePayout, User
    from services.tournament_awards import prize_text, prizes
    rows = (session.query(TournamentPrizePayout).filter_by(honour_id=honour.id,
                                                         revision=honour.prize_revision or 0)
            .order_by(TournamentPrizePayout.id).all()) if honour.id else []
    if rows:
        lines = []
        for row in rows:
            amount = prize_text({"coins": row.coins, "gems": row.gems})
            if not amount:
                continue
            user = session.get(User, row.user_id) if row.user_id else None
            label = ("@" + user.username if user.username else user.first_name or str(user.telegram_id)) if user else (
                str(row.recipient_tg_id) if row.recipient_tg_id else "Unassigned")
            state = {"paid": "Credited", "pending": "Pending", "failed": "Failed"}.get(row.status, "Pending")
            lines.append(f"{state}: {amount} → {escape(label)}")
        return "\n".join(lines)
    paid = prize_text({"coins": honour.prize_coins, "gems": honour.prize_gems})
    if paid:
        user = session.get(User, honour.user_id) if honour.user_id else None
        label = ("@" + user.username if user.username else user.first_name or str(user.telegram_id)) if user else ""
        return f"Credited: {paid}" + (f" → {escape(label)}" if label else "")
    pending = prize_text(prizes(tour).get(honour.award))
    if honour.prize_revision and honour.prize_snapshot_coins == 0 and not honour.prize_snapshot_gems:
        return "Cancelled — no cash prize"
    return f"Pending: {pending} — assign a recipient" if pending and (honour.team_name or honour.player_name) else ""


def recipient_receipt_page(session, telegram_id, before_id=None, limit=12):
    """A complete, bounded Telegram page and the cursor for older receipts."""
    from models import TournamentPrizePayout
    from services.tournament_awards import AWARD_LABEL, prize_text
    query = session.query(TournamentPrizePayout).filter_by(recipient_tg_id=int(telegram_id))
    if before_id:
        query = query.filter(TournamentPrizePayout.id < int(before_id))
    rows = query.order_by(TournamentPrizePayout.id.desc()).limit(limit + 1).all()
    lines = ["💰 <b>TOURNAMENT PRIZE RECEIPTS</b>", ""]
    if not rows:
        return "\n".join(lines + ["No tournament prize receipts yet."]), None
    consumed, last_id = 0, None
    for row in rows[:limit]:
        receipt = []
        state = {"paid": "Credited", "pending": "Pending", "failed": "Failed — awaiting retry",
                 "reversed": "Reversed — previous credit deducted", "cancelled": "Cancelled — never credited"}.get(row.status, row.status)
        receipt.append(f"<b>#{row.id} · {escape(row.tournament_name)}</b>")
        receipt.append(f"{escape(AWARD_LABEL.get(row.award, row.award))} · {escape(row.team_name or '—')}")
        amount = prize_text({"coins": row.coins, "gems": row.gems}) or "0 coins"
        receipt.append(f"{state}: {amount}")
        if row.balance_coins_after is not None:
            receipt.append(f"Balance after credit: {row.balance_coins_after:,} coins + {row.balance_gems_after or 0:,} gems")
        if row.paid_at:
            receipt.append(row.paid_at.strftime("%d %b %Y %H:%M UTC"))
        if row.reversed_at:
            receipt.append("Reversed: " + row.reversed_at.strftime("%d %b %Y %H:%M UTC"))
            if row.reversal_coins_after is not None:
                receipt.append(f"Balance after reversal: {row.reversal_coins_after:,} coins + {row.reversal_gems_after or 0:,} gems")
        receipt.append("")
        if len("\n".join(lines + receipt)) > 3500:
            break
        lines.extend(receipt)
        consumed += 1
        last_id = row.id
    return "\n".join(lines), last_id if consumed < len(rows) else None


def recipient_receipts(session, telegram_id, limit=12):
    """Private profile view; look up by Telegram identity even before payment."""
    return recipient_receipt_page(session, telegram_id, limit=limit)[0]
