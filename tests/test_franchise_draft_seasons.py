"""Franchise drafts continue a league's history and publish playable squads."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def competition(tmp_path):
    from database import Base
    from models import ChallengeLeague, ChallengePlayer, ChallengeTeam, Player, Tournament, TournamentTeam
    from services import auction_service as A, league_season_service as LS

    engine = create_engine("sqlite:///" + str(tmp_path / "draft-seasons.db"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    mode = A._ensure_default_mode(db)
    league = ChallengeLeague(mode_id=mode.id, name="Previous league",
                             tournament_command="draftseason", is_active=True)
    db.add(league)
    db.flush()
    teams = [ChallengeTeam(league_id=league.id, name=name, sort_order=i)
             for i, name in enumerate(("North", "South"))]
    players = [Player(name="Season Player " + str(i), rating=90, category="Batsman",
                      country="England", version="Base", bat_hand="Right",
                      bowl_hand="Right", bowl_style="Medium Pacer",
                      bat_rating=90, bowl_rating=70, is_active=True)
               for i in range(4)]
    db.add_all(teams + players)
    db.flush()
    for team, player in zip(teams, players):
        db.add(ChallengePlayer(team_id=team.id, name=player.name,
                               source_player_id=player.id))
    tour = Tournament(name="Season 1", league_id=league.id, kind="challenge",
                      status="completed", format="Round Robin", league_format="single_rr")
    db.add(tour)
    db.flush()
    for i, team in enumerate(teams):
        db.add(TournamentTeam(tournament_id=tour.id, challenge_team_id=team.id,
                              name=team.name, owner_tg_id=101 + i,
                              owner_name="Owner " + str(i),
                              co_owner_ids_json=json.dumps([201 + i])))
    db.flush()
    series = LS.create_series(db, "Test competition")
    previous = LS.attach_season(db, series, tournament=tour)
    db.commit()
    yield SimpleNamespace(db=db, A=A, LS=LS, league=league, teams=teams,
                          tour=tour, series=series, previous=previous, players=players)
    db.close()
    engine.dispose()


def test_draft_continues_teams_owners_and_previous_season_without_old_squads(competition):
    c = competition
    from services import franchise_draft as D

    season, auction = c.LS.start_franchise_draft(c.db, c.series, purse_lakh=18000, rounds=3)
    c.db.commit()
    assert season.squad_mode == c.LS.MODE_DRAFT
    assert season.season_no == 2
    assert season.auction_season_id == auction.id
    assert auction.previous_league_id == c.league.id
    assert D.get_draft(c.db, auction).rounds == 3
    assert auction.min_squad_size == auction.max_squad_size == 3
    assert auction.min_overseas == 0 and auction.max_overseas == 3
    assert auction.min_retentions == auction.max_retentions == 0
    assert not auction.rtm_enabled
    field = c.A.franchises(c.db, auction.id)
    assert [team.name for team in field] == ["North", "South"]
    assert [team.owner_tg_id for team in field] == [101, 102]
    assert [c.A.co_owner_ids(team) for team in field] == [[201], [202]]
    for team in field:
        assert team.purse_total_lakh == team.purse_remaining_lakh == 18000
        assert c.A.squad(c.db, team.id) == []
        assert sum(row.amount_lakh for row in c.A.ledger(c.db, team.id)) == 18000
    from models import ChallengePlayer
    assert c.db.query(ChallengePlayer).count() == 2
    assert "Franchise draft" in c.LS.mode_label(season)


def test_draft_cloning_preserves_roles_and_squad_rules_but_resets_retention(competition):
    c = competition
    old = c.A.create_season(c.db, "Previous auction", min_squad_size=1, max_squad_size=4,
                            max_retentions=2, min_retentions=1, rtm_enabled=True,
                            role_maximums_json=json.dumps({"Batsman": 2}),
                            rating_rules_json=json.dumps([{"max_rating": 90, "min_players": 1}]))
    old.status = c.A.STATUS_COMPLETED
    old.league_id = c.league.id
    for i, team in enumerate(c.teams):
        c.A.create_franchise(c.db, old, team.name, owner_tg_id=101 + i,
                             co_owner_ids_json=json.dumps([201 + i]),
                             team_manager_ids_json=json.dumps([301 + i]))
    c.previous.auction_season_id = old.id
    c.db.commit()
    _, auction = c.LS.start_franchise_draft(c.db, c.series, purse_lakh=9000, rounds=2)
    assert auction.previous_season_id == old.id
    assert c.A.role_maximums(auction) == c.A.role_maximums(old)
    assert c.A.rating_rules(auction) == c.A.rating_rules(old)
    assert auction.retention_deadline_at is None
    assert auction.min_retentions == auction.max_retentions == 0
    assert not auction.rtm_enabled
    assert [c.A.team_manager_ids(team) for team in c.A.franchises(c.db, auction.id)] == [[301], [302]]


def test_draft_adopts_an_empty_next_setup_and_refuses_signed_players(competition):
    c = competition
    next_auction = c.A.season_from_league(c.db, c.league, "Already prepared")
    c.db.commit()
    season, adopted = c.LS.start_franchise_draft(c.db, c.series, rounds=2)
    assert adopted.id == next_auction.id
    c.db.rollback()
    # A pre-existing setup can contain a manager signing that is an ordinary
    # SOLD lot rather than retention. It must also prevent a silent reset.
    next_auction = c.db.get(type(next_auction), next_auction.id)
    c.A.add_players_to_pool(c.db, next_auction, [c.players[0]])
    from models import AuctionLot
    lot = c.db.query(AuctionLot).filter_by(season_id=next_auction.id).one()
    lot.status = c.A.LOT_SOLD
    lot.acquisition = c.A.ACQ_AUCTION
    lot.sold_to_id = c.A.franchises(c.db, next_auction.id)[0].id
    lot.sold_price_lakh = 100
    c.db.commit()
    with pytest.raises(c.LS.LeagueSeasonError, match="already has signed players"):
        c.LS.start_franchise_draft(c.db, c.series, rounds=2)
    assert lot.status == c.A.LOT_SOLD


@pytest.mark.parametrize("status,expected", [
    ("setup", "start the draft"), ("live", "draft live"),
    ("paused", "draft paused"), ("completed", "Draft done"),
    ("cancelled", "Draft cancelled"),
])
def test_season_stage_describes_draft_progress(competition, status, expected):
    c = competition
    season, auction = c.LS.start_franchise_draft(c.db, c.series, rounds=1)
    auction.status = status
    assert expected in c.LS.season_stage(c.db, season)["label"]


def test_next_draft_requires_completed_season_and_positive_purse(competition):
    c = competition
    with pytest.raises(c.LS.LeagueSeasonError, match="more than zero"):
        c.LS.start_franchise_draft(c.db, c.series, purse_lakh=0)
    c.tour.status = "active"
    with pytest.raises(c.LS.LeagueSeasonError, match="not completed"):
        c.LS.start_franchise_draft(c.db, c.series)


def test_completed_draft_publishes_squads_and_continues_into_owned_tournament(competition):
    c = competition
    from models import ChallengePlayer, TournamentTeam
    from services import franchise_draft as D

    season, auction = c.LS.start_franchise_draft(c.db, c.series, purse_lakh=10000, rounds=1)
    c.A.bind_chat(c.db, auction, -1007654321)
    c.A.add_players_to_pool(c.db, auction, c.players[2:])
    D.set_rating_prices(c.db, auction, "90:5cr")
    D.start(c.db, auction)
    c.db.commit()
    for player in c.players[2:]:
        actor = D.turn(c.db, auction).owner_tg_id
        confirmation = D.preview_pick(c.db, auction, actor, player.name)
        c.db.commit()
        D.confirm_pick(c.db, auction, confirmation.token, actor)
        c.db.commit()
    assert auction.status == c.A.STATUS_COMPLETED
    league = c.LS.publish_season_squads(c.db, season)
    c.db.commit()
    assert league.id != c.league.id
    assert season.league_id == league.id
    assert c.db.query(ChallengePlayer).count() == 4
    assert c.LS.season_stage(c.db, season)["key"] == "ready"
    tour, _message = c.LS.create_season_tournament(c.db, season)
    c.db.commit()
    entries = c.db.query(TournamentTeam).filter_by(tournament_id=tour.id).order_by(TournamentTeam.name).all()
    assert [row.owner_tg_id for row in entries] == [101, 102]
    assert [json.loads(row.co_owner_ids_json) for row in entries] == [[201], [202]]
    assert league.tournament_command == "draftseason"
    assert c.league.tournament_command is None
    assert not c.league.is_active

    # A later draft receives its own untouched config, never the old cursor.
    tour.status = "completed"
    c.db.commit()
    third, fresh = c.LS.start_franchise_draft(c.db, c.series, rounds=2)
    assert third.season_no == 3
    assert fresh.previous_season_id == auction.id
    assert D.get_draft(c.db, fresh).rounds == 2
    assert D.get_draft(c.db, fresh).cursor == 0
    assert D.get_draft(c.db, auction).cursor == 2
