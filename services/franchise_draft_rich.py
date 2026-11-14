"""Draft presentation: native rich cards and matching, escaped HTML fallbacks.

Renderers never commit or call Telegram. Prices, rows and mentions feed both
formats so the confirmation always means the same thing on every client.
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import json
import logging

from services import rich_message as R

logger = logging.getLogger(__name__)


def plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(plain(item) for item in value)
    if isinstance(value, dict):
        return plain(value.get('text', ''))
    return str(value if value is not None else '')


def inline_html(value):
    if isinstance(value, list):
        return ''.join(inline_html(item) for item in value)
    if not isinstance(value, dict):
        return escape(str(value if value is not None else ''))
    content = inline_html(value.get('text', ''))
    tag = {'bold':'b', 'italic':'i', 'code':'code', 'underline':'u',
           'strikethrough':'s', 'spoiler':'tg-spoiler'}.get(value.get('type'))
    if tag:
        return f'<{tag}>{content}</{tag}>'
    if value.get('type') == 'url':
        return f'<a href="{escape(str(value.get("url", "")), quote=True)}">{content}</a>'
    return content


class _InlineParser(HTMLParser):
    """Split on real line boundaries while preserving formatting and mentions."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = [[]]
        self.styles = []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.handle_data('\n')
        elif tag in ('b','strong','i','em','code','pre','u','s','strike','del','a','tg-spoiler'):
            self.styles.append((tag,dict(attrs)))

    def handle_endtag(self, tag):
        if self.styles and self.styles[-1][0] == tag:
            self.styles.pop()

    def handle_data(self, text):
        for number, piece in enumerate(text.split('\n')):
            if number:
                self.lines.append([])
            if not piece:
                continue
            node = piece
            for tag, attrs in reversed(self.styles):
                if tag == 'a':
                    node = R.link(node,attrs.get('href',''))
                else:
                    kind = {'b':'bold','strong':'bold','i':'italic','em':'italic',
                            'pre':'code','code':'code','u':'underline','s':'strikethrough',
                            'strike':'strikethrough','del':'strikethrough','tg-spoiler':'spoiler'}[tag]
                    node = {'type':kind,'text':node}
            self.lines[-1].append(node)


def inline_lines(text):
    parser = _InlineParser()
    parser.feed(str(text or ''))
    parser.close()
    return parser.lines


def _only_style(line, kind):
    return bool(line) and all(isinstance(n,dict) and n.get('type') == kind for n in line)


def blocks_for_html(text):
    """Style short replies, forms, old events and help as structured blocks."""
    try:
        lines = inline_lines(text)
        blocks, commands = [], []

        def flush_commands():
            if commands:
                blocks.append(R.table([[R.cell('Command',header=True),R.cell('Usage',header=True)]] +
                                      [[R.cell(a),R.cell(b)] for a,b in commands],
                                      bordered=True,striped=True,compact=True))
                commands.clear()

        index = 0
        while index < len(lines):
            line = lines[index]
            value = plain(line).strip()
            index += 1
            if not value:
                flush_commands()
                continue
            if _only_style(line,'code') and value.startswith('/'):
                description = lines[index] if index < len(lines) else []
                if description and not _only_style(description,'code') and not _only_style(description,'bold'):
                    commands.append((line,description))
                    index += 1
                    continue
            flush_commands()
            if not blocks and len(value) <= 120:
                blocks.append(R.heading(line,size=2))
            elif _only_style(line,'bold') and len(value) <= 120:
                blocks.append(R.heading(line,size=3))
            elif value.startswith('⏭') and any(isinstance(n,dict) and n.get('type') == 'url' for n in line):
                blocks.append(R.pullquote(line,caption='Next franchise'))
            else:
                blocks.append(R.paragraph(line))
        flush_commands()
        return blocks or [R.paragraph('Draft updated.')]
    except Exception:
        logger.exception('Draft rich text could not be built; using HTML')
        return None


