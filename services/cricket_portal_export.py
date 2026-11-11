"""Read-only, version-aware spreadsheet exports of public competition stats.

Scorecards are authoritative where available; aggregate-only archives retain
their unknown fields. Nothing here rebuilds stats or changes scoring records.
The XLSX writer uses the same standard-library approach as player downloads.
"""
import csv
import io
import math
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from services import cricket_portal_statistics as S

# key, public heading, explanation. This is also the workbook's data dictionary.
COLUMNS = (
    ('name', 'Player Name', 'Recorded player name. Each identified card version has its own row.'),
    ('version', 'Version', 'Current card version; blank when the recorded identity cannot be resolved.'),
    ('rating', 'Rating', 'Current squad override or catalogue overall rating, not a historical match rating.'),
    ('bat_rating', 'Batting Rating', 'Current squad override or catalogue batting rating.'),
    ('bowl_rating', 'Bowling Rating', 'Current squad override or catalogue bowling rating.'),
    ('role', 'Role', 'Current squad or catalogue playing role.'),
    ('country', 'Country', 'Current squad or catalogue country.'),
    ('teams', 'Team(s)', 'All recorded teams represented within this row, separated by semicolons.'),
    ('season', 'Season(s)', 'Contributing seasons, in competition order.'),
    ('seasons', 'Seasons', 'Number of contributing seasons for this player/version.'),
    ('matches', 'Matches', 'Distinct recorded fixtures, plus appearances stored in aggregate-only archives.'),
    ('innings', 'Batting Innings', 'Recorded batting innings; DNB does not count.'),
    ('not_outs', 'Not Outs', 'Batting innings minus dismissals, when both counts are known.'),
    ('runs', 'Runs', 'Total runs credited to the batter.'),
    ('balls', 'Balls Faced', 'Total recorded balls faced.'),
    ('outs', 'Dismissals', 'Number of times dismissed.'),
    ('highest_score', 'Highest Score', 'Highest individual innings score.'),
    ('highest_score_not_out', 'Highest Score Not Out', 'TRUE for an unbeaten highest score. An unbeaten innings wins a tied score.'),
    ('average', 'Batting Average', 'Total runs / dismissals. Blank when never dismissed or evidence is incomplete.'),
    ('sr', 'Strike Rate', '100 x total runs / total balls faced; calculated from totals, not averaged innings rates.'),
    ('fours', 'Fours', 'Recorded batting boundaries worth four.'),
    ('sixes', 'Sixes', 'Recorded batting boundaries worth six.'),
    ('fifties', 'Fifties', 'Innings of 50 to 99 runs; hundreds are counted separately.'),
    ('hundreds', 'Hundreds', 'Innings of at least 100 runs.'),
    ('ducks', 'Ducks', 'Dismissals for zero runs; unbeaten zeroes are excluded.'),
    ('bat_dots', 'Batting Dot Balls', 'Balls faced with no batter runs, from scorecards or verified deliveries.'),
    ('dot_percentage', 'Batting Dot %', '100 x batting dot balls / balls faced.'),
    ('boundary_percentage', 'Boundary Runs %', '100 x (4 x fours + 6 x sixes) / total batting runs.'),
    ('fastest_50', 'Fastest Fifty (Balls)', 'Fewest recorded balls to reach 50; blank if any qualifying innings lacks milestone evidence.'),
    ('fastest_100', 'Fastest Hundred (Balls)', 'Fewest recorded balls to reach 100; blank if any qualifying innings lacks milestone evidence.'),
    ('fastest_150', 'Fastest 150 (Balls)', 'Fewest recorded balls to reach 150; blank if any qualifying innings lacks milestone evidence.'),
    ('bowling_innings', 'Bowling Innings', 'Recorded innings with a bowling spell.'),
    ('bowled', 'Balls Bowled', 'Total legal bowling deliveries. Use this column for cross-format analysis.'),
    ('overs', 'Overs (6-ball)', 'Six-ball equivalent as overs.balls TEXT, not a decimal number. 1.4 means ten balls.'),
    ('conceded', 'Runs Conceded', 'Total runs charged to the bowler.'),
    ('wickets', 'Wickets', 'Wickets credited to the bowler; run outs are excluded.'),
    ('best_bowling', 'Best Bowling', 'Most wickets in one spell; fewer conceded runs break a tie. Includes wicketless spells.'),
    ('economy', 'Economy (per 6 balls)', '6 x total runs conceded / legal balls bowled, including The100 matches.'),
    ('bowling_average', 'Bowling Average', 'Total runs conceded / wickets; blank when no wickets or evidence is incomplete.'),
    ('bowling_strike_rate', 'Bowling Strike Rate', 'Legal balls bowled / wickets; blank when no wickets or evidence is incomplete.'),
    ('bowl_dots', 'Bowling Dot Balls', 'Recorded legal deliveries conceding no runs.'),
    ('bowling_dot_percentage', 'Bowling Dot %', '100 x bowling dot balls / legal balls bowled.'),
    ('three_fers', '3-Wicket Hauls', 'Spells taking three or four wickets; five-wicket hauls are counted separately.'),
    ('five_fers', '5-Wicket Hauls', 'Spells taking at least five wickets.'),
    ('hattricks', 'Hat Tricks', 'Recorded bowling hat tricks. Missing historical evidence stays blank.'),
    ('catches', 'Catches', 'All verified catches, including wicketkeeper catches and caught-and-bowled.'),
    ('keeper_catches', 'Keeper Catches', 'Wicketkeeper catches; already included in Catches.'),
    ('stumpings', 'Stumpings', 'Verified wicketkeeper stumpings.'),
    ('direct_runouts', 'Direct Run Outs', 'Run outs explicitly recorded as direct hits.'),
    ('assisted_runouts', 'Assisted Run Outs', 'Recorded assisted run-out involvements, including named helpers.'),
    ('unclassified_runouts', 'Unclassified Run Outs', 'Verified run outs without a recorded direct/assisted classification.'),
    ('potm', 'Player of the Match', 'Recorded match awards. Awards with ambiguous same-name identities stay unknown.'),
    ('scorecard_matches', 'Scorecard Matches', 'Distinct fixtures supplying detailed player scorecards.'),
    ('aggregate_seasons', 'Aggregate-only Seasons', 'Contributing seasons available only as stored player totals.'),
    ('source', 'Data Source', 'Scorecards, stored aggregate totals, or a combination of both.'),
    ('notes', 'Data Notes', 'Identity or coverage limitations affecting this row. Blanks mean unknown or undefined, not zero.'),
)
DECIMALS = {'average', 'sr', 'dot_percentage', 'boundary_percentage', 'economy',
            'bowling_average', 'bowling_strike_rate', 'bowling_dot_percentage'}
