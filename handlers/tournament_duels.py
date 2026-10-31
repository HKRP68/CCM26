"""Tournament-scoped captain records and real batter-versus-bowler encounters."""
import html
import logging
from types import SimpleNamespace

from database import get_session
from models import Tournament, TournamentMatch, User
from services import tournament_duels
from services.telegram_user_service import resolve_command_target

logger = logging.getLogger(__name__)


def _tournament(session, args):
    args = list(args or [])
    if args and args[0].startswith('#'):
        try:
            tour = session.get(Tournament, int(args.pop(0)[1:]))
        except ValueError:
            tour = None
        if not tour:
            raise ValueError('Tournament not found. Use # followed by its tournament ID.')
        return tour, args
    tours = session.query(Tournament).filter_by(is_active=True).all()
    if len(tours) != 1:
        choices = ', '.join(f'#{t.id} {t.name}' for t in tours)
        raise ValueError('Choose a tournament: add #ID after the command.'
                         + (f' Active: {choices}' if choices else ' No tournament is active.'))
    return tours[0], args


async def tvs_handler(update, context):
    """/tvs [#ID] Batter name | Bowler name (optionally Name @ Team)."""
    await _duel_handler(update, context, whole_season=False)


async def svs_handler(update, context):
    """/svs [#ID] Batter | Bowler — current and linked saved/live seasons."""
    await _duel_handler(update, context, whole_season=True)


async def _duel_handler(update, context, *, whole_season):
    command = 'svs' if whole_season else 'tvs'
    session = get_session()
    try:
        tour, args = _tournament(session, context.args)
        names = ' '.join(args).split('|')
        if len(names) != 2 or not all(n.strip() for n in names):
            raise ValueError(f'Use /{command} [#ID] Batter name | Bowler name\n'
                             f'Example: /{command} Virat Kohli | Jasprit Bumrah\n'
                             'For duplicate names use Player name @ Team name.')
        coverage = ''
        if whole_season:
            rows, recorded, total = tournament_duels.season_duels(session, tour)
            coverage = (f'\n\n📚 Current + linked seasons · '
                        f'Recorded encounters available in {recorded}/{total} seasons.')
        else:
            rows = tournament_duels.archived_duels(session, tour.id)
        row = tournament_duels.compare(rows, *names, across_seasons=whole_season)
        if row is None:
            await update.message.reply_text(
                'No recorded deliveries for this matchup in the selected '
                + ('seasons' if whole_season else 'tournament') + ' yet. '
                'Matchups are tracked from this update onward; older scorecards '
                'and manual results do not contain ball-level encounters.' + coverage)
            return
        bat, bowl = row['batter'], row['bowler']
        sr = f"{row['runs'] * 100 / row['balls']:.1f}" if row['balls'] else '—'
        avg = f"{row['runs'] / row['dismissals']:.1f}" if row['dismissals'] else '—'
        e = html.escape
        await update.message.reply_text(
            f"🏏 <b>{e(tour.name)} — {'Season Matchup' if whole_season else 'Player vs Player'}</b>\n"
            f"{e(bat['name'])} ({e(bat['team'])})\nvs "
            f"{e(bowl['name'])} ({e(bowl['team'])})\n\n"
            f"Runs: <b>{row['runs']}</b> · Balls faced: <b>{row['balls']}</b>\n"
            f"Dismissals: <b>{row['dismissals']}</b> · Dots: {row['dots']}\n"
            f"4s: {row['fours']} · 6s: {row['sixes']}\n"
            f"Strike rate: <b>{sr}</b> · Average: {avg}\n\n"
            'Main innings only. Extras excluded from runs; run outs excluded from dismissals.' + coverage,
            parse_mode='HTML')
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        logger.exception('/%s failed', command)
        await update.message.reply_text('Could not load player matchups.')
    finally:
        session.close()


def captain_record(matches, me_id, other_id):
    """Use the fixture winner (including Super Overs), not equal main scores."""
    wins = losses = ties = 0
    for m in matches:
        if {m.host_user_id, m.target_user_id} != {me_id, other_id}:
            continue
        my_team = m.team1_id if m.host_user_id == me_id else m.team2_id
        other_team = m.team2_id if m.host_user_id == me_id else m.team1_id
        if m.winner_team_id is None:
            ties += 1
        elif m.winner_team_id == my_team:
            wins += 1
        elif m.winner_team_id == other_team:
            losses += 1
    return wins, losses, ties


async def th2h_handler(update, context):
    """/th2h [#ID] @user, or reply to a captain's message."""
    session = get_session()
    try:
        tour, args = _tournament(session, context.args)
        me = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        other, _ = resolve_command_target(session, update, SimpleNamespace(args=args), 'th2h')
        if not me or not other:
            raise ValueError('Use /th2h [#ID] @username, or reply to their message with /th2h.')
        if me.id == other.id:
            raise ValueError('Choose another captain to compare with.')
        matches = (session.query(TournamentMatch)
                   .filter_by(tournament_id=tour.id, status='completed')
                   .filter(TournamentMatch.host_user_id.in_((me.id, other.id)),
                           TournamentMatch.target_user_id.in_((me.id, other.id)))
                   .order_by(TournamentMatch.completed_at.desc(), TournamentMatch.id.desc()).all())
        wins, losses, ties = captain_record(matches, me.id, other.id)
        from handlers.h2h import _plain_name
        e = html.escape
        await update.message.reply_text(
            f"⚔️ <b>{e(tour.name)} — Captain Head-to-Head</b>\n"
            f"{e(_plain_name(me))} vs {e(_plain_name(other))}\n\n"
            f"Played: <b>{wins + losses + ties}</b>\n"
            f"🏆 Wins: <b>{wins}</b> · Losses: <b>{losses}</b>\n"
            f"🤝 Ties / no result: <b>{ties}</b>\n\n"
            'Includes Super Over winners. Only this tournament’s recorded encounters count.',
            parse_mode='HTML')
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        logger.exception('/th2h failed')
        await update.message.reply_text('Could not load the tournament head-to-head record.')
    finally:
        session.close()
