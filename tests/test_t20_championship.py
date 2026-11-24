"""World T20 fixtures and scorecards must stay on their scheduled championship."""
from collections import Counter
from datetime import date, datetime, timedelta
from fractions import Fraction
from random import Random

import pytest


def test_balanced_home_and_away_calendar():
    from services.t20_championship_schedule import plan

    start = date(2026, 1, 1)
    rows = plan(list(range(10)), 6, 3, start, start + timedelta(days=27), 2)
    home_counts = Counter(home for home, _, _, _ in rows)
    away_counts = Counter(away for _, away, _, _ in rows)
    assert len(rows) == len({frozenset((home, away)) for home, away, _, _ in rows}) == 30
    assert set(home_counts.values()) == set(away_counts.values()) == {3}
    for team in range(10):
        days = [day for home, away, day, _ in rows if team in (home, away)]
        assert len(days) == 6
        assert all((b - a).days >= 5 for a, b in zip(days, days[1:]))
    with pytest.raises(ValueError, match="28 calendar days"):
        plan(list(range(10)), 6, 3, start, start + timedelta(days=26), 2)

    odd = plan(list(range(7)), 3, 5, start, start + timedelta(days=40), 0)
    degrees = Counter(team for home, away, _, _ in odd for team in (home, away))
    assert sorted(degrees.values()) == [2, 3, 3, 3, 3, 3, 3]


def test_series_length_range_fits_the_calendar_and_balances_matches():
    from services.t20_championship_schedule import plan

    start = date(2026, 1, 1)
    teams = list(range(10))
    end = start + timedelta(days=42)
    rows = plan(teams, 6, 4, start, end, 2, max_duration=7, rng=Random(2))
    lengths = {count for _, _, _, count in rows}
    assert len(lengths) >= 2 and all(4 <= length <= 7 for length in lengths)
    assert rows != plan(teams, 6, 4, start, end, 2, max_duration=7, rng=Random(8))
    totals = []
    for team in teams:
        tours = [(day, count) for home, away, day, count in rows if team in (home, away)]
        totals.append(sum(count for _, count in tours))
        assert all((following - day).days >= count + 2
                   for (day, count), (following, _) in zip(tours, tours[1:]))
        assert tours[-1][0] + timedelta(days=tours[-1][1] - 1) <= end
    assert max(totals) - min(totals) <= 5

    tight = plan(teams, 6, 4, start, start + timedelta(days=33), 2, max_duration=7)
    assert {count for _, _, _, count in tight} == {4}
    small = plan(teams[:2], 1, 4, start, start + timedelta(days=4), 2, max_duration=7)
    assert small[0][-1] == 5
    odd = plan(teams[:7], 3, 6, start, start + timedelta(days=59), 2, max_duration=8, rng=Random(1))
    totals = [sum(count for home, away, _, count in odd if team in (home, away)) for team in teams[:7]]
    assert max(totals) - min(totals) <= 5
    with pytest.raises(ValueError, match="maximum at or above"):
        plan(teams, 6, 7, start, end, 2, max_duration=4)


@pytest.fixture
def competition():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database import Base
    from models import ChallengePlayer, ChallengeTeam, T20ChampionshipTeam, User
    from services import t20_championship as cup

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, autoflush=False) as db:
            championship = cup.create(db, "World T20", "/wt20c")
            for i, (name, stadium) in enumerate(
                    (("India", "Eden Gardens"), ("Australia", "Melbourne Cricket Ground"),
                     ("England", "Lord's Cricket Ground")), 1):
                club = ChallengeTeam(league=championship.league, name=name)
                club.players = [ChallengePlayer(name=f"{name} player {j}") for j in range(11)]
                db.add(T20ChampionshipTeam(championship=championship, team=club, owner_id=i,
                                           home_stadium=stadium))
                db.add(User(telegram_id=i, first_name=name))
            db.commit()
            yield db, championship
    finally:
        engine.dispose()


def test_played_fixture_stats_series_and_final(competition):
    from models import Match, User
    from services.match_constants import PITCH_TYPES
    from services import t20_championship as cup, t20_championship_stats as stats

    db, championship = competition
    india, australia, england = championship.teams
    today = date.today()
    now = datetime.utcnow()
    championship.final_format = "best_of_3"
    first = cup.add_series(db, championship, australia, india, 3, today)
    second = cup.add_series(db, championship, india, england, 3, today)
    for fixture in first.fixtures + second.fixtures:
        fixture.opens_at, fixture.deadline = now - timedelta(hours=1), now + timedelta(hours=1)
    db.flush()
    cup.start(championship, now)
    db.flush()

    rows = cup.series_list(db, championship)
    pending = cup.next_fixtures(rows)
    opening = first.fixtures[0]
    assert cup.fixture_status(championship, opening, pending, now) == "available"
    assert cup.fixture_status(championship, first.fixtures[1], pending, now) == "upcoming"
    assert cup.fixture_status(championship, second.fixtures[0], pending, now) == "upcoming"
    assert cup.fixture_status(championship, opening, pending, now + timedelta(hours=2)) == "postponed"
    with pytest.raises(ValueError, match="locked"):
        cup.update_points(championship, cup.POINTS)

    host = db.query(User).filter_by(telegram_id=india.owner_id).one()
    guest = db.query(User).filter_by(telegram_id=australia.owner_id).one()
    game = Match(user1_id=guest.id, user2_id=host.id, status="active",
                 stadium=australia.home_stadium, pitch_type=PITCH_TYPES[0])
    db.add(game)
    db.flush()
    tag = dict(championship_id=championship.id, fixture_id=opening.id, users=[guest.id, host.id],
               teams=[australia.id, india.id], pitch_mode="home", stadium=australia.home_stadium)
    cup.bind(db, tag, game, guest, host)
    db.flush()
    assert opening.status == "live"
    with pytest.raises(cup.FixtureUnavailable):
        cup.bind(db, tag, game, guest, host)
    game.status = "abandoned"
    db.flush()
    assert cup.heal_abandoned(db) == 1
    db.refresh(opening)
    assert opening.status == "scheduled" and opening.match_id is None
    game.status = "active"
    cup.bind(db, tag, game, guest, host)
    db.flush()

    state = {
        "match_id": game.id, "t20_championship": tag, "inn1_team": india.name,
        "inn1_bat_team": india.name, "inn1_bowl_team": australia.name,
        "inn1_bat_team_id": host.id, "inn1_bowl_team_id": guest.id,
        "inn1_runs": 150, "inn1_wickets": 10, "total_runs": 100, "total_wickets": 4,
        "bat_team_id": guest.id, "bowl_team_id": host.id,
        "bat_team_name": australia.name, "bowl_team_name": india.name,
        "inn1_bat_xi": [{"roster_id": 1, "name": "India batter"}],
        "inn1_bowl_xi": [{"roster_id": 2, "name": "Australia bowler"}],
        "bat_xi": [{"roster_id": 2, "name": "Australia bowler"}],
        "bowl_xi": [{"roster_id": 1, "name": "India batter"}, {"roster_id": 3, "name": "India keeper"}],
        "inn1_bat_stats": {"1": {"runs": 80, "balls": 60, "out": True, "fours": 4, "sixes": 3}},
        "inn1_bowl_stats": {"2": {"balls": 100, "runs": 150, "wickets": 10}},
        "bat_stats": {"2": {"runs": 100, "balls": 120}},
        "bowl_stats": {"1": {"balls": 120, "runs": 100, "wickets": 4, "hattrick": True}},
        "record_deliveries": [{"wicket": True, "opponent": india.name, "fielder": "India keeper",
                               "how": "caught behind", "keeper": True}],
    }
    game.status, game.winner_id = "completed", host.id
    cup.record_played(db, state, winner_user_id=host.id)
    cup.record_played(db, state, winner_user_id=host.id)  # a retried finish counts once
    table = {row["team"].id: row for row in cup.standings(championship, rows)}
    assert table[india.id]["played"] == 1
    assert table[india.id]["nrr"] == Fraction(5, 2)
    assert table[india.id]["pct"] == 100

    cup.set_result(db, championship, first.fixtures[1], "tie")
    cup.set_result(db, championship, first.fixtures[2], "forfeit", india.id)
    assert cup.series_score(first) == (0, 2)
    assert cup.fixture_status(championship, second.fixtures[0], cup.next_fixtures(rows), now) == "available"
    cup.set_result(db, championship, second.fixtures[0], "no_result")
    cup.set_result(db, championship, second.fixtures[1], "win", england.id)
    cup.set_result(db, championship, second.fixtures[2], "win", india.id)
    db.flush()
    rows = cup.series_list(db, championship)
    final = rows[-1]
    assert final.is_final and len(final.fixtures) == 3
    assert (final.home_id, final.away_id) == (india.id, england.id)
    cup.set_result(db, championship, final.fixtures[0], "win", india.id)
    cup.set_result(db, championship, final.fixtures[1], "win", india.id, captain_id=host.telegram_id)
    assert final.fixtures[2].status == "cancelled"
    assert championship.champion_id == india.id
    standings = cup.standings(championship, rows)
    assert standings[0]["pct"] == Fraction(200, 3)
    assert standings[0]["played"] == 6
    assert standings[0]["away_wins"] == 1
    assert standings[0]["series_drawn"] == 1

    india_stats = stats.aggregate(rows, team_id=india.id, series_id=first.id)
    figures = {row["name"]: row for row in india_stats}
    assert figures["India batter"]["bat_runs"] == 80
    assert figures["India batter"]["hattricks"] == 1
    assert figures["India keeper"]["matches"] == 1
    assert figures["India keeper"]["keeper_dismissals"] == 1
    from handlers.t20_championship import scorecard
    assert "India 150/10 (16.4 overs)" in scorecard(opening)