FIELDING = ('catches', 'keeper_catches', 'stumpings', 'direct_runouts',
            'assisted_runouts', 'unclassified_runouts')


def _prepare_games(games):
    """Enrich missing dot figures only when the saved deliveries are complete."""
    for game in games:
        game['lines'] = [dict(line) for line in game.get('lines', [])]
        counts = Counter((S._norm(l.get('name')), S._norm(l.get('team_name'))) for l in game['lines'])
        game['_name_counts'] = counts
        analytics = game.get('analytics') or {}
        coverage = analytics.get('coverage') or {}
        complete = all((coverage.get(str(i)) or {}).get('complete') for i in (1, 2))
        missing_fielder = any(event.get('wicket') and not event.get('fielder')
            and any(kind in str(event.get('how') or '') for kind in ('caught', 'c&b', 'c & b', 'stump', 'run out'))
            for event in analytics.get('deliveries', []))
        dots = defaultdict(lambda: [0, 0])
        if complete:
            for event in analytics.get('deliveries', []):
                if event.get('faced') and not event.get('bat_runs'):
                    dots[S._norm(event.get('batter')), S._norm(event.get('team'))][0] += 1
                if event.get('legal') and not event.get('conceded'):
                    dots[S._norm(event.get('bowler')), S._norm(event.get('opponent'))][1] += 1
        for line in game['lines']:
            key = S._norm(line.get('name')), S._norm(line.get('team_name'))
            line['_fielding'] = None
            if not complete or counts[key] != 1:
                continue
            for kind, index in (('bat', 0), ('bowl', 1)):
                if line.get(f'{kind}_dots') is None and S._played(line, kind):
                    line[f'{kind}_dots'] = dots[key][index]
            fielding = dict.fromkeys(FIELDING, 0)
            for row in analytics.get('fielding', []):
                if (S._norm(row.get('name')), S._norm(row.get('team'))) == key:
                    for field in FIELDING:
                        fielding[field] += S._int(row.get(field)) or 0
                    fielding['catches'] += S._int(row.get('keeper_catches')) or 0
            line['_fielding'] = None if missing_fielder else fielding


