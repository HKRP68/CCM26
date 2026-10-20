"""Fixture conditions and private, website-managed match presentation settings."""
import json


def playoff_settings(tour):
    try:
        raw = json.loads(getattr(tour, 'playoff_conditions_json', None) or '{}')
        return raw if isinstance(raw, dict) else {}
    except (ValueError, TypeError):
        return {}


def save_playoff_settings(session, tour, pitches, stadiums):
    from services.league_schedule_service import FIXTURE_PITCHES
    from services.tournament_stadiums import resolve
    clean_pitches = list(dict.fromkeys(pitches))
    if any(p not in FIXTURE_PITCHES for p in clean_pitches):
        raise ValueError('Choose valid playoff pitches.')
    clean_stadiums = []
    for name in stadiums:
        canonical = resolve(name)
        if not canonical:
            raise ValueError(f'{name} is not in Stadium Data.')
        if canonical not in clean_stadiums:
            clean_stadiums.append(canonical)
    tour.playoff_conditions_json = json.dumps({'pitches': clean_pitches, 'stadiums': clean_stadiums})
    session.flush()
    from models import TournamentMatch, TournamentTeam
    from services.league_schedule_service import pick_pitch, PREFERRED_MODES, tour_preferred_pitches
    from services.tournament_stadiums import venue_for
    import random
    homes = {t.id: t.home_stadium for t in session.query(TournamentTeam).filter_by(tournament_id=tour.id)}
    for fixture in session.query(TournamentMatch).filter_by(tournament_id=tour.id, status='scheduled'):
        if not is_playoff(fixture):
            continue
        fallback = tour_preferred_pitches(tour) if tour.pitch_mode in PREFERRED_MODES else []
        if clean_pitches or fallback:
            fixture.pitch_type = random.choice(clean_pitches or fallback)
        else:
            fixture.pitch_type = None if tour.pitch_mode == 'host' else pick_pitch(tour.pitch_mode)
        fixture.venue = venue_for(fixture, tour, homes)
    session.flush()


def is_playoff(fixture):
    return (fixture.stage or 'league') not in ('league', 'group')


def day_night(fixture, league_index=None):
    """Four night, two day, repeated in fixture order; playoffs always night."""
    if is_playoff(fixture):
        return 'Night'
    index = league_index if league_index is not None else max(0, (fixture.match_no or 1) - 1)
    return 'Night' if index % 6 < 4 else 'Day'


def fixture_period(session, fixture):
    from models import TournamentMatch
    fixtures = (session.query(TournamentMatch).filter_by(tournament_id=fixture.tournament_id)
                .filter(TournamentMatch.stage.in_(('league', 'group')))
                .order_by(TournamentMatch.match_no, TournamentMatch.round_no, TournamentMatch.id).all())
    index = next((i for i, fx in enumerate(fixtures) if fx.id == fixture.id), 0)
    return day_night(fixture, index)


def set_super_overs(session, tour, fixture_id, count):
    from models import TournamentMatch
    fx = session.get(TournamentMatch, int(fixture_id))
    if not fx or fx.tournament_id != tour.id:
        raise ValueError('Fixture not found in this tournament.')
    if fx.status != 'scheduled':
        raise ValueError('Only unstarted fixtures can be changed.')
    if count not in (0, 1, 2):
        raise ValueError('Choose normal play, one Super Over or two Super Overs.')
    fx.planned_super_overs = count
    session.flush()
    return fx


def apply_to_draft(session, fixture, draft):
    from services.pitch_report import build_pitch_report
    if fixture is None:
        return
    period = fixture_period(session, fixture)
    draft['fixture_day_night'] = period
    draft['_planned_super_overs'] = int(fixture.planned_super_overs or 0)
    from models import Tournament
    from services.league_schedule_service import pitch_locked
    tour = session.get(Tournament, fixture.tournament_id)
    if fixture.pitch_type and (pitch_locked(tour) or is_playoff(fixture)):
        draft['pitch_type'] = fixture.pitch_type
    pitch = draft.get('pitch_type') or 'Hard'
    _, draft['conditions'] = build_pitch_report(pitch, day_night=period)


def constrain_tie_outcome(outcome, *, runs, par, remaining, wickets, wicket_limit):
    """Keep a planned tied innings feasible, before scoring any delivery.

    All totals, batter/bowler stats and commentary consume the same outcome.
    Normal matches never call this function. Scores are never rewritten later.
    """
    if remaining <= 0:
        return outcome
    need = max(0, int(par) - int(runs))
    low, high = max(0, need - 6 * (remaining - 1)), min(6, need)
    # Preserve engine outcomes whenever they keep the tie achievable.
    extra = outcome.get('is_extra') or outcome.get('type') == 'extra'
    out = outcome.get('batter_out') or outcome.get('type') == 'wicket'
    scored = int(outcome.get('runs') or 0)
    if not extra and low <= scored <= high and not (out and wickets >= wicket_limit - 1 and need != scored):
        return outcome
    scored = max(low, min(high, scored))
    return dict(outcome, type='run', runs=scored,
                is_extra=False, extra_type=None, batter_out=False, wicket_type=None)


def fixture_sections(fixtures):
    """League first, then a separate section for each playoff stage."""
    from services.tournament_webapp import STAGE_LABEL
    stages = ('league', 'group', 'round_of_32', 'round_of_16', 'quarterfinal',
              'qualifier1', 'eliminator', 'semifinal', 'qualifier2', 'final')
    groups = {}
    for fixture in sorted(fixtures, key=lambda f: (f.match_no or 0, f.round_no or 0, f.id)):
        stage = fixture.stage or 'league'
        key = 'league' if stage in ('league', 'group') else stage
        groups.setdefault(key, []).append(fixture)
    order = sorted(groups, key=lambda k: stages.index(k) if k in stages else len(stages))
    return [('League stage' if k == 'league' else f"Playoffs — {STAGE_LABEL.get(k, k.replace('_', ' ').title())}", groups[k]) for k in order]


def cap_first_innings(outcome, runs, maximum):
    """Keep the first innings within the legal chase's attainable run budget."""
    scored = int(outcome.get('runs') or 0)
    extra = outcome.get('is_extra') or outcome.get('type') == 'extra'
    penalty = int(extra and outcome.get('extra_type') in ('Wide', 'No Ball'))
    if runs + scored + penalty <= maximum:
        return outcome
    return dict(outcome, type='run', runs=max(0, maximum-runs), is_extra=False,
                extra_type=None, batter_out=False, wicket_type=None)