def test_command_names_and_no_final(competition):
    from services import t20_championship as cup

    db, championship = competition
    assert cup.find_command(db, "wt20cscorecard") == (championship, "scorecard")
    with pytest.raises(ValueError, match="/wt20ctable"):
        cup.create(db, "Conflicting championship", "/wt20ctable")
    other = cup.create(db, "Different championship", "/wt20b")
    assert cup.find_command(db, "wt20btable") == (other, "table")
    assert cup.find_command(db, "wt20ctable") == (championship, "table")

    india, australia, _ = championship.teams
    first = cup.add_series(db, championship, india, australia, 3, date.today())
    db.flush()
    championship.final_format = "none"
    cup.update_points(championship, {**cup.POINTS, "win": 4, "tie": 2})
    cup.start(championship)
    cup.set_result(db, championship, first.fixtures[0], "win", india.id)
    cup.set_result(db, championship, first.fixtures[1], "tie")
    first.fixtures[2].status = "cancelled"
    cup.advance(db, championship)
    assert championship.champion_id == india.id
    assert len(cup.series_list(db, championship)) == 1
    assert cup.standings(championship, [first])[0]["pct"] == 75


def test_telegram_dispatch_uses_the_selected_squads_and_next_fixture(competition, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import challenge
    from handlers.t20_championship import dispatch
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    now = datetime.utcnow()
    today = (now + cup.IST_OFFSET).date()
    cup.generate(db, championship, team_count=2, opponents=1, matches=3,
                 start=today, end=today + timedelta(days=2), rest=0,
                 team_ids=[india.id, australia.id])
    db.flush()
    cup.start(championship, now)
    db.flush()
    row, = cup.series_list(db, championship)
    assert not england.is_participating
    assert len(cup.standings(championship, [row])) == 2

    message = SimpleNamespace(text="/wt20c", reply_to_message=None, reply_text=AsyncMock())
    ctx = SimpleNamespace(bot=SimpleNamespace(), bot_data={}, job_queue=None)
    update = SimpleNamespace(effective_message=message, message=message,
                             effective_chat=SimpleNamespace(id=-9876),
                             effective_user=SimpleNamespace(id=row.away.owner_id, is_bot=False))
    assert asyncio.run(dispatch(update, ctx, db, "wt20c")) is True
    assert "2 teams" in message.reply_text.await_args.args[0]

    message.text = "/wt20c playoffs"
    assert asyncio.run(dispatch(update, ctx, db, "wt20c")) is True
    assert "Top-four semifinals are off." in message.reply_text.await_args.args[0]

    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=row.home.owner_id, is_bot=False))
    message.text = f"/wt20cplay {row.fixtures[1].id}"
    assert asyncio.run(dispatch(update, ctx, db, "wt20cplay")) is True
    assert "No fixture" in message.reply_text.await_args.args[0]
    assert not ctx.bot_data

    monkeypatch.setattr(challenge.R, "send_rich_message", AsyncMock(return_value=SimpleNamespace(message_id=1)))
    message.text = "/wt20c"
    assert asyncio.run(dispatch(update, ctx, db, "wt20c")) is True
    draft = next(value for key, value in ctx.bot_data.items()
                 if key.startswith("challenge_team_draft_"))
    assert draft["t20_championship"]["fixture_id"] == row.fixtures[0].id
    assert draft["ball_format"] == "T20"
    assert draft["league_id"] == championship.league_id
    assert draft["host_tg_id"] == row.home.owner_id
    assert draft["target_tg_id"] == row.away.owner_id
    assert draft["stadium"] == row.home.home_stadium
    assert [len(challenge._query_team_players(db, draft, side)) for side in ("host", "target")] == [11, 11]

    after_pitch = AsyncMock()
    monkeypatch.setattr(challenge, "_after_pitch_selected", after_pitch)
    monkeypatch.setattr(challenge.R, "edit_rich", AsyncMock(return_value=True))
    query = SimpleNamespace(data=f"cl_pitch_{draft['draft_id']}_0", message=message,
                            from_user=update.effective_user, answer=AsyncMock())
    asyncio.run(challenge.challenge_pitch_callback(SimpleNamespace(callback_query=query), ctx))
    assert "Only the host" in query.answer.await_args.args[0]
    assert not draft.get("pitch_type")
    query.from_user = message.reply_to_message.from_user
    asyncio.run(challenge.challenge_pitch_callback(SimpleNamespace(callback_query=query), ctx))
    assert draft["pitch_type"] == challenge.PITCH_TYPES[0]
    after_pitch.assert_awaited_once()


def test_series_win_bonus_adjusts_earned_and_available_points(competition):
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    championship.final_format = "none"
    # A championship created before bonuses stored only the match-point keys.
    championship.points = '{"win":2,"tie":1,"no_result":1,"loss":0,"forfeit":0}'
    assert cup.point_system(championship)["series_win_bonus"] == 0
    cup.update_points(championship, {**cup.POINTS, "series_win_bonus": 1})
    short = cup.add_series(db, championship, india, australia, 3, date.today())
    long = cup.add_series(db, championship, australia, england, 5, date.today())
    cancelled = cup.add_series(db, championship, england, india, 3, date.today())
    for fixture in cancelled.fixtures:
        fixture.status = "cancelled"
    db.flush()
    cup.start(championship)

    def table():
        return {row["team"].id: row for row in cup.standings(championship, [short, long, cancelled])}

    cup.set_result(db, championship, short.fixtures[0], "win", india.id)
    cup.set_result(db, championship, short.fixtures[1], "win", india.id)
    assert (table()[india.id]["points"], table()[india.id]["max_points"]) == (4, 4)

    cup.set_result(db, championship, short.fixtures[2], "win", australia.id)
    rows = table()
    assert (rows[india.id]["bonus"], rows[india.id]["points"], rows[india.id]["max_points"]) == (1, 5, 7)
    assert rows[india.id]["pct"] == Fraction(500, 7)
    assert (rows[australia.id]["bonus"], rows[australia.id]["max_points"]) == (0, 7)

    for fixture, winner in zip(long.fixtures, (australia, england, australia, england, None)):
        cup.set_result(db, championship, fixture, "win" if winner else "no_result",
                       winner.id if winner else None)

    rows = table()
    assert (rows[england.id]["bonus"], rows[england.id]["points"], rows[england.id]["max_points"]) == (0, 5, 11)
    assert rows[australia.id]["max_points"] == 18
    assert rows[india.id]["max_points"] == 7


