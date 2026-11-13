"""Native draft setup stays guided, role checked and distinct from bidding."""
import json

import pytest

from test_franchise_bot_ui import (ADMIN, DELEGATE, OWNER, context, input_step,
                                   menu, run, site, update)  # noqa: F401


def as_draft(site, rounds=3):
    from services import franchise_draft as D
    D.enable(site.db, site.season, rounds=rounds, by_tg_id=DELEGATE)
    site.db.commit()
    return D


def render(site, menu, view, argument='', actor=DELEGATE):
    data = menu.read_workspace(site.db, actor, site.season.id, 1, view, argument)
    text, markup = menu.render_workspace(data, actor, site.season.id, view, argument, context(), 1)
    return data, text, [button for row in markup.inline_keyboard for button in row]


def test_new_draft_guided_form_saves_name_rounds_purse_and_selection(site, menu):
    from models import AuctionSeason
    from services import franchise_draft as D
    ctx = context()
    run(menu._start_form(update(), ctx, 'newdraft', None))
    input_step(menu, ctx, DELEGATE, 'Premier Draft Season 2')
    input_step(menu, ctx, DELEGATE, '18')
    event = input_step(menu, ctx, DELEGATE, '17500')
    assert '✅' in event.effective_message.replies[-1][0]
    selected = ctx.user_data['franchise_season_id']
    assert selected != site.season.id
    fresh = site.db.get(AuctionSeason, selected)
    assert fresh.name == 'Premier Draft Season 2'
    assert fresh.opening_purse_lakh == 17500
    assert fresh.min_squad_size == fresh.max_squad_size == 18
    assert D.get_draft(site.db, fresh).rounds == 18
    assert not ctx.user_data.get(menu.PROMPT_KEY)


def test_existing_leagues_offer_auction_and_draft_and_seed_draft_form(site, menu):
    from models import AuctionSeason, ChallengeLeague, ChallengeTeam
    from services import franchise_draft as D
    mode = site.A._ensure_default_mode(site.db)
    league = ChallengeLeague(mode_id=mode.id, name='Previous Premier', is_active=True)
    site.db.add(league)
    site.db.flush()
    site.db.add_all([ChallengeTeam(league_id=league.id, name=name) for name in ('North', 'South')])
    site.db.commit()
    _, text, buttons = render(site, menu, 'leagues', '1')
    routes = [button.callback_data for button in buttons if button.callback_data]
    assert 'fixed-price draft' in text
    assert any(route.endswith(f'f:fromleague:{league.id}') for route in routes)
    assert any(route.endswith(f'f:fromleaguedraft:{league.id}') for route in routes)
    ctx = context()
    event = update(callback=menu._callback(DELEGATE, site.season.id, f'f:fromleaguedraft:{league.id}'))
    run(menu.callback_handler(event, ctx))
    record = ctx.user_data[menu.PROMPT_KEY]
    assert record['step'] == 1 and record['payload']['league_id'] == league.id
    input_step(menu, ctx, DELEGATE, 'Premier draft next season')
    input_step(menu, ctx, DELEGATE, '12')
    input_step(menu, ctx, DELEGATE, '15000')
    fresh = site.db.get(AuctionSeason, ctx.user_data['franchise_season_id'])
    assert fresh.previous_league_id == league.id
    assert D.get_draft(site.db, fresh).rounds == 12
    assert fresh.opening_purse_lakh == 15000
    assert {team.name for team in site.A.franchises(site.db, fresh.id)} == {'North', 'South'}


def test_native_draft_pricing_order_and_rounds_use_shared_engine(site, menu):
    D = as_draft(site)
    sid = site.season.id
    for action, values in [
        ('native_fd_prices', {'text': '85:15cr\n84:12.5cr'}),
        ('native_fd_custom', {'text': 'Website Player:16.25cr'}),
        ('native_fd_order', {'text': 'Others | Owners'}),
        ('native_fd_rounds', {'rounds': 5}),
    ]:
        menu.execute_action(site.db, DELEGATE, sid, 1, action, values)
        site.db.commit()
    config = D.get_draft(site.db, site.season)
    assert json.loads(config.rating_prices_json) == {'85': 1500, '84': 1250}
    assert list(json.loads(config.custom_prices_json).values()) == [1625]
    assert [team.name for team in D.round_order(site.db, site.season, 1)] == ['Others', 'Owners']
    assert config.rounds == site.season.min_squad_size == site.season.max_squad_size == 5
    data, text, buttons = render(site, menu, 'franchisedraft')
    assert data['franchise_draft']['rounds'] == 5
    assert 'Draft' in text and '5' in text
    assert any(button.text == 'Rating prices' for button in buttons)
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons if button.callback_data)


@pytest.mark.parametrize('action,values', [
    ('native_fd_prices', {'text': '85:15cr'}),
    ('native_fd_custom', {'text': 'Website Player:20cr'}),
    ('native_fd_order', {'text': 'Others|Owners'}),
    ('native_fd_rounds', {'rounds': 6}),
    ('native_fd_start', {}),
])
def test_owner_cannot_use_native_draft_manager_actions(site, menu, action, values):
    as_draft(site)
    with pytest.raises(menu.W.AccessError, match='Managers'):
        menu.execute_action(site.db, OWNER, site.season.id, 1, action, values)


def test_draft_settings_offer_role_and_rating_rules_without_auction_settings(site, menu):
    as_draft(site)
    _, _, buttons = render(site, menu, 'fields', 'squad-1')
    labels = {button.text for button in buttons}
    assert {'Minimum players by role', 'Maximum players by role', 'Required lower-rated players'} <= labels
    assert not any('overseas' in label.lower() or 'squad size' in label.lower() for label in labels)
    _, _, buttons = render(site, menu, 'settings')
    labels = {button.text for button in buttons}
    assert not {'Clock & bidding', 'Retention', 'Draft & RTM', 'Trades'} & labels
    _, _, buttons = render(site, menu, 'fields', 'general-1')
    assert 'Opening purse' in {button.text for button in buttons}
    assert not any('base-price' in button.text for button in buttons)


def test_owner_draft_room_has_public_state_and_no_manager_controls(site, menu):
    as_draft(site)
    data, text, buttons = render(site, menu, 'franchisedraft', actor=OWNER)
    assert data['franchise_draft']['cursor'] == 0
    assert 'No overseas limit' in text
    assert '/dpick Joe Root' in text
    assert not any(button.text in ('Rating prices', 'Custom player prices', 'Undo last pick', 'Total rounds') for button in buttons)
    _, _, home_buttons = render(site, menu, 'home', actor=OWNER)
    assert any('Draft room' in button.text for button in home_buttons)
    assert not any('Live room' in button.text for button in home_buttons)


def test_draft_native_forms_and_views_keep_telegram_callback_limits(site, menu):
    as_draft(site)
    for view, argument in [('home', ''), ('franchisedraft', ''), ('control', ''), ('settings', ''),
                           ('fields', 'squad-1'), ('rules', ''), ('finish', '')]:
        _, text, buttons = render(site, menu, view, argument)
        assert len(text) <= 4096
        assert all(len(button.callback_data.encode()) <= 64 for button in buttons if button.callback_data)