class Card:
    def __init__(self,title,subtitle=None):
        self.blocks = [R.heading(title,size=2)]
        self.parts = [f'<b>{escape(title)}</b>']
        if subtitle:
            self.text(R.italic(subtitle))

    def text(self,value):
        self.blocks.append(R.paragraph(value))
        self.parts.append(inline_html(value))
        return self

    def section(self,title):
        self.blocks.append(R.heading(title,size=3))
        self.parts.append(f'<b>{escape(title)}</b>')
        return self

    def hero(self,value,caption=None):
        self.blocks.append(R.pullquote(value,caption=caption))
        content = inline_html(R.bold(value))
        if caption:
            content += '\n' + inline_html(R.italic(caption))
        self.parts.append('<blockquote>'+content+'</blockquote>')
        return self

    def facts(self,rows):
        self.blocks.append(R.table([[R.cell(label),R.cell(value,align='right')] for label,value in rows],compact=True))
        lines = [f'<b>{escape(label)}</b>: {inline_html(value)}' for label,value in rows]
        self.parts.append('<blockquote>'+'\n'.join(lines)+'</blockquote>')
        return self

    def table(self,headers,rows,*,align_right=(),caption=None,details=None,opened=False):
        cells = [[R.cell(header,header=True) for header in headers]]
        cells += [[R.cell(value,align='right' if i in align_right else 'left') for i,value in enumerate(row)] for row in rows]
        block = R.table(cells,bordered=True,striped=True,compact=True,caption=caption)
        self.blocks.append(R.details(R.bold(details),[block],is_open=opened) if details else block)
        if caption or details:
            self.parts.append('<b>'+escape(details or caption)+'</b>')
        self.parts.append('<i>'+' · '.join(escape(h) for h in headers)+'</i>')
        # Each row is independently balanced, so long fallbacks split safely.
        for row in rows:
            self.parts.append(' · '.join(inline_html(value).replace('\n',' / ') for value in row))
        return self

    def footer(self,value):
        self.blocks.append(R.footer(value))
        self.parts.append(inline_html(R.italic(value)))
        return self

    def append(self,other):
        self.blocks.extend(other.blocks)
        self.parts.extend(other.parts)
        return self

    def pair(self):
        return self.blocks,'\n\n'.join(self.parts)


def _services():
    from services import auction_service as A, franchise_draft as D
    return A,D


def _money(season,value):
    _,D = _services()
    return D._money(season,int(value or 0))


def _clock_rows(config,season):
    _,D=_services()
    seconds=D.timer_seconds(config)
    rows = [('Pick time',f'{seconds//60} minutes' if seconds and seconds%60==0 else f'{seconds} seconds' if seconds else 'Timer off'),
            ('On timeout',D.timeout_policy_for(config))]
    if config.pick_deadline_at:
        rows.append(('Deadline (UTC)',config.pick_deadline_at.strftime('%d %b · %H:%M:%S')))
    elif season.status == 'paused' and config.paused_seconds_left is not None:
        rows.append(('Time preserved',f'{config.paused_seconds_left // 60}m {config.paused_seconds_left % 60}s'))
    return rows


def next_turn(session,season):
    _,D = _services()
    config = D.get_draft(session,season)
    info = D.turn_info(session,season)
    team = info['team']
    if team is None:
        if season.status != 'completed':
            return Card('⚙️ Prepare your draft').text('Add franchises, assign owners and set player prices before starting.').footer('/dteam · /dprices · /dorder · /dstart')
        return Card('🏁 Draft complete').text('All draft picks are complete. Review squads, then publish or add rounds.').footer('/dpublish · /dsquad')
    if season.status != 'live':
        label = '⏸ Draft paused' if season.status == 'paused' else '⚙️ Ready for setup'
        card = Card(label).text(['Next team: ',R.bold(team.name[:60])])
    else:
        card = Card('⏭ On the clock').hero(team.name[:60],caption='Skipped-pick round' if info['catch_up'] else f'Round {info["round_no"]}')
        owners = D.owner_ids(team)
        nodes = []
        for index,identity in enumerate(owners[:12]):
            if nodes:nodes.append('  ·  ')
            name = (team.owner_name or 'Owner')[:40] if identity == team.owner_tg_id else 'Co-owner'
            nodes.append(R.mention(name,identity))
        if nodes:card.text(nodes)
        if len(owners)>12:card.footer(f'+{len(owners)-12} co-owners can also pick.')
    card.facts(_clock_rows(config,season))
    return card.footer('/dpick Player Name · /dskip' if season.status == 'live' else '/dstart or /dresume when ready')


