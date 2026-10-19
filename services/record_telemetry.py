"""Delivery evidence for future records, captured alongside official scoring.

A versioned JSON stream survives normal state saves and is embedded once in the
first real player scorecard line. Legacy/partially observed innings stay labelled.
"""
from collections import defaultdict


def phase(state):
    total = max(1, int(state.get('overs') or 20))
    unit = max(1, int(state.get('current_over') or 1))
    hundred = state.get('ball_format') == 'The100'
    pp = (5 if hundred else 6) if total >= 20 else max(1, round(total * .3))
    death = (4 if hundred else 5) if total >= 20 else max(1, round(total * .2))
    return 'powerplay' if unit <= pp else 'death' if unit > total-death else 'middle'


def snapshot(state, striker, bowler):
    rid, bid = str(striker['roster_id']), str(bowler['roster_id'])
    bat = (state.get('bat_stats') or {}).get(rid, {})
    bowl = (state.get('bowl_stats') or {}).get(bid, {})
    order = state.get('batting_order') or state.get('bat_xi') or []
    pair = [striker]
    idx = state.get('non_striker_idx')
    if idx is not None and 0 <= idx < len(order):
        pair.append(order[idx])
    return dict(innings=int(state.get('innings') or 1),phase=phase(state),
                team=state.get('bat_team_name'),opponent=state.get('bowl_team_name'),
                runs=int(state.get('total_runs') or 0),wickets=int(state.get('total_wickets') or 0),
                legal=sum(int(s.get('balls') or 0) for s in (state.get('bowl_stats') or {}).values()),
                bat_runs=int(bat.get('runs') or 0),bat_balls=int(bat.get('balls') or 0),
                bowl_runs=int(bowl.get('runs') or 0),bowl_wickets=int(bowl.get('wickets') or 0),
                batter=striker.get('name'),batter_id=striker.get('player_id'),rid=rid,
                bowler=bowler.get('name'),bowler_id=bowler.get('player_id'),bid=bid,
                pair=[dict(name=p.get('name'),rid=p.get('roster_id')) for p in pair])


def capture(state, before, outcome):
    """One call per applied delivery; replayed states retain the same stream."""
    bat = (state.get('bat_stats') or {}).get(before['rid'], {})
    bowl = (state.get('bowl_stats') or {}).get(before['bid'], {})
    kind = str(outcome.get('extra_type') or outcome.get('type') or '').lower().replace(' ', '')
    illegal = kind in ('wide','noball')
    wicket = int(state.get('total_wickets') or 0) > before['wickets']
    how = str(bat.get('how_out') or outcome.get('wicket_type') or outcome.get('how') or '').lower().replace('runout','run out')
    fielder = bat.get('fielder') or outcome.get('fielder')
    if wicket and how in ('caught and bowled','caught & bowled','c&b','c & b'):
        fielder = before['bowler']
        bat['fielder'] = fielder
    if wicket and not fielder and ('stump' in how or how == 'caught behind'):
        from services.impact_player import active_players
        selected_keeper = next((p for p in active_players(state.get('bowl_xi') or []) if str(p.get('category') or '').lower().replace(' ','') == 'wicketkeeper'),None)
        if selected_keeper:
            fielder = selected_keeper.get('name')
            bat['fielder'] = fielder
    keeper = False
    for player in state.get('bowl_xi') or []:
        if player.get('name') == fielder:
            keeper = str(player.get('category') or player.get('role') or '').lower().replace(' ', '') in ('wicketkeeper','wk','keeper')
            break
    stream = state.setdefault('record_deliveries', [])
    starts = state.setdefault('record_innings_starts', {})
    starts.setdefault(str(before['innings']),dict(complete=before['runs']==0 and before['wickets']==0 and before['legal']==0,
                                                  legal=before['legal'],runs=before['runs']))
    row = {k:before[k] for k in ('innings','phase','team','opponent','batter','batter_id','rid','bowler','bowler_id','bid','pair')}
    row.update(seq=len(stream)+1,legal=not illegal,
               team_runs=int(state.get('total_runs') or 0)-before['runs'],
               bat_runs=int(bat.get('runs') or 0)-before['bat_runs'],
               faced=int(bat.get('balls') or 0)-before['bat_balls'],
               conceded=int(bowl.get('runs') or 0)-before['bowl_runs'],
               wicket=wicket,bowler_wicket=int(bowl.get('wickets') or 0)>before['bowl_wickets'],
               wicket_number=before['wickets']+1,how=how if wicket else None,
               fielder=fielder if wicket else None,keeper=keeper,
               direct_hit=outcome.get('direct_hit') if wicket and 'run out' in how else None,
               assists=outcome.get('assists') or [] if wicket and 'run out' in how else [])
    stream.append(row)


