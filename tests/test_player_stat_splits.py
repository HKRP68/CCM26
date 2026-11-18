"""Tournament player filters use the recorded match and delivery evidence."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def competition(tmp_path):
    from models import Base, Match, Tournament, TournamentMatch, TournamentTeam, User
    from services import league_season_service as seasons, tournament_service

    engine = create_engine(f"sqlite:///{tmp_path / 'splits.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as session:
        tour = Tournament(name="T20 Cup", is_active=True, status="active",
                          fixtures_all_at_once=True, schedule_generated=True,
                          command_snapshot="tcup")
        session.add(tour)
        session.flush()
        a = TournamentTeam(tournament_id=tour.id, name="Mumbai Indians", short_name="MI",
                           owner_tg_id=12345, owner_name="A&B <Owner>",
                           co_owner_ids_json="[21000, 12345]")
        b = TournamentTeam(tournament_id=tour.id, name="Rajasthan Royals", owner_tg_id=34567)
        session.add_all([a, b, User(telegram_id=21000, username="co_owner")])
        session.flush()
        ids = tour.id, a.id, b.id
        match = Match(user1_id=101, user2_id=102, pitch_type="Green")
        session.add(match)
        session.flush()

        def add(runs, *, winner, pitch, detail=None, linked_match=None, result="win"):
            line = dict(name="Virat Kohli", team_name=a.name, user_id=101, roster_id=10,
                        batted=True, bat_runs=runs, bat_balls=2, bat_out=bool(detail))
            if detail:
                line.update(record_detail=detail, bowled=True, bowl_wickets=1,
                            bowl_runs=3, bowl_balls=1)
            session.add(TournamentMatch(tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
                                        winner_team_id=winner, pitch_type=pitch, result_type=result,
                                        match_id=linked_match, scorecard_json=json.dumps([line])))

        def delivery(phase, runs, *, wicket=False):
            return dict(innings=1, phase=phase, team=a.name, batter="Virat Kohli", rid="10",
                        opponent=b.name, bat_runs=runs, faced=1, wicket=wicket)
        bowling = dict(innings=2, phase="powerplay", team=b.name, opponent=a.name,
                       bowler="Virat Kohli", bid="10", conceded=3, legal=True, bowler_wicket=True)
        detail = dict(version=1, innings_starts={"1": {"complete": True}, "2": {"complete": True}},
                      deliveries=[delivery("powerplay", 4), delivery("middle", 6, wicket=True),
                                  bowling])
        add(10, winner=b.id, pitch="Flat", detail=detail, linked_match=match.id)
        add(20, winner=b.id, pitch="Green")
        add(30, winner=a.id, pitch="Green")
        add(40, winner=b.id, pitch="Flat")
        add(50, winner=None, pitch="Green", result="tie")
        add(60, winner=None, pitch="Green", result="no_result")
        add(70, winner=b.id, pitch=None)
        session.add(TournamentMatch(tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
                                    status="scheduled", match_no=9))
        tournament_service.recompute_player_stats(session, tour.id)
        series = seasons.create_series(session, "IPL")
        seasons.attach_season(session, series, tournament=tour)
    yield sessions, ids
    engine.dispose()


def _update(chat_type="supergroup", *, bot=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), chat_id=42)
    if bot is not None:
        msg.get_bot = lambda: bot
    return SimpleNamespace(message=msg, effective_message=msg,
                           effective_user=SimpleNamespace(id=10000),
                           effective_chat=SimpleNamespace(type=chat_type))


def test_filter_commands_and_coverage(competition, monkeypatch):
    from handlers import league_seasons, tournament

    sessions, (tour_id, _, _) = competition
    monkeypatch.setattr(tournament, "get_session", sessions)
    monkeypatch.setattr(league_seasons, "get_session", sessions)
    update = _update()
    asyncio.run(tournament.statstour_handler(
        update, SimpleNamespace(args=[f"#{tour_id}", "Virat", "|", "Loss", "|", "Green", "|", "PP"])))
    text = update.message.reply_text.await_args.args[0]
    assert "Losses · Green · Powerplay" in text
    assert "4 runs off 1 ball" in text
    assert "1 wkt · 3 runs off 1 ball" in text
    assert "2 matches had missing filter data" in text

    for query in ("Virat | Loss, Won | Green", "IPL | Virat Kohli | Loss, Won | Green"):
        update = _update()
        asyncio.run(league_seasons.seasons_handler(update, SimpleNamespace(args=query.split())))
        text = update.message.reply_text.await_args.args[0]
        assert "Losses · Green" in text
        assert "Wins · Green" in text
        assert text.count("30 runs off") >= 2
        assert "Season by season" in text


def test_saved_match_splits_and_combined_match_count(competition):
    from models import Tournament
    from services import player_stat_splits as splits, season_archive

    sessions, (tour_id, _, _) = competition
    with sessions.begin() as session:
        archived = season_archive.archive_tournament(session, session.get(Tournament, tour_id))
        filters = splits.parse_filters(["Loss", "Green", "All Phases"])
        report = splits.Report()
        report.add_season(splits.saved_matches(session, archived, phases=True), "virat kohli", filters)
        assert report.total.matches == 1
        assert (report.total.runs, report.total.wickets, report.total.outs) == (10, 1, 1)
        assert report.groups[("loss", "green", "middle")].runs == 6
        assert report.unavailable_matches == 2

        results = splits.Report()
        filters = splits.parse_filters(["Tie, No Result", "Green"])
        results.add_season(splits.saved_matches(session, archived, phases=False), "virat kohli", filters)
        assert results.groups[("tie", "green", None)].runs == 50
        assert results.groups[("no result", "green", None)].runs == 60


def test_tagteam_mentions_only_one_team_and_limits_repeat_calls(competition, monkeypatch):
    from handlers import cl_tournament

    sessions, (tour_id, _, _) = competition
    monkeypatch.setattr(cl_tournament, "get_session", sessions)
    update = _update()
    context = SimpleNamespace(args=[f"#{tour_id}", "MI"], chat_data={})
    asyncio.run(cl_tournament.tagteam_handler(update, context))
    text = update.message.reply_text.await_args.args[0]
    assert text.count("tg://user?id=12345") == 1
    assert "A&amp;B &lt;Owner&gt;" in text
    assert 'tg://user?id=21000">@co_owner</a>' in text
    assert "34567" not in text
    assert "Open fixture #9 vs <b>Rajasthan Royals</b>" in text
    asyncio.run(cl_tournament.tagteam_handler(update, context))
    assert "just tagged" in update.message.reply_text.await_args.args[0]

    private = _update("private")
    asyncio.run(cl_tournament.tagteam_handler(private, context))
    assert "match group" in private.message.reply_text.await_args.args[0]


def test_new_tournament_replies_send_native_rich_blocks(competition, monkeypatch):
    from handlers import cl_tournament, league_seasons, tournament
    from services import rich_message as rich
    from services.rich_message_html import render

    sessions, (tour_id, _, _) = competition
    for handler_module in (tournament, league_seasons, cl_tournament):
        monkeypatch.setattr(handler_module, "get_session", sessions)
    monkeypatch.setattr(rich.config, "RICH_TEXT_ENABLED", True)
    bot = SimpleNamespace(_post=AsyncMock(return_value={
        "message_id": 7, "date": 0, "chat": {"id": 42, "type": "supergroup"},
    }))

    for handler, query in (
        (tournament.statstour_handler, f"#{tour_id} Virat | Loss | Green | PP"),
        (league_seasons.seasons_handler, "IPL | Virat | Loss | Green | PP"),
        (cl_tournament.tagteam_handler, f"#{tour_id} MI"),
    ):
        update = _update(bot=bot)
        asyncio.run(handler(update, SimpleNamespace(args=query.split(), chat_data={})))
        update.message.reply_text.assert_not_awaited()
        endpoint, payload = bot._post.await_args.args
        assert endpoint == "sendRichMessage"
        blocks = payload["rich_message"]["blocks"]
        body = render(blocks)
        if handler is cl_tournament.tagteam_handler:
            assert 'tg://user?id=12345">A&amp;B &lt;Owner&gt;</a>' in body
            assert 'tg://user?id=21000">@co_owner</a>' in body
            assert "34567" not in body
            assert "Open fixture #9 vs <b>Rajasthan Royals</b>" in body
        else:
            assert any(block["type"] == "table" for block in blocks)
            assert "Losses · Green · Powerplay" in body
            assert "4 runs off 1 ball" in body
            assert "2 matches had missing filter data" in body
            if handler is league_seasons.seasons_handler:
                assert "<details><summary><b>Season by season</b></summary>" in body
                assert payload["reply_markup"].inline_keyboard


def test_long_rich_season_report_keeps_all_rows_and_buttons():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from handlers import player_stat_splits as cards
    from services import player_stat_splits as splits
    from utils.message_chunks import DEFAULT_CHUNK_LIMIT

    filters = splits.parse_filters(["Win"])
    report = splits.Report()
    report.total = splits.Split(matches=40, runs=80, faced=40)
    season_rows = [(f"Season {number} <Cup>", splits.Split(matches=1, runs=2, faced=1))
                   for number in range(1, 41)]
    pages = cards.pages("IPL", "Virat", filters, [("Total season stats", report, season_rows)])
    assert len(pages) > 1
    labels = [
        child["text"][0]["text"]
        for blocks, _ in pages for block in blocks if block["type"] == "details"
        for child in block["blocks"] if child["type"] == "paragraph"
    ]
    assert labels == [label for label, _ in season_rows]
    assert all(len(text) <= DEFAULT_CHUNK_LIMIT for _, text in pages)
    assert "Season 40 &lt;Cup&gt;" in pages[-1][1]

    message = _update().message
    back = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="ls_o_1_0_10000")]])
    asyncio.run(cards.reply_pages(message, pages, reply_markup=back))
    assert [call.kwargs["reply_markup"] for call in message.reply_text.await_args_list] == (
        [None] * (len(pages) - 1) + [back]
    )
