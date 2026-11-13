"""Exercise the draft entry in a real browser without performing any signings."""
from pathlib import Path
import shutil

import pytest


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