def board(session,season):
    A,D = _services()
    config = D.get_draft(session,season)
    teams = D._ordered_teams(session,season)
    total = config.rounds * len(teams)
    signed = sum(int(t.squad_size or 0) for t in teams)
    queue = D.skipped_turns(session,season)
    round_no = min(config.rounds,config.cursor // max(1,len(teams)) + 1)
    phase = 'Skipped-pick round' if config.cursor >= total and queue else f'Round {round_no}/{config.rounds}'
    status = {'live':'🟢 Live','paused':'⏸ Paused','setup':'⚙️ Setup','completed':'🏁 Completed','cancelled':'🚫 Cancelled'}.get(season.status,season.status)
    filled = min(10,int(signed / total * 10)) if total else 0
    card = Card('🏏 CMU Franchise Draft 2.0',season.name[:70])
    upcoming=None
    grid=D.schedule(session,season)
    future=grid[config.cursor+1:] if config.cursor<len(grid) else []
    future += [session.get(__import__('models').AuctionFranchise,e['franchise_id']) for e in (queue[1:] if config.cursor>=len(grid) else queue)]
    upcoming=next((t for t in future if t is not None and t.squad_size<config.rounds),None)
    card.facts([('Status',status),('Round',phase),('Signed',R.bold(f'{signed}/{total}')),
                ('Confirmed draft selections',str(sum(int(t.draft_picks_used or 0) for t in teams))),
                ('Retained contracts',str(sum(int(t.retained_count or 0) for t in teams))),
                ('Pick / scheduled turns',f'{min(config.cursor+1,total)}/{total}'),
                ('Pending squad places',str(max(0,total-signed))),
                ('Available players',str(session.query(__import__('models').AuctionLot).filter_by(season_id=season.id,status=A.LOT_QUEUED).count())),
                ('Franchises',str(len(teams))),('Next franchise',upcoming.name[:45] if upcoming else '—'),
                ('Deferred picks',str(len(queue)))])
    card.text([R.code('▰'*filled+'▱'*(10-filled)),f'  {signed} players signed'])
    tail = next_turn(session,season) if season.status in ('live','paused','completed','setup') else Card('🚫 Draft cancelled').text('Squads and purse history are preserved.')
    rows = []
    # The pinned fallback must remain one message even with long names/IDs.
    base_size = len(card.pair()[1])+len(tail.pair()[1])+200
    for team in teams:
        row = [R.bold(team.name[:45]),f'{team.squad_size}/{config.rounds}',R.bold(_money(season,team.purse_remaining_lakh))]
        cost = len(' · '.join(inline_html(x) for x in row))+2
        if rows and (len(rows)>=8 or base_size+cost>3500):break
        rows.append(row);base_size+=cost
    if rows:card.table(['Franchise','Squad','Purse left'],rows,align_right=(1,2),caption='Teams & purses')
    if len(rows)<len(teams):card.footer(f'{len(teams)-len(rows)} more franchises · /dsquad for every team')
    card.footer(f'Squad size: {season.min_squad_size}–{config.rounds} · No overseas limit · /dskips')
    card.append(tail)
    return card.pair()


def confirmation(session,season,quote,lot,team):
    _,D = _services()
    config = D.get_draft(session,season)
    card = Card('✍️ Confirm signing',team.name)
    card.hero(lot.name,caption=f'{lot.category} · {lot.rating} OVR')
    card.facts([('Player database ID',str(lot.player_id)),('Pool ID',f'#{lot.id}'),
                ('Version / country',f'{lot.version or "Base"} · {lot.country}'),
                ('Batting / bowling',f'{lot.bat_rating} / {lot.bowl_rating}'),
                ('Current purse',_money(season,team.purse_remaining_lakh)),
                ('Signing price',R.bold(_money(season,quote.price_lakh))),
                ('Purse after signing',R.bold(_money(season,team.purse_remaining_lakh-quote.price_lakh))),
                ('Squad after signing',f'{team.squad_size+1}/{config.rounds}')])
    card.facts(_clock_rows(config,season))
    card.footer('✅ Sign to confirm · ❌ Cancel to keep your purse. '+
                ('The pick timer keeps running.' if config.pick_minutes else 'This confirmation expires after ten minutes.'))
    return card.pair()


def player_choices(session,season,lots):
    _,D=_services()
    card=Card('🔎 Choose a player / version',season.name)
    card.text(f'{len(lots)} available cards match that name. Choose the exact player below; nothing has been signed.')
    rows=[]
    for lot in lots[:12]:
        try:price=_money(season,D.price_for(session,season,lot))
        except D.AuctionError:price='Price required'
        rows.append([f'#{lot.id} {lot.name}',lot.version or 'Base',str(lot.rating),lot.category,price])
    card.table(['Player','Version','OVR','Category','Price'],rows)
    if len(lots)>12:card.footer(f'Showing 12 of {len(lots)} matches. Type more of the name or use /dsearch to narrow the choice.')
    card.footer('Only your active franchise owner or co-owner can select. A signing still requires confirmation.')
    return card.pair()


def receipt(session,season,lot,team,*,phase=None):
    _,D = _services()
    config = D.get_draft(session,season)
    card = Card('✅ Signing confirmed · CMU',phase or season.name)
    card.hero(lot.name,caption=f'{team.name} · {lot.rating} OVR · {lot.category}')
    card.text(f'{lot.country} · {lot.version or "Base"} · Player ID {lot.player_id} · Pool #{lot.id}')
    card.facts([('Signed for',R.bold(_money(season,lot.sold_price_lakh))),
                ('Purse left',R.bold(_money(season,team.purse_remaining_lakh))),
                ('Squad',f'{team.squad_size}/{config.rounds}')])
    card.append(next_turn(session,season))
    return card.pair()


def event_card(session,season,kind,text,*,lot=None,team=None,metadata=None):
    metadata = metadata or {}
    try:
        if kind == 'fd_picked' and lot is not None and team is not None:
            phase = 'Skipped-pick round' if metadata.get('catch_up') else f'Round {metadata.get("round_no", "")}'
            if metadata.get('automatic'):phase+=' · Automatic selection (approved timeout policy)'
            if metadata.get('admin_override'):phase+=' · Administrator signing: '+str(metadata.get('reason',''))
            return receipt(session,season,lot,team,phase=phase)
        if kind in ('fd_started','fd_resumed'):
            return board(session,season)
        if kind == 'fd_completed':
            card=Card('🏆 CMU Franchise Draft completed',season.name)
            card.text('All final squads satisfy the configured category, rating and purse rules.')
            blocks,text=purses(session,season)
            card.blocks.extend(blocks);card.parts.append(text)
            card.footer('/dsquad · /dhistory · /dpublish to continue the tournament season')
            return card.pair()
        if kind == 'fd_skipped' and team is not None:
            _,D = _services()
            timeout = bool(metadata.get('timeout'))
            card = Card('⏱ Pick time expired' if timeout else '⏭ Turn skipped')
            card.hero(team.name,caption='Team ran out of pick time' if timeout else 'Pick deferred to the catch-up round')
            card.facts([('Original round',str(metadata.get('round_no','—'))),
                        ('Purse deducted',_money(season,0)),
                        ('Deferred picks',str(len(D.skipped_turns(session,season))))])
            card.text('Your pick joins the back of the skipped-pick queue. No player is signed.')
            if metadata.get('admin_override'):card.text('🛡 Administrator skip · '+str(metadata.get('reason','')))
            card.append(next_turn(session,season))
            return card.pair()
    except Exception:
        logger.exception('Draft event card failed; preserving its original message')
    return blocks_for_html(text),text


def pool(session,season,page=1):
    A,D = _services()
    lots = D._available(session,season)
    pages = max(1,(len(lots)+11)//12)
    page = max(1,min(pages,int(page)))
    card = Card('🔎 Available draft players',f'{season.name} · Page {page}/{pages}')
    card.text(f'{len(lots)} players available · fixed signing prices')
    rows = []
    for lot in lots[(page-1)*12:page*12]:
        try:price = R.bold(_money(season,D.price_for(session,season,lot)))
        except A.AuctionError:price = '⚠️ Price needed'
        rows.append([R.code(f'#{lot.id} / p{lot.player_id}'),[R.bold(lot.name),'\n',R.italic(f'{lot.version or "Base"} · {lot.category} · {lot.country}')],
                     f'{lot.rating} / {lot.bat_rating} / {lot.bowl_rating}',price])
    if rows:card.table(['Pool / player ID','Player / version / role','OVR / BAT / BOWL','Price'],rows,align_right=(2,3))
    else:card.hero('No available players',caption='Ask a Draft Admin to add players before resuming.')
    card.footer('Pick publicly in the bound draft group: /dpick Joe Root · /dpick #123')
    return card.pair()


def squad(session,season,team):
    A,D = _services()
    config = D.get_draft(session,season)
    if team.season_id != season.id:raise A.AuctionError('That franchise belongs to another draft.')
    lots = A.squad(session,team.id)
    card = Card('👥 Draft squad',team.name)
    card.facts([('Players',R.bold(f'{team.squad_size}/{config.rounds}')),
                ('Retained / drafted',f'{team.retained_count or 0} / {team.draft_picks_used or 0}'),
                ('Open acquisition slots',str(max(0,config.rounds-team.squad_size))),
                ('Purse left',R.bold(_money(season,team.purse_remaining_lakh))),
                ('Spent',_money(season,team.purse_total_lakh-team.purse_remaining_lakh))])
    if not lots:card.hero('No players signed yet',caption='Your confirmed signings will appear here.')
    for start in range(0,len(lots),12):
        rows = [[str(i),[R.bold(lot.name),'\n',R.italic(f'{lot.version or "Base"} · {lot.category} · {lot.country} · p{lot.player_id}')],str(lot.rating),_money(season,lot.sold_price_lakh)]
                for i,lot in enumerate(lots[start:start+12],start+1)]
        card.table(['#','Player / role','OVR','Signed for'],rows,align_right=(0,2,3),
                   details=f'Players {start+1}–{min(len(lots),start+12)}',opened=start==0)
    counts,minimums,maximums = A.role_counts(session,team.id),A.role_minimums(season),A.role_maximums(season)
    card.table(['Role','Have','Min','Max'],[[role,str(counts.get(role,0)),str(minimums.get(role,0)),
               str(maximums[role]) if role in maximums else '—'] for role in A.SQUAD_ROLES],
               align_right=(1,2,3),caption='Squad balance')
    if A.rating_rules(season):
        ratings = A.squad_ratings(session,team.id)
        rows = [[f'≤{r["max_rating"]}',str(sum(x<=r['max_rating'] for x in ratings)),str(r['min_players'])] for r in A.rating_rules(season)]
        card.table(['Rating','Have','Minimum'],rows,align_right=(1,2),caption='Rating requirements')
    card.footer('No overseas limit · /dpurse for all team purses')
    return card.pair()


def purses(session,season):
    A,D = _services()
    config = D.get_draft(session,season)
    card = Card('💰 Draft purses',season.name)
    rows = [[R.bold(t.name),_money(season,t.purse_total_lakh-t.purse_remaining_lakh),
             R.bold(_money(season,t.purse_remaining_lakh)),f'{t.squad_size}/{config.rounds}'] for t in A.franchises(session,season.id)]
    if rows:card.table(['Franchise','Spent','Purse left','Squad'],rows,align_right=(1,2,3))
    else:card.text('No franchises yet. A Draft Admin can add teams with /dteam.')
    card.footer('Only confirmed signings spend purse. Skips cost nothing.')
    return card.pair()


def rules(session,season):
    A,D = _services()
    config = D.get_draft(session,season)
    card = Card('📜 Draft rulebook',season.name)
    card.facts([('Minimum squad',str(season.min_squad_size)),('Rounds / maximum squad',str(config.rounds)),('Pick authority','Current team owner or co-owner'),
                ('Overseas players','No limit'),('Maximum retained contracts',str(D._loads(config.settings_json,{}).get('retention_max',0)))]+_clock_rows(config,season))
    minimums,maximums = A.role_minimums(season),A.role_maximums(season)
    card.table(['Role','Minimum','Maximum'],[[role,str(minimums.get(role,0)),str(maximums[role]) if role in maximums else 'No cap']
               for role in A.SQUAD_ROLES],align_right=(1,2),caption='Squad composition')
    rows = [[f'≤{r["max_rating"]}',f'At least {r["min_players"]}'] for r in A.rating_rules(season)]
    if rows:card.table(['Rating','Players required'],rows,align_right=(1,),caption='Rating rules')
    else:card.text('Rating rules: no extra requirement.')
    card.section('🔄 Turn order & catch-up picks')
    card.text('The first team moves to the end each round. Voluntary skips and timeouts join a catch-up round in skip order after scheduled rounds.')
    card.text('Approved retained contracts occupy squad places. Teams with a full squad receive no further acquisition turn.')
    card.text('Custom player prices override rating prices. Each signing requires confirmation; the timer continues while the confirmation is open.')
    card.footer('Draft Admins: /drolemin · /drolemax · /dratingrule · /dtimer · /dprices · /dcustom')
    return card.pair()


def order(session,season,page=None):
    _,D = _services()
    config = D.get_draft(session,season)
    teams = D._ordered_teams(session,season)
    current = min(config.rounds,config.cursor//max(1,len(teams))+1) if page is None else max(1,min(config.rounds,int(page)))
    card = Card('🔄 Rotating draft order',f'{season.name} · {config.rounds} rounds')
    for number in range(current,min(config.rounds,current+2)+1):
        rows = [[str((number-1)*len(teams)+i),R.bold(t.name),R.code(f'#{t.id}')] for i,t in enumerate(D.round_order(session,season,number),1)]
        if rows:card.table(['Pick','Franchise','Team ID'],rows,align_right=(0,),details=f'Round {number}',opened=number==current)
    if not teams:card.text('No franchises yet. Add teams with /dteam before setting the order.')
    card.footer('Each new round moves the first team to the end. /dskips shows the catch-up queue.')
    return card.pair()


def preview_action(session,season,action):
    data=json.loads(action.preview_json)
    before,after=data['before'],data['after']
    title={'adminpick':'🛡 Confirm administrator signing','adminskip':'⏭ Confirm administrator skip','removeteam':'🚪 Confirm franchise removal'}.get(action.kind,'🛡 Confirm draft configuration')
    card=Card(title,f'{season.name} · {action.kind}')
    card.text('Nothing has been applied. This preview expires after ten minutes and checks your current role again.')
    card.facts([('Rounds',f'{before["rounds"]} → {after["rounds"]}'),
                ('Minimum squad',f'{before.get("min_squad_size",before["rounds"])} → {after.get("min_squad_size",after["rounds"])}'),
                ('Maximum squad',f'{before.get("max_squad_size",before["rounds"])} → {after.get("max_squad_size",after["rounds"])}'),
                ('Status',f'{before["status"]} → {after["status"]}'),
                ('Eligible players',f'{before["eligible"]} → {after["eligible"]}'),
                ('Reason',action.reason or 'Setup configuration')])
    if action.kind=='finish':card.text('Every franchise meets its minimum squad size and final rules. Confirm to close all remaining optional picks; squads and purses stay unchanged.')
    if action.kind in ('adminpick','adminskip'):
        payload=json.loads(action.payload_json)
        team=session.get(__import__('models').AuctionFranchise,int(payload['team_id']))
        card.section('🛡 Administrator signing' if action.kind=='adminpick' else '⏭ Administrator skip')
        card.facts([('Franchise',team.name),('Administrator',str(action.actor_tg_id))])
        if action.kind=='adminpick':
            A,D=_services()
            lot=D.find_player(session,season,payload['player'],partial=True)
            card.facts([('Player',f'{lot.name} · {lot.version or "Base"} · #{lot.id}'),
                        ('Overall / category',f'{lot.rating} · {lot.category}'),
                        ('Signing price',_money(season,D.price_for(session,season,lot)))])
        else:card.text('This turn moves to the back of the catch-up queue. No purse is deducted. A paused draft stays paused.')
    if action.kind=='start':card.facts([('Opening franchise',after['current'] or 'All squad places occupied'),
        ('Pick time','Timer off' if not after['timer_seconds'] else str(after['timer_seconds'])+' seconds'),
        ('On expiry',after['settings'].get('timeout_policy','skip'))])
    if action.kind in ('team','removeteam'):
        card.table(['Franchise','Owner / ID','Co-owners','Managers'],[[t['name'],f'{t["owner_name"] or "Owner"} · {t["owner"] or "Missing"}',
             ', '.join(map(str,t['co_owners'])) or 'None',', '.join(map(str,t['managers'])) or 'None'] for t in after['teams']])
        if action.kind=='removeteam':
            removal=data.get('removal') or {}
            players=removal.get('players',[])
            card.facts([('Removing franchise',removal.get('name','—')),('Players released',str(len(players))),
                        ('Remaining purse to share',_money(season,removal.get('remaining_purse',0))),
                        ('On the clock before',before['current'] or '—'),('On the clock after',after['current'] or 'Complete')])
            card.text('The unspent purse is divided equally among the remaining franchises. Any odd lakh are allocated one each in franchise display order. Player prices and custom overrides remain in place.')
            if players:card.table(['Released player / pool ID','Version','OVR'],
                [[f'#{p["id"]} {p["name"]}',p['version'],str(p['rating'])] for p in players[:15]])
            if len(players)>15:card.footer(f'{len(players)-15} more players will also return to the pool.')
            old_purses={t['id']:t['purse'] for t in before['teams']}
            card.table(['Remaining franchise','Purse added','Purse after'],
                [[t['name'],_money(season,t['purse']-old_purses[t['id']]),_money(season,t['purse'])] for t in after['teams']])
            card.text('Surviving teams keep their pending selection order and skipped-pick queue. All pending pick confirmations expire. A live draft continues; a paused draft stays paused unless every remaining squad is complete. Undo cannot cross this removal; later picks can still be undone normally.')
    if action.kind=='order':card.text('Initial order after confirmation: '+' → '.join(after['order']))
    if action.kind=='rules':
        card.table(['Role','Minimum','Maximum'],[[role,str(after['minimums'].get(role,0)),str(after['maximums'].get(role,'No cap'))] for role in _services()[0].SQUAD_ROLES])
        for rule in after['rating_rules']:card.text(f'Rating ≤{rule["max_rating"]}: at least {rule["min_players"]} players')
    if action.kind=='opening':card.facts([('Opening purse',_money(season,before['opening_purse'])+' → '+_money(season,after['opening_purse']))])
    if action.kind=='bind':card.facts([('Telegram group',str(before['chat_id'] or 'Not bound')+' → '+str(after['chat_id']))])
    if action.kind in ('publish','continue'):card.text('All final squads are validated. '+('Publish and lock squads for the season.' if action.kind=='publish' else 'Prepare the next tournament from these finalized squads.'))
    if action.kind in ('draftadmin_add','draftadmin_remove'):
        settings=json.loads(action.payload_json)
        card.text(('Appoint Draft Admin: ' if action.kind=='draftadmin_add' else 'Remove Draft Admin: ')+str(settings['telegram_id'])+' '+str(settings.get('name','')))
    imported=data.get('team_import')
    if imported:
        source=imported['source']
        card.section('🏟 Import from previous '+source['kind'])
        card.text(f'{source["name"]} · #{source["id"]} · {imported["added"]} new franchises')
        card.table(['Franchise / action','Owner / ID','Co-owners','Managers / other details'],
            [[r['name']+' · '+r['operation'],f'{r["final"]["owner_name"] or "Owner"} · {r["final"]["owner_tg_id"] or "Missing"}',
              ', '.join(map(str,json.loads(r['final']['co_owner_ids_json'] or '[]'))) or 'None',
              ' · '.join(filter(None,['Managers: '+', '.join(map(str,json.loads(r['final']['team_manager_ids_json'] or '[]'))),r['final']['short_name'],r['final']['city'],r['final']['logo_url'],*r['warnings']])) or '—'] for r in imported['rows']])
        card.text('Existing assigned roles and budgets are preserved. New teams use this draft’s starting purse. Squads require the configured retention or draft signings.')
    delta=after['eligible']-before['eligible']
    if delta>0:card.text(f'{delta} players added after confirmation.')
    if before['settings']!=after['settings'] and not imported:
        options=after['settings']
        seconds=options.get('pick_seconds')
        rows=[('Timeout handling',{'pause':'Pause and wait','admin':'Pause for administrator','skip':'Defer to catch-up queue','auto':'Highest-rated eligible, affordable player'}.get(options.get('timeout_policy'),'Pause and wait')),
              ('Maximum retained contracts',str(options.get('retention_max',0)))]
        if seconds is not None:rows.insert(0,('Pick time','Timer off' if not seconds else f'{seconds} seconds'))
        card.facts(rows)
    rows=[[t['name'],str(t['squad']),str(t['slots']),_money(season,t['purse'])] for t in after['teams']]
    if rows:card.table(['Franchise','Squad','Open slots','Purse after'],rows)
    prices=data.get('prices',[])
    changed=[p for p in prices if p['previous_price']!=p['price']]
    if action.kind=='prices':
        counts={}
        for p in prices:counts[str(p['rating'])]=counts.get(str(p['rating']),0)+1
        card.table(['Configured rating','Price','Eligible players'],
            [[rating,_money(season,value),str(counts.get(rating,0))]
             for rating,value in sorted(after['rating_prices'].items(),key=lambda pair:int(pair[0]),reverse=True)])
    if prices:
        card.text(f'{len(prices)} eligible players priced; {len(changed)} prices change. Custom overrides take priority.')
        shown=[p for p in prices if p['id'] in data['requested_ids']] if action.kind=='custom' else prices[:20]
        card.table(['Player / version','Previous','Rating price','Final price'],
            [[f'#{p["id"]} {p["name"]} · {p["version"] or "Base"}',
              _money(season,p['previous_price']) if p['previous_price'] is not None else 'Missing',
              _money(season,p['rating_price']) if p['rating_price'] is not None else 'Missing',
              _money(season,p['price'])] for p in shown])
        if action.kind!='custom' and len(prices)>20:card.footer(f'{len(prices)-20} more eligible players use the same price rules.')
    return card.pair()


def history(session,season,page=1):
    from models import AuctionEvent
    page=max(1,int(page))
    events=(session.query(AuctionEvent).filter(AuctionEvent.season_id==season.id)
            .order_by(AuctionEvent.id.desc()).offset((page-1)*12).limit(12).all())
    card=Card('📜 CMU Draft history',f'{season.name} · Page {page}')
    rows=[]
    for event in events:
        try:detail=json.loads(event.detail_json or '{}')
        except (TypeError,ValueError):detail={}
        if not isinstance(detail,dict):detail={}
        player=detail.get('player_snapshot') or {}
        team=detail.get('franchise_snapshot') or {}
        if player:
            description=f'{team.get("name", "")} · {player["name"]} · {player.get("version") or "Base"} · p{player["id"]}'
            if player.get('price_lakh') is not None:description+=' · '+_money(season,player['price_lakh'])
        else:
            description='\n'.join(plain(line) for line in inline_lines(detail.get('html') or event.headline))[:700]
        rows.append([str(event.id),[R.bold(event.headline),'\n',description],'Website admin' if event.by_tg_id and event.by_tg_id<0 else str(event.by_tg_id or 'System')])
    if rows:card.table(['Event','Action / player / price','Actor'],rows)
    else:card.text('No events on this page.')
    card.footer('Signings, skips, retained contracts and administrative corrections remain in the audit log after undo.')
    return card.pair()


def readiness(session,season,check=None):
    from services.franchise_draft_admin import readiness as inspect
    data=check if check is not None else inspect(session,season)
    card=Card('✅ Ready to start' if data['ready'] else '⚠️ Draft setup needs attention',season.name)
    card.facts([('Rounds',str(data['rounds'])),('Franchises',str(len(data['teams']))),('Available players',str(data['eligible']))])
    for error in data['errors'][:30]:card.text('• '+error)
    if data['ready']:card.text('Owners, prices, budgets, pool supply and category/rating requirements passed the pre-draft check.')
    card.footer('/dstart opens a confirmation preview · /draftcommands lists setup controls')
    return card.pair()


def skips(session,season,page=1):
    _,D = _services()
    queue = D.skipped_turns(session,season)
    teams = {team.id:team for team in D._ordered_teams(session,season)}
    pages = max(1,(len(queue)+11)//12)
    page = max(1,min(pages,int(page)))
    card = Card('⏭ Skipped-pick round',f'{season.name} · Page {page}/{pages}')
    card.text(f'{len(queue)} deferred picks · played after the scheduled rounds, in skip order.')
    rows = [[str(i),R.bold(teams[e['franchise_id']].name if e['franchise_id'] in teams else 'Missing franchise'),
             str(e['round_no']),f'Requeued {e["retries"]}×' if e.get('retries') else 'Queued']
            for i,e in enumerate(queue[(page-1)*12:page*12],(page-1)*12+1)]
    if rows:card.table(['Order','Franchise','From round','Status'],rows,align_right=(0,2))
    else:card.hero('No skipped picks',caption='Every team keeps its scheduled turns.')
    card.footer('Skipping again moves that pick to the back. No purse is deducted until a player is signed.')
    return card.pair()


def prices(session,season,*,custom=False):
    A,D = _services()
    config = D.get_draft(session,season)
    prices = json.loads((config.custom_prices_json if custom else config.rating_prices_json) or '{}')
    lot_names = {str(lot.id):lot.name for lot in A.lots(session,season.id)} if custom else {}
    card = Card('💎 Custom player prices' if custom else '💰 Rating prices',season.name)
    entries = sorted(prices.items(),key=lambda item:int(item[0]),reverse=not custom)
    if entries:
        rows = [[R.bold(lot_names.get(key,key) if custom else key),R.bold(_money(season,value))] for key,value in entries]
        card.table(['Player' if custom else 'Rating','Signing price'],rows,align_right=(1,))
    else:card.hero('No prices configured yet',caption='Set prices before starting the draft.')
    card.text('Custom player prices override rating prices. Existing signings keep their agreed price.')
    card.footer('After launch, pause and use /dcorrect with a reason to correct future prices. Signed contracts keep their original price.')
    card.footer('Update with /dcustom\nJoe Root:25.5cr' if custom else 'Update with /dprices\n96:23cr\n95:21cr')
    return card.pair()


def team_directory(session,season):
    A,D = _services()
    config = D.get_draft(session,season)
    card = Card('👥 Franchise squads',season.name)
    rows = [[R.bold(t.name),f'{t.squad_size}/{config.rounds}',R.bold(_money(season,t.purse_remaining_lakh))] for t in A.franchises(session,season.id)]
    if rows:card.table(['Franchise','Squad','Purse left'],rows,align_right=(1,2))
    else:card.text('No franchises added yet. A Draft Admin can add them with /dteam.')
    card.footer('Choose a franchise below for players, spending and rule progress.')
    return card.pair()


def admin_directory(session):
    _,D = _services()
    card = Card('🛡 Draft Admins')
    rows = [[R.mention(row.name or 'Draft Admin',row.tg_id),R.code(str(row.tg_id))] for row in D.draft_admins(session)]
    if rows:card.table(['Draft Admin','Telegram ID'],rows)
    else:card.text('No appointed Draft Admins yet.')
    card.text('Bot Owners and Bot Admins also have draft administration.')
    card.footer('/dadminadd TelegramID · /dadminremove TelegramID · Bot Owners/Admins only')
    return card.pair()
