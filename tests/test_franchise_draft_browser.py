"""Exercise the draft entry in a real browser without performing any signings."""
from pathlib import Path
import shutil

import pytest
from test_franchise_website import site  # noqa: F401
from test_franchise_draft_seasons_page import page, competition  # noqa: F401


def test_website_resume_is_immediate_and_live_timer_form_does_not_need_a_reason():
    playwright=pytest.importorskip('playwright.sync_api')
    executable=shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:pytest.skip('Chromium is required for this browser test')
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':1200,'height':950})
            page.set_content('<main id="draft"></main>')
            page.add_script_tag(content="""
                window.calls=[];window.statusValue='paused';
                async function api(path,body){
                  calls.push(body);
                  if(body.action==='draft_resume'){statusValue='live';return {ok:true,data:{message:'Draft resumed.'}};}
                  if(body.action==='draft_preview')return {ok:true,data:{confirmation:{type:'admin',token:'clock-quote',html:'<b>New pick timer: 480 seconds</b>'}}};
                  return {ok:true,data:{ok:true,can_manage_draft:true,auctions:[{id:7,name:'Draft seven',mode:'draft'}],
                    season:{id:7,name:'Draft seven',status:statusValue},teams:[{id:11,name:'North'}],
                    draft:{rounds:2,min_squad_size:2,max_squad_size:2,status:statusValue,started:true,revision:4,seconds:300,policy:'pause',current_team:'North'},
                    draft_html:'<b>Draft Board</b>',page:1,pages:1}};
                }
            """)
            page.add_script_tag(path=str(Path(__file__).parents[1]/'static/js/franchise_draft.js'))
            page.evaluate("FranchiseDraftApp.open('draft')")
            button=page.get_by_role('button',name='▶ Resume Draft',exact=True)
            assert button.is_enabled()
            button.click()
            page.get_by_role('button',name='⏸ Pause Draft',exact=True).wait_for(state='visible')
            assert page.evaluate("calls.filter(c=>c.action).map(c=>c.action)")==['draft_resume']
            assert page.locator('[data-fd-dialog]').is_hidden()
            page.get_by_role('button',name='⚙️ Draft Settings',exact=True).click()
            clock=page.locator('[data-fd-form="clock"]')
            clock.locator('xpath=..').locator('summary').click()
            clock.locator('[name="seconds"]').fill('480')
            clock.get_by_role('button',name='Preview change',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='visible')
            assert page.evaluate("calls.at(-1).operation")=='clock'
            assert page.evaluate("calls.at(-1).values.seconds")=='480'
            assert page.evaluate("calls.at(-1).reason")==''
        finally:browser.close()


def test_auto_draft_button_previews_and_requires_confirmation_before_completion():
    playwright=pytest.importorskip('playwright.sync_api')
    executable=shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:pytest.skip('Chromium is required for this browser test')
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':1200,'height':950})
            page.set_content('<main id="draft"></main>')
            page.add_script_tag(content="""
                window.calls=[];window.statusValue='live';
                async function api(path,body){
                  calls.push(body);
                  if(body.action==='draft_preview')return {ok:true,data:{confirmation:{type:'admin',token:'auto-quote',html:'<b>Complete Auto Draft Plan</b><p>Joe Root · 25.5 Cr · North</p>'}}};
                  if(body.action==='draft_answer'){if(body.accept)statusValue='completed';return {ok:true,data:{message:body.accept?'Auto draft complete.':'Cancelled.'}};}
                  return {ok:true,data:{ok:true,can_manage_draft:true,auctions:[{id:7,name:'Draft seven',mode:'draft'}],
                    season:{id:7,name:'Draft seven',status:statusValue},teams:[{id:11,name:'North'}],
                    draft:{rounds:2,min_squad_size:2,max_squad_size:2,status:statusValue,started:true,revision:4,seconds:300,policy:'skip',current_team:'North'},
                    draft_html:'<b>Draft Board</b>',page:1,pages:1}};
                }
            """)
            page.add_script_tag(path=str(Path(__file__).parents[1]/'static/js/franchise_draft.js'))
            page.evaluate("FranchiseDraftApp.open('draft')")
            page.get_by_role('button',name='🤖 Auto Draft Remaining Slots',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='visible')
            assert page.evaluate('calls.at(-1).operation')=='autofill'
            assert page.evaluate('statusValue')=='live'
            page.get_by_role('button',name='❌ Cancel',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='hidden')
            assert page.evaluate('statusValue')=='live'
            page.get_by_role('button',name='🤖 Auto Draft Remaining Slots',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='visible')
            page.get_by_role('button',name='✅ Confirm',exact=True).click()
            page.get_by_role('button',name='🏆 Finalize & Publish Squads',exact=True).wait_for(state='visible')
            assert page.evaluate('statusValue')=='completed'
        finally:browser.close()