def test_best_of_five_semifinals_seed_a_full_five_match_final(competition):
    from handlers.t20_championship import render
    from models import ChallengePlayer, ChallengeTeam, T20ChampionshipFixture, T20ChampionshipTeam
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    championship.playoff_format = "best_of_5"
    championship.final_format = "five_matches"
    championship.final_venue = "Neutral venue"
    now = datetime.utcnow()
    championship.playoff_deadline = now + timedelta(days=5)
    championship.final_deadline = now + timedelta(days=10)
    cup.update_points(championship, {**cup.POINTS, "series_win_bonus": 2})
    league1 = cup.add_series(db, championship, india, england, 3, date.today())
    db.flush()
    with pytest.raises(ValueError, match="four participating teams"):
        cup.start(championship)
    club = ChallengeTeam(league=championship.league, name="South Africa")
    club.players = [ChallengePlayer(name=f"South Africa player {j}") for j in range(11)]
    south_africa = T20ChampionshipTeam(championship=championship, team=club, owner_id=4,
                                      home_stadium="Newlands Cricket Ground")
    db.add(south_africa)
    db.flush()
    league2 = cup.add_series(db, championship, australia, south_africa, 3, date.today())
    db.flush()
    cup.start(championship)

    for fixture in league1.fixtures:
        cup.set_result(db, championship, fixture, "win", india.id)
    for fixture, winner in zip(league2.fixtures, (australia, south_africa, australia)):
        cup.set_result(db, championship, fixture, "win", winner.id)
    db.commit()

    rows = cup.series_list(db, championship)
    first, second = rows[-2:]
    assert (first.semifinal_no, first.home_id, first.away_id) == (1, india.id, england.id)
    assert (second.semifinal_no, second.home_id, second.away_id) == (2, australia.id, south_africa.id)
    assert [len(series.fixtures) for series in (first, second)] == [5, 5]
    assert all(f.deadline == championship.playoff_deadline for f in first.fixtures + second.fixtures)
    assert any("Semifinal 2" in text for text in render(db, championship, "playoffs", []))
    with pytest.raises(ValueError, match="postseason"):
        cup.editable_schedule(championship)

    for fixture in first.fixtures[:3]:
        cup.set_result(db, championship, fixture, "win", england.id)
    assert [f.status for f in first.fixtures[3:]] == ["cancelled", "cancelled"]
    for fixture, winner in zip(second.fixtures, (australia, south_africa, south_africa, australia, None)):
        cup.set_result(db, championship, fixture, "win" if winner else "tie",
                       winner.id if winner else None)
    assert cup.needs_decider(second)
    assert len(cup.series_list(db, championship)) == 4

    decider = T20ChampionshipFixture(number=6, opens_at=now, deadline=championship.playoff_deadline)
    second.fixtures.append(decider)
    db.flush()
    cup.set_result(db, championship, decider, "win", south_africa.id)
    db.commit()

    rows = cup.series_list(db, championship)
    final = rows[-1]
    assert final.is_final and (final.home_id, final.away_id) == (england.id, south_africa.id)
    assert final.venue == "Neutral venue"
    assert len(final.fixtures) == 5
    assert all(f.deadline == championship.final_deadline for f in final.fixtures)
    for fixture in final.fixtures[:3]:
        cup.set_result(db, championship, fixture, "win", england.id)
    assert not championship.completed_at
    assert all(f.status == "scheduled" for f in final.fixtures[3:])
    for fixture in final.fixtures[3:]:
        cup.set_result(db, championship, fixture, "win", south_africa.id)
    assert championship.champion_id == england.id

    table = cup.standings(championship, rows)
    assert [row["team"].id for row in table] == [india.id, australia.id, south_africa.id, england.id]
    assert [row["bonus"] for row in table] == [2, 2, 0, 0]
    assert all(row["played"] == 3 and row["max_points"] == 8 for row in table)


@pytest.fixture
def roster_admin(competition, monkeypatch):
    from sqlalchemy.orm import Session

    import admin
    from models import Player, User
    from services import t20_championship as cup

    db, _ = competition
    championship = cup.create(db, "Source Cup", "sourcecup")
    destination = cup.create(db, "Destination Cup", "destinationcup")
    rival = cup.create(db, "Separate Cup", "separatecup")
    db.add(User(telegram_id=555, username="ownername"))
    for name, version, rating, active in (
        ("Virat Kohli", "Base", 88, True),
        ("Virat Kohli", "2026", 99, True),
        ("Virat Kohli", "Inactive", 100, False),
        ("Jasprit Bumrah", "Base", 90, True),
        ("Glenn Maxwell", "Base", 90, True),
    ):
        db.add(Player(name=name, version=version, rating=rating, is_active=active,
                      category="Batsman", country="India", bat_hand="Right", bowl_hand="Right",
                      bowl_style="Fast", bat_rating=rating, bowl_rating=30))
    db.commit()

    monkeypatch.setattr(admin, "get_session", lambda: Session(bind=db.get_bind(), autoflush=False))
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    client = admin.app.test_client()
    with client.session_transaction() as auth:
        auth["admin"] = True
    return client, db, championship, destination, rival


def _roster_upload(client, championship_id, filename, data):
    from io import BytesIO

    return client.post(f"/world-t20/{championship_id}/teams/import",
                       data={"roster_file": (BytesIO(data), filename)}, follow_redirects=True)


def test_admin_bulk_roster_templates_and_roundtrip(roster_admin):
    import json

    from services import t20_championship_io as roster_io

    client, db, championship, destination, rival = roster_admin
    base = f"/world-t20/{championship.id}"
    page = client.get(base + "?tab=teams")
    assert b"Bulk teams and squads" in page.data
    for file_format in roster_io.MIMETYPES:
        template = client.get(f"{base}/teams/template.{file_format}")
        assert template.status_code == 200
        assert f"world-t20-squad-template.{file_format}" in template.headers["Content-Disposition"]
        teams = roster_io.read_file(template.data, file_format)
        assert len(teams) == 2 and teams[0]["players"] == ["Virat Kohli", "Jasprit Bumrah"]

    csv_data = (
        "team_name,short_name,logo_url,primary_colour,secondary_color,owner,co_owner,player_name,home_stadium\n"
        "=India,IND,/static/challenge_leagues/india.png,#abc,#123456,@OwnerName,556,Virat Kohli,Eden\n"
        "=india,,,,,,,Jasprit Bumrah\n"
        "=India,,,,,,,virat kohli\n"
        '"Australia, XI",AUS,,#ffcd00,,999,,Glenn Maxwell\n'
        "Reserve XI,RES,,,,999,,\n"
    ).encode()
    response = _roster_upload(client, championship.id, "source.CSV", csv_data)
    assert response.status_code == 200
    assert b"Teams created 3" in response.data and b"Players added 3" in response.data

    db.expire_all()
    india, australia, reserve = roster_io.championship_teams(db, championship.id)
    assert india.name == "=India"
    assert (india.owner_id, india.co_owner_id) == (555, 556)
    assert india.home_stadium == "Eden Gardens"
    assert india.team.primary_color == "#aabbcc" and india.team.secondary_color == "#123456"
    assert [p.source_player.version for p in india.team.players] == ["2026", "Base"]
    assert australia.name == "Australia, XI" and not reserve.team.players
    assert not roster_io.championship_teams(db, rival.id)

    csv_export = client.get(base + "/teams/export.csv")
    assert csv_export.status_code == 200 and b"'=India" in csv_export.data
    json_export = client.get(base + "/teams/export.json")
    assert json.loads(json_export.data)["teams"][0]["owner"] == "555"
    assert roster_io.read_file(csv_export.data, "csv")[0]["team_name"] == "=India"

    response = _roster_upload(client, destination.id, "copy.json", json_export.data)
    assert response.status_code == 200 and b"Players added 3" in response.data
    response = _roster_upload(client, destination.id, "copy.csv", csv_export.data)
    assert response.status_code == 200 and b"already up to date" in response.data
    new_json = client.get(f"/world-t20/{destination.id}/teams/export.json")
    assert json.loads(json_export.data) == json.loads(new_json.data)

    minimal = b"team_name,player_name\n=INDIA,Glenn Maxwell\n"
    response = _roster_upload(client, destination.id, "one-player.csv", minimal)
    assert b"Teams updated 1" in response.data and b"Players added 1" in response.data
    db.expire_all()
    copied = roster_io.championship_teams(db, destination.id)[0]
    assert copied.name == "=India" and copied.owner_id == 555 and copied.team.primary_color == "#aabbcc"
    assert len(copied.team.players) == 3


