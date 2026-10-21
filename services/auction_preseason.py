"""Retention previews, live readiness, eligible picks and the persisted draft clock."""
import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import func



def claim_turn(session, season):
    """Compare-and-swap: manual picks and multiple workers share one turn lock.

    Held until the caller commits. All callers roll back on a refusal, including
    the clock cursor and money operations in the same transaction.
    """
    from services import auction_service as A
    from models import AuctionSeason
    session.flush()
    revision = int(season.draft_clock_revision or 0)
    changed = (session.query(AuctionSeason)
               .filter(AuctionSeason.id == season.id,
                       func.coalesce(AuctionSeason.draft_clock_revision, 0) == revision)
               .update({AuctionSeason.draft_clock_revision: revision + 1},
                       synchronize_session=False))
    if not changed:
        raise A.AuctionError("The draft turn changed. Refresh and try again.")
    session.expire_all()


def range_progress(session, season, franchise, *, draft=False):
    from services import auction_service as A
    if not draft and season.retention_mode != "ranges":
        return []
    return [dict(rule, count=A.range_count(session, franchise, rule, draft=draft))
            for rule in A.range_rules(season, draft=draft)]


def retention_preview(session, season, franchise):
    from services import auction_service as A
    kept = A.retained(session, franchise.id)
    rows = [dict(id=lot.id, name=lot.name, rating=lot.rating,
                 price_lakh=int(lot.sold_price_lakh or 0)) for lot in kept]
    ranges = range_progress(session, season, franchise)
    purse = int(franchise.purse_remaining_lakh or 0)
    digest = [season.id, franchise.id, rows, purse, ranges,
              season.min_retentions, season.max_retentions,
              season.retention_max_spend_lakh, season.retention_mode,
              str(season.retention_deadline_at), str(season.retention_locked_at),
              str(franchise.retention_finalized_at)]
    token = hashlib.sha256(json.dumps(digest, sort_keys=True).encode()).hexdigest()[:16]
    return dict(team_id=franchise.id, name=franchise.name, players=rows, ranges=ranges,
                spent_lakh=sum(row['price_lakh'] for row in rows),
                purse_lakh=purse, token=token, finalized=bool(franchise.retention_finalized_at))


def confirm_retention(session, season, franchise, token, *, by_tg_id=None):
    # Retention and releases also take this lock, so the preview is rechecked
    # after a concurrent roster change has committed.
    from services import auction_service as A
    claim_turn(session, season)
    current = retention_preview(session, season, franchise)
    if current['token'] != token:
        raise A.AuctionError("Your squad or rules changed. Open a fresh retention preview.")
    return A.finalize_retention(session, season, franchise, by_tg_id=by_tg_id)


def readiness(session, season, *, now=None):
    from services import auction_service as A
    now = now or datetime.utcnow()
    new_ids = {f.id for f in A.expansion_franchises(session, season)}
    closed = A.retention_locked(season) or (not A.retention_configured(season) and season.retention_mode != 'ranges')
    teams = []
    for f in A.franchises(session, season.id):
        new = f.id in new_ids
        teams.append(dict(id=f.id, name=f.name, new=new,
                          final=bool(f.retention_finalized_at or closed),
                          retained=int(f.retained_count or 0),
                          spent_lakh=A.retention_spent(session, f.id),
                          purse_lakh=int(f.purse_remaining_lakh or 0),
                          picks_left=A.picks_left(f),
                          ranges=range_progress(session, season, f, draft=new)))
    turn = A.pick_turn(session, season) if closed and season.status == A.STATUS_SETUP else None
    deadline = season.draft_pick_deadline_at
    return dict(teams=teams, pending=[f['name'] for f in teams if not f['new'] and not f['final']],
                retention_closed=closed, next_team=turn.name if turn else None,
                timer_seconds=int(season.draft_pick_seconds or 0),
                timer_paused=bool(season.draft_clock_paused),
                timeout_action=season.draft_timeout_action or 'pause',
                deadline=deadline.isoformat() + 'Z' if deadline else None,
                seconds_left=max(0, int((deadline-now).total_seconds())) if deadline else None)


