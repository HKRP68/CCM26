"""/clsquad — public league rosters, defaulting to your tournament team."""
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from services import challenge_squad as S
from services import rich_message as R

logger = logging.getLogger(__name__)
PREFIX = 'clsq_'
PAGE_SIZE = 20
USAGE = ('<code>/clsquad</code> — your tournament team, or browse leagues\n'
         '<code>/clsquad Mumbai</code> — find a team\n'
         '<code>/clsquad IPL | Mumbai</code> — choose a league and team')


def _picker(title, rows, key, page=0, *, label=None, nav=None):
    page = max(0, min(int(page), max(0, (len(rows) - 1) // PAGE_SIZE)))
    buttons = [[InlineKeyboardButton((label(row) if label else row.name)[:64],
                                    callback_data=f'{PREFIX}{key}_{row.id}')]
               for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    navigation = []
    if nav and page:
        navigation.append(InlineKeyboardButton('‹ Previous', callback_data=f'{PREFIX}{nav}_{page - 1}'))
    if nav and (page + 1) * PAGE_SIZE < len(rows):
        navigation.append(InlineKeyboardButton('Next ›', callback_data=f'{PREFIX}{nav}_{page + 1}'))
    if navigation:
        buttons.append(navigation)
    text = f'<b>{escape(title)}</b>\n\n{USAGE}'
    if not rows:
        text += '\n\nNo teams are configured here yet.'
    elif len(rows) > PAGE_SIZE:
        text += f'\n\nPage {page + 1} of {(len(rows) + PAGE_SIZE - 1) // PAGE_SIZE}'
    return None, text, InlineKeyboardMarkup(buttons) if buttons else None


def _leagues(session, page=0, title='Challenge League squads — choose a league'):
    return _picker(title, S.league_rows(session), 'league', page, nav='leagues')


def _league_teams(session, league, page=0):
    return _picker(f'{league.name} — choose a team', S.team_rows(session, league.id), 'team', page,
                   nav=f'league_{league.id}')


def _owned_picker(pairs, page=0):
    labels = {entry.id: f'{entry.name} · {tour.name}' for tour, entry in pairs}
    return _picker('Your tournament teams — choose a squad', [entry for _, entry in pairs],
                   'owned', page, label=lambda row: labels[row.id], nav='mine')


def _roster(session, team, *, tour=None, entry=None):
    blocks, text = S.squad_view(session, team, tour=tour, entry=entry)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Other teams in this league',
        callback_data=f'{PREFIX}league_{team.league_id}')]])
    return blocks, text, keyboard


def _owned_roster(session, tour, entry):
    team = S.tournament_team(session, tour, entry)
    if team is None:
        return None, (f'<b>{escape(entry.name)}</b>\n'
                      'No Challenge League roster is linked to this tournament team. '
                      'Ask an admin to check its team assignment.'), None
    return _roster(session, team, tour=tour, entry=entry)


def _choose_team(session, rows, query):
    if len(rows) == 1:
        return _roster(session, rows[0])
    if len(rows) > PAGE_SIZE:
        return _leagues(session, title='Several teams match — choose a league first')
    if rows:
        return _picker(f'Which “{query}” team?', rows, 'team',
                       label=lambda team: f'{team.name} · {team.league.name}')
    return _leagues(session, title=f'No team matches “{query}” — choose a league')


def _lookup(session, query):
    leagues = S.league_rows(session)
    if '|' in query:
        league_name, name = [part.strip() for part in query.split('|', 1)]
        found = S.matches(leagues, league_name, ('name', 'short_code', 'command', 'tournament_command'))
        if len(found) != 1:
            return _leagues(session, title='Choose the league for this squad')
        if not name:
            return _league_teams(session, found[0])
        return _choose_team(session, S.matches(S.team_rows(session, found[0].id), name, ('name', 'short_name')), name)
    league = S.matches(leagues, query, ('name', 'short_code', 'command', 'tournament_command'), partial=False)
    if len(league) == 1:
        return _league_teams(session, league[0])
    if len(league) > 1:
        return _picker('Which league?', league, 'league')
    return _choose_team(session, S.matches(S.team_rows(session), query, ('name', 'short_name')), query)


async def clsquad_handler(update, context):
    session = get_session()
    try:
        query = ' '.join(context.args or []).strip()
        if query:
            result = _lookup(session, query)
        else:
            owned = S.owned_teams(session, update.effective_user.id if update.effective_user else None)
            result = (_owned_roster(session, *owned[0]) if len(owned) == 1 else
                      _owned_picker(owned) if owned else _leagues(session))
        blocks, text, markup = result
        await R.reply_rich(update.effective_message, blocks, text, reply_markup=markup)
    except Exception:
        logger.exception('/clsquad failed')
        await R.reply_rich(update.effective_message, None, '⚠️ Could not load the squad. Please try again.')
    finally:
        session.close()


async def clsquad_callback(update, context):
    q = update.callback_query
    parts = (q.data or '').removeprefix(PREFIX).split('_')
    if (not (q.data or '').startswith(PREFIX) or len(parts) not in (2, 3)
            or any(not part.isascii() or not part.isdigit() for part in parts[1:])):
        await q.answer('Unknown squad selection.', show_alert=True)
        return
    session = get_session()
    try:
        kind, ident = parts[0], int(parts[1])
        page = int(parts[2]) if len(parts) == 3 else 0
        roster = False
        if kind == 'leagues' and len(parts) == 2:
            result = _leagues(session, ident)
        elif kind == 'mine' and len(parts) == 2:
            result = _owned_picker(S.owned_teams(session, q.from_user.id), ident)
        elif kind == 'league':
            league = next((row for row in S.league_rows(session) if row.id == ident), None)
            if league is None:
                await q.answer('This league is no longer available.', show_alert=True)
                return
            result = _league_teams(session, league, page)
        elif kind == 'team' and len(parts) == 2:
            team = next((row for row in S.team_rows(session) if row.id == ident), None)
            if team is None:
                await q.answer('This team is no longer available.', show_alert=True)
                return
            result, roster = _roster(session, team), True
        elif kind == 'owned' and len(parts) == 2:
            from models import TournamentTeam
            entry = session.get(TournamentTeam, ident)
            tours = {tour.id: tour for tour in S.tournament_rows(session)}
            tour = tours.get(entry.tournament_id) if entry else None
            if tour is None:
                await q.answer('This tournament team is no longer available.', show_alert=True)
                return
            result, roster = _owned_roster(session, tour, entry), True
        else:
            await q.answer('Unknown squad selection.', show_alert=True)
            return
        await q.answer()
        blocks, text, markup = result
        # A squad may span several messages. Reply in full because edit_rich's
        # single-message fallback intentionally trims oversized cards.
        if roster:
            await R.reply_rich(q.message, blocks, text, reply_markup=markup)
        else:
            await R.edit_rich(q, blocks, text, reply_markup=markup)
    except Exception:
        logger.exception('/clsquad selection failed')
        await R.reply_rich(q.message, None, '⚠️ Could not load the squad. Please try again.')
    finally:
        session.close()
