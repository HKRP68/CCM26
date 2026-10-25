"""Telegram Mini App surface for the shared franchise auction.

No commits and no Telegram sends here. The existing scheduler announces the
same AuctionEvents whether an action came from chat, the console or this app.
All prices are integer lakh. Never expose negotiation MAPs or other teams'
private shortlists, purse ledgers or draft trade offers.
"""

import html
import json
import os
import re
from datetime import datetime

from models import (AuctionBid, AuctionEvent, AuctionFranchise, AuctionLot, AuctionRetentionOffer,
                    AuctionRetentionTalk, AuctionSeason, AuctionTrade, Player)
from services import auction_service as A
from services import auction_preseason as P
from services import auction_trade_service as T
from services import retention_negotiation as RN
from services import auction_antispam as SPAM
from services.admin_ids import is_admin


class AccessError(A.AuctionError):
    pass


def integer(value, name, low=0, high=1_000_000):
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
        raise A.AuctionError(f"{name} must be a whole number.")
    number = int(value)
    if not low <= number <= high:
        raise A.AuctionError(f"{name} must be between {low} and {high}.")
    return number


def boolean(value, name):
    if not isinstance(value, bool):
        raise A.AuctionError(f"{name} must be true or false.")
    return value


def stamp(value):
    return value.isoformat() + "Z" if value else None


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", value or ""))


def admin_for(db, tg_id):
    # Mini App administration requires the verified Telegram bot-admin identity.
    # Delegated auction admins retain their existing Telegram permissions.
    return is_admin(tg_id)


def season_row(season):
    return dict(id=season.id, name=season.name, status=season.status,
                chat_id=str(season.chat_id) if season.chat_id else None)


def available_seasons(db, tg_id, *, check_member=None, origin_chat_id=None):
    admin = admin_for(db, tg_id)
    rows = db.query(AuctionSeason).order_by(AuctionSeason.id.desc()).all()
    owned = {f.season_id for f in db.query(AuctionFranchise).all()
             if A.may_bid_for(f, tg_id)}
    # A chat hint never grants access. Spectators require an affirmative
    # membership check; owners and admins do not depend on the Telegram API.
    return [s for s in rows if admin or s.id in owned or
            (origin_chat_id and s.chat_id == origin_chat_id and check_member
             and check_member(s.chat_id, tg_id))]


def resolve(db, tg_id, season_id=None, *, check_member=None, origin_chat_id=None):
    rows = available_seasons(db, tg_id, check_member=check_member,
                             origin_chat_id=origin_chat_id)
    if season_id is not None:
        sid = integer(season_id, "Auction id", 1)
        season = next((s for s in rows if s.id == sid), None)
        if season is None:
            # A direct link can name a group without relying on activity history.
            candidate = db.get(AuctionSeason, sid)
            if not (candidate and candidate.chat_id and check_member
                    and check_member(candidate.chat_id, tg_id)):
                raise AccessError("This auction is available to its owners, admins and group members.")
            season = candidate
            rows.append(season)
    else:
        season = next((s for s in rows if s.chat_id == origin_chat_id), None)
        season = season or next((s for s in rows if s.status in A.STATUS_ACTIVE), None)
        season = season or next(iter(rows), None)
    return season, rows


def team(db, season, raw_id):
    row = db.get(AuctionFranchise, integer(raw_id, "Team id", 1))
    if row is None or row.season_id != season.id:
        raise A.AuctionError("That team is not in this auction.")
    return row


def own_team(db, season, tg_id, raw_id=None):
    row = (team(db, season, raw_id) if raw_id is not None
           else A.franchise_for_actor(db, season.id, tg_id))
    if row is None or not A.may_bid_for(row, tg_id):
        raise AccessError("Only this franchise's owner or co-owner can act for it.")
    return row


def lot_for(db, season, raw_id):
    row = db.get(AuctionLot, integer(raw_id, "Lot id", 1))
    if row is None or row.season_id != season.id:
        raise A.AuctionError("That player is not in this auction.")
    return row


def player_for(db, raw_id):
    row = db.get(Player, integer(raw_id, "Player id", 1))
    if row is None or not row.is_active:
        raise A.AuctionError("That player is unavailable.")
    return row


def lot_row(lot):
    return dict(id=lot.id, player_id=lot.player_id, number=lot.lot_no,
                name=lot.name, rating=lot.rating, role=lot.category,
                bat_rating=lot.bat_rating, bowl_rating=lot.bowl_rating,
                country=lot.country, version=lot.version, overseas=bool(lot.is_overseas),
                set_name=A.set_label(lot), status=lot.status,
                base_lakh=lot.base_price_lakh, bid_lakh=lot.current_bid_lakh,
                bidder_id=lot.current_bidder_id, bids=lot.bid_count,
                sold_to_id=lot.sold_to_id, price_lakh=lot.sold_price_lakh, sold_at=stamp(lot.sold_at),
                acquisition=lot.acquisition, deadline=stamp(lot.deadline_at),
                rtm_stage=lot.rtm_stage, rtm_holder_id=lot.previous_franchise_id)


def team_row(db, season, row, tg_id):
    return dict(id=row.id, name=row.name, short_name=row.short_name, logo_url=row.logo_url,
                mine=A.may_bid_for(row, tg_id), purse_lakh=row.purse_remaining_lakh,
                opening_lakh=row.purse_total_lakh, size=row.squad_size,
                overseas=A.overseas_count(db, row.id), roles=A.role_counts(db, row.id),
                max_bid_lakh=max(0, A.max_bid_now(season, row)),
                rtm_left=A.rtm_cards_left(row), retained=row.retained_count,
                picks_left=A.picks_left(row), finalized=bool(row.retention_finalized_at),
                warnings=A.squad_warnings(db, season, row))


def management_payload(db, season, raw_team=None):
    from services import auction_management as M
    field = A.franchises(db, season.id)
    selected = team(db, season, raw_team) if raw_team is not None else next(iter(field), None)
    return dict(managed_team_id=selected.id if selected else None,
                roster_block_reason=M.roster_block_reason(db, season),
                management_locked=bool(season.published_at or season.status == A.STATUS_CANCELLED),
                team_ledger=[dict(kind=e.kind, amount_lakh=e.amount_lakh, note=e.note,
                                  at=stamp(e.created_at)) for e in A.ledger(db, selected.id, limit=40)]
                if selected else [])


