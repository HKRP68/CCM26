"""Plan and apply bulk signings through the existing franchise draft engine.

The planner is read-only and bounded. Application uses normal draft quotes,
signings, purses, turns and journals in the administrator's single transaction.
"""
from collections import Counter
from datetime import datetime
import secrets
import time

from services import auction_service as A, franchise_draft as D

MAX_SEARCH_NODES = 50000
MAX_PLAN_SECONDS = 10.0


def _editable(db, season):
    draft = D.get_draft(db, season)
    if draft is None:
        raise A.AuctionError('Choose a Franchise Draft season first.')
    if season.league_id or season.published_at or season.status not in (A.STATUS_SETUP,A.STATUS_LIVE,A.STATUS_PAUSED):
        raise A.AuctionError('Auto draft is available during setup, live or paused drafts before finalization.')
    return draft


def _pending(db, season, teams):
    """Expand the real pending regular/catch-up turns, allowing retained slots."""
    draft = D.get_draft(db,season)
    left = {t.id:draft.rounds-int(t.squad_size or 0) for t in teams}
    turns = []
    grid = D.schedule(db,season)
    for position in range(int(draft.cursor),len(grid)):
        team = grid[position]
        if left[team.id] > 0:
            turns.append(dict(team_id=team.id,round_no=position//len(teams)+1,catch_up=False))
            left[team.id] -= 1
    for entry in D.skipped_turns(db,season):
        if entry['franchise_id'] not in left:
            raise A.AuctionError('A skipped franchise is missing. Repair the draft queue before auto drafting.')
        if left[entry['franchise_id']] > 0:
            turns.append(dict(team_id=entry['franchise_id'],round_no=entry['round_no'],catch_up=True))
            left[entry['franchise_id']] -= 1
    if any(n != 0 for n in left.values()):
        raise A.AuctionError('Remaining squad slots do not match the scheduled turns. Review draft order and skipped picks.')
    return turns


def plan(db, season):
    """Find a complete feasible allocation, preferring ratings in draft order.

    Backtracking prevents an expensive early pick from blocking another team.
    Budget/category/rating lower bounds prune impossible branches. Equivalent
    cards are collapsed only where their names impose identical restrictions.
    No partial plan is returned on failure or a search-budget expiry.
    """
    began = time.monotonic()
    draft = _editable(db,season)
    teams = D._ordered_teams(db,season)
    if not teams or any(not t.owner_tg_id or int(t.owner_tg_id)<=0 for t in teams):
        raise A.AuctionError('Assign a valid owner to every franchise before auto drafting.')
    turns = _pending(db,season,teams)
    if not turns:
        raise A.AuctionError('There are no remaining squad slots to fill.')
    all_lots = A.lots(db,season.id)
    identities = [p.player_id for p in all_lots]
    if None in identities or len(identities)!=len(set(identities)):
        raise A.AuctionError('Repair missing or duplicate player database IDs before auto drafting.')
    pool = [p for p in all_lots if p.status==A.LOT_QUEUED]
    prices = {p.id:D.price_for(db,season,p) for p in pool}
    if len(pool)<len(turns):
        raise A.AuctionError(f'Auto draft needs {len(turns)} players for the remaining slots; only {len(pool)} are available. No players were signed.')
    lows, highs, rules = A.role_minimums(season), A.role_maximums(season), A.rating_rules(season)
    states = {}
    for team in teams:
        signed = A.squad(db,team.id)
        names = {p.name.strip().casefold() for p in signed}
        if len(signed)!=int(team.squad_size or 0) or len(names)!=len(signed):
            raise A.AuctionError(f'Repair {team.name}’s squad count or duplicate player versions before auto drafting.')
        if A.ledger_total(db,team.id)!=int(team.purse_remaining_lakh or 0):
            raise A.AuctionError(f'Reconcile {team.name}’s purse with its ledger before auto drafting.')
        states[team.id] = dict(name=team.name,slots=draft.rounds-len(signed),budget=int(team.purse_remaining_lakh or 0),
            roles=Counter(p.category for p in signed),ratings=[sum(p.rating<=r['max_rating'] for p in signed) for r in rules],names=names)
    cards = sorted(pool,key=lambda p:(-int(p.rating),prices[p.id],p.id))
    used = set()
    name_frequency = Counter(p.name.strip().casefold() for p in pool)
    owned_names = set().union(*(s['names'] for s in states.values()))
    nodes = 0

    def check_budget():
        if nodes>=MAX_SEARCH_NODES or time.monotonic()-began>MAX_PLAN_SECONDS:
            raise A.AuctionError('Auto draft could not find a safe complete plan within the search limit. No players were signed. Add affordable eligible players or simplify overlapping squad rules, then preview again.')

    def candidate_ok(state,p):
        if p.id in used or p.category not in A.SQUAD_ROLES or not p.name.strip():return False
        if p.name.strip().casefold() in state['names'] or prices[p.id]>state['budget']:return False
        counts = state['roles']
        if counts[p.category]+1>highs.get(p.category,draft.rounds):return False
        remaining = state['slots']-1
        if sum(max(0,n-counts[role]-(p.category==role)) for role,n in lows.items())>remaining:return False
        if any(max(0,r['min_players']-have-(p.rating<=r['max_rating']))>remaining for r,have in zip(rules,state['ratings'])):return False
        return True

    def candidates(team_id):
        state = states[team_id]
        equivalent = set()
        for p in cards:
            if not candidate_ok(state,p):continue
            name = p.name.strip().casefold()
            key = (p.category,p.rating,prices[p.id],name if name_frequency[name]>1 or name in owned_names else None)
            if key in equivalent:continue
            equivalent.add(key)
            yield p

    def feasible():
        check_budget()
        available = [p for p in cards if p.id not in used]
        if len(available)<sum(s['slots'] for s in states.values()):return 'Too few available players remain.'
        room_roles = Counter(p.category for p in available)
        for role,n in lows.items():
            if sum(max(0,n-s['roles'][role]) for s in states.values())>room_roles[role]:
                return f'Too few {role} players remain for all franchises.'
        for index,rule in enumerate(rules):
            if sum(max(0,rule['min_players']-s['ratings'][index]) for s in states.values())>sum(p.rating<=rule['max_rating'] for p in available):
                return f'Too few players rated ≤{rule["max_rating"]} remain for all franchises.'
        for state in states.values():
            check_budget()
            slots = state['slots']
            if slots<0 or state['budget']<0:return f'{state["name"]} has an invalid purse or squad size.'
            if any(state['roles'][role]>cap for role,cap in highs.items()):return f'{state["name"]} exceeds a category maximum.'
            owed = {role:max(0,n-state['roles'][role]) for role,n in lows.items()}
            needs = [max(0,r['min_players']-have) for r,have in zip(rules,state['ratings'])]
            if not slots:
                if any(owed.values()) or any(needs):return f'{state["name"]} cannot meet its mandatory squad rules.'
                continue
            eligible = [p for p in available if p.name.strip().casefold() not in state['names'] and p.category in A.SQUAD_ROLES
                        and p.name.strip() and state['roles'][p.category]<highs.get(p.category,draft.rounds)]
            cheapest = {}
            for p in eligible:
                name = p.name.strip().casefold()
                cheapest[name] = min(cheapest.get(name,prices[p.id]),prices[p.id])
            if len(cheapest)<slots:return f'{state["name"]} needs {slots} more distinct eligible players.'
            floor = sum(sorted(cheapest.values())[:slots])
            if floor>state['budget']:
                return f'{state["name"]} needs at least {D._money(season,floor)} for its {slots} remaining slots; purse {D._money(season,state["budget"])}.'
            if any(owed.values()) or any(needs) or highs:
                try:
                    floor = D._completion_flow(eligible,slots,state['roles'],owed,highs,rules,needs,lambda p:prices[p.id],min_slots=slots)
                except A.AuctionError as exc:return f'{state["name"]}: {exc}'
                if floor>state['budget']:return f'{state["name"]} cannot afford the players required by its category/rating rules.'
        return None

    def change(team_id,p,direction):
        state = states[team_id]
        state['slots'] -= direction
        state['budget'] -= direction*prices[p.id]
        state['roles'][p.category] += direction
        for i,r in enumerate(rules):state['ratings'][i] += direction*(p.rating<=r['max_rating'])
        name = p.name.strip().casefold()
        if direction==1:
            used.add(p.id);state['names'].add(name)
        else:
            used.remove(p.id);state['names'].remove(name)

    failure = feasible()
    if failure:raise A.AuctionError('Auto draft cannot fill every remaining slot: '+failure+' No players were signed.')
    chosen, stack, depth = [], [candidates(turns[0]['team_id'])], 0
    while stack:
        check_budget()
        p = next(stack[-1],None)
        if p is None:
            stack.pop()
            if not chosen:break
            depth -= 1
            change(turns[depth]['team_id'],chosen.pop(),-1)
            continue
        nodes += 1
        change(turns[depth]['team_id'],p,1)
        failure = feasible()
        if failure:
            change(turns[depth]['team_id'],p,-1)
            continue
        chosen.append(p);depth += 1
        if depth==len(turns):
            picks = [dict(turn,lot_id=p.id,player_id=p.player_id,name=p.name,version=p.version or 'Base',rating=p.rating,
                          category=p.category,country=p.country,price_lakh=prices[p.id]) for turn,p in zip(turns,chosen)]
            return dict(batch_id=secrets.token_hex(12),picks=picks,total_lakh=sum(p['price_lakh'] for p in picks),
                        strategy='Highest feasible rating in the existing draft order; required roles, ratings and completion budgets are protected.')
        stack.append(candidates(turns[depth]['team_id']))
    raise A.AuctionError('Auto draft cannot find a complete allocation within the available players, purses and squad rules. No players were signed. Add eligible players or adjust the rules/purses and preview again.')


def apply(db,season,actor,payload,reason='',*,trusted_admin=False):
    from services.franchise_draft_admin import require_admin,validate_final
    require_admin(db,actor,trusted_admin)
    draft = _editable(db,season)
    D._claim(db,season,draft)
    _editable(db,season)
    picks = payload.get('picks',[])
    teams = D._ordered_teams(db,season)
    if any(A.ledger_total(db,t.id)!=int(t.purse_remaining_lakh or 0) for t in teams):
        raise A.AuctionError('A franchise purse no longer matches its ledger. Reconcile it before auto drafting.')
    pending = _pending(db,season,teams)
    if not picks or len(picks)!=len(pending):raise A.AuctionError('The auto draft plan no longer matches all remaining slots. Generate a new preview.')
    now = datetime.utcnow()
    if season.status==A.STATUS_SETUP:D.start(db,season,by_tg_id=None if trusted_admin else actor,now=now)
    else:
        D._invariants(season,draft)
        season.status=A.STATUS_LIVE
        D._pass_full_teams(db,season)
    # The administrator approved the entire batch. Freeze its internal clock
    # reference without changing the configured duration or timeout policy.
    draft.pick_deadline_at=draft.paused_seconds_left=None
    for expected,pick in zip(pending,picks):
        info = D.turn_info(db,season)
        if (info['team'] is None or info['team'].id!=pick['team_id'] or pick['team_id']!=expected['team_id']
                or info['round_no']!=pick['round_no'] or bool(info['catch_up'])!=bool(pick['catch_up'])):
            raise A.AuctionError('The auto draft order changed. Generate a new preview.')
        lot = db.get(D.AuctionLot,int(pick['lot_id']))
        if lot is None or D.price_for(db,season,lot)!=int(pick['price_lakh']):
            raise A.AuctionError('An auto draft player or price changed. Generate a new preview.')
        quote = D.preview_pick(db,season,actor,'#'+str(lot.id),now=now,_admin_reason=reason or 'Auto draft remaining slots',_trusted_admin=trusted_admin)
        D.confirm_pick(db,season,quote.token,actor,now=now,_admin_reason=reason or 'Auto draft remaining slots',
                       _trusted_admin=trusted_admin,_bulk_action=payload['batch_id'])
    validate_final(db,season)
    if any(t.squad_size!=draft.rounds for t in A.franchises(db,season.id)) or season.status!=A.STATUS_COMPLETED:
        raise A.AuctionError('The auto draft did not complete every squad. No part of this batch may be saved.')
    team_lines='\n'.join(f'{D._e(t.name)} · {t.squad_size}/{draft.rounds} · {D._money(season,t.purse_remaining_lakh)}'
                         for t in A.franchises(db,season.id)[:8])
    D._event(db,season,'fd_autofill_completed',
        f'🤖 <b>Auto Draft completed</b>\n<b>{len(picks)}</b> remaining squad places filled.\n'
        f'💰 Signing total: <b>{D._money(season,payload["total_lakh"])}</b>\n'
        'Every squad meets its category, rating and purse requirements. Individual signings are saved in draft history.\n\n'
        +team_lines+'\n\n'+
        'Use /dsquad, /dpurse and /dhistory to review the results.',by_tg_id=actor,
        metadata={'batch_id':payload['batch_id'],'picks':picks,'reason':reason})