def test_admin_bulk_roster_errors_leave_the_whole_file_unapplied(roster_admin):
    import json

    from services import t20_championship_io as roster_io

    client, db, championship, _, _ = roster_admin
    base = f"/world-t20/{championship.id}"
    rows = [
        {"team_name": "Valid Squad", "owner": 555, "players": ["Virat Kohli"]},
        {"team_name": "Missing Player XI", "owner": 555, "players": ["Unknown Player"]},
        {"team_name": "Missing Owner XI", "players": ["Glenn Maxwell"]},
    ]
    result = _roster_upload(client, championship.id, "teams.json", json.dumps(rows).encode())
    assert result.status_code == 422
    assert b"Unknown Player" in result.data and b"Missing Owner XI" in result.data
    db.expire_all()
    assert not roster_io.championship_teams(db, championship.id)

    result = _roster_upload(client, championship.id, "teams.csv",
                            b"team_name,owner\nTeam,555\nteam,777\n")
    assert result.status_code == 422 and b"conflicting owner" in result.data
    result = _roster_upload(client, championship.id, "roster.csv", b"\xff")
    assert result.status_code == 422 and b"UTF-8" in result.data
    result = _roster_upload(client, championship.id, "bad.json",
                            b'{"team_name":"Unsafe URL","owner":555,"logo_url":"/static/../secrets.png"}')
    assert result.status_code == 422 and b"public HTTP(S)" in result.data

    result = _roster_upload(client, championship.id, "teams.json", json.dumps(rows[:1]).encode())
    assert result.status_code == 200
    championship.started_at = datetime.utcnow()
    db.commit()
    result = _roster_upload(client, championship.id, "teams.json", json.dumps(rows[:1]).encode())
    assert b"Import squads before the championship starts." in result.data
    assert b'id="t20-roster-file"' not in result.data
    assert client.get(base + "/teams/export.csv").status_code == 200

    with client.session_transaction() as auth:
        auth.clear()
    assert client.get(base + "/teams/export.json").status_code == 302
    assert client.post(base + "/teams/import").status_code == 302


def test_admin_saves_series_length_range_and_supports_seven_match_series(roster_admin):
    from models import T20Championship
    from services import t20_championship as cup

    client, db, _, _, _ = roster_admin
    championship = db.query(T20Championship).filter_by(command="wt20c").one()
    base = f"/world-t20/{championship.id}?tab=schedule"
    payload = {"action": "generate", "team_count": 3, "opponents": 2, "matches": 4,
               "max_matches": 7, "start_date": "2026-01-01", "end_date": "2026-02-01",
               "rest_days": 1}
    response = client.post(base, data=payload, follow_redirects=True)
    assert b"Championship saved." in response.data
    assert b"Length of Series" in response.data and b"League matches per team" in response.data
    db.expire_all()
    assert (championship.matches_per_series, championship.max_matches_per_series) == (4, 7)
    rows = cup.series_list(db, championship)
    lengths = sorted(len(row.fixtures) for row in rows)
    assert len(set(lengths)) > 1 and all(4 <= length <= 7 for length in lengths)

    response = client.post(base, data={**payload, "max_matches": 3}, follow_redirects=True)
    assert b"maximum at or above the minimum" in response.data
    db.expire_all()
    assert sorted(len(row.fixtures) for row in cup.series_list(db, championship)) == lengths

    first = min(rows, key=lambda row: len(row.fixtures))
    first_length = len(first.fixtures)
    for day in range(20, 20 + 7 - first_length):
        response = client.post(base, data={"action": "add_fixture", "series_id": first.id,
                                          "opens_at": f"2026-02-{day}T10:00",
                                          "deadline": f"2026-02-{day}T23:00"},
                               follow_redirects=True)
        assert b"Championship saved." in response.data
    response = client.post(base, data={"action": "add_series", "home": first.home_id,
                                      "away": first.away_id, "matches": 7, "start_date": "2026-03-01"},
                           follow_redirects=True)
    assert b"Championship saved." in response.data
    db.expire_all()
    assert sorted(len(row.fixtures) for row in cup.series_list(db, championship)) == sorted(
        [*lengths[1:], 7, 7])


def test_random_pitch_is_drawn_once_per_fixture_and_home_ground_is_required(competition, monkeypatch):
    from models import Match, User
    from services import t20_championship as cup
    from services.match_constants import PITCH_TYPES

    db, championship = competition
    india, australia, _ = championship.teams
    now = datetime.utcnow()
    series = cup.add_series(db, championship, australia, india, 3, date.today(),
                            stadium=india.home_stadium)
    for fixture in series.fixtures:
        fixture.opens_at, fixture.deadline = now - timedelta(hours=1), now + timedelta(hours=1)
    championship.pitch_mode = "random"
    db.flush()
    cup.start(championship)
    db.flush()
    opener, second, _ = series.fixtures

    draws = iter(PITCH_TYPES[:2])
    monkeypatch.setattr(cup.random, "choice", lambda choices: next(draws))
    assert cup.prepare_conditions(db, championship.id, opener.id)[1:] == (
        "random", australia.home_stadium, PITCH_TYPES[0])
    assert cup.prepare_conditions(db, championship.id, opener.id)[-1] == PITCH_TYPES[0]
    host = db.query(User).filter_by(telegram_id=australia.owner_id).one()
    guest = db.query(User).filter_by(telegram_id=india.owner_id).one()
    game = Match(user1_id=host.id, user2_id=guest.id, status="active",
                 stadium=india.home_stadium, pitch_type=PITCH_TYPES[0])
    db.add(game)
    db.flush()
    tag = dict(championship_id=championship.id, fixture_id=opener.id,
               teams=[australia.id, india.id], pitch_mode="random", stadium=australia.home_stadium)
    with pytest.raises(cup.FixtureUnavailable, match="conditions changed"):
        cup.bind(db, tag, game, host, guest)
    game.stadium, game.pitch_type = australia.home_stadium, PITCH_TYPES[1]
    with pytest.raises(cup.FixtureUnavailable, match="conditions changed"):
        cup.bind(db, tag, game, host, guest)
    game.pitch_type = PITCH_TYPES[0]
    cup.bind(db, tag, game, host, guest)
    cup.set_result(db, championship, opener, "win", australia.id)
    assert cup.prepare_conditions(db, championship.id, second.id)[-1] == PITCH_TYPES[1]
    australia.home_stadium = None
    db.flush()
    with pytest.raises(cup.FixtureUnavailable, match="Set a home stadium for Australia"):
        cup.prepare_conditions(db, championship.id, second.id)


