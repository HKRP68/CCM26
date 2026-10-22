"""Add exact catalogue editions to existing auction sets without rebuilding the pool."""
import re
from sqlalchemy import func


def parse_players(raw):
    from services.auction_service import AuctionError
    rows = []
    for entry in (raw or '').split(','):
        if not entry.strip():
            continue
        fields = [part.strip() for part in entry.split('|')]
        if len(fields) != 2 or not all(fields):
            raise AuctionError('Usage: /aaddpl Player Name | Version, Player Name | Version')
        rows.append(tuple(fields))
    if not rows:
        raise AuctionError('Usage: /aaddpl Player Name | Version, Player Name | Version')
    return rows


def _player(session, name, version):
    from models import Player
    from services import player_query
    from services.auction_service import AuctionError
    query = player_query.master_player_query(session).filter(
        func.lower(func.trim(Player.name)) == name.lower())
    matches = [p for p in query.all()
               if (p.version or 'Base').strip().casefold() == version.casefold()]
    if len(matches) != 1:
        reason = 'No active master card matches' if not matches else 'Multiple active cards match'
        raise AuctionError(f'{reason} {name} | {version}. Check the exact player name and version.')
    return matches[0]


def _set_for(player, groups):
    from services import auction_service as A
    from services.auction_auto_sets import role_key
    rating, role = int(player.rating or 0), role_key(player.category)

    def choose(candidates):
        if len(candidates) == 1:
            return candidates[0]['name']
        raise A.AuctionError(f'More than one existing set fits {player.name}: '
                             + ', '.join(g['name'] for g in candidates) + '. Clarify the set ranges first.')

    bands = [(g, A.parse_rating_range(g['name'])) for g in groups]
    matching = [(g, band) for g, band in bands if band and band[0] <= rating <= band[1]]
    if matching:
        width = min(band[1] - band[0] for _, band in matching)
        return choose([g for g, band in matching if band[1] - band[0] == width])

    top = max(g['high'] for g in groups)
    marquee = [g for g in groups if g['name'].casefold() == 'marquee'
               and rating >= g['low'] and (rating <= g['high'] or g['high'] == top)]
    if marquee:
        return choose(marquee)

    role_labels = {'batsmen': 'bat', 'bowlers': 'bowl', 'allrounders': 'ar', 'wicketkeepers': 'wk'}
    role_sets = []
    known = set()
    for group in groups:
        label = re.sub(r'[^a-z0-9]', '', group['name'].lower())
        match = re.fullmatch(r'(batsmen|bowlers|allrounders|wicketkeepers)(\d*)', label)
        if match:
            known.add(group['name'])
            if role_labels[match[1]] == role:
                role_sets.append(dict(group, tier=int(match[2] or 1)))
    if role_sets:
        eligible = [g for g in role_sets if rating >= g['low']]
        floor = max(g['low'] for g in eligible) if eligible else min(g['low'] for g in role_sets)
        return min((g for g in role_sets if g['low'] == floor), key=lambda g: g['tier'])['name']

    custom = [g for g, band in bands if not band and g['name'] not in known
              and g['name'].casefold() != 'marquee']
    matching = [g for g in custom if g['low'] <= rating <= g['high'] and role in g['roles']]
    if matching:
        score = min((len(g['roles']), g['high'] - g['low']) for g in matching)
        return choose([g for g in matching if (len(g['roles']), g['high'] - g['low']) == score])
    if len(groups) == 1 and custom and custom[0]['name'] == A.DEFAULT_SET:
        return custom[0]['name']
    raise A.AuctionError(f'No existing set fits {player.name} ({rating}, {player.category}). '
                         'Configure a matching set with /apool or /aautosets first.')


def add_named_players(session, season, raw):
    """Validate the whole batch, add new cards, and keep each set together."""
    from services import auction_service as A
    from services.auction_auto_sets import role_key
    from services import auction_preseason as P
    requests = parse_players(raw)
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise A.AuctionError('Pause the auction before adding players to its pool.')
    P.claim_turn(session, season)
    players = [_player(session, *request) for request in requests]
    existing = {lot.player_id: lot for lot in A.lots(session, season.id)}
    entries = A.list_sets(session, season)
    groups = []
    for entry in entries:
        if entry['name'] == A.RELEASED_LAST_SEASON_SET:
            continue
        rows = [lot for lot in existing.values() if A.set_label(lot) == entry['name']]
        if rows:
            groups.append(dict(name=entry['name'], low=min(l.rating or 0 for l in rows),
                               high=max(l.rating or 0 for l in rows),
                               roles={role_key(l.category) for l in rows}))
    planned, results, seen = [], [], set(existing)
    for player in players:
        if player.id in seen:
            label = A.set_label(existing[player.id]) if player.id in existing else next(
                label for p, label in planned if p.id == player.id)
            results.append(dict(player=player, set=label, added=False))
            continue
        if not groups:
            raise A.AuctionError('Create auction sets with /apool or /aautosets before using /aaddpl.')
        label = _set_for(player, groups)
        planned.append((player, label))
        seen.add(player.id)
        results.append(dict(player=player, set=label, added=True))
    for player, label in planned:
        A.add_players_to_pool(session, season, [player], set_name=label)
    if planned:
        queue = A.queued_lots(session, season)
        ordered = [lot for entry in entries for lot in queue if A.set_label(lot) == entry['name']]
        A._requeue(session, season, ordered)
    numbers = {entry['name']: entry['set_no'] for entry in A.list_sets(session, season)}
    for result in results:
        result['set_no'] = numbers[result['set']]
    return results
