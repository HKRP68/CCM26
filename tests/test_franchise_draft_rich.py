"""Draft cards keep prices, permissions and retry snapshots in both formats."""
import asyncio
from datetime import timedelta
from html.parser import HTMLParser
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from services import franchise_draft_rich as DR, rich_message as R
from test_franchise_draft_handlers import (ADMIN, CHAT, COOWNER, OTHER, OWNER,
                                          context, live, room, update)  # noqa: F401


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def flatten(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ' '.join(flatten(child) for child in value)
    if isinstance(value, dict):
        return ' '.join(flatten(value.get(key, '')) for key in ('text','summary','caption','cells','blocks'))
    return ''


class BalancedHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack=[]

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop()==tag


def assert_card(pair):
    blocks,text=pair
    assert blocks[0]['type']=='heading'
    assert json.loads(json.dumps(blocks))==blocks
    for node in nodes(blocks):
        if node.get('type')=='table':
            for row in node['cells']:
                for cell in row:
                    assert cell['align'] in ('left','center','right')
                    assert cell['valign'] in ('top','middle','bottom')
    for part in R.html_parts(text):
        assert len(part)<=R.CHUNK_LIMIT
        parser=BalancedHTML()
        parser.feed(part)
        assert not parser.stack, 'A split fallback must keep all formatting balanced'
    return flatten(blocks),text


@pytest.mark.parametrize('view,raw',[
    ('draft',''),('dpool','1'),('dsquad','Team One'),('dorder',''),
    ('dskips',''),('drules',''),('dpurse',''),('dprices',''),('dcustom',''),('dadmins',''),
])
def test_draft_views_use_structured_cards_with_matching_values(room,view,raw):
    packet=room.H.execute(room.db,ADMIN,CHAT,room.season.id,view,raw)
    rich,html=assert_card((packet['blocks'],packet['text']))
    for label in ('Team One','Team Two','Joe Root','₹23','₹100','96'):
        if label in html:
            assert label in rich
    assert any(node.get('type') in ('table','pullquote') for node in nodes(packet['blocks'])) or view=='dadmins'


def test_live_board_highlights_turn_deadline_and_both_authorized_owners(room):
    live(room)
    blocks,text=DR.board(room.db,room.season)
    rich,html=assert_card((blocks,text))
    assert 'On the clock' in rich and '10 minutes' in rich
    assert 'Deadline (UTC)' in rich
    assert any(node.get('type')=='pullquote' and node.get('text')=='Team One' for node in nodes(blocks))
    for identity in (OWNER,COOWNER):
        assert any(node.get('url')==f'tg://user?id={identity}' for node in nodes(blocks))
        assert f'tg://user?id={identity}' in html


def test_custom_confirmation_and_receipt_show_the_same_charge(room):
    room.D.set_custom_prices(room.db,room.season,'Joe Root:25.5cr',by_tg_id=ADMIN)
    room.db.commit()
    live(room)
    packet=room.H.execute(room.db,OWNER,CHAT,room.season.id,'dpick','Joe Root')
    rich,html=assert_card((packet['blocks'],packet['text']))
    for value in ('Joe Root','25.5','74.5','1/2'):
        assert value in rich and value in html
    assert room.one.purse_remaining_lakh==10000 and room.one.squad_size==0
    confirm=packet['markup'].inline_keyboard[0][0].callback_data
    receipt=room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,confirm)
    room.db.commit()
    rich,html=assert_card((receipt['blocks'],receipt['text']))
    for value in ('Joe Root','25.5','74.5','1/2','Team Two'):
        assert value in rich and value in html
    assert f'tg://user?id={OTHER}' in html
    assert room.one.purse_remaining_lakh==7450 and room.one.squad_size==1
    assert not any(b.callback_data.startswith(('fd_y_','fd_n_')) for row in receipt['markup'].inline_keyboard for b in row)


def test_names_are_literal_in_native_cards_and_escaped_in_html(room):
    room.one.name='Root & <Co> "XI"'
    lot=room.A.lots(room.db,room.season.id)[0]
    lot.name='Joe <Root> & Co'
    for pair in (DR.board(room.db,room.season),DR.pool(room.db,room.season),DR.team_directory(room.db,room.season)):
        rich,html=assert_card(pair)
        if 'Root & <Co>' in rich:
            assert 'Root &amp; &lt;Co&gt;' in html
        if 'Joe <Root>' in rich:
            assert 'Joe &lt;Root&gt; &amp; Co' in html
        assert '<Co>' not in html and '<Root>' not in html


def test_order_uses_collapsible_rounds_and_preserves_every_team_in_long_fallbacks(room):
    for index in range(30):
        room.D.add_team(room.db,room.season,f'Franchise {index:02d} '+('&Long name '*8),900000+index,by_tg_id=ADMIN)
    blocks,text=DR.order(room.db,room.season)
    rich,html=assert_card((blocks,text))
    assert len(R.html_parts(text))>1
    assert any(node.get('type')=='details' and node.get('is_open') for node in nodes(blocks))
    for team in room.A.franchises(room.db,room.season.id):
        assert team.name in rich
        assert team.name.replace('&','&amp;') in html
    _,board_html=assert_card(DR.board(room.db,room.season))
    assert len(board_html)<=R.CHUNK_LIMIT and 'more franchises' in board_html


