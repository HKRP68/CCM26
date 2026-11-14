"""Additional controls for the existing franchise draft handler and engine."""
import re
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from services import auction_service as A,franchise_draft as D,franchise_draft_rich as R
from services import franchise_draft_admin as M

COMMANDS=('dpreview','dsearch','dhistory','dclock','dcorrect','dpoolremove','dretention','dretain','dunretain','dimportteams','dapick','daskip')


def action_packet(db,season,actor,kind,payload,reason=''):
    action=M.prepare(db,season,actor,kind,payload,reason)
    blocks,text=R.preview_action(db,season,action)
    label={'adminpick':'✅ Confirm Signing','adminskip':'⏭ Confirm Team Skip'}.get(kind,'✅ Apply configuration')
    markup=InlineKeyboardMarkup([[
        InlineKeyboardButton(label,callback_data=f'fd_c_y_{action.token}'),
        InlineKeyboardButton('❌ Cancel',callback_data=f'fd_c_n_{action.token}')]])
    return dict(text=text,blocks=blocks,markup=markup,sid=season.id)


def route(db,season,actor,command,raw):
    from handlers import franchise_draft as H
    if command in ('dapick','daskip'):
        D.require_draft_admin(db,actor)
        parts=[p.strip() for p in raw.split('|')]
        if len(parts)!=(3 if command=='dapick' else 2) or not all(parts):
            raise A.AuctionError('Use /dapick Team | Player name or #poolID | reason.' if command=='dapick' else 'Use /daskip Team | reason.')
        team=H._team(db,season,parts[0],actor)
        payload={'team_id':team.id}
        if command=='dapick':payload['player']=parts[1]
        return action_packet(db,season,actor,'adminpick' if command=='dapick' else 'adminskip',payload,parts[-1])
    if command=='dimportteams':
        D.require_draft_admin(db,actor)
        if not raw.strip():
            blocks,text=R.Card('🏟 Import previous franchises',season.name).text('Copy teams, owners, co-owners and available team details from a previous tournament or league. Preview before applying.').pair()
            return dict(text=text,blocks=blocks,sid=season.id,markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏟 Choose previous tournament / league',callback_data=f'fa:{actor}:{season.id}:v:draftsources:1')]]))
        match=re.fullmatch(r'\s*(league|tournament)\s+(\d+)\s*',raw,re.IGNORECASE)
        if not match:raise A.AuctionError('Use /dimportteams league 12 or /dimportteams tournament 34, or open /dimportteams for source buttons.')
        return action_packet(db,season,actor,'importteams',{'source_kind':match[1].lower(),'source_id':int(match[2])})
    if command=='dpreview':
        D.require_draft_admin(db,actor)
        blocks,text=R.readiness(db,season)
        return dict(text=text,blocks=blocks,markup=H._keyboard(db,season,actor),sid=season.id)
    if command=='dhistory':return H._render_view(db,season,actor,'history',raw or '1')
    if command=='dsearch':
        query=raw.strip().casefold()
        lots=[p for p in D._available(db,season) if query in f'{p.name} {p.version or "Base"} {p.category} {p.country} {p.player_id} #{p.id}'.casefold()][:12]
        card=R.Card('🔎 Search draft players',season.name)
        rows=[]
        for p in lots:
            try:price=D.price_for(db,season,p);value=R._money(season,price)
            except A.AuctionError:value='Missing price'
            rows.append([f'#{p.id} / p{p.player_id}',f'{p.name} · {p.version or "Base"}',f'{p.rating} / {p.bat_rating} / {p.bowl_rating}',f'{p.category} · {p.country}',value])
        if rows:card.table(['Pool / player ID','Player / version','OVR / BAT / BOWL','Role / country','Price'],rows)
        else:card.text('No available players match. Try a name, version, role, country or database ID.')
        card.footer('Choose the exact version below, or /dpick #PoolID. A squad may hold one version of each cricketer.')
        blocks,text=card.pair()
        buttons=pick_buttons(db,season,actor,lots)+list(H._keyboard(db,season,actor).inline_keyboard)
        return dict(text=text,blocks=blocks,markup=InlineKeyboardMarkup(buttons),sid=season.id)
    reason=''
    if command=='dcorrect':
        parts=raw.split('|',2)
        if len(parts)!=3 or not parts[1].strip():raise A.AuctionError('Use /dcorrect prices | reason | 96:23cr. Other kinds: custom, rounds, undo, pooladd, poolremove, purse, clock, resolve.')
        kind,reason,raw=[p.strip() for p in parts]
        command={'prices':'dprices','custom':'dcustom','rounds':'drounds','undo':'dundo','pooladd':'dpooladd','poolremove':'dpoolremove','purse':'dpurse','clock':'dclock','resolve':'dresolve'}.get(kind)
        if command is None:raise A.AuctionError('Choose prices, custom, rounds, undo, pooladd, poolremove or purse.')
    if command in ('dprices','dcustom') and raw:
        return action_packet(db,season,actor,'prices' if command=='dprices' else 'custom',{'text':raw},reason)
    if command in ('drounds','daddrounds') and raw:
        if command=='drounds' and '|' in raw:
            parts=[p.strip() for p in raw.split('|')]
            if len(parts)!=2:raise A.AuctionError('Use /drounds minimum | maximum. Rounds equal the maximum squad size.')
            return action_packet(db,season,actor,'rounds',{'min_squad_size':H._whole(parts[0],'Minimum squad size'),'rounds':H._whole(parts[1],'Maximum squad size')},reason)
        count=H._whole(raw,'Rounds')+(D.get_draft(db,season).rounds if command=='daddrounds' else 0)
        return action_packet(db,season,actor,'rounds',{'rounds':count},reason)
    if command=='dfinish':return action_packet(db,season,actor,'finish',{},raw or reason)
    if command in ('dstart','dresume','dundo'):
        return action_packet(db,season,actor,'undo' if command=='dundo' else 'start',{},reason or (raw if command=='dundo' else ''))
    if command=='dclock':
        pieces=[p.strip().lower() for p in raw.split('|')]
        match=re.fullmatch(r'(\d+)\s*(s|sec|seconds|m|min|minutes)?',pieces[0])
        if not match or len(pieces)!=2:raise A.AuctionError('Use /dclock 30s | pause, /dclock 120s | skip, /dclock 5m | auto, or /dclock 0 | admin.')
        seconds=int(match[1])*(60 if (match[2] or '').startswith('m') else 1)
        return action_packet(db,season,actor,'clock',{'seconds':seconds,'policy':pieces[1]},reason)
    if command=='dresolve':
        if raw.strip().casefold()!='skip':raise A.AuctionError('Use /dcorrect resolve | inactive-team reason | skip while paused.')
        return action_packet(db,season,actor,'resolve',{'operation':'skip'},reason)
    if command in ('dpooladd','dpoolremove'):
        return action_packet(db,season,actor,'pooladd' if command=='dpooladd' else 'poolremove',{'text':raw},reason)
    if command=='dpurse' and '|' in raw:
        team,amount=[p.strip() for p in raw.split('|',1)]
        return action_packet(db,season,actor,'purse',{'team':team,'amount':amount},reason)
    if command=='dretention':return action_packet(db,season,actor,'retention',{'maximum':H._whole(raw)},reason)
    if command=='dretain':
        parts=[p.strip() for p in raw.split('|')]
        if len(parts)!=3:raise A.AuctionError('Use /dretain Team | #PoolID or pPlayerID | 10cr. Configure /dretention first.')
        team=H._team(db,season,parts[0],actor)
        return action_packet(db,season,actor,'retain',{'team_id':team.id,'player':parts[1],'price':parts[2]},reason)
    if command=='dunretain':
        parts=[p.strip() for p in raw.split('|')]
        if len(parts)!=2:raise A.AuctionError('Use /dunretain Team | #PoolID before starting.')
        team=H._team(db,season,parts[0],actor)
        return action_packet(db,season,actor,'unretain',{'team_id':team.id,'player':parts[1]},reason)
    return None


def pick_buttons(db,season,actor,lots):
    current=D.turn(db,season) if season.status==A.STATUS_LIVE else None
    if current is None or actor not in D.owner_ids(current):return []
    revision=D.get_draft(db,season).revision
    return [[InlineKeyboardButton(f'🏏 {p.name[:30]} · {p.version or "Base"}',callback_data=f'fd_p_{actor}_{season.id}_{p.id}_{revision}')]
            for p in lots]


def public_keyboard(season):
    rows=[[(label,view) for label,view in row] for row in (
        [('📊 Draft Board','board'),('🏏 Available Players','pool')],
        [('💰 Team Purses','purses'),('👥 Franchise Squads','teams')],
        [('📋 Full Order','order'),('📜 History','history')],
        [('💼 My Franchise','me'),('↻ Refresh','board')])]
    return InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data=f'fd_b_{season.id}_{view}') for label,view in row] for row in rows])
