"""Read-only season/career insights built from verified record evidence."""
from collections import defaultdict

ADVANCED_CATEGORIES = (
    ('partnership','Highest partnership','runs',True),
    ('p50','Fastest fifty partnership','legal balls',False),
    ('p100','Fastest hundred partnership','legal balls',False),
    ('catches','Most recorded fielding catches','catches',True),
    ('keeping','Most recorded keeper dismissals','dismissals',True),
    ('runouts','Most recorded run-out involvements','involvements',True),
) + tuple((f'{phase}_{kind}',f'{label}: most {unit}',unit,True)
          for phase,label in [('powerplay','Powerplay'),('middle','Middle overs'),('death','Death overs')]
          for kind,unit in [('runs','runs'),('wkts','wickets')])



def safe_image_url(value):
    value=(value or '').strip()
    return value if value.startswith(('https://','http://')) or (value.startswith('/') and not value.startswith('//')) else None

def advanced(games, team=None):
    partnerships, phases, fielding = [], {}, {}
    coverage = 0
    for game in games:
        analytics = game.get('analytics') or {}
        coverage += sum(bool(x.get('complete')) for x in (analytics.get('coverage') or {}).values())
        for stand in analytics.get('partnerships') or []:
            if stand.get('complete') and (not team or stand.get('team') == team):
                partnerships.append(dict(stand,source=game))
        for row in analytics.get('phases') or []:
            if team and row.get('team') != team:
                continue
            key = (row['side'],row['name'],row['phase'])
            total = phases.setdefault(key,dict(row,runs=0,balls=0,wickets=0))
            for metric in ('runs','balls','wickets'):
                total[metric] += row[metric]
        for row in analytics.get('fielding') or []:
            if team and row.get('team') != team:
                continue
            total = fielding.setdefault(row['name'],dict(row,**{k:0 for k in ('catches','keeper_catches','stumpings','direct_runouts','assisted_runouts','unclassified_runouts')}))
            for metric in ('catches','keeper_catches','stumpings','direct_runouts','assisted_runouts','unclassified_runouts'):
                total[metric] += row[metric]
    by_wicket={}
    for stand in sorted(partnerships,key=lambda s:-s['runs']):
        by_wicket.setdefault(stand['wicket'],stand)
    return dict(by_wicket=[by_wicket[k] for k in sorted(by_wicket)],partnerships=sorted(partnerships,key=lambda s:-s['runs']),
                phases=sorted(phases.values(),key=lambda r:(r['phase'],r['side'],-r['runs'])),
                fielding=sorted(fielding.values(),key=lambda r:-(r['catches']+r['keeper_catches']+r['stumpings'])),coverage=coverage)


def append_boards(add, insights):
    for stand in insights['partnerships']:
        who = ' & '.join(stand['names'])
        add('partnership',who,stand['runs'],stand['source'],stand['team'])
        for mark in (50,100):
            balls = stand['milestones'].get(str(mark))
            if balls is not None:
                add(f'p{mark}',who,balls,stand['source'],stand['team'])
    for row in insights['fielding']:
        for category,value in [('catches',row['catches']),('keeping',row['keeper_catches']+row['stumpings']),
                               ('runouts',row['direct_runouts']+row['assisted_runouts']+row['unclassified_runouts'])]:
            if value:
                add(category,row['name'],value,side=row['team'])
    for row in insights['phases']:
        metric = 'runs' if row['side']=='bat' else 'wkts'
        value = row['runs'] if metric=='runs' else row['wickets']
        if value:
            add(f"{row['phase']}_{metric}",row['name'],value,side=row['team'])