def telegram_link(season):
    chat = str(season.chat_id or "")
    message = season.board_message_id
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}/{message or 1}"
    username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    return f"https://t.me/{username}?start=auction" if username else None


def base_payload(db, season, tg_id, rows):
    result = dict(ok=True, auctions=[season_row(s) for s in rows],
                  admin=admin_for(db, tg_id), bot_admin=is_admin(tg_id),
                  server_time=stamp(datetime.utcnow()), season=None)
    if season is None:
        return result
    result.update(season={**season_row(season), "currency": season.currency_label,
                          "max_squad": season.max_squad_size,
                          "min_squad": season.min_squad_size,
                          "telegram_url": telegram_link(season),
                          "published": bool(season.published_at),
                          "league_id": season.league_id},
                  my_team_ids=[f.id for f in A.franchises(db, season.id)
                               if A.may_bid_for(f, tg_id)])
    return result


def live_payload(db, season, tg_id):
    from services.auction_simulator import playback_status
    current = A.current_lot(db, season)
    field = A.franchises(db, season.id)
    teams = [team_row(db, season, f, tg_id) for f in field]
    live = lot_row(current) if current else None
    if current:
        minimum = A.next_min_bid(season, current)
        live.update(next_lakh=minimum, step_lakh=A.increment_for(season, minimum),
                    rtm_price_lakh=A.rtm_price(season, current),
                    extensions=current.extensions_used)
        for f, row in zip(field, teams):
            reason = "Only owners and co-owners can bid." if not row['mine'] else ""
            if not reason:
                try:
                    A.validate_bid(db, season, current, f, minimum, by_tg_id=tg_id)
                except A.AuctionError as exc:
                    reason = str(exc)
            row['bid_reason'] = reason
        bids = db.query(AuctionBid).filter_by(lot_id=current.id).order_by(AuctionBid.id.desc()).limit(20).all()
    else:
        bids = []
    names = {f.id: f.name for f in field}
    return dict(lot=live, teams=teams, counts=A.pool_counts(db, season.id),
                playback=playback_status(db, season),
                direct_bids=A.direct_bids_on(season),
                upcoming=[lot_row(l) for l in A.queued_lots(db, season, limit=5)],
                bids=[dict(id=b.id, team=names.get(b.franchise_id, "Team"),
                           amount_lakh=b.amount_lakh, source=b.source,
                           void=bool(b.is_void), at=stamp(b.created_at)) for b in bids],
                events=[dict(id=e.id, kind=e.kind, text=plain(e.headline),
                             at=stamp(e.created_at)) for e in A.recent_events(db, season.id, 20)])


def arena_payload(db, season, tg_id):
    """Public room data for the optional Arena; no other team's private ledger."""
    result = live_payload(db, season, tg_id)
    for row in result['teams']:
        row['squad'] = [lot_row(lot) for lot in A.squad(db, row['id'])]
    result['max_overseas'] = season.max_overseas
    result['clock_seconds'] = season.bid_seconds
    event = (db.query(AuctionEvent)
             .filter(AuctionEvent.season_id == season.id,
                     AuctionEvent.kind.in_(('lot_sold', 'lot_unsold', 'rtm_matched')))
             .order_by(AuctionEvent.id.desc()).first())
    lot = db.get(AuctionLot, event.lot_id) if event and event.lot_id else None
    result['last_result'] = ({**lot_row(lot), 'event_id': event.id, 'at': stamp(event.created_at)}
                             if lot and lot.season_id == season.id and lot.status in
                             (A.LOT_SOLD, A.LOT_UNSOLD) else None)
    return result