def test_full_command_guide_retains_all_commands_as_tables_in_rich_and_split_html():
    from handlers.franchise_commands import draft_command_menu,draft_commands_html
    text=draft_commands_html()
    blocks=DR.blocks_for_html(text)
    rich,html=assert_card((blocks,text))
    assert any(node.get('type')=='table' for node in nodes(blocks))
    assert len(R.html_parts(html))>1
    for name,_ in draft_command_menu():
        assert '/'+name in rich and '/'+name in html


def latest_event(room,kind):
    row=room.db.query(room.models.AuctionEvent).filter_by(season_id=room.season.id,kind=kind).order_by(room.models.AuctionEvent.id.desc()).first()
    return row,json.loads(row.detail_json)


def test_announcements_store_the_original_purse_and_next_owner_for_delayed_delivery(room):
    live(room)
    for actor,name in ((OWNER,'Joe Root'),(OTHER,'Ben Stokes'),(OTHER,'Pat Cummins')):
        quote=room.D.preview_pick(room.db,room.season,actor,name)
        room.D.confirm_pick(room.db,room.season,quote.token,actor)
        room.db.commit()
        if actor==OWNER:
            first,snapshot=latest_event(room,'fd_picked')
    assert room.two.purse_remaining_lakh==5400
    stored=json.loads(room.db.get(room.models.AuctionEvent,first.id).detail_json)
    assert stored==snapshot
    rich,html=assert_card((stored['blocks'],stored['html']))
    assert '₹77 Cr' in rich and '₹77 Cr' in html
    assert f'tg://user?id={OTHER}' in html
    assert stored['round_no']==1 and stored['catch_up'] is False


def test_timeout_card_names_skipped_team_and_next_owners_without_spending(room):
    live(room)
    draft=room.D.get_draft(room.db,room.season)
    room.D.resolve_timeout(room.db,room.season,now=draft.pick_deadline_at+timedelta(seconds=1))
    room.db.commit()
    _,detail=latest_event(room,'fd_skipped')
    rich,html=assert_card((detail['blocks'],detail['html']))
    assert 'Pick time expired' in rich and 'Team One' in rich
    assert detail['timeout'] is True
    assert f'tg://user?id={OTHER}' in html
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000


def test_renderer_failure_preserves_signing_and_fallback_event(room,monkeypatch):
    live(room)
    def broken(*args,**kwargs):
        raise ValueError('Renderer failed')
    monkeypatch.setattr(DR,'event_card',broken)
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Joe Root')
    lot=room.D.confirm_pick(room.db,room.season,quote.token,OWNER)
    room.db.commit()
    _,detail=latest_event(room,'fd_picked')
    assert 'Joe Root' in detail['html']
    assert room.one.purse_remaining_lakh==7700 and lot.sold_to_id==room.one.id
    assert not detail.get('blocks')


@pytest.fixture
def rich_bot(monkeypatch):
    monkeypatch.setattr(R.config,'RICH_TEXT_ENABLED',True)
    R.reset_support_latch()
    result={'message_id':99,'date':1,'chat':{'id':CHAT,'type':'supergroup'},'text':'Draft card'}
    bot=SimpleNamespace(_post=AsyncMock(return_value=result),send_message=AsyncMock(),edit_message_text=AsyncMock())
    yield bot
    R.reset_support_latch()


def test_command_send_attempts_native_rich_with_the_same_permission_buttons(room,rich_bot):
    event=update(OWNER)
    event.effective_message.get_bot=lambda:rich_bot
    packet=room.H.execute(room.db,OWNER,CHAT,room.season.id,'draft')
    asyncio.run(room.H._send(event,packet))
    endpoint,payload=rich_bot._post.call_args.args
    assert endpoint=='sendRichMessage'
    assert payload['rich_message']['blocks']==packet['blocks']
    assert payload['reply_markup']==packet['markup']
    assert not event.effective_message.replies


def test_unsupported_rich_endpoint_falls_back_without_losing_price_or_buttons(room,rich_bot):
    live(room)
    event=update(OWNER)
    event.effective_message.get_bot=lambda:rich_bot
    rich_bot._post.side_effect=BadRequest('Unknown method sendRichMessage')
    packet=room.H.execute(room.db,OWNER,CHAT,room.season.id,'dpick','Joe Root')
    asyncio.run(room.H._send(event,packet))
    text,options=event.effective_message.replies[-1]
    assert text==packet['text'] and options['parse_mode']=='HTML'
    assert options['reply_markup']==packet['markup']
    assert 'Joe Root' in text and '₹23 Cr' in text and '₹77 Cr' in text