def validate_candidate(session, season, franchise, lot):
    from services import auction_service as A
    if franchise.season_id != season.id or lot.season_id != season.id:
        raise A.AuctionError("That team or player belongs to another auction.")
    if season.status != A.STATUS_SETUP or A.picks_left(franchise) <= 0:
        raise A.AuctionError("This team has no draft picks available.")
    if lot.status != A.LOT_QUEUED:
        raise A.AuctionError("That player is no longer available in the pool.")
    price = A.draft_price(session, season, franchise, lot)
    A.validate_preauction_signing(session, season, franchise, lot, price)
    return price


def eligible_players(session, season, franchise, *, rating=None, role=None,
                     country=None, max_cost_lakh=None, query=None, page=1, limit=30):
    """Read-only eligibility: identical price, quotas and squad rules to a pick."""
    from services import auction_service as A
    from models import AuctionLot
    if franchise is None:
        return dict(players=[], total=0, page=1, pages=1)
    if franchise.season_id != season.id:
        raise A.AuctionError("That team belongs to another auction.")
    rows = session.query(AuctionLot).filter(AuctionLot.season_id == season.id,
                                           AuctionLot.status == A.LOT_QUEUED)
    if rating:
        span = A.parse_rating_range(rating)
        if span is None:
            raise A.AuctionError("A rating range looks like 91-96 or 90+.")
        rows = rows.filter(AuctionLot.rating.between(*span))
    if role:
        rows = rows.filter(func.lower(AuctionLot.category) == role.strip().lower())
    if country:
        rows = rows.filter(func.lower(AuctionLot.country) == country.strip().lower())
    if query:
        rows = rows.filter(AuctionLot.name.ilike('%' + query.replace('%', '').replace('_', '') + '%'))
    try:
        page = max(1, int(page))
        limit = min(100, max(1, int(limit)))
    except (TypeError, ValueError):
        raise A.AuctionError("Page and page size must be whole numbers.")
    matches = []
    for lot in rows.order_by(AuctionLot.rating.desc(), AuctionLot.name, AuctionLot.id):
        try:
            price = validate_candidate(session, season, franchise, lot)
        except A.AuctionError:
            continue
        if max_cost_lakh is None or price <= max_cost_lakh:
            matches.append(dict(id=lot.id, player_id=lot.player_id, name=lot.name,
                                rating=lot.rating, role=lot.category, country=lot.country,
                                price_lakh=price))
    pages = max(1, (len(matches) + limit - 1)//limit)
    page = min(page, pages)
    return dict(players=matches[(page-1)*limit:page*limit], total=len(matches), page=page, pages=pages)


def shortlist_ids(franchise):
    from services import auction_service as A
    return A._loads(franchise.draft_shortlist_json, [])


def edit_shortlist(session, season, franchise, action, player=None):
    from services import auction_service as A
    from models import AuctionLot
    claim_turn(session, season)
    if season.status != A.STATUS_SETUP or franchise.season_id != season.id:
        raise A.AuctionError("Edit a shortlist during this auction’s setup.")
    ids = shortlist_ids(franchise)
    if action == 'clear':
        ids = []
    elif player is not None and action in ('add', 'remove'):
        if action == 'add':
            lot = session.query(AuctionLot).filter_by(season_id=season.id, player_id=player.id).first()
            if lot is None:
                raise A.AuctionError("Add that player to the auction pool first.")
            validate_candidate(session, season, franchise, lot)
            if player.id not in ids:
                if len(ids) >= 50:
                    raise A.AuctionError("A shortlist holds at most 50 players.")
                ids.append(player.id)
        else:
            ids = [i for i in ids if i != player.id]
    else:
        raise A.AuctionError("Use add <player>, remove <player>, or clear.")
    franchise.draft_shortlist_json = json.dumps(ids)
    session.flush()
    return ids


def shortlist(session, season, franchise):
    from services import auction_service as A
    from models import AuctionLot
    rows = []
    for pid in shortlist_ids(franchise):
        lot = session.query(AuctionLot).filter_by(season_id=season.id, player_id=pid).first()
        if lot is None:
            continue
        try:
            cost = validate_candidate(session, season, franchise, lot)
            reason = None
        except A.AuctionError as exc:
            cost, reason = None, str(exc)
        rows.append(dict(player_id=pid, name=lot.name, eligible=reason is None,
                         price_lakh=cost, reason=reason))
    return rows


def clock_ready(session, season):
    from services import auction_service as A
    if season.status != A.STATUS_SETUP:
        return False
    try:
        A._pick_window(session, season)
    except A.AuctionError:
        return False
    return A.pick_turn(session, season) is not None


def sync_clock(session, season, *, now=None):
    from services import auction_service as A
    now = now or datetime.utcnow()
    if not season.draft_pick_seconds or not clock_ready(session, season):
        season.draft_pick_deadline_at = None
        season.draft_clock_turn = None
        session.flush()
        return
    turn = A.pick_turn(session, season)
    made = sum(int(f.draft_picks_used or 0) for f in A.pick_order(session, season))
    token = f'{made}:{turn.id}'
    if season.draft_clock_turn != token:
        season.draft_clock_turn = token
        season.draft_clock_paused = 0
        season.draft_pick_deadline_at = now + timedelta(seconds=season.draft_pick_seconds)
    elif not season.draft_clock_paused and season.draft_pick_deadline_at is None:
        season.draft_pick_deadline_at = now + timedelta(seconds=season.draft_pick_seconds)
    session.flush()


def set_clock(session, season, *, seconds=None, action=None, operation='save', now=None):
    from services import auction_service as A
    claim_turn(session, season)
    if season.status != A.STATUS_SETUP:
        raise A.AuctionError("Configure the expansion clock before the auction opens.")
    was_enabled = bool(season.draft_pick_seconds)
    if action is not None:
        if action not in ('pause', 'skip', 'shortlist'):
            raise A.AuctionError("Timeout action is pause, skip or shortlist.")
        season.draft_timeout_action = action
    if seconds is not None:
        try:
            seconds = int(seconds)
        except (ValueError, TypeError):
            raise A.AuctionError("Pick seconds must be a whole number.")
        if seconds != 0 and not 10 <= seconds <= 86400:
            raise A.AuctionError("Use 0 to disable, or 10–86400 seconds per pick.")
        season.draft_pick_seconds = seconds
    if operation not in ('save', 'pause', 'resume', 'off'):
        raise A.AuctionError("Clock operation is save, pause, resume or off.")
    if operation == 'off':
        season.draft_pick_seconds = 0
    if not was_enabled and season.draft_pick_seconds:
        from models import AuctionEvent
        season.draft_announced_event_id = (session.query(func.max(AuctionEvent.id))
            .filter(AuctionEvent.season_id == season.id).scalar() or 0)
    # A saved configuration gives the current team a full turn.
    season.draft_pick_deadline_at = None
    season.draft_clock_turn = None
    season.draft_clock_paused = 0
    sync_clock(session, season, now=now)
    if operation == 'pause':
        season.draft_clock_paused = 1
        season.draft_pick_deadline_at = None
    session.flush()


def resolve_timeout(session, season, *, now=None):
    from services import auction_service as A
    from models import Player
    now = now or datetime.utcnow()
    # Claim before reading/arming the clock so concurrent workers cannot both
    # rewrite the same deadline or spend the same turn.
    claim_turn(session, season)
    sync_clock(session, season, now=now)
    deadline = season.draft_pick_deadline_at
    if deadline is None or deadline > now or season.draft_clock_paused:
        return None
    team = A.pick_turn(session, season)
    action = season.draft_timeout_action or 'pause'
    if action == 'shortlist':
        for row in shortlist(session, season, team):
            if row['eligible']:
                player = session.query(Player).filter_by(id=row['player_id']).first()
                return A.draft_pick(session, season, team, player, now=now)
        reason = 'No eligible shortlisted player remains.'
    elif action == 'skip':
        try:
            # Validate the minimum before spending the turn, so a refused
            # skip leaves a pause rather than an endlessly retrying timer.
            needed = sum(max(0, r['min_count'] - A.range_count(session, team, r, draft=True))
                         for r in A.range_rules(season, draft=True))
            if A.picks_left(team) - 1 < needed:
                raise A.AuctionError('The remaining pick is needed for a range minimum.')
            result = A.skip_pick(session, season, team, now=now)
            sync_clock(session, season, now=now)
            return result
        except A.AuctionError as exc:
            reason = str(exc)
    else:
        reason = 'The team’s pick time expired.'
    season.draft_clock_paused = 1
    season.draft_pick_deadline_at = None
    A.log_event(session, season, 'draft_clock_paused',
                f'⏸ {_escape(team.name)} draft clock paused. {_escape(reason)}', franchise=team)
    session.flush()
    return 'paused'


def _escape(value):
    import html
    return html.escape(value)
