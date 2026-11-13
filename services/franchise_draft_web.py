"""Browser presentation of the existing draft; signing stays in its secure bot flow."""
import os
import re

from services import auction_service as A, franchise_draft as D, franchise_draft_rich as R


def payload(db, season, result, request):
    from services.auction_webapp import integer, telegram_link
    username = (os.getenv('FRANCHISE_AUCTION_BOT_USERNAME') or os.getenv('FRANCHISE_BOT_USERNAME') or '').strip().lstrip('@')
    bot_url = f'https://t.me/{username}?start=draft_{season.id if season else 0}' if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else None
    result.update(draft_bot_url=bot_url, draft_group_url=telegram_link(season)
                  if season and str(season.chat_id or '').startswith('-100') else None)
    if season is None:
        return result
    if D.get_draft(db, season) is None:
        raise A.AuctionError('Choose a Franchise Draft season.')
    part = request.get('part', 'board')
    page = integer(request.get('page', 1), 'Page', 1, 100_000)
    config = D.get_draft(db, season)
    teams = A.franchises(db, season.id)
    pages = 1
    if part == 'board':
        _, text = R.board(db, season)
    elif part == 'pool':
        pages = max(1, (len(D._available(db, season)) + 11) // 12)
        page = min(page, pages)
        _, text = R.pool(db, season, page)
    elif part == 'order':
        pages = (config.rounds + 2) // 3
        page = min(page, pages)
        _, text = R.order(db, season, page=1 + (page - 1) * 3)
    elif part == 'history':
        from models import AuctionEvent
        count = db.query(AuctionEvent).filter_by(season_id=season.id).count()
        pages = max(1, (count + 11) // 12)
        page = min(page, pages)
        _, text = R.history(db, season, page)
    elif part == 'squads':
        selected = request.get('team_id')
        team = next((t for t in teams if str(t.id) == str(selected)), None) if selected else next(iter(teams), None)
        if selected and team is None:
            raise A.AuctionError('Choose a franchise from this draft season.')
        _, text = R.squad(db, season, team) if team else R.Card('👥 Franchise squads').text('No franchises added yet.').pair()
        result['team_id'] = team.id if team else None
    elif part in ('purses', 'rules'):
        _, text = (R.purses if part == 'purses' else R.rules)(db, season)
    else:
        raise A.AuctionError('Choose a draft board, pool, order, history, squads, purses or rules.')
    result.update(draft_html=text, draft_part=part, page=page, pages=pages,
                  teams=[dict(id=t.id, name=t.name) for t in teams])
    return result
