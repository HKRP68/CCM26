"""Public transfer facts from completed trades in linked auction seasons.

The website reads these on demand. No work is added to a match or auction
transaction, and private negotiations, finances and moderator metadata stay out.
"""
import json


def _json(raw, fallback):
    try:
        data = json.loads(raw or '')
        return data if isinstance(data, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _ids(raw):
    values = set()
    for item in _json(raw, []):
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if number > 0:
            values.add(number)
    return values


def _names(trade, side, lots):
    # The execution snapshot preserves names if a catalogue entry was renamed
    # or removed later. Only the public name field is ever read from it.
    snapshot = _json(trade.snapshot_json, {})
    recorded = snapshot.get(side)
    names = []
    if isinstance(recorded, list):
        names = [row['name'].strip()[:150] for row in recorded if isinstance(row, dict)
                 and isinstance(row.get('name'), str) and row['name'].strip()]
    if not names:
        raw = trade.lots_a_json if side == 'a' else trade.lots_b_json
        names = [lots[lot_id].name for lot_id in sorted(_ids(raw))
                 if lot_id in lots and lots[lot_id].season_id == trade.season_id]
    return list(dict.fromkeys(names))


def _label(names):
    shown = ', '.join(names[:6])
    return shown + (f' and {len(names) - 6} more' if len(names) > 6 else '')


def transfer_stories(session, seasons):
    """Return at most 30 published transfer facts for the selected season(s)."""
    from models import AuctionTrade, AuctionLot
    from sqlalchemy.orm import joinedload
    auction_ids = {s.auction_season_id for s in seasons if s.auction_season_id is not None}
    if not auction_ids:
        return []
    trades = (session.query(AuctionTrade)
              .options(joinedload(AuctionTrade.team_a), joinedload(AuctionTrade.team_b))
              .filter(AuctionTrade.season_id.in_(auction_ids), AuctionTrade.status == 'completed')
              .order_by(AuctionTrade.completed_at.desc(), AuctionTrade.id.desc()).limit(30).all())
    lot_ids = set()
    for trade in trades:
        lot_ids.update(_ids(trade.lots_a_json))
        lot_ids.update(_ids(trade.lots_b_json))
    lots = {lot.id: lot for lot in session.query(AuctionLot).filter(
        AuctionLot.id.in_(lot_ids), AuctionLot.season_id.in_(auction_ids))} if lot_ids else {}
    stories = []
    for trade in trades:
        a, b = trade.team_a, trade.team_b
        if not a or not b or a.season_id != trade.season_id or b.season_id != trade.season_id:
            continue
        parts = []
        names_a, names_b = _names(trade, 'a', lots), _names(trade, 'b', lots)
        if names_a:
            parts.append(f'{_label(names_a)}: {a.name} → {b.name}.')
        if names_b:
            parts.append(f'{_label(names_b)}: {b.name} → {a.name}.')
        stamp = trade.completed_at.isoformat() if trade.completed_at else None
        if stamp and trade.completed_at.tzinfo is None:
            stamp += 'Z'
        stories.append(dict(kind='transfer', title=f'{a.name} and {b.name} complete a trade',
            body=' '.join(parts) or f'A completed trade between {a.name} and {b.name} is recorded.',
            date=stamp, match_id=None, url=None))
    return stories
