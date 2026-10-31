"""Franchise head-to-head evidence across live and archived linked seasons."""
import json
import re


def _key(name):
    return ' '.join(str(name or '').split()).casefold()


def tournament_snapshot(session, tournament_id):
    """Snapshot completed results before deleting their tournament and teams.

    Team names, rather than captains or per-season row ids, identify franchises.
    A manifest with no matches is still known evidence; NULL means unavailable.
    """
    from models import Match, TournamentMatch, TournamentTeam
    teams = {t.id: t for t in session.query(TournamentTeam)
             .filter_by(tournament_id=tournament_id)}
    matches = []
    for row, margin in (session.query(TournamentMatch, Match.margin_type)
                         .outerjoin(Match, Match.id == TournamentMatch.match_id)
                         .filter(TournamentMatch.tournament_id == tournament_id,
                                 TournamentMatch.status == 'completed')):
        a, b = teams.get(row.team1_id), teams.get(row.team2_id)
        if not a or not b or _key(a.name) == _key(b.name):
            continue
        legacy_nr = row.result_type is None and _key(row.result_text) in ('no result', 'abandoned')
        if row.result_type == 'no_result' or legacy_nr:
            result, winner = 'no_result', None
        elif row.winner_team_id in (a.id, b.id):
            result, winner = 'win', teams[row.winner_team_id].name
        elif row.winner_team_id is None and row.result_type in (None, 'tie'):
            result, winner = 'tie', None
        else:
            continue  # a broken result cannot establish a win or a tie
        when = row.completed_at or row.created_at
        super_over = (_key(margin).replace('_', ' ') == 'super over'
                      or bool(re.search(r'super[\s_-]*over', row.result_text or '', re.I)))
        matches.append(dict(
            fixture_id=row.id, team1=a.name, team2=b.name, winner=winner,
            result=result, super_over=super_over, simulated=bool(row.is_simulated),
            score1=dict(runs=row.inn1_runs, wickets=row.inn1_wickets),
            score2=dict(runs=row.inn2_runs, wickets=row.inn2_wickets),
            when=when.isoformat() if when else '', stage=row.stage,
            match_no=row.match_no))
    return dict(teams=[dict(name=t.name, short_name=t.short_name) for t in teams.values()],
                matches=matches)


def _sources(session, tour):
    from models import StatsSeason, Tournament
    from services import season_archive as SA
    refs = dict.fromkeys([SA.tour_ref(tour.id)] + SA.effective_refs(session, tour))
    sources = []
    for ref in refs:
        kind, number = SA._parse_ref(ref)
        source = session.get(Tournament if kind == 't' else StatsSeason, number)
        if source is None:
            continue
        if kind == 't':
            snapshot = tournament_snapshot(session, source.id)
            when = source.completed_at or source.activated_at or source.created_at
        else:
            try:
                snapshot = json.loads(source.franchise_results_json or 'null')
            except (ValueError, TypeError):
                snapshot = None
            when = source.completed_at or source.archived_at
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('teams'), list)
                or not isinstance(snapshot.get('matches'), list)):
            snapshot = None
        sources.append(dict(ref=ref, label=source.name, snapshot=snapshot,
                            when=when.isoformat() if when else ''))
    return sources


def _resolve(sources, query):
    query = _key(query)
    candidates = {}
    for source in sources:
        for team in (source['snapshot'] or {}).get('teams', []):
            name = team.get('name')
            if not name:
                continue
            entry = candidates.setdefault(_key(name), dict(name=name, aliases=set()))
            if team.get('short_name'):
                entry['aliases'].add(_key(team['short_name']))
    exact = {k: v for k, v in candidates.items() if query == k}
    aliases = {k: v for k, v in candidates.items() if query in v['aliases']}
    matches = exact or aliases or {k: v for k, v in candidates.items() if query in k}
    if not query or not matches:
        raise ValueError(f'Franchise “{query}” was not found in the selected seasons. Use its full team name.')
    if len(matches) > 1:
        options = ', '.join(v['name'] for v in list(matches.values())[:6])
        raise ValueError(f'More than one franchise matches “{query}”. Use a full name: {options}')
    key, entry = next(iter(matches.items()))
    return key, entry['name']


def _tally(matches, a, b):
    counts = dict(played=len(matches), wins_a=0, wins_b=0, ties=0,
                  no_results=0, super_a=0, super_b=0, simulated=0)
    for m in matches:
        counts['simulated'] += int(bool(m.get('simulated')))
        if m['result'] == 'no_result':
            counts['no_results'] += 1
        elif m['result'] == 'tie':
            counts['ties'] += 1
        else:
            side = 'a' if _key(m['winner']) == a else 'b'
            counts[f'wins_{side}'] += 1
            counts[f'super_{side}'] += int(bool(m.get('super_over')))
    return counts


def compare(session, tour, team_a, team_b):
    sources = _sources(session, tour)
    a, name_a = _resolve(sources, team_a)
    b, name_b = _resolve(sources, team_b)
    if a == b:
        raise ValueError('Choose two different franchises.')
    meetings, seasons = [], []
    highest = {'a': None, 'b': None}
    for source in sources:
        matches = []
        for m in (source['snapshot'] or {}).get('matches', []):
            if {_key(m.get('team1')), _key(m.get('team2'))} != {a, b}:
                continue
            if m.get('result') not in ('win', 'tie', 'no_result'):
                continue
            if m['result'] == 'win' and _key(m.get('winner')) not in (a, b):
                continue
            entry = dict(m, season=source['label'], season_ref=source['ref'],
                         season_when=source['when'])
            # Normalize every score into the requested A/B order, even when B
            # batted first. NULL is unknown; a genuine zero remains a zero.
            first_is_a = _key(m['team1']) == a
            entry['score_a'] = m.get('score1' if first_is_a else 'score2') or {}
            entry['score_b'] = m.get('score2' if first_is_a else 'score1') or {}
            matches.append(entry)
            if m['result'] != 'no_result':
                for side in ('a', 'b'):
                    score = entry[f'score_{side}']
                    runs = score.get('runs')
                    if isinstance(runs, int) and (highest[side] is None or runs > highest[side]['runs']):
                        highest[side] = dict(score, season=source['label'])
        if matches:
            meetings.extend(matches)
            seasons.append(dict(label=source['label'], ref=source['ref'],
                                when=source['when'], **_tally(matches, a, b)))
    meetings.sort(key=lambda m: (m.get('when') or m['season_when'],
                                 m['season_ref'], m.get('fixture_id') or 0), reverse=True)
    seasons.sort(key=lambda s: (s['when'], s['ref']), reverse=True)
    return dict(team_a=name_a, team_b=name_b, highest=highest,
                seasons=seasons, recent=meetings[:5],
                selected_seasons=len(sources),
                available_seasons=sum(s['snapshot'] is not None for s in sources),
                **_tally(meetings, a, b))