def test_championship_commands_produce_rich_replies(competition, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as commands

    db, championship = competition
    from services import t20_championship as cup
    cup.add_series(db, championship, *championship.teams[:2], 3, date.today())
    db.flush()
    championship.name = "T20 <Cup>"
    deliver = AsyncMock()
    monkeypatch.setattr(commands.R, "reply_rich", deliver)
    message = SimpleNamespace(text="", reply_to_message=None)
    update = SimpleNamespace(effective_message=message)
    for command in ("wt20c", "wt20chelp", "wt20cinfo", "wt20ctable", "wt20cfixtures",
                    "wt20cteams", "wt20cseries", "wt20cfinal", "wt20cstats", "wt20cplayer",
                    "wt20cawards", "wt20cscorecard", "wt20cplay", "wt20cteamfix",
                    "wt20c teamfixtures", "wt20c playoffs"):
        deliver.reset_mock()
        name = command.split()[0]
        message.text = "/" + command
        assert asyncio.run(commands.dispatch(update, None, db, name))
        _, blocks, fallback = deliver.await_args.args
        assert blocks[0]["type"] == "heading"
        assert "T20 &lt;Cup&gt;" in fallback
        if command == "wt20ctable":
            assert blocks[1]["type"] == "table"
        if command == "wt20chelp":
            assert blocks[1]["type"] == "list"
        if command in ("wt20cfixtures", "wt20cseries", "wt20cteams"):
            assert any(block["type"] == "details" for block in blocks)
            assert "<blockquote expandable>" in fallback


def test_admin_can_extend_a_postponed_fixture_by_reference(competition, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as commands
    from services import t20_championship as cup

    db, championship = competition
    today = (datetime.utcnow() + cup.IST_OFFSET).date()
    series = cup.add_series(db, championship, *championship.teams[:2], 3, today - timedelta(days=2))
    db.flush()
    first, second, _ = series.fixtures
    old_open, old_deadline = first.opens_at, first.deadline
    first.status = "postponed"
    second.status = "completed"
    db.commit()
    tomorrow = today + timedelta(days=1)
    reference = cup.match_reference(first)
    assert cup.find_fixture([series], reference.lower()) is first
    assert cup.find_fixture([series], f"#{first.id}") is first

    send = AsyncMock()
    monkeypatch.setattr(commands.R, "reply_rich", send)
    monkeypatch.setattr(commands, "is_admin", lambda uid: uid == 99)
    message = SimpleNamespace(text=f"/wt20c deadline {reference} {tomorrow}")
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))
    assert asyncio.run(commands.dispatch(update, None, db, "wt20c"))
    assert "Only bot admins" in send.await_args.args[-1]
    assert first.deadline == old_deadline and first.status == "postponed"

    update.effective_user.id = 99
    assert asyncio.run(commands.dispatch(update, None, db, "wt20c"))
    assert "New deadline" in send.await_args.args[-1] and reference in send.await_args.args[-1]
    assert first.deadline == cup.day_window(tomorrow)[1] and first.opens_at == old_open
    assert first.status == "scheduled" and championship.end_at == first.deadline
    message.text = f"/wt20c deadline {cup.match_reference(second)} {tomorrow} 13:00"
    assert asyncio.run(commands.dispatch(update, None, db, "wt20c"))
    assert "Only scheduled or postponed" in send.await_args.args[-1]
    message.text = f"/wt20c deadline {reference} {today} 10:00"
    assert asyncio.run(commands.dispatch(update, None, db, "wt20c"))
    assert "later than the current one" in send.await_args.args[-1]
    following_day = tomorrow + timedelta(days=1)
    message.text = f"/wt20c deadline #{first.id} {following_day} 15:30"
    assert asyncio.run(commands.dispatch(update, None, db, "wt20c"))
    assert first.deadline == datetime.combine(following_day, datetime.min.time()) + timedelta(hours=10)


