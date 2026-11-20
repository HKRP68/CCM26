"""World T20 fixtures and scorecards must stay on their scheduled championship."""
from collections import Counter
from datetime import date, datetime, timedelta
from fractions import Fraction

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
    rows = plan(teams, 6, 4, start, end, 2, max_duration=7)
    assert {count for _, _, _, count in rows} == {4, 5, 6, 7}
    for team in teams:
        tours = [(day, count) for home, away, day, count in rows if team in (home, away)]
        assert sum(count for _, count in tours) == 33
        assert all((following - day).days >= count + 2
                   for (day, count), (following, _) in zip(tours, tours[1:]))
        assert tours[-1][0] + timedelta(days=tours[-1][1] - 1) <= end

    tight = plan(teams, 6, 4, start, start + timedelta(days=33), 2, max_duration=7)
    assert {count for _, _, _, count in tight} == {4}
    small = plan(teams[:2], 1, 4, start, start + timedelta(days=4), 2, max_duration=7)
    assert small[0][-1] == 5
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
            for i, name in enumerate(("India", "Australia", "England"), 1):
                club = ChallengeTeam(league=championship.league, name=name)
                club.players = [ChallengePlayer(name=f"{name} player {j}") for j in range(11)]
                db.add(T20ChampionshipTeam(championship=championship, team=club, owner_id=i))
                db.add(User(telegram_id=i, first_name=name))
            db.commit()
            yield db, championship
    finally:
        engine.dispose()


def test_played_fixture_stats_series_and_final(competition):
    from models import Match, User
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
    game = Match(user1_id=host.id, user2_id=guest.id, status="active")
    db.add(game)
    db.flush()
    tag = dict(championship_id=championship.id, fixture_id=opening.id, users=[host.id, guest.id],
               teams=[india.id, australia.id], pitch=None, stadium=None)
    cup.bind(db, tag, game, host, guest)
    db.flush()
    assert opening.status == "live"
    with pytest.raises(cup.FixtureUnavailable):
        cup.bind(db, tag, game, host, guest)
    game.status = "abandoned"
    db.flush()
    assert cup.heal_abandoned(db) == 1
    db.refresh(opening)
    assert opening.status == "scheduled" and opening.match_id is None
    game.status = "active"
    cup.bind(db, tag, game, host, guest)
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
                             effective_user=SimpleNamespace(id=india.owner_id, is_bot=False))
    assert asyncio.run(dispatch(update, ctx, db, "wt20c")) is True
    assert "2 teams" in message.reply_text.await_args.args[0]

    message.text = "/wt20c playoffs"
    assert asyncio.run(dispatch(update, ctx, db, "wt20c")) is True
    assert "Top-four semifinals are off." in message.reply_text.await_args.args[0]

    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=australia.owner_id, is_bot=False))
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
    assert [len(challenge._query_team_players(db, draft, side)) for side in ("host", "target")] == [11, 11]


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
    south_africa = T20ChampionshipTeam(championship=championship, team=club, owner_id=4)
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
        "team_name,short_name,logo_url,primary_colour,secondary_color,owner,co_owner,player_name\n"
        "=India,IND,/static/challenge_leagues/india.png,#abc,#123456,@OwnerName,556,Virat Kohli\n"
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
    assert b"Length of Series" in response.data and b"7 matches" in response.data
    db.expire_all()
    assert (championship.matches_per_series, championship.max_matches_per_series) == (4, 7)
    rows = cup.series_list(db, championship)
    assert sorted(len(row.fixtures) for row in rows) == [4, 5, 7]

    response = client.post(base, data={**payload, "max_matches": 3}, follow_redirects=True)
    assert b"maximum at or above the minimum" in response.data
    db.expire_all()
    assert sorted(len(row.fixtures) for row in cup.series_list(db, championship)) == [4, 5, 7]

    first = next(row for row in rows if len(row.fixtures) == 5)
    for day in (20, 21):
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
    assert sorted(len(row.fixtures) for row in cup.series_list(db, championship)) == [4, 7, 7, 7]