def test_button_redraw_uses_native_table_card_and_preserves_navigation(room,rich_bot):
    event=update(OWNER,callback=room.H._cb(OWNER,room.season.id,'pool','1'))
    event.callback_query.get_bot=lambda:rich_bot
    packet=room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,event.callback_query.data)
    asyncio.run(room.H._send(event,packet,edit=True))
    endpoint,payload=rich_bot._post.call_args.args
    assert endpoint=='editMessageText'
    assert any(node.get('type')=='table' for node in nodes(payload['rich_message']['blocks']))
    assert payload['reply_markup']==packet['markup']


def test_event_delivery_uses_saved_card_instead_of_current_team_state(room,rich_bot):
    from services import franchise_draft_delivery as DD
    live(room)
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Joe Root')
    room.D.confirm_pick(room.db,room.season,quote.token,OWNER)
    room.db.commit()
    event,detail=latest_event(room,'fd_picked')
    room.season.announced_event_id=event.id-1
    room.db.commit()
    assert asyncio.run(DD.drain_events(rich_bot,room.db,room.season))==1
    assert rich_bot._post.call_args.args[1]['rich_message']['blocks']==detail['blocks']
    assert room.season.announced_event_id==event.id


def test_import_prompts_and_results_use_rich_cards_for_drafts(room,rich_bot,monkeypatch):
    from handlers import franchise_player_import as H
    from telegram.ext import ApplicationHandlerStop
    monkeypatch.setattr(H,'get_session',room.sessions)
    event=update(ADMIN)
    event.effective_message.get_bot=lambda:rich_bot
    ctx=context(room.season.id)
    asyncio.run(H.importplayers_handler(event,ctx))
    prompt=rich_bot._post.call_args.args[1]['rich_message']['blocks']
    assert 'Import draft players' in flatten(prompt)
    assert '/dprices' in flatten(prompt) and 'No overseas limit' in flatten(prompt)
    csv=b'name,rating,role,country\nNew Keeper,95,WK,India\n'
    file=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(csv)))
    event.effective_message.document=SimpleNamespace(file_name='players.csv',file_size=len(csv),get_file=AsyncMock(return_value=file))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(H.player_document_router(event,ctx))
    result=rich_bot._post.call_args.args[1]['rich_message']['blocks']
    assert 'Player import complete' in flatten(result)
    assert 'Added to this draft' in flatten(result) and '/dpool' in flatten(result)


def test_native_workspace_preserves_rich_board_tables_and_role_context(room,rich_bot,monkeypatch):
    from handlers import franchise_bot as H
    monkeypatch.setattr(H,'get_session',room.sessions)
    event=update(OWNER)
    event.effective_message.get_bot=lambda:rich_bot
    asyncio.run(H._show(event,context(room.season.id),'franchisedraft'))
    blocks=rich_bot._post.call_args.args[1]['rich_message']['blocks']
    assert any(node.get('type')=='table' for node in nodes(blocks))
    assert 'Team One' in flatten(blocks) and 'Roles:' in flatten(blocks)
    assert '/dpick Joe Root' in flatten(blocks)


def test_timer_off_confirmation_does_not_claim_the_timer_is_running(room):
    room.D.set_timer(room.db,room.season,0,by_tg_id=ADMIN)
    room.db.commit()
    live(room)
    packet=room.H.execute(room.db,OWNER,CHAT,room.season.id,'dpick','Joe Root')
    rich,html=assert_card((packet['blocks'],packet['text']))
    assert 'Timer off' in rich and 'Timer off' in html
    assert 'timer keeps running' not in rich.casefold()


def test_native_draft_workspace_keeps_long_pool_instead_of_truncating(room,monkeypatch):
    from handlers import franchise_bot as H
    monkeypatch.setattr(H,'get_session',room.sessions)
    for index in range(6):
        player=room.models.Player(name=f'Extra player {index}',rating=96,category='Batsman',country='England',
            version='Base',is_active=True,bat_rating=96,bowl_rating=50,
            bat_hand='Right',bowl_hand='Right',bowl_style='Medium Pacer')
        room.db.add(player)
        room.db.flush()
        room.A.add_players_to_pool(room.db,room.season,[player])
    for lot in room.A.lots(room.db,room.season.id):
        lot.name='&'*80+str(lot.id)
        room.db.get(room.models.Player,lot.player_id).name=lot.name
    room.db.commit()
    data=H.read_workspace(room.db,ADMIN,room.season.id,CHAT,'pool','1')
    text,_=H.render_workspace(data,ADMIN,room.season.id,'pool','1',context(),CHAT)
    assert len(text)>3650
    assert data['franchise_draft']['pool'] in text
    assert '… Open the next page' not in text
    event=update(ADMIN,callback=f'fa:{ADMIN}:{room.season.id}:v:pool:1')
    asyncio.run(H._show(event,context(room.season.id),'pool',argument='1'))
    replies=event.effective_message.replies
    assert len(replies)>1
    joined='\n'.join(t for t,_ in replies)
    for lot in room.A.lots(room.db,room.season.id):
        assert lot.name.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;') in joined
    assert replies[-1][1]['reply_markup'] is not None
    for _,options in replies[:-1]:
        assert options.get('reply_markup') is None