def detail(state):
    if 'record_deliveries' not in state:
        return None
    return dict(version=1,deliveries=state['record_deliveries'],
                innings_starts=state.get('record_innings_starts') or {},
                ball_format=state.get('ball_format') or 'T20',overs=state.get('overs') or 20)


def analyse(detail, lines=None):
    """Partnerships, phase figures and fielding credits from explicit evidence."""
    if not isinstance(detail, dict) or detail.get('version') != 1:
        return dict(partnerships=[],phases=[],fielding=[],deliveries=[],coverage={})
    events = detail.get('deliveries') or []
    # An aggregate-only scorecard correction cannot silently leave old phase,
    # partnership or fielding evidence official. Require event totals to agree
    # whenever the innings were observed from the start.
    if lines is not None and events and all(x.get('complete') for x in (detail.get('innings_starts') or {}).values()):
        totals = defaultdict(lambda:dict(bat_runs=0,bat_balls=0,bowl_runs=0,bowl_balls=0,bowl_wickets=0,outs=0))
        for event in events:
            b=totals[event.get('batter')]
            b['bat_runs']+=event['bat_runs'];b['bat_balls']+=event['faced'];b['outs']+=int(event['wicket'])
            b=totals[event.get('bowler')]
            b['bowl_runs']+=event['conceded'];b['bowl_balls']+=int(event['legal']);b['bowl_wickets']+=int(event['bowler_wicket'])
        for line in lines:
            counts=totals.get(line.get('name'))
            if counts is None:
                continue
            mismatch=any(counts[k]!=int(line.get(k) or 0) for k in ('bat_runs','bat_balls','bowl_runs','bowl_balls','bowl_wickets'))
            mismatch=mismatch or bool(counts['outs'])!=bool(line.get('bat_out'))
            if mismatch:
                return dict(partnerships=[],phases=[],fielding=[],deliveries=events,coverage={},invalid_reason='Scorecard totals differ from saved deliveries; update event evidence before using advanced records.')
    stands, phases, fielders = {}, {}, {}
    for e in events:
        if not isinstance(e,dict):
            continue
        pair = e.get('pair') or []
        names = sorted(p.get('name') or 'Unknown' for p in pair)
        key = (e['innings'], e['wicket_number'], tuple(names))
        stand = stands.setdefault(key,dict(innings=e['innings'],wicket=e['wicket_number'],
            team=e.get('team'),names=names,runs=0,balls=0,contributions={},milestones={},closed=False,
            complete=(detail.get('innings_starts') or {}).get(str(e['innings']),{}).get('complete',False)))
        old = stand['runs']
        stand['runs'] += e['team_runs'];stand['balls'] += int(e['legal'])
        who = e.get('batter') or 'Unknown'
        stand['contributions'][who] = stand['contributions'].get(who,0)+e['bat_runs']
        for n in (50,100,150,200):
            if old < n <= stand['runs']:
                stand['milestones'].setdefault(str(n),stand['balls'])
        stand['closed'] = stand['closed'] or e['wicket']
        for side, name, team in [('bat',e.get('batter'),e.get('team')),('bowl',e.get('bowler'),e.get('opponent'))]:
            p = phases.setdefault((side,name,e['phase']),dict(side=side,name=name,team=team,phase=e['phase'],runs=0,balls=0,wickets=0))
            p['runs'] += e['bat_runs'] if side=='bat' else e['conceded']
            p['balls'] += e['faced'] if side=='bat' else int(e['legal'])
            p['wickets'] += int(e['bowler_wicket']) if side=='bowl' else 0
        if e['wicket'] and e.get('fielder'):
            f = fielders.setdefault(e['fielder'],dict(name=e['fielder'],team=e.get('opponent'),catches=0,keeper_catches=0,stumpings=0,direct_runouts=0,assisted_runouts=0,unclassified_runouts=0))
            how = e.get('how') or ''
            if 'caught' in how or how in ('c&b','c & b'):
                f['keeper_catches' if e.get('keeper') else 'catches'] += 1
            elif 'stump' in how:
                f['stumpings'] += 1
            elif 'run out' in how:
                field = 'direct_runouts' if e.get('direct_hit') is True else 'assisted_runouts' if e.get('direct_hit') is False else 'unclassified_runouts'
                f[field] += 1
                for name in set(e.get('assists') or [])-{e['fielder']}:
                    helper=fielders.setdefault(name,dict(name=name,team=e.get('opponent'),catches=0,keeper_catches=0,stumpings=0,direct_runouts=0,assisted_runouts=0,unclassified_runouts=0))
                    helper['assisted_runouts'] += 1
    return dict(partnerships=list(stands.values()),phases=list(phases.values()),fielding=list(fielders.values()),
                deliveries=events,coverage=detail.get('innings_starts') or {})
