"""Simulation stays read-only and bench advice respects the complete XI rules."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services import chemistry, chemistry_planner as planner, xi_rules


def squad(bench=True):
    specs = ([('Batsman', 'India')] * 3 + [('Batsman', 'Australia')]
             + [('Wicket Keeper', 'South Africa')]
             + [('All-rounder', 'England')] * 2 + [('Bowler', 'Australia')] * 4)
    if bench:
        specs += [('Batsman', 'India'), ('Batsman', 'India')]
    pairs = []
    for position, (category, country) in enumerate(specs, 1):
        version = {1: 'IPL2026', 2: 'T20WC', 12: 'BBL2025', 13: 'BBL2025'}.get(position, 'Base')
        player = SimpleNamespace(id=position, parent_player_id=None, name=f'Card {position}',
                                 category=category, country=country, version=version,
                                 rating=79 if position == 12 else 85, bat_rating=80,
                                 bowl_rating=85 if category == 'Bowler' else 30,
                                 bowl_style='Fast')
        pairs.append((SimpleNamespace(id=position, order_position=position), player))
    return pairs


def test_preview_and_recommendations_do_not_mutate_the_roster():
    roster = squad()
    original = deepcopy(roster)
    preview = planner.preview_swap(roster, 4, 12, config={})
    advice = planner.recommend_bench_swaps(iter(roster), config={})
    assert roster == original
    assert preview['before']['total'] == chemistry.calculate_role_report(
        [p for _, p in roster[:11]], config={})['total']
    changed = [p for _, p in roster[:11]]
    changed[3] = roster[11][1]
    assert preview['after']['total'] == chemistry.calculate_role_report(changed, config={})['total']
    assert preview['delta'] > 0 and preview['rating_delta'] == -6
    assert preview['after']['variety_types'] == 3
    assert preview['valid'] and advice


def test_suggestions_rank_chemistry_then_rating_and_only_show_each_bench_card_once():
    advice = planner.recommend_bench_swaps(squad(), limit=10, config={})
    assert [item['incoming']['roster_id'] for item in advice] == [13, 12]
    assert all(item['delta'] > 0 and item['valid'] for item in advice)
    assert len({item['incoming']['roster_id'] for item in advice}) == len(advice)
    assert len(planner.recommend_bench_swaps(squad(), limit=1, config={})) == 1


def test_illegal_duplicate_versions_and_keeper_removal_are_never_recommended():
    roster = squad()
    # A batter variant of the keeper cannot enter: keeping the keeper duplicates
    # the cricketer, and replacing the keeper leaves the XI without one.
    for _, player in roster[11:]:
        player.parent_player_id = 5
    preview = planner.preview_swap(roster, 4, 12, config={})
    assert not preview['valid'] and any('Duplicate' in warning for warning in preview['warnings'])
    assert planner.recommend_bench_swaps(roster, config={}) == []
    preview = planner.preview_swap(roster, 5, 12, config={})
    assert any('Wicket Keeper' in warning for warning in preview['warnings'])


def test_country_limit_and_role_counts_are_checked():
    roster = squad()
    for _, player in roster[:7]:
        player.country = 'India'
    roster[11][1].category = 'Bowler'
    preview = planner.preview_swap(roster, 8, 12, config={})
    assert not preview['valid']
    assert any('Max 7' in warning for warning in preview['warnings'])
    preview = planner.preview_swap(roster, 5, 12, config={})
    assert any('Wicket Keeper' in warning for warning in preview['warnings'])
    for item in planner.recommend_bench_swaps(roster, config={}):
        changed = list(roster)
        out_index = next(i for i, (entry, _) in enumerate(changed) if entry.id == item['outgoing']['roster_id'])
        in_index = next(i for i, (entry, _) in enumerate(changed) if entry.id == item['incoming']['roster_id'])
        changed[out_index], changed[in_index] = changed[in_index], changed[out_index]
        assert xi_rules.validate_roster_xi(changed)[0]
        assert chemistry.validate_country_rule([p for _, p in changed[:11]])[0]


def test_same_zone_swaps_keep_chemistry_and_partial_xis_have_no_invented_score():
    roster = squad()
    assert planner.preview_swap(roster, 1, 4, {})['delta'] == 0
    assert not planner.preview_swap(roster, 12, 13, {})['membership_changed']
    partial = planner.preview_swap(roster[:5], 1, 4, {})
    assert partial['before'] is None and partial['after'] is None and partial['delta'] is None
    assert planner.recommend_bench_swaps(roster[:11], config={}) == []
    assert planner.recommend_bench_swaps(roster, limit=0, config={}) == []


@pytest.mark.parametrize('first,second', [(1, 1), (1, 100), (100, 2)])
def test_only_current_owned_entries_can_be_previewed(first, second):
    with pytest.raises(ValueError):
        planner.preview_swap(squad(), first, second, config={})


def test_configured_versions_apply_to_both_sides_and_invalidate_old_previews():
    roster = squad()
    config = {'chemistry_variety_target': 3,
              'chemistry_variety_versions_json': ['IPL2026', 'T20WC']}
    preview = planner.preview_swap(roster, 4, 12, config)
    assert preview['after']['variety_types'] == preview['before']['variety_types'] == 2
    assert planner.roster_token(roster, config) != planner.roster_token(roster, {})
    previous = planner.roster_token(roster, config)
    roster[0][1].rating += 1
    assert planner.roster_token(roster, config) != previous


def test_rendering_exposes_rating_tradeoffs_and_escapes_card_names():
    roster = squad()
    roster[11][1].name = 'Incoming <script> & player'
    preview = planner.preview_swap(roster, 4, 12, {})
    text = planner.render_swap_preview(preview)
    assert 'Incoming &lt;script&gt; &amp; player' in text
    assert 'Average OVR' in text and 'Confirm to apply' in text
    advice = planner.render_recommendations([preview])
    assert 'OVR -6' in advice and 'Incoming &lt;script&gt;' in advice


def test_apply_changes_only_the_two_slots_and_normalizes_old_gaps():
    roster = squad()
    for position, (entry, _) in enumerate(roster, 1):
        entry.order_position = position * 2
    planner.apply_swap(roster, 4, 12)
    assert roster[3][0].order_position == 12 and roster[11][0].order_position == 4
    assert sorted(entry.order_position for entry, _ in roster) == list(range(1, 14))
