"""Audited team corrections shared by the website and Telegram Mini App.

Callers authenticate admins and own the transaction. Imports stay local because
the auction test suites replace the database/model modules between fixtures.
"""

import html
import re
from datetime import datetime


def whole(value, label, minimum=0):
    from services import auction_service as A
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise A.AuctionError(f"{label} must be a whole number of lakh.")
    number = int(value)
    if not minimum <= number <= 1_000_000:
        raise A.AuctionError(f"{label} must be between {minimum} and 1000000 lakh.")
    return number


def reason_required(note):
    from services import auction_service as A
    reason = str(note or '').strip()
    if not reason:
        raise A.AuctionError("Add a reason so this change can be traced in the ledger.")
    return reason[:200]


def roster_block_reason(session, season):
    from services import auction_service as A
    if season.published_at:
        return "Squads are published. Correct players in the Challenge League."
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        return "Pause the auction before changing team players."
    if A.current_lot(session, season):
        return "Resolve the current player before changing team players."
    return ''


def _claim(session, season, franchise, *, roster=False):
    from services import auction_service as A, auction_preseason as P
    P.claim_turn(session, season)
    if franchise.season_id != season.id:
        raise A.AuctionError("That team is not in this auction.")
    if season.published_at or season.status == A.STATUS_CANCELLED:
        raise A.AuctionError("This auction is published or cancelled and cannot be edited here.")
    if roster:
        reason = roster_block_reason(session, season)
        if reason:
            raise A.AuctionError(reason)


def adjust_purse(session, season, franchise, *, mode, amount_lakh,
                 expected_purse_lakh, note, by_tg_id=None):
    from services import auction_service as A
    if by_tg_id is not None:
        A.require_auction_manager(session, by_tg_id)
    amount = whole(amount_lakh, 'Amount')
    expected = whole(expected_purse_lakh, 'Displayed purse')
    reason = reason_required(note)
    if mode not in ('add', 'deduct', 'set'):
        raise A.AuctionError("Choose Add funds, Deduct funds or Set balance.")
    _claim(session, season, franchise)
    if franchise.purse_remaining_lakh != expected:
        raise A.PriceMoved("The team's purse changed. Refresh and review the new balance.")
    delta = amount if mode == 'add' else -amount if mode == 'deduct' else amount - expected
    # A standing winning bid has not been charged yet. A correction must leave
    # enough for that bid as well as the minimum-squad reserve.
    lot = A.current_lot(session, season)
    if lot and lot.current_bidder_id == franchise.id:
        reserve = max(0, season.min_squad_size - franchise.squad_size - 1) * season.min_base_price_lakh
        if expected + delta < int(lot.current_bid_lakh or 0) + reserve:
            raise A.AuctionError("That balance cannot cover this team's standing bid and squad reserve.")
    return A.correct_purse(session, season, franchise, delta, note=reason,
                           by_tg_id=by_tg_id, expected_balance=expected)


def assign_player(session, season, franchise, player_id, *, price_lakh, note,
                  by_tg_id=None):
    from models import AuctionBid, AuctionLot, AuctionFranchise, Player
    from services import auction_service as A, player_query
    if by_tg_id is not None:
        A.require_auction_manager(session, by_tg_id)
    price = whole(price_lakh, 'Signing price')
    reason = reason_required(note)
    _claim(session, season, franchise, roster=True)
    player = player_query.master_player_query(session).filter(Player.id == player_id).first()
    if player is None:
        raise A.AuctionError("Choose an active master player from the catalogue.")
    lot = session.query(AuctionLot).filter_by(season_id=season.id, player_id=player.id).first()
    if lot is None:
        A.add_players_to_pool(session, season, [player], set_name='Admin additions')
        lot = session.query(AuctionLot).filter_by(season_id=season.id, player_id=player.id).one()
    if lot.status not in (A.LOT_QUEUED, A.LOT_UNSOLD) or lot.sold_to_id:
        raise A.AuctionError("This player is already signed or unavailable. Refresh the player list.")
    # A league stores one row per cricketer name, even when the catalogue has
    # multiple versions. Do not let a manual signing collapse on publication.
    name = (lot.name or '').strip().casefold()
    if any((p.name or '').strip().casefold() == name for p in A.squad(session, franchise.id)):
        raise A.AuctionError("This team already has a card of that player.")
    from services.auction_trade_service import traded_lot_ids
    if lot.id in traded_lot_ids(session, season.id):
        raise A.AuctionError("This player has completed trade history. Resolve the trade first.")
    A.validate_preauction_signing(session, season, franchise, lot, price, verb='sign')
    A.check_role_rules(session, season, franchise, lot)
    # The season claim serializes manual signings with retention/draft work;
    # conditional updates also protect against a sale or an external purse edit.
    claimed = (session.query(AuctionLot)
               .filter(AuctionLot.id == lot.id, AuctionLot.status.in_((A.LOT_QUEUED, A.LOT_UNSOLD)),
                       AuctionLot.sold_to_id.is_(None))
               .update(dict(status=A.LOT_SOLD, acquisition='admin', sold_to_id=franchise.id,
                            sold_price_lakh=price, sold_at=datetime.utcnow(), deadline_at=None,
                            current_bid_lakh=None, current_bidder_id=None, bid_count=0,
                            going_stage=0, extensions_used=0), synchronize_session=False))
    debited = (session.query(AuctionFranchise)
               .filter(AuctionFranchise.id == franchise.id,
                       AuctionFranchise.purse_remaining_lakh == franchise.purse_remaining_lakh,
                       AuctionFranchise.squad_size == franchise.squad_size)
               .update({AuctionFranchise.purse_remaining_lakh: AuctionFranchise.purse_remaining_lakh - price,
                        AuctionFranchise.squad_size: AuctionFranchise.squad_size + 1}, synchronize_session=False))
    if not claimed or not debited:
        raise A.PriceMoved("The player or team changed. Refresh before signing.")
    session.query(AuctionBid).filter_by(lot_id=lot.id, is_void=False).update(
        dict(is_void=True, voided_by_tg_id=by_tg_id), synchronize_session=False)
    session.expire_all()
    A._ledger(session, franchise, A.LEDGER_PURCHASE, -price, lot=lot,
              note=f"Admin signing: {lot.name}. {reason}", by_tg_id=by_tg_id)
    A.log_event(session, season, 'admin_player_added',
                f"📝 {html.escape(lot.name)} added to <b>{html.escape(franchise.name)}</b> "
                f"for {A.render_money(price, season.currency_label)}.",
                lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True,
                detail=dict(price_lakh=price, reason=reason))
    session.flush()
    return lot