def _resolver(session, entries, selected):
    """Batch catalogue reads; old roster IDs are never resolved after archival."""
    from models import ChallengePlayer, ChallengeTeam, Player
    from sqlalchemy import func, or_
    from services.cricket_portal_competitions import _loads
    seasons = {item.id: item for item in selected}
    roster_ids = {S._int(line.get('roster_id')) for _, line, _ in entries} - {None, 0}
    rosters = {row.id: (row, league, team) for row, league, team in session.query(
        ChallengePlayer, ChallengeTeam.league_id, ChallengeTeam.name).join(
        ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id).filter(
        ChallengePlayer.id.in_(roster_ids))} if roster_ids else {}
    ids = {S._int(line.get('player_id')) for _, line, _ in entries} - {None, 0}
    ids.update(row.source_player_id for row, _, _ in rosters.values() if row.source_player_id)
    names = {str(line.get('name') or '').strip().lower() for _, line, _ in entries}
    catalogue = session.query(Player).filter(or_(Player.id.in_(ids), func.lower(func.trim(Player.name)).in_(names))).all() if entries else []
    by_id = {row.id: row for row in catalogue}
    by_name = defaultdict(list)
    for row in catalogue:
        by_name[S._norm(row.name)].append(row)

    def resolve(game, line):
        name = line.get('name') or 'Unknown'
        pid, rid = S._int(line.get('player_id')), S._int(line.get('roster_id'))
        selected_season = seasons[game['season_id']]
        roster = rosters.get(rid)
        if roster:
            row, league, team = roster
            if (not selected_season.tournament_id or league != selected_season.league_id
                    or S._norm(row.name) != S._norm(name)
                    or S._norm(team) != S._norm(line.get('team_name'))
                    or (pid and row.source_player_id != pid)):
                roster = None
        details = {str(k).lower(): v for k, v in _loads(roster[0].details_json, {}).items()} if roster else {}
        if not pid and roster:
            pid = roster[0].source_player_id
        if not pid and not roster:
            candidates = by_name[S._norm(name)]
            if line.get('version'):
                candidates = [p for p in candidates if S._norm(p.version) == S._norm(line['version'])]
            if len(candidates) == 1:
                pid = candidates[0].id
        master = by_id.get(pid)
        def value(attr, *aliases):
            for alias in aliases:
                if details.get(alias) is not None:
                    return details[alias]
            return getattr(master, attr, None)
        meta = dict(name=name, version=value('version', 'version') or line.get('version'),
            rating=S._int(value('rating', 'rating', 'overall', 'ovr')),
            bat_rating=S._int(value('bat_rating', 'bat_rating', 'batting_rating', 'bat', 'batting')),
            bowl_rating=S._int(value('bowl_rating', 'bowl_rating', 'bowling_rating', 'bowl', 'bowling')),
            role=value('category', 'category', 'role'), country=value('country', 'country'))
        if pid:
            key = ('player', pid)
        elif roster:
            key = ('roster', selected_season.id, rid)
        else:
            key = ('unknown', selected_season.id, S._norm(name), S._norm(line.get('team_name')), S._norm(meta['version']))
        meta['_identity_note'] = 'Version unavailable; identity kept separate by season/team.' if not pid and not roster else 'Recorded card is no longer in the catalogue.' if pid and master is None else ''
        return key, meta
    return resolve