def test_draft_browser_navigation_pagination_and_stale_responses():
    playwright=pytest.importorskip('playwright.sync_api')
    executable=shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:pytest.skip('Chromium is required for this browser test')
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':390,'height':844})
            page.set_content('<main id="draft"></main>')
            page.add_script_tag(content=r"""
              function escapeHtml(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
              window.calls=[];window.hold=false;
              async function api(path,body){
                calls.push({path,body});
                const result={ok:true,data:{ok:true,auctions:[{id:7,name:'Draft seven',mode:'draft',status:'live'},{id:9,name:'Auction nine',mode:'auction',status:'setup'}],
                  season:{id:7,name:'Draft seven',mode:'draft'},draft_html:'<b>Live Draft Board</b>',page:body.page||1,pages:body.part==='pool'?2:1,
                  teams:[{id:11,name:'North'},{id:12,name:'South'}],team_id:body.team_id||11,draft_bot_url:'https://t.me/CMUDraftBot?start=draft_7'}};
                if(hold)return new Promise(resolve=>window.release=()=>resolve(result));
                return result;
              }
            """)
            page.add_script_tag(path=str(Path(__file__).parents[1]/'static/js/franchise_draft.js'))
            page.evaluate("FranchiseDraftApp.open('draft')")
            assert page.locator('[data-fd-season] option').all_text_contents()==['Draft seven · live']
            assert page.locator('a').get_attribute('href').endswith('start=draft_7')
            page.get_by_role('button',name='🏏 Available Players',exact=True).click()
            page.get_by_role('button',name='Next →',exact=True).click()
            page.wait_for_function('calls.at(-1).body.page===2')
            page.get_by_role('button',name='👥 Squads',exact=True).click()
            page.locator('[data-fd-team]').select_option('12')
            page.wait_for_function('calls.at(-1).body.team_id===12')
            assert page.evaluate("calls.every(c=>c.path==='/api/webapp/auction'&&c.body.mode==='draft'&&c.body.view==='franchisedraft')")
            # A request finishing after logout or navigation must not restore
            # the old account's board, or schedule another refresh.
            page.evaluate('hold=true')
            page.get_by_role('button',name='↻ Refresh',exact=True).click()
            page.wait_for_function('typeof release==="function"')
            page.evaluate('FranchiseDraftApp.stop();release()')
            assert page.locator('#draft').inner_html()==''
        finally:
            browser.close()


def test_management_forms_use_real_authenticated_backend_and_confirm_before_mutation(site):
    from test_franchise_website import login,ADMIN,OWNER
    from models import AuctionSeason
    import json
    playwright=pytest.importorskip('playwright.sync_api')
    executable=shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:pytest.skip('Chromium is required for this browser test')
    token=login(site,ADMIN)
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page=browser.new_page(viewport={'width':1200,'height':950})
            page.route('https://draft.test/api/**',lambda route: route.fulfill(
                status=(response:=site.client.post(route.request.url.removeprefix('https://draft.test'),
                    json=route.request.post_data_json,headers={'X-CSRF-Token':route.request.headers.get('x-csrf-token','')})).status_code,
                content_type='application/json',body=response.get_data(as_text=True)))
            page.route('https://draft.test/',lambda route:route.fulfill(content_type='text/html',body='<main id="draft"></main>'))
            page.goto('https://draft.test/')
            page.add_script_tag(content='const csrf='+json.dumps(token)+''';
                async function api(path,body){const response=await fetch('https://draft.test'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});return {ok:response.ok,data:await response.json()};}''')
            page.add_script_tag(path=str(Path(__file__).parents[1]/'static/js/franchise_draft.js'))
            page.evaluate("FranchiseDraftApp.open('draft')")
            create=page.locator('[data-fd-form="create"]')
            create.locator('[name="name"]').fill('Browser-controlled draft')
            create.locator('[name="rounds"]').fill('1')
            create.get_by_role('button',name='Create Draft',exact=True).click()
            page.get_by_role('button',name='🏟 Franchises & Owners',exact=True).click()
            form=page.locator('[data-fd-form="team"]')
            form.locator('[name="name"]').fill('Browser franchise')
            form.locator('[name="owner_id"]').fill(str(OWNER))
            form.get_by_role('button',name='Preview change',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='visible')
            season=site.db.query(AuctionSeason).filter_by(name='Browser-controlled draft').one()
            assert site.A.franchises(site.db,season.id)==[]
            page.get_by_role('button',name='✅ Confirm',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='hidden')
            site.db.expire_all();assert site.A.franchises(site.db,season.id)[0].owner_tg_id==OWNER
            page.get_by_role('button',name='⚙️ Draft Settings',exact=True).click()
            prices=page.locator('[data-fd-form="prices"]')
            prices.locator('xpath=..').locator('summary').click()
            prices.locator('[name="text"]').fill('85:25.5cr')
            prices.get_by_role('button',name='Preview change',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='visible')
            page.get_by_role('button',name='❌ Cancel',exact=True).click()
            page.locator('[data-fd-dialog]').wait_for(state='hidden')
            from services import franchise_draft as D
            assert not json.loads(D.get_draft(site.db,season).rating_prices_json or '{}')
            page.get_by_role('button',name='✅ Readiness Check',exact=True).click()
            page.locator('.fd-rich').wait_for(state='visible')
            assert page.get_by_role('button',name='▶ Start Draft',exact=True).is_disabled()
        finally:
            browser.close()