def watch_list(session, series, season, *, state=None, limit=12):
    from services import league_records as R
    # No live aggregates are stored: add this innings/match to the historical
    # baseline exactly once, the same way record alerts do.
    live = R._live_game(state,season) if state and state.get('match_id') else None
    data = R.boards(session,series,season,live=live)
    career = R.boards(session,series,live=live)
    names = None
    if state:
        names = {R._name(p.get('name')) for key in ('bat_xi','bowl_xi','inn1_bat_xi','inn1_bowl_xi') for p in state.get(key) or []}
    result=[]
    for scope,table in [('season',data),('career',career)]:
        rows=table['players']
        for p in rows:
            if names is not None and R._name(p['name']) not in names:
                continue
            for field,step,window,unit in [('runs',500,50,'runs'),('wkts',25,2,'wickets')]:
                current=p[field];target=(current//step+1)*step
                if current and 0 < target-current <= window:
                    result.append(dict(player=p['name'],text=f"{p['name']}: {target-current} {unit} to {target} {scope} {unit}",
                                       key=f'{scope}:{field}:{target}:{R._name(p["name"])}'))
        for category,field,window,unit in [('runs','runs',50,'runs'),('wkts','wkts',2,'wickets')]:
            board=next(b for b in table['boards'] if b['key']==category)
            if not board['holders']:
                continue
            best=board['holders'][0]['value']
            for p in rows:
                if names is not None and R._name(p['name']) not in names:
                    continue
                away=best-p[field]+1
                if 0 < p[field] < best and 0 < away <= window:
                    result.append(dict(player=p['name'],text=f"{p['name']}: {away} {unit} to take the {scope} {board['label'].lower()} lead",
                                       key=f'{scope}:lead:{category}:{R._name(p["name"])}'))
    if state:
        for line in (live or {}).get('lines') or []:
            if not line.get('batted') or line.get('bat_out'):
                continue
            for threshold in (50,100,150):
                board=next(b for b in data['boards'] if b['key']==f'f{threshold}')
                if not board['holders']:
                    continue
                best=board['holders'][0]['value'];runs=line['bat_runs'];balls=line['bat_balls']
                if 0 < threshold-runs <= 10 and balls < best and threshold-runs <= 6*(best-balls):
                    result.append(dict(player=line['name'],text=f"{line['name']}: {threshold-runs} runs needed with {best-balls} faced balls to equal the fastest {threshold}",
                                       key=f'fast:{threshold}:{R._name(line["name"])}'))
    return result[:limit]


def player_profile(session,series,name):
    from services import league_records as R, league_season_service as LS
    wanted=R._name(name)
    career=R.boards(session,series)
    player=next((p for p in career['players'] if R._name(p['name'])==wanted),None)
    if player is None:
        return None
    breakdown=[];performances=[];awards=[];season_ids=[]
    for season in LS.seasons(session,series):
        season_ids.append(season.id)
        data=R.boards(session,series,season)
        row=next((p for p in data['players'] if R._name(p['name'])==wanted),None)
        if row:
            breakdown.append(dict(season_id=season.id,title=LS.season_title(season),stats=row))
        for game in data['matches']:
            for line in game['lines']:
                if R._name(line.get('name'))==wanted:
                    performances.append(dict(line=line,source=game))
        for key,honour in LS.honours(season).items():
            if key=='best_xi' and wanted in {R._name(n) for n in (honour or {}).get('players',[])}:
                awards.append(dict(season=LS.season_title(season),award='Official Best XI',value=honour.get('criteria')))
            if R._name((honour or {}).get('player'))==wanted:
                awards.append(dict(season=LS.season_title(season),award=key.replace('_',' ').title(),value=honour.get('value')))
    records=[dict(label=b['label'],display=e['display'],unit=b['unit'],key=b['key'],source=e['source'])
             for b in career['boards'] for e in b['holders'] if R._name(e['name'])==wanted]
    personal=[]
    for b in career['boards']:
        entries=[e for e in b['entries'] if R._name(e['name'])==wanted]
        if entries:
            personal.append(dict(label=b['label'],display=entries[0]['display'],unit=b['unit']))
    history=[]
    from models import LeagueRecordPublication
    import json
    for post in session.query(LeagueRecordPublication).filter(LeagueRecordPublication.season_id.in_(season_ids),LeagueRecordPublication.kind.like('record:%')).order_by(LeagueRecordPublication.id.desc()).limit(200):
        meta=json.loads(post.metadata_json or '{}')
        actors=(meta.get('fields') or {}).get('player','').split(' & ')
        if wanted in {R._name(n) for n in actors}:
            history.append(dict(match_id=post.match_id,text=post.text,revision=post.revision,status=post.status))
    image=None
    from models import Player
    catalogue=session.query(Player).filter(Player.name.ilike(player['name'])).order_by(Player.id.desc()).first()
    if catalogue:
        image=safe_image_url(catalogue.image_url)
    performances.sort(key=lambda x:R.game_order(x['source']),reverse=True)
    return dict(name=player['name'],stats=player,seasons=breakdown,recent=performances[:5],
                performances=performances,awards=awards,records=records,record_history=history,personal=personal,image=image,
                insights=advanced(list({(p['source']['season_id'],p['source']['fixture_id']):p['source'] for p in performances}.values())))


def honours_page(session,series,season):
    from services import league_records as R, league_season_service as LS
    data=R.boards(session,series,season)
    # Best XI is an explicitly labelled statistical selection with role quotas;
    # official admin awards remain separate from this derived selection.
    from models import Player
    roles={R._name(p.name):p.category for p in session.query(Player).order_by(Player.id)}
    pool=[]
    for p in data['players']:
        pool.append(dict(p,role=roles.get(R._name(p['name']),'Unknown'),selection_points=p['runs']+20*p['wkts']))
    chosen=[]
    for role,slots in [('Wicket Keeper',1),('Batsman',4),('All Rounder',2),('Bowler',4)]:
        candidates=sorted([p for p in pool if p['role'].lower().replace('-',' ').replace(' ','')==role.lower().replace(' ','')],key=lambda p:(-p['selection_points'],p['name']))
        chosen.extend(candidates[:slots])
    return dict(honours=LS.honours(season),standings=R.standings_view(session,season),data=data,
                best_xi=chosen,best_xi_complete=len(chosen)==11,
                best_xi_rule='Statistical XI: runs + 20 × wickets; 1 keeper, 4 batters, 2 all-rounders, 4 bowlers. Ties by name. Unfilled roles stay vacant.',
                highlights=[b for b in data['boards'] if b['key'] in ('hs','fig','partnership','total','f50','f100','f150')])