def test_today_command_shows_owned_team_matches_opening_or_due_in_ist(competition, monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as commands
    from services import t20_championship as cup

    db, championship = competition
    utc_now = datetime(2026, 11, 19, 20)
    today = date(2026, 11, 20)
    monkeypatch.setattr(commands, "datetime", SimpleNamespace(utcnow=lambda: utc_now))
    india, australia, england = championship.teams
    australia.co_owner_id = 88
    home = cup.add_series(db, championship, australia, india, 3, today - timedelta(days=1))
    other = cup.add_series(db, championship, india, england, 3, today)
    home.fixtures[0].deadline = cup.day_window(today)[1]
    db.flush()
    send = AsyncMock()
    monkeypatch.setattr(commands.R, "reply_rich", send)
    monkeypatch.setattr(commands, "get_session", lambda: nullcontext(db))
    update = SimpleNamespace(effective_message=SimpleNamespace(), effective_user=SimpleNamespace(id=88))
    asyncio.run(commands.today_handler(update, SimpleNamespace(args=[])))
    text = send.await_args.args[-1]
    assert "Australia fixtures" in text and "20 Nov 2026" in text
    assert all(cup.match_reference(f) in text for f in home.fixtures[:2])
    assert cup.match_reference(home.fixtures[2]) not in text
    assert cup.match_reference(other.fixtures[0]) not in text


def test_today_reminder_previews_rechecks_and_sends_once_per_captain(competition, monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_match_reminders as reminders
    from services import t20_championship as cup

    db, championship = competition
    utc_now = datetime(2026, 11, 19, 20)
    today = date(2026, 11, 20)
    india, australia, england = championship.teams
    australia.co_owner_id = 88
    first = cup.add_series(db, championship, india, australia, 3, today)
    second = cup.add_series(db, championship, england, australia, 3, today)
    cup.start(championship, utc_now)
    db.commit()
    monkeypatch.setattr(reminders, "datetime", SimpleNamespace(utcnow=lambda: utc_now))
    monkeypatch.setattr(reminders, "is_admin", lambda uid: uid in (99, 100))
    monkeypatch.setattr(reminders, "get_session", lambda: nullcontext(db))
    send, edit, reply = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(reminders.R, "send_rich_message", send)
    monkeypatch.setattr(reminders.R, "edit_rich", edit)
    monkeypatch.setattr(reminders.R, "reply_rich", reply)
    monkeypatch.setattr(reminders, "SEND_GAP_SECONDS", 0)
    opener = SimpleNamespace(id=99)
    message = SimpleNamespace(message_thread_id=321)
    chat = SimpleNamespace(id=-123, type="supergroup")
    update = SimpleNamespace(effective_message=message, effective_user=opener, effective_chat=chat)
    context = SimpleNamespace(args=["today"], bot=object())

    def preview():
        asyncio.run(reminders.matchremind_handler(update, context))
        button = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        return SimpleNamespace(data=button.callback_data, from_user=opener, answer=AsyncMock(), message=message)

    update.callback_query = preview()
    send.assert_not_awaited()
    update.callback_query.from_user = SimpleNamespace(id=100)
    asyncio.run(reminders.matchremind_callback(update, context))
    send.assert_not_awaited()
    update.callback_query.from_user = opener
    second.fixtures[0].status = "postponed"
    db.commit()
    asyncio.run(reminders.matchremind_callback(update, context))
    send.assert_not_awaited()
    assert "pending fixtures have changed" in edit.await_args.args[-1]
    second.fixtures[0].status = "scheduled"
    db.commit()

    update.callback_query = preview()
    asyncio.run(reminders.matchremind_callback(update, context))
    deliveries = {call.args[1]: call for call in send.await_args_list}
    assert send.await_count == len(deliveries) == 5
    assert set(deliveries) == {-123, 1, 2, 3, 88}
    assert deliveries[-123].kwargs["message_thread_id"] == 321
    assert 'href="tg://user?id=88"' in deliveries[-123].args[-1]
    assert all(cup.match_reference(series.fixtures[0]) in deliveries[88].args[-1] for series in (first, second))
    assert first.fixtures[0].last_reminded_at == second.fixtures[0].last_reminded_at == utc_now

    asyncio.run(reminders.matchremind_callback(update, context))
    assert send.await_count == 5


def test_admin_home_stadium_and_pitch_settings(roster_admin):
    from services import t20_championship_io as roster_io

    client, db, championship, _, _ = roster_admin
    base = f"/world-t20/{championship.id}"
    response = client.post(base + "?tab=teams", data={"action": "add_team", "team_name": "Australia",
                                                    "owner": 555, "home_stadium": "MCG"},
                           follow_redirects=True)
    assert b"Melbourne Cricket Ground" in response.data
    assert roster_io.championship_teams(db, championship.id)[0].home_stadium == "Melbourne Cricket Ground"
    settings = base + "?tab=settings"
    response = client.post(settings, data={"action": "pitch_settings", "pitch_mode": "random"},
                           follow_redirects=True)
    assert b"Championship saved." in response.data
    assert b'value="random" selected' in response.data
    db.refresh(championship)
    assert championship.pitch_mode == "random"
    response = client.post(settings, data={"action": "pitch_settings", "pitch_mode": "guest"},
                           follow_redirects=True)
    assert b"Choose a pitch selection mode." in response.data
    db.refresh(championship)
    assert championship.pitch_mode == "random"


def test_admin_regenerates_upcoming_series_around_live_and_completed_fixtures(roster_admin):
    from models import Match, T20Championship, User
    from services import t20_championship as cup

    client, db, _, _, _ = roster_admin
    championship = db.query(T20Championship).filter_by(command="wt20c").one()
    india, australia, england = championship.teams
    now = datetime.utcnow()
    today = (now + cup.IST_OFFSET).date()
    kept = cup.add_series(db, championship, australia, india, 3, today)
    first = cup.add_series(db, championship, india, england, 5, today + timedelta(days=5))
    second = cup.add_series(db, championship, england, australia, 3, today + timedelta(days=12))
    db.flush()
    cup.start(championship, now)
    cup.set_result(db, championship, kept.fixtures[0], "win", australia.id)
    users = db.query(User).filter(User.telegram_id.in_((india.owner_id, australia.owner_id))).all()
    live_match = Match(user1_id=users[0].id, user2_id=users[1].id, status="active")
    db.add(live_match)
    db.flush()
    kept.fixtures[1].match_id = live_match.id
    kept.fixtures[1].status = "live"
    original = [(f.id, f.status, f.match_id, f.winner_id, f.opens_at, f.deadline) for f in kept.fixtures]
    original_future = {row.id: (row.home_id, row.away_id, row.fixtures[0].id)
                       for row in (first, second)}
    db.commit()

    base = f"/world-t20/{championship.id}?tab=schedule"
    assert b"Regenerate upcoming series" in client.get(base).data
    data = {"action": "regenerate", "matches": 4, "max_matches": 4,
            "start_date": today, "end_date": today + timedelta(days=23), "rest_days": 2}
    rejected = client.post(base, data={**data, "end_date": today}, follow_redirects=True)
    assert b"calendar days" in rejected.data
    db.expire_all()
    assert len(first.fixtures) == 5 and len(second.fixtures) == 3

    response = client.post(base, data=data, follow_redirects=True)
    assert b"Upcoming league series regenerated." in response.data
    db.expire_all()
    rows = cup.series_list(db, championship)
    assert [(f.id, f.status, f.match_id, f.winner_id, f.opens_at, f.deadline) for f in kept.fixtures] == original
    assert {row.id: (row.home_id, row.away_id, row.fixtures[0].id)
            for row in rows[1:]} == original_future
    assert [len(row.fixtures) for row in rows] == [3, 4, 4]
    assert all((row.fixtures[0].opens_at - kept.fixtures[-1].deadline).days >= 2 for row in rows[1:])
    assert (rows[2].fixtures[0].opens_at - rows[1].fixtures[-1].deadline).days >= 2
    assert cup.next_fixtures(rows)[australia.id] == kept.fixtures[1].id


def test_team_fixture_command_filters_paginated_cards_and_resolves_co_owners(competition, monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as commands
    from models import T20ChampionshipCommand
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    australia.co_owner_id = 9876
    australia.team.short_name = "AUS"
    for index in range(6):
        cup.add_series(db, championship, australia, india, 3, date.today() + timedelta(days=4*index))
    cup.add_series(db, championship, england, india, 3, date.today())
    db.flush()

    # Older championships do not have a saved teamfix alias yet.
    db.query(T20ChampionshipCommand).filter_by(command="wt20cteamfix").delete()
    send, edit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(commands.R, "reply_rich", send)
    monkeypatch.setattr(commands.R, "edit_rich", edit)
    monkeypatch.setattr(commands, "get_session", lambda: nullcontext(db))
    message = SimpleNamespace(text="/wt20cteamfix", reply_to_message=None)
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=9876))
    assert asyncio.run(commands.dispatch(update, None, db, "wt20cteamfix"))
    send.assert_awaited_once()
    _, blocks, fallback = send.await_args.args
    assert "Australia fixtures" in fallback and "India tour of England" not in fallback
    assert sum(block["type"] == "details" for block in blocks) == 3
    assert fallback.count("<blockquote expandable>") == 3
    assert "Page 1 of 2" in fallback and len(fallback) < commands.R.CHUNK_LIMIT
    button = send.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Next ›"

    query = SimpleNamespace(data=button.callback_data, answer=AsyncMock())
    asyncio.run(commands.fixture_page_callback(SimpleNamespace(callback_query=query), None))
    _, next_blocks, next_fallback = edit.await_args.args
    assert "Page 2 of 2" in next_fallback and "India tour of England" not in next_fallback
    assert sum(block["type"] == "details" for block in next_blocks) == 3
    query.answer.assert_awaited_once()

    for text in ("/wt20c teamfixtures AUS", "/wt20cteamfix Australia", f"/wt20cteamfix {australia.id}"):
        send.reset_mock()
        message.text = text
        update.effective_user.id = 12345
        assert asyncio.run(commands.dispatch(update, None, db, text.split()[0][1:]))
        assert "Australia fixtures" in send.await_args.args[2]


def test_long_series_is_paged_without_splitting_html_sections(competition):
    import re

    from handlers import t20_championship as commands
    from services import t20_championship as cup

    db, championship = competition
    india, australia, _ = championship.teams
    championship.name = "&" * 120
    india.team.name = "&" * 120
    australia.team.name = "<" * 120
    championship.max_matches_per_series = 40
    tour = cup.add_series(db, championship, india, australia, 40, date.today(), venue="&" * 120)
    db.flush()
    rows = cup.series_list(db, championship)
    seen = set()
    page = 0
    while True:
        blocks, fallback, markup = commands._fixture_page(championship, rows, "fixtures", india.id, 0, page)
        assert len(fallback) < commands.R.CHUNK_LIMIT
        assert fallback.count("<blockquote expandable>") == fallback.count("</blockquote>")
        assert any(block["type"] == "details" for block in blocks)
        message = re.sub("<[^>]*>", "", fallback)
        seen.update(int(match) for match in re.findall(r"Match \d+ · #(\d+)", message))
        if not markup or not any(button.text == "Next ›" for button in markup.inline_keyboard[0]):
            break
        page += 1
    assert page > 0 and seen == {fixture.id for fixture in tour.fixtures}


def test_open_sequential_opens_every_fixture_but_keeps_order(competition):
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    championship.final_format = "none"
    now = datetime.utcnow()
    future = date.today() + timedelta(days=30)
    s1 = cup.add_series(db, championship, india, australia, 3, future)
    s2 = cup.add_series(db, championship, india, england, 3, future + timedelta(days=10))
    db.flush()
    cup.start(championship, now)
    db.flush()
    rows = cup.series_list(db, championship)
    pending = cup.next_fixtures(rows)

    # Calendar mode: the future-dated opener is not yet playable.
    assert cup.fixture_status(championship, s1.fixtures[0], pending, now) == "upcoming"
    assert cup.lock_reason(championship, s1.fixtures[0], pending, now)[0] == cup.LOCK_OPENS_LATER

    cup.set_schedule_mode(championship, "open_sequential", now + timedelta(days=60))
    assert cup.open_mode(championship)
    pending = cup.next_fixtures(rows)
    # Every league fixture is open, so S1M1 is playable at once despite its date.
    assert cup.fixture_status(championship, s1.fixtures[0], pending, now) == "available"
    assert cup.effective_deadline(championship, s1.fixtures[0]) == now + timedelta(days=60)
    # The next match in the series stays locked behind the opener.
    assert cup.fixture_status(championship, s1.fixtures[1], pending, now) == "upcoming"
    assert cup.lock_reason(championship, s1.fixtures[1], pending, now) == (
        cup.LOCK_PREVIOUS_MATCH, cup.match_reference(s1.fixtures[0]))
    # England is ready for S2M1 but India is busy in S1, so it waits.
    assert cup.lock_reason(championship, s2.fixtures[0], pending, now)[0] == cup.LOCK_WAITING_OPPONENT

    # Finishing S1 unlocks S2's opener; subsequent matches stay locked.
    for fixture in s1.fixtures:
        cup.set_result(db, championship, fixture, "win", india.id)
    pending = cup.next_fixtures(rows)
    assert cup.fixture_status(championship, s2.fixtures[0], pending, now) == "available"
    assert cup.fixture_status(championship, s2.fixtures[1], pending, now) == "upcoming"
    # Past the shared deadline a scheduled fixture reads as deadline-passed.
    late = now + timedelta(days=61)
    assert cup.fixture_status(championship, s2.fixtures[0], pending, late) == "postponed"
    assert cup.lock_reason(championship, s2.fixtures[0], pending, late)[0] == "Postponed — Deadline Passed"


def test_unrelated_teams_progress_simultaneously_in_open_mode(competition):
    from models import ChallengePlayer, ChallengeTeam, T20ChampionshipTeam, User
    from services import t20_championship as cup

    db, championship = competition
    india, australia, england = championship.teams
    championship.final_format = "none"
    club = ChallengeTeam(league=championship.league, name="South Africa")
    club.players = [ChallengePlayer(name=f"SA p{j}") for j in range(11)]
    sa = T20ChampionshipTeam(championship=championship, team=club, owner_id=4,
                             home_stadium="Newlands Cricket Ground")
    db.add(sa)
    db.add(User(telegram_id=4, first_name="South Africa"))
    db.flush()
    now = datetime.utcnow()
    future = date.today() + timedelta(days=20)
    india_aus = cup.add_series(db, championship, india, australia, 3, future)
    eng_sa = cup.add_series(db, championship, england, sa, 3, future + timedelta(days=3))
    db.flush()
    cup.start(championship, now)
    cup.set_schedule_mode(championship, "open_sequential", now + timedelta(days=40))
    rows = cup.series_list(db, championship)
    pending = cup.next_fixtures(rows)
    # Two disjoint series both open at once — no global round barrier.
    assert cup.fixture_status(championship, india_aus.fixtures[0], pending, now) == "available"
    assert cup.fixture_status(championship, eng_sa.fixtures[0], pending, now) == "available"


def test_set_schedule_mode_inherits_and_validates(competition):
    from services import t20_championship as cup

    db, championship = competition
    with pytest.raises(ValueError, match="availability mode"):
        cup.set_schedule_mode(championship, "bogus")
    championship.end_at = None
    championship.league_deadline = None
    with pytest.raises(ValueError, match="league-stage deadline"):
        cup.set_schedule_mode(championship, "open_sequential")
    now = datetime.utcnow()
    championship.end_at = now + timedelta(days=5)
    cup.set_schedule_mode(championship, "open_sequential")  # safely inherits the league end
    assert championship.league_deadline == championship.end_at
    cup.set_schedule_mode(championship, "calendar")
    assert championship.schedule_mode == "calendar" and cup.open_mode(championship) is False
    championship.completed_at = now
    with pytest.raises(ValueError, match="locked once the championship ends"):
        cup.set_schedule_mode(championship, "open_sequential")


def test_stats_command_aliases_resolve_with_and_without_rows(competition):
    from models import T20ChampionshipCommand
    from services import t20_championship as cup

    db, championship = competition
    assert cup.find_command(db, "wt20cstats") == (championship, "stats")
    assert cup.find_command(db, "wt20ctournamentstats") == (championship, "tournamentstats")
    assert cup.find_command(db, "wt20cstatstour") == (championship, "statstour")
    assert cup.find_command(db, "wt20cplayer") == (championship, "player")
    # A championship created before these commands existed has no alias rows.
    db.query(T20ChampionshipCommand).filter(
        T20ChampionshipCommand.command.in_(("wt20cstatstour", "wt20ctournamentstats"))
    ).delete(synchronize_session=False)
    db.flush()
    assert cup.find_command(db, "wt20cstatstour") == (championship, "statstour")
    assert cup.find_command(db, "wt20ctournamentstats") == (championship, "tournamentstats")


def _play_one_match(db, championship):
    """Record one completed India-vs-Australia match and return its series."""
    from models import Match, User
    from services.match_constants import PITCH_TYPES
    from services import t20_championship as cup

    india, australia, _ = championship.teams
    now = datetime.utcnow()
    series = cup.add_series(db, championship, australia, india, 3, date.today())
    for fixture in series.fixtures:
        fixture.opens_at, fixture.deadline = now - timedelta(hours=1), now + timedelta(hours=1)
    db.flush()
    cup.start(championship, now)
    db.flush()
    host = db.query(User).filter_by(telegram_id=india.owner_id).one()
    guest = db.query(User).filter_by(telegram_id=australia.owner_id).one()
    game = Match(user1_id=guest.id, user2_id=host.id, status="active",
                 stadium=australia.home_stadium, pitch_type=PITCH_TYPES[0])
    db.add(game)
    db.flush()
    tag = dict(championship_id=championship.id, fixture_id=series.fixtures[0].id,
               users=[guest.id, host.id], teams=[australia.id, india.id],
               pitch_mode="home", stadium=australia.home_stadium)
    cup.bind(db, tag, game, guest, host)
    db.flush()
    state = {
        "match_id": game.id, "t20_championship": tag, "inn1_team": india.name,
        "inn1_bat_team": india.name, "inn1_bowl_team": australia.name,
        "inn1_bat_team_id": host.id, "inn1_bowl_team_id": guest.id,
        "inn1_runs": 186, "inn1_wickets": 4, "total_runs": 150, "total_wickets": 10,
        "bat_team_id": guest.id, "bowl_team_id": host.id,
        "bat_team_name": australia.name, "bowl_team_name": india.name,
        "inn1_bat_xi": [{"roster_id": 1, "name": "Abhishek Sharma"}],
        "inn1_bowl_xi": [{"roster_id": 2, "name": "Mitchell Starc"}],
        "bat_xi": [{"roster_id": 2, "name": "Mitchell Starc"}],
        "bowl_xi": [{"roster_id": 1, "name": "Abhishek Sharma"},
                    {"roster_id": 3, "name": "India keeper"}],
        "inn1_bat_stats": {"1": {"runs": 125, "balls": 60, "out": False, "fours": 10,
                                  "sixes": 8, "balls_to_50": 22, "balls_to_100": 48}},
        "inn1_bowl_stats": {"2": {"balls": 24, "runs": 40, "wickets": 1, "dots": 8}},
        "bat_stats": {"2": {"runs": 10, "balls": 8, "out": True}},
        "bowl_stats": {"1": {"balls": 24, "runs": 20, "wickets": 4, "dots": 12}},
        "record_deliveries": [{"wicket": True, "opponent": india.name,
                               "fielder": "India keeper", "how": "caught behind", "keeper": True}],
    }
    game.status, game.winner_id = "completed", host.id
    cup.record_played(db, state, winner_user_id=host.id)
    db.flush()
    return series


def test_stats_commands_render_leaderboards_and_player_cards(competition, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as H

    db, championship = competition
    india, australia, england = championship.teams
    championship.final_format = "none"
    _play_one_match(db, championship)

    message = SimpleNamespace()

    # /wt20cstats is a ranked category board, not a per-player dump.
    reply = AsyncMock()
    monkeypatch.setattr(H.R, "reply_rich", reply)
    asyncio.run(H._reply_leaderboard(message, db, championship, [], opener=india.owner_id))
    fallback = reply.await_args.args[2]
    assert "Championship Stats" in fallback and "Most Runs" in fallback
    assert "Abhishek Sharma" in fallback and "🥇" in fallback
    assert reply.await_args.kwargs["reply_markup"] is not None

    # /wt20cstatstour NAME is the full, independently collapsible player card.
    reply = AsyncMock()
    monkeypatch.setattr(H.R, "reply_rich", reply)
    asyncio.run(H._reply_player_stats(message, db, championship, ["Abhishek", "Sharma"]))
    card = reply.await_args.args[2]
    for needed in ("This Season Stats", "🏏 Batting", "🎯 Bowling", "🧤 Fielding",
                   "Awards &amp; Performance", "Additional Batting Records",
                   "Additional Bowling Records", "Recent Form",
                   "HS: 125*", "SR: 208.33", "Overs: 4.0", "Fastest 50: 22",
                   "Boundary %: 70.4%", "Dot-ball %: 50.0%", "Maidens: —"):
        assert needed in card, needed
    # Batting and bowling (and the other sections) each get their own collapsible.
    assert card.count("<blockquote expandable>") >= 6

    # Category navigation is requester-locked and switches the board.
    @contextmanager
    def _session():
        yield db
    monkeypatch.setattr(H, "get_session", _session)
    edit = AsyncMock()
    monkeypatch.setattr(H.R, "edit_rich", edit)
    data = H._stats_cb(championship.id, "wkts", 0, 0, india.owner_id)
    opener_q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=india.owner_id),
                               answer=AsyncMock())
    asyncio.run(H.leaderboard_callback(SimpleNamespace(callback_query=opener_q), None))
    assert "Most Wickets" in edit.await_args.args[2]
    stranger_q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=999999), answer=AsyncMock())
    asyncio.run(H.leaderboard_callback(SimpleNamespace(callback_query=stranger_q), None))
    assert "Only the person who opened" in stranger_q.answer.await_args.args[0]