def test_admin_manage_draft_settings_upload_with_csrf_and_expired_session(page, monkeypatch):
    """Use the actual admin HTML, cookies, CSRF and fetch path in Chromium."""
    from urllib.parse import urlsplit
    from services import franchise_draft as D
    import json
    playwright=pytest.importorskip('playwright.sync_api')
    executable=shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:pytest.skip('Chromium is required for this browser test')
    c=page.c
    season=c.A.create_season(c.db,'Admin browser draft')
    D.enable(c.db,season,rounds=2)
    player=c.db.query(__import__('models').Player).first()
    c.A.add_players_to_pool(c.db,season,[player])
    D.set_rating_prices(c.db,season,f'{player.rating}:1cr')
    c.db.commit()
    monkeypatch.setitem(page.client.application.config,'WTF_CSRF_ENABLED',True)
    with playwright.sync_playwright() as engine:
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            tab=browser.new_page(viewport={'width':1200,'height':950})
            tab.add_init_script("HTMLDialogElement.prototype.showModal = function(){throw new Error('Native dialog unavailable in this WebView');};")
            def serve(route):
                req=route.request;path=urlsplit(req.url).path
                headers={key:value for key,value in req.headers.items() if key in ('x-csrftoken','referer','content-type')}
                response=page.client.open(path,method=req.method,data=req.post_data,headers=headers,base_url='https://localhost')
                route.fulfill(status=response.status_code,content_type=response.content_type,body=response.data)
            tab.route('**/*',serve)
            tab.goto(f'https://localhost/franchise-drafts/{season.id}/workspace')
            tab.get_by_role('button',name='⚙️ Draft Settings',exact=True).click()
            sizes=tab.locator('[data-fd-form="rounds"]')
            sizes.locator('xpath=..').locator('summary').click()
            sizes.locator('[name="min_squad_size"]').fill('1')
            sizes.locator('[name="rounds"]').fill('3')
            assert sizes.locator('[data-fd-rounds]').inner_text()=='3'
            sizes.get_by_role('button',name='Preview change',exact=True).click()
            tab.locator('[data-fd-dialog]').wait_for(state='visible')
            assert '1' in tab.locator('[data-fd-dialog]').inner_text()
            tab.get_by_role('button',name='✅ Confirm',exact=True).click()
            tab.locator('[data-fd-dialog]').wait_for(state='hidden')
            c.db.expire_all()
            assert (season.min_squad_size,season.max_squad_size,D.get_draft(c.db,season).rounds)==(1,3,3)
            form=tab.locator('[data-fd-form="custom"]')
            form.locator('xpath=..').locator('summary').click()
            import csv,io
            sheet=io.StringIO();writer=csv.writer(sheet)
            writer.writerow(['player_id','name','version','rating','category','price_lakh'])
            writer.writerow([player.id,player.name,player.version or 'Base',player.rating,player.category,2550])
            form.locator('input[type=file]').set_input_files({'name':'prices.csv','mimeType':'text/csv','buffer':sheet.getvalue().encode()})
            tab.wait_for_function("document.querySelector('[data-fd-form=custom] textarea').value.includes('price_lakh')")
            form.get_by_role('button',name='Preview change',exact=True).click()
            tab.locator('[data-fd-dialog]').wait_for(state='visible')
            assert '25.5 Cr' in tab.locator('[data-fd-dialog]').inner_text()
            c.db.expire_all();assert json.loads(D.get_draft(c.db,season).custom_prices_json or '{}')=={}
            tab.get_by_role('button',name='✅ Confirm',exact=True).click()
            tab.locator('[data-fd-dialog]').wait_for(state='hidden')
            c.db.expire_all();lot=c.A.lots(c.db,season.id)[0]
            assert D.price_for(c.db,season,lot)==2550
            # Missing CSRF returns a useful JSON error without destroying the session.
            response=page.client.post('/api/admin/franchise-draft',json={'season_id':season.id},base_url='https://localhost')
            assert response.status_code==400 and 'session expired' in response.json['message']
            with page.client.session_transaction() as state:state.clear()
            tab.get_by_role('button',name='↻ Refresh',exact=True).click()
            tab.get_by_role('alert').filter(has_text='session expired').wait_for()
        finally:
            browser.close()