def remove_player(session, season, franchise, lot, *, note, by_tg_id=None,
                  expected_price_lakh=None, expected_sold_at=None):
    from models import AuctionBid, AuctionFranchise, AuctionLot
    from services import auction_service as A
    if by_tg_id is not None:
        A.require_auction_manager(session, by_tg_id)
    from services.auction_trade_service import traded_lot_ids
    reason = reason_required(note)
    _claim(session, season, franchise, roster=True)
    if lot.season_id != season.id or lot.status != A.LOT_SOLD or lot.sold_to_id != franchise.id:
        raise A.PriceMoved("This player is no longer on that team. Refresh the squad.")
    if ((expected_price_lakh is not None and whole(expected_price_lakh, 'Displayed refund') != int(lot.sold_price_lakh or 0))
            or (expected_sold_at is not None and expected_sold_at !=
                (lot.sold_at.isoformat() + 'Z' if lot.sold_at else ''))):
        raise A.PriceMoved("This signing changed. Refresh the squad and review the refund before removing it.")
    if lot.id in traded_lot_ids(session, season.id):
        raise A.AuctionError("This player has completed trade history. Reverse the trade before removing them.")
    price = int(lot.sold_price_lakh or 0)
    if lot.acquisition == A.ACQ_RETAINED:
        A.unretain(session, season, franchise, lot, by_tg_id=by_tg_id)
    elif lot.acquisition == A.ACQ_DRAFTED:
        A.undo_pick(session, season, lot, by_tg_id=by_tg_id)
    else:
        acquisition = lot.acquisition
        claimed = (session.query(AuctionLot)
                   .filter_by(id=lot.id, status=A.LOT_SOLD, sold_to_id=franchise.id)
                   .update(dict(status=A.LOT_QUEUED, acquisition=A.ACQ_AUCTION,
                                sold_to_id=None, sold_price_lakh=None, sold_at=None),
                           synchronize_session=False))
        if not claimed:
            raise A.PriceMoved("This player changed. Refresh the squad before removing them.")
        updates = {AuctionFranchise.purse_remaining_lakh: AuctionFranchise.purse_remaining_lakh + price,
                   AuctionFranchise.squad_size: AuctionFranchise.squad_size - 1}
        if acquisition == A.ACQ_RTM:
            from sqlalchemy import case
            updates[AuctionFranchise.rtm_cards_used] = case(
                (AuctionFranchise.rtm_cards_used > 0, AuctionFranchise.rtm_cards_used - 1), else_=0)
        changed = (session.query(AuctionFranchise)
                   .filter(AuctionFranchise.id == franchise.id, AuctionFranchise.squad_size > 0)
                   .update(updates, synchronize_session=False))
        if not changed:
            raise A.AuctionError("The squad count needs an admin review before removing this player.")
        session.expire_all()
        A._ledger(session, franchise, A.LEDGER_REFUND, price, lot=lot,
                  note=f"Player removed: {lot.name}. {reason}", by_tg_id=by_tg_id)
    # The existing retention/draft helpers create their own refund row. Keep
    # the admin's reason on that row too, without issuing a second refund.
    session.flush()
    from models import AuctionLedgerEntry
    refund = (session.query(AuctionLedgerEntry)
              .filter_by(franchise_id=franchise.id, lot_id=lot.id, kind=A.LEDGER_REFUND)
              .order_by(AuctionLedgerEntry.id.desc()).first())
    if refund and reason not in (refund.note or ''):
        refund.note = f"{refund.note or 'Player removed.'} {reason}"
    session.query(AuctionBid).filter_by(lot_id=lot.id, is_void=False).update(
        dict(is_void=True, voided_by_tg_id=by_tg_id), synchronize_session=False)
    lot.current_bid_lakh = lot.current_bidder_id = lot.deadline_at = None
    lot.bid_count = lot.going_stage = lot.extensions_used = 0
    lot.rtm_stage = lot.rtm_offered_at = lot.rtm_base_bid_lakh = lot.rtm_matched_by_id = None
    lot.lot_no = A._next_lot_no(session, season.id)
    A.log_event(session, season, 'admin_player_removed',
                f"↩️ {html.escape(lot.name)} removed from <b>{html.escape(franchise.name)}</b>; "
                f"{A.render_money(price, season.currency_label)} refunded. Player returned to the queue.",
                lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True,
                detail=dict(refund_lakh=price, reason=reason))
    session.flush()
    return price
