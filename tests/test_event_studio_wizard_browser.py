"""The Event Studio wizard, driven in a browser against the real admin screen."""

import json
import shutil
import threading
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PASSWORD = "studio-wizard-test"


def _chromium():
    """A Chromium the tests may drive, or None to skip."""
    for candidate in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(candidate)
        if found:
            return found
    bundled = "/opt/pw-browsers/chromium"
    return bundled if shutil.os.path.exists(bundled) else None


def _draft(db, *, teams=4, closes="2026-12-24T07:00", playoffs="top4_semis",
           spacing=1, generate=True):
    from models import Player
    from services import event_studio as studio

    config = studio.defaults()
    config.update(kind="live", objective_scope="tournament", opens_at="2026-12-01T09:00",
                  closes_at=closes,
                  teams=[{"key": f"team-{n}", "name": f"Team {n}", "players": []}
                         for n in range(teams)])
    config["schedule"].update(preset="round_robin", legs=1, playoffs=playoffs,
                              round_spacing_days=spacing, window_hours=20, grace=30)
    for team in config["teams"]:
        for index in range(11):
            role = "Wicketkeeper" if index == 0 else "Batsman" if index < 6 else "Bowler"
            card = Player(name=f'{team["name"]} Player {index + 1}', category=role,
                          country="India", bowl_style="Fast", bat_hand="Right",
                          bowl_hand="Right", rating=75, bat_rating=76, bowl_rating=80)
            db.add(card)
            db.flush()
            team["players"].append(card.id)
    db.add(Player(name="Searchable Star", category="Batsman", country="India",
                  bowl_style="Fast", bat_hand="Right", bowl_hand="Right",
                  rating=91, bat_rating=92, bowl_rating=30))
    db.commit()
    built = studio.normalize(config)
    if generate:
        built = studio.generate(built, "schedule")
    event = studio.create(db, "Summer Cup", "summer-cup", "operator", built)
    db.commit()
    return event.id


@pytest.fixture
def studio_page(tmp_path, monkeypatch):
    """Serve the admin app and hand back a signed-in page on an event draft."""
    executable = _chromium()
    if not executable:
        pytest.skip("Chromium unavailable")
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    from werkzeug.serving import make_server

    from database import Base
    import admin

    engine = create_engine(f"sqlite:///{tmp_path / 'wizard.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", PASSWORD, raising=False)
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_HASH", "", raising=False)

    opened = []
    with patch("services.event_studio_admin.get_session", sessions), \
            patch.object(admin, "get_session", sessions), \
            patch.dict(admin.app.config, TESTING=True, WTF_CSRF_ENABLED=False):
        server = make_server("127.0.0.1", 0, admin.app, threaded=True)
        base = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=executable, headless=True,
                                         args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                def open_draft(**kwargs):
                    db = sessions()
                    try:
                        event_id = _draft(db, **kwargs)
                    finally:
                        db.close()
                    # A timezone that is neither UTC nor the event's, so a stamp read in
                    # the wrong zone shows up rather than cancelling out.
                    page = browser.new_page(viewport={"width": 1400, "height": 1000},
                                            timezone_id="America/New_York")
                    failures = []
                    page.on("pageerror", lambda error: failures.append(str(error)))
                    page.goto(f"{base}/login")
                    page.fill('input[name="password"]', PASSWORD)
                    page.click('button[type="submit"], input[type="submit"]')
                    page.wait_for_load_state()
                    page.goto(f"{base}/events/studio/{event_id}")
                    page.wait_for_selector('[data-studio-fields]:not([hidden])', timeout=15000)
                    opened.append((page, failures))
                    return page

                yield open_draft
            finally:
                for page, failures in opened:
                    assert failures == [], f"uncaught page errors: {failures}"
                browser.close()
                server.shutdown()


def _config(page):
    return json.loads(page.locator('textarea[name="config"]').input_value())


def test_the_wizard_opens_on_the_first_step_and_navigates(studio_page):
    page = studio_page()
    assert page.locator('.studio-steps button').count() == 10
    assert page.locator('[data-studio-progress-text]').inner_text() == "Step 1 of 10"
    visible = page.locator('[data-step]:not([hidden])')
    assert visible.count() == 1 and visible.first.get_attribute("data-step") == "event"
    page.click('[data-step-to="rewards"]')
    page.wait_for_timeout(150)
    assert page.locator('[data-step]:not([hidden])').first.get_attribute("data-step") == "rewards"
    assert page.locator('[data-studio-progress-text]').inner_text() == "Step 7 of 10"


def test_the_team_editor_writes_identity_branding_and_home_stadium(studio_page):
    page = studio_page()
    card = page.locator('[data-teams-editor] details').first
    assert page.locator('[data-teams-editor] details').count() == 4
    card.locator('input[type="text"]').first.fill("Harbour XI")
    card.locator('input[type="text"]').first.dispatch_event("change")
    card.locator('input[type="text"]').nth(2).fill("Wankhede Stadium")
    card.locator('input[type="text"]').nth(2).dispatch_event("change")
    team = _config(page)["teams"][0]
    assert team["name"] == "Harbour XI"
    assert team["home_venue"] == "Wankhede Stadium"


