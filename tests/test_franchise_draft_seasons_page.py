"""League Seasons exposes franchise drafting beside its auction choices."""
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from test_franchise_draft_seasons import competition  # noqa: F401


@pytest.fixture
def page(competition, monkeypatch):
    import admin

    sessions = sessionmaker(bind=competition.db.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(admin, "get_session", sessions)
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    client = admin.app.test_client()
    with client.session_transaction() as state:
        state["admin"] = True
    yield SimpleNamespace(c=competition, client=client,
                          url=f"/league-seasons/{competition.series.id}")


def test_next_season_page_offers_draft_with_purse_rounds_and_bot_instructions(page):
    response = page.client.get(page.url)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'name="action" value="start_draft"' in text
    assert "Create franchise draft" in text
    assert 'name="draft_name"' in text
    assert 'name="rounds"' in text
    assert "Rounds / squad size" in text
    assert "No overseas limit" in text
    assert "/draft" in text and "dedicated franchise bot" in text
    assert "Start mega auction" in text
    assert "Start mini auction" in text


def test_next_season_post_creates_draft_then_shows_draft_progress(page):
    from models import AuctionSeason
    from services import franchise_draft as D

    response = page.client.post(page.url, data={
        "action": "start_draft", "draft_name": "Next-season draft", "purse": "125.5", "rounds": "18",
    })
    assert response.status_code == 302
    c = page.c
    c.db.expire_all()
    season = c.LS.latest_season(c.db, c.series)
    auction = c.db.get(AuctionSeason, season.auction_season_id)
    assert season.squad_mode == c.LS.MODE_DRAFT
    assert season.season_no == 2
    assert auction.name == "Next-season draft"
    assert auction.previous_league_id == c.league.id
    assert auction.opening_purse_lakh == 12550
    assert auction.min_squad_size == auction.max_squad_size == 18
    assert D.get_draft(c.db, auction).rounds == 18
    assert [team.owner_tg_id for team in c.A.franchises(c.db, auction.id)] == [101, 102]

    text = page.client.get(page.url).get_data(as_text=True)
    assert "Build the pool and start the draft" in text
    assert "18 draft rounds" in text
    assert "Draft setup &amp; squads" in text
    assert f'href="/auctions/{auction.id}"' in text
    assert "dedicated franchise bot" in text
    assert "Release board" not in text

    auction.status = c.A.STATUS_LIVE
    c.db.commit()
    assert "Franchise draft live" in page.client.get(page.url).get_data(as_text=True)
    auction.status = c.A.STATUS_COMPLETED
    c.db.commit()
    text = page.client.get(page.url).get_data(as_text=True)
    assert "Draft done" in text and "Publish squads" in text


@pytest.mark.parametrize("changes", [{"rounds": "0"}, {"rounds": "bad"}, {"purse": "invalid"}])
def test_invalid_draft_form_rolls_back_new_season_and_config(page, changes):
    from models import AuctionSeason, FranchiseDraft

    data = dict(action="start_draft", purse="120", rounds="15")
    data.update(changes)
    response = page.client.post(page.url, data=data)
    assert response.status_code == 302
    c = page.c
    c.db.expire_all()
    assert len(c.LS.seasons(c.db, c.series)) == 1
    assert c.db.query(AuctionSeason).count() == 0
    assert c.db.query(FranchiseDraft).count() == 0


def test_only_authenticated_admin_can_create_next_season_draft(page):
    with page.client.session_transaction() as state:
        state.clear()
    response = page.client.post(page.url, data=dict(action="start_draft", purse="120", rounds="15"))
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    page.c.db.expire_all()
    assert len(page.c.LS.seasons(page.c.db, page.c.series)) == 1


def test_shared_draft_setup_page_renders_with_bot_guidance_and_pool_tools(page):
    c = page.c
    _season, auction = c.LS.start_franchise_draft(c.db, c.series, rounds=3)
    c.db.commit()
    response = page.client.get(f"/auctions/{auction.id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Franchise draft · 3 rounds" in text
    assert "dedicated franchise bot" in text and "/draft" in text
    assert "Draft player pool" in text
    assert 'name="action" value="settings"' not in text
    assert 'name="action" value="squad_rules"' not in text
    assert 'name="action" value="retention_rules"' not in text


@pytest.mark.parametrize("action", ["settings", "squad_rules", "retention_rules"])
def test_legacy_settings_cannot_change_draft_round_or_overseas_limits(page, action):
    from services import franchise_draft as D

    c = page.c
    _season, auction = c.LS.start_franchise_draft(c.db, c.series, rounds=3)
    c.db.commit()
    original_name = auction.name
    response = page.client.post(f"/auctions/{auction.id}", data=dict(
        action=action, name="Must not change", max_squad_size="25", min_squad_size="15",
        min_overseas="1", max_overseas="1", max_retentions="4", opening_purse="1",
    ))
    assert response.status_code == 302
    c.db.expire_all()
    assert auction.name == original_name
    assert auction.min_squad_size == auction.max_squad_size == 3
    assert auction.min_overseas == 0 and auction.max_overseas == 3
    assert auction.max_retentions == 0
    assert auction.opening_purse_lakh == 12000
    assert D.get_draft(c.db, auction).rounds == 3
    assert "Manage draft rounds" in page.client.get(f"/auctions/{auction.id}").get_data(as_text=True)
