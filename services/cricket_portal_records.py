"""Public, fully ranked record books. Heavy history is cached off the live path."""
from services import cricket_portal_cache


def _record_books(session, series, season):
    from services import league_records as R
    def build():
        data = R.boards(session, series, season)
        from services.cricket_portal_statistics import _identities, _norm
        identities = _identities(session, [(None, {'name': player['name']}) for player in data['players']])
        result = {}
        for board in data['boards']:
            entries = []
            for entry in board['entries']:
                game = entry.get('source') or {}
                name = entry['name']
                kind = 'team' if board['key'] in ('total', 'chase') else 'partnership' if board['key'] in ('partnership', 'p50', 'p100') else 'player'
                entries.append(dict(rank=entry['rank'], name=name, team=entry.get('team'),
                    display=entry['display'], kind=kind, image=(identities.get(_norm(name)) or {}).get('image') if kind == 'player' else None, match_id=game.get('match_id'),
                    season=game.get('season'), venue=game.get('venue'), date=game.get('date'),
                    url=f"/cricket?match_id={game['match_id']}" if game.get('match_id') else None))
            cfg = data.get('config')
            qualification = None
            if cfg and board['key'] in ('sr', 'avg'):
                qualification = f"Minimum {cfg.min_bat_balls} recorded batting balls."
            elif cfg and board['key'] in ('econ', 'bavg', 'bsr'):
                qualification = f"Minimum {cfg.min_bowl_balls} recorded bowling balls."
            result[board['key']] = dict(key=board['key'], title=board['label'], unit=board['unit'],
                descending=board['descending'], entries=entries, qualification=qualification)
        return result
    return cricket_portal_cache.read(session, series, season, 'record_books', {}, build)


def leaderboard(session, series, season, filters):
    from services.league_records import CATEGORIES
    if filters.get('category') not in {row[0] for row in CATEGORIES}:
        raise ValueError('Choose a valid record category.')
    book = _record_books(session, series, season).get(filters.get('category', ''))
    if book is None:
        raise ValueError('Choose a valid record category.')
    query = str(filters.get('q') or '').strip().casefold()
    entries = [row for row in book['entries'] if not query or
               query in f"{row['name']} {row.get('team') or ''} {row.get('season') or ''} {row.get('venue') or ''}".casefold()]
    from services.cricket_portal_statistics import _integer_filter
    page = _integer_filter(filters.get('page'), 1, 100000)
    size = _integer_filter(filters.get('limit'), 25, 100)
    page = min(page, max(1, (len(entries) + size - 1)//size))
    return dict(key=book['key'], title=book['title'], unit=book['unit'],
        descending=book['descending'], qualification=book['qualification'],
        rows=entries[(page-1)*size:page*size], total=len(entries), all_entries=len(book['entries']),
        page=page, page_size=size, query=query,
        scope=season.name or f'Season {season.season_no}' if season else 'All linked seasons',
        note='Ranks retain exact ties. Match records list each qualifying performance; career records combine retained season totals.')