def test_the_squad_picker_resolves_a_card_by_name(studio_page):
    page = studio_page()
    card = page.locator('[data-teams-editor] details').first
    before = len(_config(page)["teams"][0]["players"])
    card.locator('input[type="search"]').first.fill("Searchable")
    option = card.locator('button', has_text="Searchable Star")
    option.first.wait_for(timeout=5000)
    option.first.click()
    page.wait_for_timeout(200)
    assert len(_config(page)["teams"][0]["players"]) == before + 1


def test_three_mode_checkboxes_drive_the_two_stored_lists(studio_page):
    page = studio_page()
    page.click('[data-step-to="participation"]')
    page.wait_for_timeout(150)
    assert page.locator('[data-mode-toggle="solo"]').is_checked()
    assert page.locator('[data-mode-toggle="multi"]').is_checked()
    assert not page.locator('[data-mode-toggle="own_roster"]').is_checked()
    page.locator('[data-mode-toggle="own_roster"]').check()
    page.wait_for_timeout(150)
    assert "own_roster" in _config(page)["mode"]
    page.locator('[data-mode-toggle="multi"]').uncheck()
    page.wait_for_timeout(150)
    assert _config(page)["competition"]["modes"] == ["solo"]


def test_the_generated_schedule_offers_five_views(studio_page):
    page = studio_page()
    page.click('[data-step-to="schedule"]')
    page.wait_for_timeout(200)
    tabs = page.locator('[data-fixture-views] [role="tab"]')
    assert [tabs.nth(index).inner_text() for index in range(tabs.count())] == [
        "Fixture table", "Calendar", "Round by round", "Team by team", "Playoff bracket"]
    assert page.locator('[data-fixture-views] tbody tr').count() == 9
    for name in ("Calendar", "Round by round", "Team by team", "Playoff bracket"):
        page.locator('[data-fixture-views] [role="tab"]', has_text=name).first.click()
        page.wait_for_timeout(120)
        assert page.locator('[data-fixture-views] [role="tabpanel"]').inner_text().strip()


def test_the_window_check_reads_the_event_timezone_not_the_browser(studio_page):
    """The event's dates are bare wall-clock stamps in its own zone, so a browser in
    another zone must still see a schedule that fits as fitting."""
    page = studio_page()
    page.click('[data-step-to="schedule"]')
    page.wait_for_timeout(200)
    assert "fits inside the event window" in page.locator('[data-fixture-fit]').inner_text()


def test_auto_fix_tightens_the_schedule_without_moving_the_event_dates(studio_page):
    # Seven rounds three days apart need three weeks; the window below gives ten days.
    page = studio_page(teams=8, playoffs="none", closes="2027-03-01T23:00", spacing=3)
    # Shrink the window after generating, so the schedule now overruns it. The whole
    # configuration lives on the Advanced step, which is where it is edited directly.
    config = _config(page)
    config["closes_at"] = "2026-12-10T23:00"
    page.click('[data-step-to="advanced"]')
    page.wait_for_timeout(150)
    page.click('[data-studio-source] > summary')  # Full configuration starts collapsed.
    page.wait_for_timeout(100)
    page.locator('textarea[name="config"]').fill(json.dumps(config))
    page.locator('textarea[name="config"]').dispatch_event("input")
    page.click('[data-step-to="schedule"]')
    page.wait_for_timeout(200)
    assert "past the event's end date" in page.locator('[data-fixture-fit]').inner_text()

    page.click('[data-fixture-autofix]')
    page.wait_for_load_state()
    page.wait_for_selector('[data-studio-fields]:not([hidden])')
    page.click('[data-step-to="schedule"]')
    page.wait_for_timeout(250)
    assert "fits inside the event window" in page.locator('[data-fixture-fit]').inner_text()
    after = _config(page)
    assert after["schedule"]["round_spacing_days"] < 3
    assert after["opens_at"] == "2026-12-01T09:00"
    assert after["closes_at"] == "2026-12-10T23:00"


def test_the_objective_and_reward_editors_write_the_stored_shapes(studio_page):
    page = studio_page()
    page.click('[data-step-to="objectives"]')
    page.wait_for_timeout(150)
    page.click('[data-objective-add]')
    page.wait_for_timeout(150)
    rows = _config(page)["tournament_objectives"]
    assert len(rows) == 1 and rows[0]["stars"] == 1 and rows[0]["metric"]

    page.click('[data-step-to="rewards"]')
    page.wait_for_timeout(150)
    cells = page.locator('[data-rewards-editor] tbody input')
    assert cells.count() == 18  # Two tiers, three results, three assets.
    cells.first.fill("1500")
    cells.first.dispatch_event("change")
    page.wait_for_timeout(150)
    assert _config(page)["rewards"]["free"]["win"]["coins"] == 1500


def test_the_review_step_summarises_the_draft(studio_page):
    page = studio_page()
    page.click('[data-step-to="publish"]')
    page.wait_for_timeout(200)
    summary = page.locator('[data-studio-summary]').inner_text()
    for heading in ("Event name", "Teams", "Total fixtures", "Event duration",
                    "Participation modes", "Maximum participants", "Maximum stars"):
        assert heading.upper() in summary.upper(), heading
    assert "9 (6 league, 3 playoff)" in summary
