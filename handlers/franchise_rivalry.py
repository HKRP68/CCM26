"""/frivalry [#ID] Team A | Team B — franchise records across linked seasons."""
import html
import logging

from database import get_session
from handlers.tournament_duels import _tournament
from services import franchise_rivalry as svc

logger = logging.getLogger(__name__)


def _e(value, limit=None):
    text = str(value or '')
    if limit and len(text) > limit:
        text = text[:limit - 1] + '…'
    return html.escape(text)


def _score(score):
    if not score or score.get('runs') is None:
        return '—'
    wickets = score.get('wickets')
    return str(score['runs']) + (f'/{wickets}' if wickets is not None else '')


def rivalry_card(tour, record):
    r = record
    decided = r['wins_a'] + r['wins_b']
    rate = f"{100 * r['wins_a'] / decided:.1f}% / {100 * r['wins_b'] / decided:.1f}%" if decided else '—'
    lines = [f'⚔️ <b>{_e(tour.name)} — Franchise rivalry</b>',
             f'<b>A:</b> {_e(r["team_a"])}', f'<b>B:</b> {_e(r["team_b"])}', '',
             f'Played: <b>{r["played"]}</b>',
             f'🏆 Wins A / B: <b>{r["wins_a"]} / {r["wins_b"]}</b>',
             f'Ties: <b>{r["ties"]}</b> · No result: <b>{r["no_results"]}</b>',
             f'Super Over wins A / B: <b>{r["super_a"]} / {r["super_b"]}</b>',
             f'Decided-match win % A / B: {rate}', '', '<b>Highest totals against each other</b>']
    for side in ('a', 'b'):
        high = r['highest'][side]
        lines.append(f'{side.upper()}: <b>{_score(high)}</b>'
                     + (f' · {_e(high["season"], 60)}' if high else ''))
    if r['seasons']:
        lines += ['', '<b>Season by season · wins A–B · ties / NR</b>']
        for season in r['seasons'][:8]:
            lines.append(f'{_e(season["label"], 60)}: <b>{season["wins_a"]}–{season["wins_b"]}</b>'
                         f' · {season["ties"]} / {season["no_results"]}')
        if len(r['seasons']) > 8:
            lines.append(f'… {len(r["seasons"]) - 8} more seasons included in totals.')
        lines += ['', '<b>Last five meetings · newest first</b>']
        for m in r['recent']:
            outcome = ('No result' if m['result'] == 'no_result' else
                       'Tied' if m['result'] == 'tie' else
                       ('A won' if svc._key(m['winner']) == svc._key(r['team_a']) else 'B won'))
            if m.get('super_over'):
                outcome += ' (SO)'
            if m.get('simulated'):
                outcome += ' [sim]'
            date = (m.get('when') or '')[:10]
            lines.append(f'{date + " · " if date else ""}{_e(m["season"], 40)}: {outcome}'
                         f' · A {_score(m["score_a"])} / B {_score(m["score_b"])}')
    else:
        lines += ['', 'No completed meetings recorded for these franchises in the selected seasons.']
    lines += ['', f'📚 Current + linked seasons · result history available in '
              f'{r["available_seasons"]}/{r["selected_seasons"]} seasons.',
              f'Includes league and playoff matches; {r["simulated"]} simulated result(s).',
              '<i>Franchises matched by full team name, regardless of captain. '
              'Older saved seasons without result history are unavailable.</i>']
    return '\n'.join(lines)


async def frivalry_handler(update, context):
    session = get_session()
    try:
        tour, args = _tournament(session, context.args)
        teams = ' '.join(args).split('|')
        if len(teams) != 2 or not all(t.strip() for t in teams):
            raise ValueError('Use /frivalry [#ID] Team A | Team B\n'
                             'Example: /frivalry MI | CSK\n'
                             'Use full franchise names if a short name is ambiguous.')
        record = svc.compare(session, tour, *teams)
        await update.message.reply_text(rivalry_card(tour, record), parse_mode='HTML')
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        logger.exception('/frivalry failed')
        await update.message.reply_text('Could not load franchise rivalry records.')
    finally:
        session.close()
