"""Exercise the draft entry in a real browser without performing any signings."""
from pathlib import Path
import shutil

import pytest
from test_franchise_website import site  # noqa: F401


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
