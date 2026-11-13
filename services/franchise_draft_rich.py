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
    A,_ = _services()
    return A.render_money(int(value or 0),season.currency_label or '₹')


def _clock_rows(config,season):
    rows = [('Pick time',f'{config.pick_minutes} minutes' if config.pick_minutes else 'Timer off')]
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
    card = Card('🏏 Franchise Draft',season.name[:70])
    card.facts([('Status',status),('Round',phase),('Signed',R.bold(f'{signed}/{total}')),
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
    card.footer(f'Squad limit: {config.rounds} · No overseas limit · /dskips')
    card.append(tail)
    return card.pair()


def confirmation(session,season,quote,lot,team):
    _,D = _services()
    config = D.get_draft(session,season)
    card = Card('✍️ Confirm signing',team.name)
    card.hero(lot.name,caption=f'{lot.category} · {lot.rating} OVR')
    card.facts([('Signing price',R.bold(_money(season,quote.price_lakh))),
                ('Purse after signing',R.bold(_money(season,team.purse_remaining_lakh-quote.price_lakh))),
                ('Squad after signing',f'{team.squad_size+1}/{config.rounds}')])
    card.facts(_clock_rows(config,season))
    card.footer('✅ Sign to confirm · ❌ Cancel to keep your purse. '+
                ('The pick timer keeps running.' if config.pick_minutes else 'This confirmation expires after ten minutes.'))
    return card.pair()


def receipt(session,season,lot,team,*,phase=None):
    _,D = _services()
    config = D.get_draft(session,season)
    card = Card('✅ Signing confirmed',phase)
    card.hero(lot.name,caption=f'{team.name} · {lot.rating} OVR · {lot.category}')
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
            return receipt(session,season,lot,team,phase=phase)
        if kind == 'fd_skipped' and team is not None:
            _,D = _services()
            timeout = bool(metadata.get('timeout'))
            card = Card('⏱ Pick time expired' if timeout else '⏭ Turn skipped')
            card.hero(team.name,caption='Team ran out of pick time' if timeout else 'Pick deferred to the catch-up round')
            card.facts([('Original round',str(metadata.get('round_no','—'))),
                        ('Purse deducted',_money(season,0)),
                        ('Deferred picks',str(len(D.skipped_turns(session,season))))])
            card.text('Your pick joins the back of the skipped-pick queue. No player is signed.')
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
        rows.append([R.code(f'#{lot.id}'),[R.bold(lot.name), '\n',R.italic(lot.category)],str(lot.rating),price])
    if rows:card.table(['Pool ID','Player / role','OVR','Price'],rows,align_right=(2,3))
    else:card.hero('No available players',caption='Ask a Draft Admin to add players before resuming.')
    card.footer('Pick by name or exact pool ID: /dpick Joe Root · /dpick #123')
    return card.pair()


def squad(session,season,team):
    A,D = _services()
    config = D.get_draft(session,season)
    if team.season_id != season.id:raise A.AuctionError('That franchise belongs to another draft.')
    lots = A.squad(session,team.id)
    card = Card('👥 Draft squad',team.name)
    card.facts([('Players',R.bold(f'{team.squad_size}/{config.rounds}')),
                ('Purse left',R.bold(_money(season,team.purse_remaining_lakh))),
                ('Spent',_money(season,team.purse_total_lakh-team.purse_remaining_lakh))])
    if not lots:card.hero('No players signed yet',caption='Your confirmed signings will appear here.')
    for start in range(0,len(lots),12):
        rows = [[str(i),[R.bold(lot.name),'\n',R.italic(lot.category)],str(lot.rating),_money(season,lot.sold_price_lakh)]
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
    card.facts([('Rounds / maximum squad',str(config.rounds)),('Pick authority','Current team owner or co-owner'),
                ('Overseas players','No limit')]+_clock_rows(config,season))
    minimums,maximums = A.role_minimums(season),A.role_maximums(season)
    card.table(['Role','Minimum','Maximum'],[[role,str(minimums.get(role,0)),str(maximums[role]) if role in maximums else 'No cap']
               for role in A.SQUAD_ROLES],align_right=(1,2),caption='Squad composition')
    rows = [[f'≤{r["max_rating"]}',f'At least {r["min_players"]}'] for r in A.rating_rules(season)]
    if rows:card.table(['Rating','Players required'],rows,align_right=(1,),caption='Rating rules')
    else:card.text('Rating rules: no extra requirement.')
    card.section('🔄 Turn order & catch-up picks')
    card.text('The first team moves to the end each round. Voluntary skips and timeouts join a catch-up round in skip order after scheduled rounds.')
    card.text('Custom player prices override rating prices. Each signing requires confirmation; the timer continues while the confirmation is open.')
    card.footer('Draft Admins: /drolemin · /drolemax · /dratingrule · /dtimer · /dprices · /dcustom')
    return card.pair()


def order(session,season):
    _,D = _services()
    config = D.get_draft(session,season)
    teams = D._ordered_teams(session,season)
    current = min(config.rounds,config.cursor//max(1,len(teams))+1)
    card = Card('🔄 Rotating draft order',f'{season.name} · {config.rounds} rounds')
    for number in range(current,min(config.rounds,current+2)+1):
        rows = [[str(i),R.bold(t.name),R.code(f'#{t.id}')] for i,t in enumerate(D.round_order(session,season,number),1)]
        if rows:card.table(['Pick','Franchise','Team ID'],rows,align_right=(0,),details=f'Round {number}',opened=number==current)
    if not teams:card.text('No franchises yet. Add teams with /dteam before setting the order.')
    card.footer('Each new round moves the first team to the end. /dskips shows the catch-up queue.')
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