def _sum_known(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


def _extremes(pairs, legacy):
    """An incomplete archive cannot establish a combined career best."""
    scores, figures = [], []
    unknown_high = unknown_best = False
    for _, line in pairs:
        if S._played(line, 'bat'):
            score = S._int(line.get('bat_runs'))
            unknown_high |= score is None
            if score is not None:
                scores.append((score, None if line.get('bat_out') is None else not bool(line['bat_out'])))
        if S._played(line, 'bowl'):
            wickets, runs = S._int(line.get('bowl_wickets')), S._int(line.get('bowl_runs'))
            unknown_best |= wickets is None or runs is None
            if wickets is not None and runs is not None:
                figures.append((wickets, -runs))
    for line in legacy:
        if any(line.get(k) for k in ('bat_innings', 'bat_runs', 'bat_balls', 'bat_outs')):
            score = S._int(line.get('highest_score'))
            if score is None or (score == 0 and line.get('bat_runs')):
                unknown_high = True
            else:
                scores.append((score, line.get('highest_score_not_out')))
        if any(line.get(k) for k in ('bowl_innings', 'bowl_wickets', 'bowl_runs', 'bowl_balls')):
            wickets, runs = S._int(line.get('best_bowl_wickets')), line.get('best_bowl_runs')
            if wickets is None or runs is None or runs < 0:
                unknown_best = True
            else:
                figures.append((wickets, -runs))
    high = max(score for score, _ in scores) if scores and not unknown_high else None
    flags = [flag for score, flag in scores if score == high]
    best = max(figures) if figures and not unknown_best else None
    return dict(highest_score=high,
        highest_score_not_out=(True if True in flags else None if not flags or None in flags else False),
        best_bowling=f'{best[0]}/{-best[1]}' if best is not None else None)


def _row(entries):
    from services.batting_milestones import saved
    pairs = [(game, line) for game, line, legacy, _ in entries if not legacy]
    legacy = [line for _, line, old, _ in entries if old]
    stats = S._aggregate(pairs, legacy)
    result = dict(entries[-1][3], **stats)
    result.pop('_identity_note', None)
    result['teams'] = '; '.join(dict.fromkeys(line['team_name'] for _, line, _, _ in entries if line.get('team_name')))
    season_names = {game['season_id']: game['season'] for game, _, _, _ in entries}
    result.update(season='; '.join(season_names.values()), seasons=len(season_names),
        scorecard_matches=len({S._match_key(game) for game, _ in pairs}),
        aggregate_seasons=len({game['season_id'] for game, _, old, _ in entries if old}),
        source='Scorecards + stored totals' if pairs and legacy else 'Stored totals' if legacy else 'Scorecards',
        overs=f"{stats['bowled'] // 6}.{stats['bowled'] % 6}" if stats['bowled'] is not None else None,
        bowling_dot_percentage=S._rate(stats['bowl_dots'], stats['bowled'], 100))
    result['hattricks'] = _sum_known([S._int(l.get('hattricks')) for _, l in pairs if S._played(l, 'bowl')]
                                    + [S._int(l.get('hattricks')) for l in legacy])
    result['three_fers'] = _sum_known([None if S._int(l.get('bowl_wickets')) is None else int(3 <= S._int(l['bowl_wickets']) < 5)
                                     for _, l in pairs if S._played(l, 'bowl')]
                                    + [S._int(l.get('three_fers')) for l in legacy])
    result.update(_extremes(pairs, legacy))
    for threshold in (50, 100, 150):
        innings = [line for _, line in pairs if S._played(line, 'bat') and (S._int(line.get('bat_runs')) or 0) >= threshold]
        timings = [saved(line, threshold, runs_key='bat_runs', balls_key='bat_balls') for line in innings]
        result[f'fastest_{threshold}'] = min(timings) if timings and None not in timings and not legacy and stats['runs'] is not None else None
    for field in FIELDING:
        result[field] = _sum_known([l['_fielding'][field] if l.get('_fielding') is not None else None for _, l in pairs]) if not legacy else None
    # A name-only award cannot select between two card versions on the same side.
    awards = []
    for game, line in pairs:
        award = next((l.get('match_potm') for l in game['lines'] if isinstance(l.get('match_potm'), dict)), None)
        key = S._norm(line.get('name')), S._norm(line.get('team_name'))
        won = award is not None and key == (S._norm(award.get('name')), S._norm(award.get('team')))
        awards.append(None if award is None or (won and game['_name_counts'][key] > 1) else int(won))
    result['potm'] = _sum_known(awards + [S._int(l.get('potm')) for l in legacy])
    notes = list(dict.fromkeys(meta['_identity_note'] for _, _, _, meta in entries if meta['_identity_note']))
    if stats['unknown_fields']:
        notes.append('Incomplete recorded fields: ' + ', '.join(stats['unknown_fields']) + '.')
    if result['catches'] is None:
        notes.append('Fielding needs complete, unambiguous delivery evidence.')
    result['notes'] = ' '.join(notes)
    return result


def snapshot(session, series, season=None):
    """All selected players, without pagination or same-name version merging."""
    from services import league_season_service as LS
    from services.season_archive import STAT_FIELDS
    selected = [season] if season is not None else LS.seasons(session, series)
    games = S._games(session, series, season)
    _prepare_games(games)
    entries = [(g, l, False) for g in games for l in g['lines'] if S._norm(l.get('name'))]
    detailed_seasons = {g['season_id'] for g, _, _ in entries}
    aggregate_seasons = 0
    for item in selected:
        if item.id in detailed_seasons:
            continue
        rows = LS.season_rows(session, item)
        aggregate_seasons += bool(rows)
        game = dict(season_id=item.id, season_no=item.season_no, season=LS.season_title(item))
        for row in rows:
            if not S._norm(row.name):
                continue
            line = {key: getattr(row, key, None) for key in (*STAT_FIELDS, 'player_id', 'roster_id', 'name', 'team_name')}
            entries.append((game, line, True))
    # Stable competition order also determines the displayed name and metadata.
    entries.sort(key=lambda e: (e[0].get('season_no') or 0, e[0].get('date') or '', e[0].get('fixture_id') or 0))
    resolve = _resolver(session, entries, selected)
    total, breakdown = defaultdict(list), defaultdict(list)
    for game, line, legacy in entries:
        key, meta = resolve(game, line)
        entry = (game, line, legacy, meta)
        total[key].append(entry)
        breakdown[game['season_id'], key].append(entry)
    sort_key = lambda row: (S._norm(row['name']), S._norm(row['version']), row['teams'], row['season'])
    totals = sorted((_row(group) for group in total.values()), key=sort_key)
    per_season = []
    for item in selected:
        per_season.extend(sorted((_row(group) for (sid, _), group in breakdown.items() if sid == item.id), key=sort_key))
    return dict(competition=series.name, scope=LS.season_title(season) if season else 'All seasons',
        generated_at=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        seasons=len(selected), scorecard_matches=len({S._match_key(g) for g, _, old in entries if not old}),
        aggregate_seasons=aggregate_seasons, totals=totals, seasons_rows=per_season)


def csv_bytes(data, layout='totals'):
    """UTF-8 with BOM for Excel; user-supplied strings are always inert text."""
    def safe(value):
        if isinstance(value, str) and (value[:1] in ('\t', '\r', '\n') or value.lstrip().startswith(('=', '+', '-', '@'))):
            return "'" + value
        return value
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow([heading for _, heading, _ in COLUMNS])
    for row in data['seasons_rows' if layout == 'seasons' else 'totals']:
        writer.writerow([safe(row.get(key)) for key, _, _ in COLUMNS])
    return out.getvalue().encode('utf-8-sig')


_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def _xml(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def _col(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _worksheet(headers, rows, keys=None, guide=False):
    tag = lambda name: f'{{{_NS}}}{name}'
    sheet = ET.Element(tag('worksheet'))
    end = f'{_col(len(headers))}{len(rows) + 1}'
    ET.SubElement(sheet, tag('dimension'), ref=f'A1:{end}')
    view = ET.SubElement(ET.SubElement(sheet, tag('sheetViews')), tag('sheetView'), workbookViewId='0')
    ET.SubElement(view, tag('pane'), xSplit='0' if guide else '2', ySplit='1',
                  topLeftCell='A2' if guide else 'C2', activePane='bottomLeft' if guide else 'bottomRight', state='frozen')
    cols = ET.SubElement(sheet, tag('cols'))
    for i, heading in enumerate(headers, 1):
        width = (32 if i == 1 else 110) if guide else min(42, max(15, len(heading) + 3))
        if not guide and keys[i - 1] in ('name', 'teams', 'season', 'notes'):
            width = 32 if keys[i - 1] != 'notes' else 65
        ET.SubElement(cols, tag('col'), min=str(i), max=str(i), width=str(width), customWidth='1')
    contents = ET.SubElement(sheet, tag('sheetData'))
    for r, values in enumerate([headers, *rows], 1):
        row = ET.SubElement(contents, tag('row'), r=str(r))
        if r == 1:
            row.set('ht', '32')
            row.set('customHeight', '1')
        for c, value in enumerate(values, 1):
            style = '1' if r == 1 else '3' if guide else '2' if keys[c - 1] in DECIMALS else '0'
            cell = ET.SubElement(row, tag('c'), r=f'{_col(c)}{r}', s=style)
            if value is None:
                continue
            if isinstance(value, bool):
                cell.set('t', 'b')
                ET.SubElement(cell, tag('v')).text = '1' if value else '0'
            elif isinstance(value, (int, float)) and math.isfinite(value):
                ET.SubElement(cell, tag('v')).text = str(value)
            else:
                cell.set('t', 'inlineStr')
                text = ET.SubElement(ET.SubElement(cell, tag('is')), tag('t'), {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
                text.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value))[:32767]
    ET.SubElement(sheet, tag('autoFilter'), ref=f'A1:{end}')
    return _xml(sheet)


def xlsx_bytes(data):
    """A portable workbook: real numeric cells, filters and frozen identities."""
    summary = [
        ('Competition', data['competition']), ('Scope', data['scope']),
        ('Generated (UTC)', data['generated_at']), ('Player/version rows', len(data['totals'])),
        ('Selected seasons', data['seasons']), ('Scorecard matches', data['scorecard_matches']),
        ('Aggregate-only seasons', data['aggregate_seasons']),
        ('Player Stats', 'One row per identified player/card version across the selected scope.'),
        ('Season Breakdown', 'One row per player/card version per season, including archived seasons.'),
        ('Ratings', 'Current squad overrides or catalogue values. Historical ratings are not stored in these scorecards.'),
        ('Unknown values', 'Blank cells mean unknown, incomplete or undefined. A known zero is written as numeric zero.'),
        ('Coverage', 'Completed, non-simulated scorecards; aggregate-only player totals when a season has no player scorecards. Live innings are excluded.'),
        ('Fielding', 'Requires complete verified delivery coverage for both innings and an unambiguous name/team identity.'),
        ('Rates', 'Calculated from combined totals. Economy is normalized to six legal balls in every format.'),
        ('Using the sheet', 'Filter any column; the first row and player/version columns stay frozen. See Stats Guide for every definition.'),
        ('Privacy', 'Only public cricket statistics and player metadata are exported.'),
    ]
    keys = [key for key, _, _ in COLUMNS]
    headings = [heading for _, heading, _ in COLUMNS]
    sheets = [('Summary', ['Detail', 'Value'], summary, None),
              ('Player Stats', headings, [[row.get(key) for key in keys] for row in data['totals']], keys),
              ('Season Breakdown', headings, [[row.get(key) for key in keys] for row in data['seasons_rows']], keys),
              ('Stats Guide', ['Column', 'Meaning'], [(heading, meaning) for _, heading, meaning in COLUMNS], None)]
    workbook = ET.Element(f'{{{_NS}}}workbook')
    nodes = ET.SubElement(workbook, f'{{{_NS}}}sheets')
    rels = ET.Element(f'{{{_PKG}}}Relationships')
    types = ET.Element(f'{{{_CT}}}Types')
    ET.SubElement(types, f'{{{_CT}}}Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types, f'{{{_CT}}}Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(types, f'{{{_CT}}}Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    ET.SubElement(types, f'{{{_CT}}}Override', PartName='/xl/styles.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
    package_rels = ET.Element(f'{{{_PKG}}}Relationships')
    ET.SubElement(package_rels, f'{{{_PKG}}}Relationship', Id='rId1', Type=f'{_REL}/officeDocument', Target='xl/workbook.xml')
    styles = f'''<styleSheet xmlns="{_NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF123747"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for i, (name, headers, rows, sheet_keys) in enumerate(sheets, 1):
            ET.SubElement(nodes, f'{{{_NS}}}sheet', name=name, sheetId=str(i), **{f'{{{_REL}}}id': f'rId{i}'})
            ET.SubElement(rels, f'{{{_PKG}}}Relationship', Id=f'rId{i}', Type=f'{_REL}/worksheet', Target=f'worksheets/sheet{i}.xml')
            ET.SubElement(types, f'{{{_CT}}}Override', PartName=f'/xl/worksheets/sheet{i}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            archive.writestr(f'xl/worksheets/sheet{i}.xml', _worksheet(headers, rows, sheet_keys, guide=sheet_keys is None))
        ET.SubElement(rels, f'{{{_PKG}}}Relationship', Id='rIdStyles', Type=f'{_REL}/styles', Target='styles.xml')
        for path, xml in (('[Content_Types].xml', _xml(types)), ('_rels/.rels', _xml(package_rels)),
                          ('xl/workbook.xml', _xml(workbook)), ('xl/_rels/workbook.xml.rels', _xml(rels)),
                          ('xl/styles.xml', styles)):
            archive.writestr(path, xml)
    return out.getvalue()