def test_player_card_shows_both_departments_and_missing_values(competition, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as H

    db, championship = competition
    championship.final_format = "none"
    _play_one_match(db, championship)
    # Mitchell Starc batted and bowled; the keeper never batted or bowled.
    reply = AsyncMock()
    monkeypatch.setattr(H.R, "reply_rich", reply)
    asyncio.run(H._reply_player_stats(message := SimpleNamespace(), db, championship, ["India", "keeper"]))
    card = reply.await_args.args[2]
    # Both departments are present as headings even for a pure fielder, with real
    # zeroes (not "—"), and the keeper's one dismissal is recorded.
    assert "🏏 Batting" in card and "🎯 Bowling" in card
    assert "Wkts: 0" in card and "WK dismissals: 1" in card


def test_match_announcement_reports_potm_series_and_next(competition):
    from services import t20_championship as cup

    db, championship = competition
    championship.final_format = "none"
    series = _play_one_match(db, championship)
    news = cup.result_news(series.fixtures[0])
    assert "POTM · " in news           # Player of the Match contribution
    assert "Series · " in news         # running series score
    assert cup.match_reference(series.fixtures[1]) in news  # next match reference
    assert "Available to Play" in news  # the next match is open for both teams


def test_admin_sets_open_sequential_schedule_availability(roster_admin):
    client, db, championship, _, _ = roster_admin
    schedule = f"/world-t20/{championship.id}?tab=schedule"
    # Open mode needs a shared league deadline; supply one explicitly (IST).
    response = client.post(schedule, data={"action": "schedule_availability",
                                           "schedule_mode": "open_sequential",
                                           "league_deadline": "2099-01-01T12:00"},
                           follow_redirects=True)
    assert b"Championship saved." in response.data
    db.refresh(championship)
    assert championship.schedule_mode == "open_sequential"
    assert championship.league_deadline is not None
    # And can be switched back to the calendar default.
    client.post(schedule, data={"action": "schedule_availability", "schedule_mode": "calendar"},
                follow_redirects=True)
    db.refresh(championship)
    assert championship.schedule_mode == "calendar"


def test_admin_selects_and_removes_squad_players(roster_admin):
    from models import Player
    from services import t20_championship_io as roster_io

    client, db, championship, _, _ = roster_admin
    base = f"/world-t20/{championship.id}?tab=teams"
    client.post(base, data={"action": "add_team", "team_name": "India", "owner": 555,
                            "home_stadium": "MCG"}, follow_redirects=True)
    db.expire_all()
    team = roster_io.championship_teams(db, championship.id)[0]
    maxwell = db.query(Player).filter_by(name="Glenn Maxwell", version="Base").one()

    # Select a player from the picker and add them.
    response = client.post(base, data={"action": "add_player", "team_id": team.id,
                                       "source_player_id": maxwell.id}, follow_redirects=True)
    assert b"Added Glenn Maxwell" in response.data
    db.expire_all()
    squad = roster_io.championship_teams(db, championship.id)[0].team.players
    assert [p.name for p in squad] == ["Glenn Maxwell"]

    # The same card cannot be added twice.
    response = client.post(base, data={"action": "add_player", "team_id": team.id,
                                       "source_player_id": maxwell.id}, follow_redirects=True)
    assert b"already in this squad" in response.data
    db.expire_all()
    assert len(roster_io.championship_teams(db, championship.id)[0].team.players) == 1

    # Remove the player again.
    member = roster_io.championship_teams(db, championship.id)[0].team.players[0]
    client.post(base, data={"action": "remove_player", "team_id": team.id,
                            "player_id": member.id}, follow_redirects=True)
    db.expire_all()
    assert roster_io.championship_teams(db, championship.id)[0].team.players == []


def test_recent_form_result_is_from_the_players_perspective(competition, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from handlers import t20_championship as H

    db, championship = competition
    championship.final_format = "none"
    _play_one_match(db, championship)  # India beat Australia

    def card_for(name):
        reply = AsyncMock()
        monkeypatch.setattr(H.R, "reply_rich", reply)
        asyncio.run(H._reply_player_stats(SimpleNamespace(), db, championship, name.split()))
        return reply.await_args.args[2]

    india = card_for("Abhishek Sharma")   # India won
    australia = card_for("Mitchell Starc")  # Australia lost
    assert "Recent Form" in india and "Win" in india
    assert "Loss" in australia and "Win" not in australia.split("Recent Form")[1]


def test_extend_deadline_rejected_for_league_fixture_in_open_mode(competition):
    from services import t20_championship as cup

    db, championship = competition
    championship.final_format = "none"
    now = datetime.utcnow()
    series = cup.add_series(db, championship, championship.teams[0], championship.teams[1], 3,
                            date.today() + timedelta(days=5))
    db.flush()
    cup.start(championship, now)
    cup.set_schedule_mode(championship, "open_sequential", now + timedelta(days=30))
    db.commit()
    reference = cup.match_reference(series.fixtures[0])
    with pytest.raises(ValueError, match="open mode"):
        cup.extend_deadline(db, championship.id, reference, "2099-01-01", now=now)


def test_best_figures_board_carries_no_overs_cell():
    from services import t20_championship_rich as rich
    from services import t20_championship_stats as stats

    row = stats._new_totals()
    row.update(team_id=1, name="Bowler", team_name="India",
               best_bowl_wickets=4, best_bowl_runs=20, bowl_balls=48)
    board = rich.leaderboard_html(
        type("C", (), {"name": "World T20", "command": "wt20c", "teams": []})(),
        rich.category("bfig"), [row])
    assert "4/20" in board and "OVERS" not in board and "8.0" not in board


def test_admin_squad_edits_allowed_during_championship(competition):
    import admin
    from models import Player
    from services import t20_championship as cup

    db, championship = competition
    india, australia, _ = championship.teams
    championship.final_format = "none"
    series = cup.add_series(db, championship, india, australia, 3, date.today())
    db.flush()
    cup.start(championship)  # championship is now in progress
    db.flush()

    card = Player(name="Shubman Gill", version="Base", rating=88, is_active=True,
                  category="Batsman", country="India", bat_hand="Right",
                  bowl_hand="Right", bowl_style="Fast", bat_rating=88, bowl_rating=30)
    db.add(card)
    db.flush()

    def mutate(data):
        with admin.app.test_request_context(method="POST", data=data):
            admin._t20_mutation(db, championship)
        db.flush()
        db.expire_all()

    # Add a player mid-championship (no live match) — allowed.
    mutate({"action": "add_player", "team_id": india.id, "source_player_id": card.id})
    assert [p.name for p in india.team.players].count("Shubman Gill") == 1
    assert len(india.team.players) == 12

    # Remove back down to 11 — allowed.
    gill = next(p for p in india.team.players if p.name == "Shubman Gill")
    mutate({"action": "remove_player", "team_id": india.id, "player_id": gill.id})
    assert len(india.team.players) == 11

    # Removing below 11 during the championship is blocked.
    with pytest.raises(ValueError, match="at least 11"):
        mutate({"action": "remove_player", "team_id": india.id,
                "player_id": india.team.players[0].id})

    # A team in a live match has its squad frozen until the match finishes.
    series.fixtures[0].status = "live"
    db.flush()
    with pytest.raises(ValueError, match="current match"):
        mutate({"action": "add_player", "team_id": india.id, "source_player_id": card.id})

    # Whole teams still cannot be added or removed once started.
    with pytest.raises(ValueError, match="before the championship starts"):
        mutate({"action": "remove_team", "team_id": australia.id})
