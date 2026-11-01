"""Website managers configure shared rules and act for an unowned franchise."""
import json
from datetime import datetime, timedelta

import pytest

from test_franchise_website import ADMIN, DELEGATE, OTHER, OWNER, auction, login, site  # noqa: F401


def manage(site, action, auth_token, **body):
    return site.client.post('/api/webapp/auction/action',
        json=dict(season_id=site.season.id, action=action, **body),
        headers={'X-CSRF-Token':auth_token})


def appointed(site, actor=DELEGATE):
    if actor == DELEGATE:
        site.A.add_auction_admin(site.db, actor, by_tg_id=ADMIN)
        site.db.commit()
    return login(site, actor)


@pytest.mark.parametrize('actor', [ADMIN, DELEGATE])
def test_global_managers_configure_complete_rules_and_assign_team_managers(site, actor):
    token = appointed(site, actor)
    settings = dict(currency_label='CR', home_country='India', min_squad_size=0,
                    max_squad_size=12, max_overseas=7, opening_purse_lakh=12000,
                    min_retentions=0, max_retentions=4, retention_min_rating=80,
                    retention_max_rating=105, retention_categories=['Batsman'],
                    retention_max_spend_lakh=5000,
                    retention_prices=[dict(slab=1, price_lakh=150)],
                    retention_deadline='2035-01-01T12:30:00+03:00',
                    rtm_enabled=True, rtm_per_team=2, rtm_window_seconds=45,
                    rtm_extra_lakh=10, draft_pick_seconds=90, draft_timeout_action='shortlist',
                    direct_bids=False, focus_mode=True, auto_accelerated=False,
                    staged_clock=[45, 20, 12, 8, 5])
    response = manage(site, 'admin_settings', token, settings=settings)
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_franchise', token, team_id=site.owner.id,
                      name='Website Franchise', owner_tg_id=str(OWNER), owner_name='Owner',
                      co_owners=[], team_managers=[str(OTHER)], short_name='WEB', city='Delhi',
                      logo_url='https://example.com/team.png', sort_order=3)
    assert response.status_code == 200, response.get_json()
    result = auction(site, token, view='admin').get_json()
    assert result['settings']['currency_label'] == 'CR'
    assert result['settings']['draft_timeout_action'] == 'shortlist'
    assert result['retention_deadline'] == '2035-01-01T09:30:00Z'
    assert result['retention_categories'] == ['Batsman']
    assert result['retention_prices'] == [dict(slab=1, price_lakh=150)]
    assert result['sets'], 'Set management must receive real existing sets'
    team = next(t for t in result['teams'] if t['id'] == site.owner.id)
    assert team['team_managers'] == [str(OTHER)]
    assert team['short_name'] == 'WEB' and team['city'] == 'Delhi' and team['sort_order'] == 3
    if actor == DELEGATE:
        denied = manage(site, 'admin_add_admin', token, telegram_id=OTHER)
        assert denied.status_code == 403, 'Auction settings do not permit global role delegation'


def test_nullable_rules_clear_and_failed_changes_rollback_atomically(site):
    token = appointed(site)
    response = manage(site, 'admin_settings', token, settings=dict(
        retention_categories=['Bowler'], retention_prices=[dict(slab=1, price_lakh=50)],
        retention_min_rating=80, retention_max_rating=90, retention_max_spend_lakh=3000,
        retention_deadline='2035-01-01T00:00:00Z'))
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_settings', token, settings=dict(
        retention_categories=None, retention_prices=None, retention_min_rating=None,
        retention_max_rating=None, retention_max_spend_lakh=None, retention_deadline=None))
    assert response.status_code == 200, response.get_json()
    before = auction(site, token, view='admin').get_json()
    assert before['settings']['retention_min_rating'] is None
    assert before['settings']['retention_max_rating'] is None
    assert before['settings']['retention_max_spend_lakh'] is None
    assert before['retention_categories'] == [] and before['retention_deadline'] is None
    response = manage(site, 'admin_settings', token, settings=dict(currency_label='BAD', min_retentions=6, max_retentions=1))
    assert response.status_code == 400, response.get_json()
    after = auction(site, token, view='admin').get_json()
    assert after['settings']['currency_label'] == before['settings']['currency_label']
    assert after['settings']['min_retentions'] == before['settings']['min_retentions']
    response = manage(site, 'admin_settings', token, settings=dict(currency_label='<svg>'))
    assert response.status_code == 400, 'Currency labels are rendered throughout the room and must stay plain text'
    response = manage(site, 'admin_settings', token, settings=dict(min_base_price_lakh=99999))
    assert response.status_code == 400, 'The computed reserve floor is not an editable rule'


def test_manager_reads_and_finalizes_retention_without_owning_team(site):
    token = appointed(site)
    response = manage(site, 'admin_settings', token, settings=dict(min_retentions=0, max_retentions=2))
    assert response.status_code == 200, response.get_json()
    result = auction(site, token, view='preseason', team_id=site.other.id)
    assert result.status_code == 200, result.get_json()
    data = result.get_json()
    assert data['team_id'] == site.other.id and data['my_team_ids'] == []
    assert site.other.id in data['manager_team_ids']
    response = manage(site, 'admin_retention_confirm', token, team_id=site.other.id, token='old-preview')
    assert response.status_code == 400
    response = manage(site, 'admin_retention_confirm', token, team_id=site.other.id, token=data['retention']['token'])
    assert response.status_code == 200, response.get_json()
    site.db.expire_all()
    assert site.other.retention_finalized_at is not None
    from models import AuctionEvent
    event = site.db.query(AuctionEvent).filter_by(season_id=site.season.id, kind='team_retention_final').order_by(AuctionEvent.id.desc()).first()
    assert event.by_tg_id == DELEGATE, 'A manager is audited as themself, never impersonates the owner'