def pool_payload(db, season, data):
    query = db.query(AuctionLot).filter_by(season_id=season.id)
    for key, column in (("status", AuctionLot.status), ("role", AuctionLot.category),
                        ("set_name", AuctionLot.set_name)):
        if data.get(key):
            query = query.filter(column == str(data[key])[:120])
    if data.get("query"):
        needle = str(data['query'])[:100].replace('%', '').replace('_', '')
        query = query.filter(AuctionLot.name.ilike('%' + needle + '%'))
    page = integer(data.get('page', 1), 'Page', 1, 100_000)
    total = query.count()
    pages = max(1, (total + 39) // 40)
    page = min(page, pages)
    return dict(players=[lot_row(l) for l in query.order_by(AuctionLot.lot_no).offset((page-1)*40).limit(40)],
                page=page, pages=pages, total=total, sets=A.list_sets(db, season))


def preseason_payload(db, season, tg_id, raw_team=None):
    row = own_team(db, season, tg_id, raw_team) if raw_team is not None else A.franchise_for_actor(db, season.id, tg_id)
    result = dict(readiness=P.readiness(db, season), mode=season.retention_mode or 'classic',
                  retention_open=A.retention_open(season), candidates=[], offers=[], talks=[],
                  retention=None, shortlist=[], eligible=None, team_id=row.id if row else None)
    if row is None:
        return result
    result.update(retention=P.retention_preview(db, season, row),
                  releasable=[lot_row(l) for l in A.releasable_squad(db, season, row)],
                  candidates=[dict(id=p.id, name=p.name, rating=p.rating, role=p.category,
                                   state=state, note=note) for p, state, note in
                              (RN.retention_candidates(db, season, row) if season.previous_league_id else [])],
                  offers=[dict(id=o.id, name=o.player_name, player_id=o.player_id,
                               price_lakh=A.offer_price(season, row, o))
                          for o in A.pending_retention_offers(db, season.id) if o.franchise_id == row.id],
                  # MAP is intentionally private to the negotiation engine.
                  talks=[dict(id=t.id, name=t.player_name, status=t.status,
                              last_offer_lakh=t.last_offer_lakh, counter_lakh=t.counter_lakh,
                              reply=t.last_reply, chances=RN.chances_left(season, t), attempts=t.attempts)
                         for t in RN.talks_for(db, season.id, row.id)],
                  shortlist=P.shortlist(db, season, row))
    if A.picks_left(row) and season.status == A.STATUS_SETUP:
        result['eligible'] = P.eligible_players(db, season, row)
    return result


def trade_row(db, season, trade, tg_id):
    a, b = T.team_on(db, trade, 'a'), T.team_on(db, trade, 'b')
    return dict(id=trade.id, status=trade.status, phase=trade.phase,
                a_id=a.id, b_id=b.id, a=a.name, b=b.name,
                mine_a=A.may_bid_for(a, tg_id), mine_b=A.may_bid_for(b, tg_id),
                a_lots=[lot_row(l) for l in T.lots_for(db, trade, 'a')],
                b_lots=[lot_row(l) for l in T.lots_for(db, trade, 'b')],
                cash_lakh=trade.cash_lakh, note=trade.note,
                expires=stamp(trade.expires_at), expired=T.is_expired(trade),
                version=stamp(trade.updated_at))


def trades_payload(db, season, tg_id):
    owned = {f.id for f in A.franchises(db, season.id) if A.may_bid_for(f, tg_id)}
    rows = db.query(AuctionTrade).filter_by(season_id=season.id).order_by(AuctionTrade.id.desc()).limit(100).all()
    return dict(window=plain(T.window_line(db, season)), rules=T.trade_rules(season),
                trades=[trade_row(db, season, t, tg_id) for t in rows
                        if t.status in (T.STATUS_COMPLETED, T.STATUS_REVERSED)
                        or t.team_a_id in owned or (t.team_b_id in owned and t.status != T.STATUS_BUILDING)
                        or is_admin(tg_id)],
                teams=[dict(id=f.id, name=f.name, mine=f.id in owned,
                            players=[lot_row(l) for l in T.trade_squad(db, season, f)])
                       for f in A.franchises(db, season.id)],
                block=[dict(id=l.id, name=l.name, team=f.name, note=entry.asking_note)
                       for entry, l, f in T.block_list(db, season)])


def rules_payload(season):
    return dict(min_squad=season.min_squad_size, max_squad=season.max_squad_size,
                min_overseas=season.min_overseas, max_overseas=season.max_overseas,
                home_country=season.home_country, opening_lakh=season.opening_purse_lakh,
                roles_min=A.role_minimums(season), roles_max=A.role_maximums(season),
                rating_rules=A.rating_rules(season), base_prices=A.base_price_rules(season),
                increments=A.increment_rules(season), bid_seconds=season.bid_seconds,
                staged=A.staged_clock(season), countdown=A.countdown_seconds(season),
                snipe_window=season.snipe_window_seconds, snipe_extend=season.snipe_extend_seconds,
                max_extensions=season.max_extensions, direct_bids=A.direct_bids_on(season),
                max_retentions=season.max_retentions, min_retentions=season.min_retentions,
                retention_deadline=stamp(season.retention_deadline_at),
                retention_ranges=A.range_rules(season), draft_ranges=A.range_rules(season, draft=True),
                retention_slots=RN.slots(season), rtm_enabled=bool(season.rtm_enabled),
                rtm_window=season.rtm_window_seconds, rtm_per_team=season.rtm_per_team,
                rtm_extra_lakh=season.rtm_extra_lakh, expansion_picks=season.expansion_picks)


def payload(db, season, tg_id, rows, data):
    result = base_payload(db, season, tg_id, rows)
    if season is None:
        return result
    view = data.get('view', 'live')
    if view == 'arena':
        result.update(arena_payload(db, season, tg_id))
    elif view == 'live':
        result.update(live_payload(db, season, tg_id))
    elif view == 'pool':
        result.update(pool_payload(db, season, data))
    elif view == 'teams':
        result['teams'] = [{**team_row(db, season, f, tg_id),
                            'squad': [lot_row(l) for l in A.squad(db, f.id)]}
                           for f in A.franchises(db, season.id)]
        own = A.franchise_for_actor(db, season.id, tg_id)
        result['ledger'] = [dict(kind=e.kind, amount_lakh=e.amount_lakh, note=e.note,
                                 at=stamp(e.created_at)) for e in A.ledger(db, own.id, limit=100)] if own else []
        bids = (db.query(AuctionBid, AuctionLot).join(AuctionLot, AuctionBid.lot_id == AuctionLot.id)
                .filter(AuctionBid.season_id == season.id, AuctionBid.franchise_id == own.id)
                .order_by(AuctionBid.id.desc()).limit(100).all()) if own else []
        result['my_bids'] = [dict(name=l.name, amount_lakh=b.amount_lakh, void=bool(b.is_void),
                                  source=b.source, at=stamp(b.created_at)) for b, l in bids]
        if result['admin']:
            result.update(management_payload(db, season, data.get('managed_team_id')))
    elif view == 'preseason':
        result.update(preseason_payload(db, season, tg_id, data.get('team_id')))
    elif view == 'eligible':
        row = own_team(db, season, tg_id, data.get('team_id'))
        result.update(P.eligible_players(db, season, row, rating=data.get('rating'),
                                        role=data.get('role'), query=data.get('query'),
                                        country=data.get('country'), page=data.get('page', 1)))
    elif view == 'trades':
        result.update(trades_payload(db, season, tg_id))
    elif view == 'rules':
        result['rules'] = rules_payload(season)
    elif view == 'admin':
        if not result['admin']:
            raise AccessError("Only bot admins can enter the admin panel.")
        from services.auction_simulator import playback_status
        result.update(rules=rules_payload(season), playback=playback_status(db, season),
                      counts=A.pool_counts(db, season.id),
                      lot=lot_row(A.current_lot(db, season)) if A.current_lot(db, season) else None,
                      trade_rules=T.trade_rules(season),
                      retention_rules=RN.rules(season),
                      settings={key: getattr(season, key) for key in A.SEASON_RULE_FIELDS},
                      previous_league_id=season.previous_league_id,
                      teams=[dict(**team_row(db, season, f, tg_id), owner_tg_id=str(f.owner_tg_id or ''),
                                  owner_name=f.owner_name, co_owners=[str(x) for x in A.co_owner_ids(f)],
                                  draft_picks=f.draft_picks_total, previous_team_id=f.previous_team_id)
                             for f in A.franchises(db, season.id)])
    else:
        raise A.AuctionError("Choose a valid auction view.")
    return result


def current_action_lot(db, season, data):
    lot = lot_for(db, season, data.get('lot_id'))
    if season.current_lot_id != lot.id or lot.status not in A.LOT_LIVE:
        raise A.PriceMoved("The player on the block changed. Refresh before acting.")
    if data.get('stage') != lot.rtm_stage:
        raise A.PriceMoved("The RTM window changed. Check the current question.")
    if 'expected_bids' in data and integer(data['expected_bids'], 'Bid count') != lot.bid_count:
        raise A.PriceMoved("The price changed. Check the latest bid before acting.")
    return lot


def perform(db, season, tg_id, data):
    """Apply a named, authorised action. The HTTP boundary owns the commit."""
    action = data.get('action')
    if not isinstance(action, str):
        raise A.AuctionError("Choose an auction action.")
    if action.startswith('admin_'):
        if not admin_for(db, tg_id):
            raise AccessError("Only bot admins can use the admin panel or its actions.")
        return admin_action(db, season, tg_id, data)
    if season is None:
        raise A.AuctionError("Choose an auction first.")
    if action.startswith('trade_'):
        return trade_action(db, season, tg_id, data)
    row = own_team(db, season, tg_id, data.get('team_id'))
    if action in ('bid', 'rtm', 'rtm_stand'):
        lot = current_action_lot(db, season, data)
        if action == 'bid':
            verdict = SPAM.check_bid(season.chat_id or season.id, tg_id,
                                     revision=SPAM.bid_revision(lot, row))
            if not verdict.allowed and not admin_for(db, tg_id):
                raise SPAM.RateLimited(verdict)
            A.place_bid(db, season, lot, row, integer(data.get('amount_lakh'), 'Bid', 1),
                        by_tg_id=tg_id, source='miniapp')
        elif action == 'rtm':
            wants = boolean(data.get('accept'), 'RTM answer')
            if lot.rtm_stage == A.RTM_INTENT:
                A.rtm_intent(db, season, lot, row, wants, by_tg_id=tg_id)
            elif lot.rtm_stage == A.RTM_DECISION:
                A.rtm_decide(db, season, lot, row, wants, by_tg_id=tg_id)
            else:
                raise A.AuctionError("The winning bidder has the next turn.")
        else:
            if lot.rtm_stage != A.RTM_FINAL_OFFER or lot.current_bidder_id != row.id:
                raise AccessError("Only the winning bidder can stand on their final offer.")
            A.rtm_to_decision(db, season, lot)
        return dict(message='Bid placed.' if action == 'bid' else 'RTM answer recorded.')
    if action in ('retain', 'talk_start', 'talk_offer', 'talk_accept', 'talk_withdraw', 'retention_confirm'):
        P.claim_turn(db, season)
        A.require_retention_owner(db, season, row, tg_id)
    if action == 'retain':
        player = player_for(db, data.get('player_id'))
        if RN.is_dynamic(season):
            raise A.AuctionError("Start a negotiation to retain this player.")
        offer = next((o for o in A.pending_retention_offers(db, season.id)
                      if o.franchise_id == row.id and o.player_id == player.id), None)
        if offer:
            A.answer_retention_offer(db, season, offer, tg_id, True)
        else:
            A.retain(db, season, row, player, by_tg_id=tg_id)
    elif action == 'retention_answer':
        offer = db.get(AuctionRetentionOffer, integer(data.get('offer_id'), 'Offer id', 1))
        if offer is None or offer.season_id != season.id or offer.franchise_id != row.id:
            raise AccessError("That retention offer is not for this team.")
        A.answer_retention_offer(db, season, offer, tg_id, boolean(data.get('accept'), 'Answer'))
    elif action == 'retention_confirm':
        P.confirm_retention(db, season, row, data.get('token'), by_tg_id=tg_id)
    elif action == 'release':
        lot = lot_for(db, season, data.get('lot_id'))
        if lot.sold_to_id != row.id:
            raise AccessError("That player does not belong to this team.")
        A.release_by_owner(db, season, lot, tg_id)
    elif action == 'talk_start':
        RN.start_talk(db, season, row, player_for(db, data.get('player_id')),
                      by_tg_id=tg_id, chat_id=season.chat_id)
    elif action in ('talk_offer', 'talk_accept', 'talk_withdraw'):
        talk = db.get(AuctionRetentionTalk, integer(data.get('talk_id'), 'Negotiation id', 1))
        if talk is None or talk.season_id != season.id or talk.franchise_id != row.id:
            raise AccessError("That negotiation is not for this team.")
        if integer(data.get('attempts'), 'Attempts') != talk.attempts:
            raise A.PriceMoved("The negotiation changed. Read the player's latest reply.")
        if action == 'talk_offer':
            RN.make_offer(db, season, talk, integer(data.get('amount_lakh'), 'Offer', 1), tg_id)
        elif action == 'talk_accept':
            RN.accept_counter(db, season, talk, tg_id)
        else:
            RN.withdraw_talk(db, season, talk, tg_id)
    elif action == 'draft_pick':
        A.draft_pick(db, season, row, player_for(db, data.get('player_id')), by_tg_id=tg_id)
    elif action == 'draft_skip':
        A.skip_pick(db, season, row, by_tg_id=tg_id)
    elif action == 'shortlist':
        operation = data.get('operation')
        player = player_for(db, data.get('player_id')) if operation != 'clear' else None
        P.edit_shortlist(db, season, row, operation, player)
    else:
        raise A.AuctionError("Unknown auction action.")
    return dict(message='Auction updated.')


def trade_action(db, season, tg_id, data):
    action = data['action']
    row = own_team(db, season, tg_id, data.get('team_id'))
    if action == 'trade_open':
        trade = T.open_trade(db, season, row, team(db, season, data.get('other_team_id')),
                             by_tg_id=tg_id, chat_id=season.chat_id)
    elif action in ('trade_block_add', 'trade_block_remove'):
        lot = lot_for(db, season, data.get('lot_id'))
        if action == 'trade_block_add':
            T.block_add(db, season, row, lot, note=str(data.get('note', ''))[:200], by_tg_id=tg_id)
        else:
            T.block_remove(db, season, row, lot)
        return dict(message='Trade block updated.')
    else:
        tid = integer(data.get('trade_id'), 'Trade id', 1)
        # Lock then refresh before checking the side/status. Two acceptance
        # requests must never execute a deal twice.
        db.query(AuctionTrade).filter_by(id=tid, season_id=season.id).update(
            {AuctionTrade.updated_at: AuctionTrade.updated_at}, synchronize_session=False)
        db.expire_all()
        trade = db.get(AuctionTrade, tid)
        if trade is None or trade.season_id != season.id:
            raise A.AuctionError("That trade is not in this auction.")
        if data.get('version') != stamp(trade.updated_at):
            raise A.PriceMoved("The trade changed. Read the latest offer before acting.")
        side = T.side_for(trade, row)
        if side is None:
            raise AccessError("Only the two franchises can answer this trade.")
        if action in ('trade_toggle', 'trade_cash', 'trade_note', 'trade_send', 'trade_cancel') and side != 'a':
            raise AccessError("Only the offering team can edit or withdraw this offer.")
        if action in ('trade_accept', 'trade_reject', 'trade_counter') and side != 'b':
            raise AccessError("Only the receiving team can answer this offer.")
        if action == 'trade_toggle':
            T.toggle_lot(db, trade, lot_for(db, season, data.get('lot_id')).id)
        elif action == 'trade_cash':
            T.set_cash(db, trade, integer(data.get('amount_lakh'), 'Cash', -1_000_000))
        elif action == 'trade_note':
            T.set_note(db, trade, str(data.get('note', ''))[:200])
        elif action in ('trade_send', 'trade_accept', 'trade_reject', 'trade_counter', 'trade_cancel'):
            fn = {'trade_send': T.send_offer, 'trade_accept': T.accept, 'trade_reject': T.reject,
                  'trade_counter': T.counter, 'trade_cancel': T.cancel}[action]
            trade = fn(db, trade, by_tg_id=tg_id)
        else:
            raise A.AuctionError("Unknown trade action.")
    return dict(message='Trade updated.', trade_id=trade.id)


def confirm_name(season, data):
    if str(data.get('confirm_name', '')).strip().casefold() != season.name.strip().casefold():
        raise A.AuctionError("Type the auction's name to confirm this action.")


def admin_action(db, season, tg_id, data):
    action = data['action'][6:]
    if action == 'create':
        name = str(data.get('name', '')).strip()
        if not name:
            raise A.AuctionError("Give the auction a name.")
        created = A.create_season(db, name)
        A.log_event(db, created, 'created', f'📝 {html.escape(created.name)} created.',
                    by_tg_id=tg_id, by_admin=True)
        return dict(message='Auction created. Add teams, a group and the player pool.', season_id=created.id)
    if season is None:
        raise A.AuctionError("Choose an auction first.")
    if action == 'start':
        A.start(db, season, by_tg_id=tg_id)
    elif action == 'pause':
        A.pause(db, season, by_tg_id=tg_id)
    elif action == 'next':
        if season.status != A.STATUS_LIVE:
            raise A.AuctionError("Start or resume the auction first.")
        if A.current_lot(db, season):
            raise A.AuctionError("Resolve the current player before opening the next.")
        A.open_next_lot(db, season)
    elif action in ('extend', 'sold', 'unsold', 'undo_bid', 'bid', 'rtm', 'rtm_stand'):
        lot = current_action_lot(db, season, data)
        if action == 'extend':
            A.extend_timer(db, season, lot, integer(data.get('seconds', 15), 'Seconds', 1, 3600), by_tg_id=tg_id)
        elif action == 'sold':
            A.sell_lot(db, season, lot, by_tg_id=tg_id, by_admin=True)
        elif action == 'unsold':
            A.pass_lot(db, season, lot, by_tg_id=tg_id, by_admin=True)
        elif action == 'undo_bid':
            A.undo_last_bid(db, season, lot, by_tg_id=tg_id)
        elif action == 'bid':
            A.place_bid(db, season, lot, team(db, season, data.get('team_id')),
                        integer(data.get('amount_lakh'), 'Bid', 1), by_tg_id=tg_id,
                        by_admin=True, source='miniapp')
        elif action == 'rtm_stand':
            if lot.rtm_stage != A.RTM_FINAL_OFFER:
                raise A.AuctionError("There is no final offer to stand on.")
            A.rtm_to_decision(db, season, lot)
        else:
            holder = A.rtm_holder(db, lot)
            if holder is None or lot.rtm_stage not in (A.RTM_INTENT, A.RTM_DECISION):
                raise A.AuctionError("There is no RTM question to answer.")
            fn = A.rtm_intent if lot.rtm_stage == A.RTM_INTENT else A.rtm_decide
            fn(db, season, lot, holder, boolean(data.get('accept'), 'Answer'), by_tg_id=tg_id)
    elif action in ('restart', 'cancel', 'publish', 'simulate'):
        confirm_name(season, data)
        if action == 'restart':
            A.restart_auction(db, season, by_tg_id=tg_id)
        elif action == 'cancel':
            A.cancel(db, season, by_tg_id=tg_id)
        elif action == 'publish':
            A.publish_to_league(db, season)
        else:
            from services.auction_simulator import simulate_finish
            simulate_finish(db, season, by_tg_id=tg_id)
    elif action == 'clone':
        clone = A.clone_season(db, season, str(data.get('name', '')), by_tg_id=tg_id)
        return dict(message='Next season created.', season_id=clone.id)
    elif action == 'playback':
        from services import auction_simulator as S
        speed = data.get('speed')
        if speed not in ('quick', 'slow', 'mute'):
            raise A.AuctionError('Choose quick, slow or mute playback.')
        if speed == 'mute':
            S.mute_playback(db, season)
        else:
            S.set_playback_speed(db, season, speed == 'quick')
    elif action == 'bind':
        A.bind_chat(db, season, integer(data.get('chat_id'), 'Group id', -10**16, -1))
    elif action == 'franchise':
        if season.status != A.STATUS_SETUP:
            raise A.AuctionError("Edit franchise ownership during setup.")
        name = str(data.get('name', '')).strip()[:120]
        if not name:
            raise A.AuctionError("Give the franchise a name.")
        row = team(db, season, data['team_id']) if data.get('team_id') else A.create_franchise(db, season, name)
        if any(f.id != row.id and f.name.casefold() == name.casefold() for f in A.franchises(db, season.id)):
            raise A.AuctionError("A franchise already has that name.")
        row.name = name
        row.owner_tg_id = integer(data.get('owner_tg_id'), 'Owner Telegram id', 1, 10**16) if data.get('owner_tg_id') else None
        row.owner_name = str(data.get('owner_name', ''))[:120] or None
        ids = data.get('co_owners', [])
        if not isinstance(ids, list) or len(ids) > 30:
            raise A.AuctionError("Give a list of up to 30 co-owner ids.")
        A.set_co_owners(db, row, [integer(x, 'Co-owner id', 1, 10**16) for x in ids])
    elif action == 'franchise_remove':
        row = team(db, season, data.get('team_id'))
        if str(data.get('confirm_name', '')).strip() != row.name:
            raise A.AuctionError("Type the team's name to remove it.")
        A.remove_franchise(db, season, row, by_tg_id=tg_id)
    elif action == 'franchise_import':
        A.import_franchises(db, season, data.get('franchises'), by_tg_id=tg_id)
    elif action == 'previous_league':
        A.link_previous_season(db, season, integer(data.get('league_id'), 'League id', 1))
    elif action == 'previous_team':
        A.set_previous_team(db, season, team(db, season, data.get('team_id')), str(data.get('team_name', '')))
    elif action == 'carry_previous':
        A.carry_previous_squads(db, season, by_tg_id=tg_id)
    elif action == 'purse':
        A.correct_purse(db, season, team(db, season, data.get('team_id')),
                        integer(data.get('amount_lakh'), 'Correction', -1_000_000),
                        note=str(data.get('note', 'Miniapp correction'))[:200], by_tg_id=tg_id)
    elif action in ('team_purse', 'team_add_player', 'team_remove_player'):
        from services import auction_management as M
        row = team(db, season, data.get('team_id'))
        if action == 'team_purse':
            balance = M.adjust_purse(db, season, row, mode=data.get('mode'),
                                     amount_lakh=data.get('amount_lakh'),
                                     expected_purse_lakh=data.get('expected_purse_lakh'),
                                     note=data.get('note'), by_tg_id=tg_id)
            return dict(message=f'{row.name} purse is now {A.render_money(balance, season.currency_label)}.')
        if action == 'team_add_player':
            lot = M.assign_player(db, season, row, integer(data.get('player_id'), 'Player id', 1),
                                   price_lakh=data.get('price_lakh'), note=data.get('note'), by_tg_id=tg_id)
            return dict(message=f'{lot.name} added to {row.name}.')
        refund = M.remove_player(db, season, row, lot_for(db, season, data.get('lot_id')),
                                  note=data.get('note'), by_tg_id=tg_id,
                                  expected_price_lakh=data.get('expected_price_lakh'),
                                  expected_sold_at=data.get('expected_sold_at'))
        return dict(message=f'Player returned to the pool. {A.render_money(refund, season.currency_label)} refunded.')
    elif action == 'settings':
        save_settings(db, season, tg_id, data.get('settings'))
    elif action == 'timer':
        A.set_timer(db, season, str(data.get('seconds', '')))
    elif action == 'increments':
        A.set_increment_rules(db, season, A.parse_increment_rules(str(data.get('rules', ''))), by_tg_id=tg_id)
    elif action in ('focus', 'direct'):
        fn = A.set_focus_mode if action == 'focus' else A.set_direct_bids
        fn(db, season, boolean(data.get('enabled'), 'Enabled'), by_tg_id=tg_id)
    elif action == 'snipe':
        A.set_anti_snipe(db, season, integer(data.get('window'), 'Window', 0, 3600),
                         integer(data.get('extend'), 'Extension', 1, 3600),
                         integer(data.get('max_extensions'), 'Extension limit', 0, 100))
    elif action == 'countdown':
        A.set_countdown(db, season, integer(data.get('seconds'), 'Countdown', 0, 30))
    elif action == 'bid_gap':
        A.set_bid_gap(db, season, integer(data.get('seconds'), 'Bid gap', 0, 60))
    elif action == 'accelerated':
        season.auto_accelerated = int(boolean(data.get('enabled'), 'Accelerated round'))
    elif action == 'pool_range':
        A.add_rating_range_to_pool(db, season, integer(data.get('low'), 'Minimum rating', 1, 100),
                                   integer(data.get('high'), 'Maximum rating', 1, 100),
                                   set_name=str(data.get('set_name', '')).strip()[:120] or None)
    elif action == 'pool_players':
        ids = data.get('player_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise A.AuctionError("Select between 1 and 500 player ids.")
        A.add_players_to_pool(db, season, [player_for(db, pid) for pid in ids],
                              set_name=str(data.get('set_name', '')).strip()[:120] or None)
    elif action == 'pool_auto_sets':
        from services.auction_auto_sets import auto_build_sets
        auto_build_sets(db, season)
    elif action == 'sets_import':
        from services import auction_sets_io as S
        raw = data.get('sets')
        groups = S.parse_sets_file(json.dumps(raw).encode('utf-8'), fmt='json')
        report = S.import_sets(db, season, groups)
        return dict(message=S.summary_text(report), report=report)
    elif action in ('set_forward', 'set_delete', 'set_move'):
        label = str(data.get('set_name', ''))[:120]
        if action == 'set_forward':
            A.bring_forward(db, season, label, by_tg_id=tg_id)
        elif action == 'set_delete':
            if data.get('confirm_name') != label:
                raise A.AuctionError("Type the set's name to delete it.")
            A.delete_set(db, season, label, by_tg_id=tg_id)
        else:
            A.move_set(db, season, label, integer(data.get('delta'), 'Move', -1000, 1000), by_tg_id=tg_id)
    elif action == 'set_order':
        names = data.get('names')
        if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
            raise A.AuctionError("Give the set names in order.")
        A.set_order(db, season, names, by_tg_id=tg_id, exact=True)
    elif action in ('relist', 'withdraw', 'reinstate', 'force_next', 'recall', 'undo_sale', 'pool_remove', 'base_price', 'undo_rtm', 'undo_pick'):
        lot = lot_for(db, season, data.get('lot_id'))
        if action == 'base_price':
            A.set_base_price(db, season, lot, integer(data.get('amount_lakh'), 'Base price', 1))
        else:
            fn = {'relist': A.relist, 'withdraw': A.withdraw_lot, 'reinstate': A.reinstate_lot,
                  'force_next': A.force_next, 'recall': A.recall_sale, 'undo_sale': A.undo_sale,
                  'pool_remove': A.remove_lot, 'undo_rtm': A.undo_rtm, 'undo_pick': A.undo_pick}[action]
            if action in ('relist', 'pool_remove'):
                fn(db, season, lot)
            else:
                fn(db, season, lot, by_tg_id=tg_id)
    elif action == 'relist_all':
        A.relist_all(db, season, by_tg_id=tg_id)
    elif action in ('retention_lock', 'retention_unlock'):
        row = team(db, season, data['team_id']) if data.get('team_id') else None
        A.set_retention_lock(db, season, action == 'retention_lock', row, by_tg_id=tg_id)
    elif action in ('retention_offer', 'retention_force', 'unretain'):
        row = team(db, season, data.get('team_id'))
        if action == 'unretain':
            A.unretain(db, season, row, lot_for(db, season, data.get('lot_id')), by_tg_id=tg_id)
        else:
            price = integer(data['amount_lakh'], 'Price', 1) if data.get('amount_lakh') is not None else None
            player = player_for(db, data.get('player_id'))
            if action == 'retention_offer':
                A.offer_retention(db, season, row, player, price, by_tg_id=tg_id)
            else:
                # retain(by_tg_id=...) is deliberately owner-only. An admin's
                # force action uses the console path and records its actor.
                A.retain(db, season, row, player, price)
    elif action == 'retention_cancel_offer':
        offer = db.get(AuctionRetentionOffer, integer(data.get('offer_id'), 'Offer id', 1))
        if offer is None or offer.season_id != season.id:
            raise A.AuctionError("That offer is not in this auction.")
        A.cancel_retention_offer(db, season, offer)
    elif action == 'rtm_cards':
        A.set_rtm_cards(db, season, team(db, season, data.get('team_id')), integer(data.get('cards'), 'Cards', 0, 100))
    elif action == 'draft_picks':
        A.set_franchise_picks(db, season, team(db, season, data.get('team_id')), integer(data.get('picks'), 'Picks', 0, 100))
    elif action == 'draft_order':
        ids = data.get('team_ids')
        if not isinstance(ids, list):
            raise A.AuctionError("Give a list of team ids.")
        A.set_draft_order(db, season, [team(db, season, fid).id for fid in ids])
    elif action == 'draft_clock':
        P.set_clock(db, season, seconds=integer(data.get('seconds', season.draft_pick_seconds), 'Seconds', 0, 3600),
                    action=data.get('timeout_action', season.draft_timeout_action), operation=data.get('operation', 'save'))
    elif action in ('trade_approve', 'trade_veto', 'trade_reverse'):
        if not is_admin(tg_id):
            raise AccessError("Only bot admins can approve, veto or reverse trades.")
        trade = db.get(AuctionTrade, integer(data.get('trade_id'), 'Trade id', 1))
        if trade is None or trade.season_id != season.id:
            raise A.AuctionError("That trade is not in this auction.")
        fn = {'trade_approve': T.approve, 'trade_veto': T.veto, 'trade_reverse': T.reverse}[action]
        fn(db, trade, by_tg_id=tg_id)
    elif action == 'trade_window':
        T.set_window(db, season, boolean(data.get('open'), 'Trade window'), by_tg_id=tg_id)
    elif action == 'trade_rules':
        if not isinstance(data.get('rules'), dict):
            raise A.AuctionError("Give trade rules as an object.")
        T.set_trade_rules(db, season, data['rules'], by_tg_id=tg_id)
    elif action in ('add_admin', 'remove_admin'):
        if not is_admin(tg_id):
            raise AccessError("Only bot admins can assign auction admins.")
        target = integer(data.get('telegram_id'), 'Telegram id', 1, 10**16)
        if action == 'add_admin':
            A.add_auction_admin(db, target, by_tg_id=tg_id)
        else:
            A.remove_auction_admin(db, target)
    else:
        raise A.AuctionError("Unknown auction admin action.")
    # Existing service events are kept; this additionally attributes rule and
    # setup changes that previously only the password-protected console logged.
    A.log_event(db, season, 'miniapp_admin', f'⚙️ Miniapp: {html.escape(action.replace("_", " "))}.',
                by_tg_id=tg_id, by_admin=True)
    return dict(message='Auction updated.')


def save_settings(db, season, tg_id, settings):
    """Explicit allowlist; never mass-assign lifecycle, purses or clock state."""
    if not isinstance(settings, dict):
        raise A.AuctionError("Give settings as an object.")
    if season.status != A.STATUS_SETUP:
        raise A.AuctionError("Edit the rulebook during setup. Live clock controls are separate.")
    scalar = {
        'min_squad_size': (0, 100), 'max_squad_size': (1, 100),
        'min_overseas': (0, 100), 'max_overseas': (0, 100),
        'min_retentions': (0, 100), 'max_retentions': (0, 100),
        'retention_max_spend_lakh': (0, 1_000_000),
        'retention_min_rating': (0, 100), 'retention_max_rating': (0, 100),
    }
    special = {'opening_purse_lakh', 'home_country', 'bid_seconds', 'staged_clock',
               'countdown_seconds', 'bid_gap_seconds', 'focus_mode', 'direct_bids',
               'auto_accelerated', 'snipe_window_seconds', 'snipe_extend_seconds', 'max_extensions',
               'role_minimums', 'role_maximums', 'rating_rules', 'base_prices', 'increments',
               'retention_mode', 'retention_slots', 'retention_rules', 'retention_ranges',
               'draft_ranges', 'retention_prices', 'expansion_picks', 'rtm_enabled',
               'rtm_per_team', 'rtm_window_seconds', 'rtm_extra_lakh', 'retention_deadline'}
    unknown = set(settings) - set(scalar) - special
    if unknown:
        raise A.AuctionError("Unknown settings: " + ', '.join(sorted(unknown)))
    for key, bounds in scalar.items():
        if key in settings:
            value = settings[key]
            if value is None and key.startswith('retention_'):
                setattr(season, key, None)
            else:
                setattr(season, key, integer(value, key.replace('_', ' '), *bounds))
    if 'home_country' in settings:
        country = str(settings['home_country']).strip()[:60]
        if not country:
            raise A.AuctionError("Give the home country.")
        season.home_country = country
    A.validate_squad_limits(season)
    A.set_role_rules(db, season, settings.get('role_minimums', A.role_minimums(season)),
                      settings.get('role_maximums', A.role_maximums(season)))
    if 'rating_rules' in settings:
        A.set_rating_rules(db, season, settings['rating_rules'])
    if 'opening_purse_lakh' in settings:
        A.set_opening_purse(db, season, integer(settings['opening_purse_lakh'], 'Opening purse', 1), by_tg_id=tg_id)
    for key, fn in (('focus_mode', A.set_focus_mode), ('direct_bids', A.set_direct_bids)):
        if key in settings:
            fn(db, season, boolean(settings[key], key), by_tg_id=tg_id)
    if 'auto_accelerated' in settings:
        season.auto_accelerated = int(boolean(settings['auto_accelerated'], 'Accelerated round'))
    if 'staged_clock' in settings:
        value = settings['staged_clock']
        if not isinstance(value, list) or len(value) != 5:
            raise A.AuctionError("The staged clock needs opening, reset, warning 1, warning 2 and countdown seconds.")
        A.set_timer(db, season, ' '.join(str(integer(v, 'Seconds', 0, 3600)) for v in value))
    elif 'bid_seconds' in settings:
        A.set_timer(db, season, integer(settings['bid_seconds'], 'Seconds', 1, 3600))
    if 'countdown_seconds' in settings:
        A.set_countdown(db, season, integer(settings['countdown_seconds'], 'Countdown', 0, 30))
    if 'bid_gap_seconds' in settings:
        A.set_bid_gap(db, season, integer(settings['bid_gap_seconds'], 'Bid gap', 0, 60))
    if any(k in settings for k in ('snipe_window_seconds', 'snipe_extend_seconds', 'max_extensions')):
        A.set_anti_snipe(db, season, *[integer(settings.get(k, getattr(season, k)), k, 0, 3600)
                                      for k in ('snipe_window_seconds', 'snipe_extend_seconds', 'max_extensions')])
    if 'increments' in settings:
        A.set_increment_rules(db, season, settings['increments'], by_tg_id=tg_id)
    if 'base_prices' in settings:
        rows = settings['base_prices']
        if not isinstance(rows, list) or not rows:
            raise A.AuctionError("Give at least one base-price band.")
        cleaned = []
        for row in rows:
            if not isinstance(row, dict):
                raise A.AuctionError("A base-price band is an object.")
            low = integer(row.get('min_rating'), 'Minimum rating', 0, 100)
            high = integer(row.get('max_rating', 100), 'Maximum rating', low, 100)
            cleaned.append(dict(min_rating=low, max_rating=high,
                                base_lakh=integer(row.get('base_lakh'), 'Base price', 1)))
        season.base_price_rules_json = json.dumps(cleaned)
    if 'retention_mode' in settings:
        RN.set_mode(db, season, settings['retention_mode'], by_tg_id=tg_id)
    if 'retention_slots' in settings and settings['retention_slots'] != RN.slots(season):
        RN.save_slots(db, season, settings['retention_slots'], by_tg_id=tg_id)
    if 'retention_rules' in settings:
        RN.save_rules(db, season, settings['retention_rules'])
    for key, draft in (('retention_ranges', False), ('draft_ranges', True)):
        if key in settings and settings[key] != A.range_rules(season, draft=draft):
            A.set_range_rules(db, season, settings[key], draft=draft)
    if 'retention_prices' in settings:
        rows = settings['retention_prices']
        if not isinstance(rows, list):
            raise A.AuctionError("Retention prices are a list of slab / price_lakh rows.")
        season.retention_price_rules_json = json.dumps([
            dict(slab=integer(r.get('slab'), 'Slab', 1, 100),
                 price_lakh=integer(r.get('price_lakh'), 'Price', 1)) for r in rows])
    if 'retention_deadline' in settings:
        raw = settings['retention_deadline']
        try:
            from datetime import timezone
            value = datetime.fromisoformat(str(raw).replace('Z', '+00:00')) if raw else None
            if value and value.tzinfo:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            season.retention_deadline_at = value
        except ValueError:
            raise A.AuctionError("Use an ISO date for the retention deadline.")
    if 'expansion_picks' in settings:
        A.set_expansion_picks(db, season, integer(settings['expansion_picks'], 'Expansion picks', 0, 100))
    if any(k in settings for k in ('rtm_enabled', 'rtm_per_team', 'rtm_window_seconds', 'rtm_extra_lakh')):
        A.set_rtm_rules(db, season,
                        enabled=boolean(settings.get('rtm_enabled', bool(season.rtm_enabled)), 'RTM'),
                        per_team=integer(settings.get('rtm_per_team', season.rtm_per_team), 'RTM cards', 0, 100),
                        window_seconds=integer(settings.get('rtm_window_seconds', season.rtm_window_seconds), 'RTM seconds', 1, 3600),
                        extra_lakh=integer(settings.get('rtm_extra_lakh', season.rtm_extra_lakh), 'RTM premium'))
    if season.min_retentions > season.max_retentions:
        raise A.AuctionError("Minimum retentions cannot exceed maximum retentions.")