def test_manager_drafts_for_any_team_and_shared_turn_rules_still_apply(site):
    token = appointed(site)
    for row in (site.owner, site.other):
        response = manage(site, 'admin_draft_picks', token, team_id=row.id, picks=1)
        assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_draft_order', token, team_ids=[site.owner.id, site.other.id])
    assert response.status_code == 200, response.get_json()
    data = auction(site, token, view='eligible', team_id=site.owner.id).get_json()
    assert data['players']
    player = data['players'][0]['player_id']
    response = manage(site, 'admin_shortlist', token, team_id=site.owner.id, player_id=player, operation='add')
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_draft_pick', token, team_id=site.other.id, player_id=player)
    assert response.status_code == 400, 'Managers cannot skip the draft turn order'
    response = manage(site, 'admin_draft_pick', token, team_id=site.owner.id, player_id=player)
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_draft_skip', token, team_id=site.other.id)
    assert response.status_code == 200, response.get_json()
    site.db.expire_all()
    assert site.owner.draft_picks_used == 1 and site.other.draft_picks_used == 1


def test_draft_clock_trade_deadline_and_pool_tools_are_current(site):
    token = appointed(site)
    response = manage(site, 'admin_draft_clock', token, seconds=86400, timeout_action='shortlist')
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_draft_clock', token, operation='pause')
    assert response.status_code == 200, response.get_json()
    result = auction(site, token, view='admin').get_json()
    assert result['settings']['draft_pick_seconds'] == 86400
    assert result['settings']['draft_timeout_action'] == 'shortlist'
    response = manage(site, 'admin_trade_deadline', token, deadline_at='2035-01-01T12:00:00Z', deadline_matches=4)
    assert response.status_code == 200, response.get_json()
    result = auction(site, token, view='admin').get_json()
    assert result['trade_deadline_at'] == '2035-01-01T12:00:00Z'
    assert result['trade_deadline_matches'] == 4
    response = manage(site, 'admin_trade_deadline', token, deadline_at=None, deadline_matches=None)
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_pool_auto_sets', token, options=dict(marquee=1, set_size=2, versions=['Base']), replace=False)
    assert response.status_code == 200, response.get_json()
    response = manage(site, 'admin_pool_clear', token, confirm_name='incorrect')
    assert response.status_code == 400
    response = manage(site, 'admin_pool_clear', token, confirm_name=site.season.name)
    assert response.status_code == 200, response.get_json()
    from models import AuctionLot
    assert site.db.query(AuctionLot).filter_by(season_id=site.season.id).count() == 0


def test_team_manager_assignment_does_not_grant_settings_access(site):
    token = appointed(site, ADMIN)
    response = manage(site, 'admin_franchise', token, team_id=site.owner.id, name=site.owner.name,
                      owner_tg_id=str(OWNER), co_owners=[], team_managers=[OTHER])
    assert response.status_code == 200, response.get_json()
    team_token = login(site, OTHER)
    for action, values in [('admin_settings', dict(settings={'currency_label':'NO'})),
                           ('admin_franchise', dict(name='Forged', team_managers=[OTHER])),
                           ('admin_draft_skip', dict(team_id=site.owner.id))]:
        assert manage(site, action, team_token, **values).status_code == 403
    data = auction(site, team_token).get_json()
    assert 'team_manager' in data['roles']


def test_range_limits_use_shared_caps_and_can_reset_automatic_maximum(site):
    token = appointed(site)
    ranges = [dict(min_rating=70, max_rating=95, min_count=0, max_count=3, price_lakh=100)]
    response = manage(site, 'admin_settings', token, settings=dict(
        retention_ranges=ranges, retention_mode='ranges', min_retentions=0,
        retention_range_max_total=2, rtm_enabled=True, rtm_per_team=3))
    assert response.status_code == 200, response.get_json()
    data = auction(site, token, view='admin').get_json()
    assert data['settings']['max_retentions'] == 2
    assert all(team['rtm_left'] == 2 for team in data['teams'])
    response = manage(site, 'admin_settings', token, settings=dict(retention_range_max_total=None))
    assert response.status_code == 200, response.get_json()
    data = auction(site, token, view='admin').get_json()
    assert data['settings']['retention_range_max_total'] is None
    assert data['settings']['max_retentions'] == 3
    assert all(team['rtm_left'] == 3 for team in data['teams']), 'Changing retention capacity synchronizes automatic RTM cards'
    response = manage(site, 'admin_settings', token, settings=dict(min_retentions=2, retention_range_max_total=1))
    assert response.status_code == 400, 'A manager cannot set an overall cap below required retentions'
    data = auction(site, token, view='admin').get_json()
    assert data['settings']['retention_range_max_total'] is None
    assert data['settings']['max_retentions'] == 3
