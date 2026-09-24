"""The rich-text renderings added to stats, matches, claims and fixtures.

``tests/test_lineup_rich_text.py`` guards the first surface to get a block tree
(the Playing XI) and the transport under all of them. This module guards the
rest, on the same terms:

* the block tree and the HTML string are two live renderings of one thing, so
  the tests read both and compare whatever a user acts on — a rank, a batting
  position, a score — rather than pinning the block tree on its own;
* every table cell carries the ``align``/``valign`` the API demands, and every
  tree survives a JSON round trip, because a payload Telegram refuses costs the
  rich rendering silently;
* a builder that cannot build has to return ``None`` rather than raise, since
  that is what tells its caller to send the HTML instead.
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Several handlers import database, which builds an engine at import time. No
# test here reads a row, but none of them may leave a stray DB in the repo root.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP.name}")

from handlers import claim as claim_handler  # noqa: E402
from handlers import team as team_handler  # noqa: E402
from handlers import tournament as tour_handler  # noqa: E402
from services import cl_tournament_rich as ctr  # noqa: E402
from services import match_broadcast  # noqa: E402
from services import match_rich  # noqa: E402
from services import rich_message as R  # noqa: E402
from services import scorecard_delivery  # noqa: E402


def tearDownModule():
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# ── Shared assertions ────────────────────────────────────────────────

def flatten(node):
    """A block or RichText tree as plain text, for reading a rendering back."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten(n) for n in node)
    parts = [flatten(node.get("text")), flatten(node.get("summary")),
             flatten(node.get("caption")), flatten(node.get("reference"))]
    for row in node.get("cells") or ():
        parts.append(" ".join(flatten(cell) for cell in row))
    for item in node.get("items") or ():
        parts.append(flatten(item.get("label")))
        parts.append(flatten(item.get("blocks")))
    parts.append(flatten(node.get("blocks")))
    return " ".join(p for p in parts if p)


def tables(blocks):
    """Every table in a block tree, including ones nested in a ``details``."""
    found = []
    for block in blocks or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "table":
            found.append(block)
        found.extend(tables(block.get("blocks")))
        for item in block.get("items") or ():
            found.extend(tables(item.get("blocks")))
    return found


class BlockTreeAssertions:
    """Mixed into every case below: what any block tree must satisfy."""

    def assertWellFormed(self, blocks):
        self.assertTrue(blocks, "builder produced nothing")
        # A payload Telegram cannot parse is a rendering silently lost.
        self.assertEqual(json.loads(json.dumps(blocks)), blocks)
        seen = 0
        for table in tables(blocks):
            for row in table["cells"]:
                for cell in row:
                    # align and valign are required by the API, not optional.
                    self.assertIn(cell["align"], ("left", "center", "right"))
                    self.assertIn(cell["valign"], ("top", "middle", "bottom"))
                    seen += 1
        return seen


# ── services/rich_message.py — the vocabulary added for these surfaces ──

class RichVocabularyTests(unittest.TestCase, BlockTreeAssertions):

    def test_checklist_carries_its_own_ticks(self):
        block = R.checklist([(True, "1 Wicket Keeper"), (False, "5 Bowlers")])
        self.assertTrue(block["is_checkbox"])
        self.assertEqual([item["is_checked"] for item in block["items"]],
                         [True, False])

    def test_blockquote_is_expandable_only_when_asked(self):
        self.assertNotIn("is_expandable", R.blockquote([R.paragraph("hi")]))
        self.assertTrue(
            R.blockquote([R.paragraph("hi")], expandable=True)["is_expandable"])

    def test_html_parts_leaves_a_short_message_whole(self):
        self.assertEqual(R.html_parts("<b>short</b>"), ["<b>short</b>"])

    def test_html_parts_cuts_a_long_message_under_the_limit(self):
        section = "<blockquote>" + ("x" * 500) + "</blockquote>"
        parts = R.html_parts("\n\n".join([section] * 20))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), R.CHUNK_LIMIT)
        # Nothing may be dropped on the way through.
        self.assertEqual(sum(p.count("<blockquote>") for p in parts), 20)


# ── Tournament stats ─────────────────────────────────────────────────

def _leader_rows():
    return [("Kohli", "RCB", "412"), ("Rohit", "MI", "388"),
            ("Gill", "GT", "355"), ("Pant", "DC", "301")]


class TournamentStatsBlockTests(unittest.TestCase, BlockTreeAssertions):

    def setUp(self):
        self.tour = SimpleNamespace(name="Summer Smash", id=1)

    def test_leaderboard_ranks_match_the_html_rendering(self):
        rows = _leader_rows()
        blocks = tour_handler._leaderboard_blocks(self.tour, "runs", rows)
        self.assertWellFormed(blocks)

        board = tables(blocks)[0]["cells"]
        # One header row, then one row per player, in the order given.
        self.assertEqual(len(board), len(rows) + 1)
        self.assertEqual([flatten(row[1]) for row in board[1:]],
                         [name for name, _team, _value in rows])
        self.assertEqual([flatten(row[3]) for row in board[1:]],
                         [value for _name, _team, value in rows])

        html = tour_handler._render(self.tour, "runs", rows)
        for name, _team, value in rows:
            self.assertIn(name, html)
            self.assertIn(value, html)

    def test_medals_go_to_the_first_three_and_nobody_else(self):
        board = tables(tour_handler._leaderboard_blocks(
            self.tour, "runs", _leader_rows()))[0]["cells"]
        self.assertEqual([flatten(row[0]) for row in board[1:]],
                         ["🥇", "🥈", "🥉", "4."])

    def test_value_column_is_named_per_category(self):
        for category, column in (("runs", "RUNS"), ("wkts", "WKTS"),
                                 ("econ", "ECON"), ("mvp", "PTS")):
            board = tables(tour_handler._leaderboard_blocks(
                self.tour, category, _leader_rows()))[0]["cells"]
            self.assertEqual(flatten(board[0][3]), column)

    def test_mvp_board_keeps_its_footnote(self):
        blocks = tour_handler._leaderboard_blocks(self.tour, "mvp",
                                                  _leader_rows())
        self.assertIn("/mvp", flatten(blocks))
        self.assertNotIn("/mvp", flatten(tour_handler._leaderboard_blocks(
            self.tour, "runs", _leader_rows())))

    def test_empty_board_says_so_instead_of_rendering_a_bare_table(self):
        blocks = tour_handler._leaderboard_blocks(self.tour, "runs", [])
        self.assertEqual(tables(blocks), [])
        self.assertIn("No qualifying players yet", flatten(blocks))

    def test_player_card_shows_batting_and_bowling_separately(self):
        row = SimpleNamespace(
            name="Kohli", team_name="RCB", matches=7, bat_runs=412,
            bat_balls=280, bat_fours=40, bat_sixes=12, highest_score=98,
            bowl_wickets=2, bowl_runs=60, bowl_balls=36,
            best_bowl_wickets=1, best_bowl_runs=18)
        with patch.object(tour_handler.tournament_service, "batting_average",
                          return_value=58.85):
            blocks = tour_handler._player_stats_blocks(self.tour, [row])
        self.assertWellFormed(blocks)
        captions = [flatten(t.get("caption")) for t in tables(blocks)]
        self.assertIn("🏏 Batting", captions)
        self.assertIn("🎯 Bowling", captions)
        flat = flatten(blocks)
        self.assertIn("412", flat)
        self.assertIn("1/18", flat)

    def test_second_match_for_a_name_is_collapsed_behind_the_first(self):
        def row(name):
            return SimpleNamespace(
                name=name, team_name="RCB", matches=1, bat_runs=10,
                bat_balls=10, bat_fours=1, bat_sixes=0, highest_score=10,
                bowl_wickets=0, bowl_runs=0, bowl_balls=0,
                best_bowl_wickets=None, best_bowl_runs=None)
        with patch.object(tour_handler.tournament_service, "batting_average",
                          return_value=None):
            blocks = tour_handler._player_stats_blocks(
                self.tour, [row("Kohli"), row("Kohli Jr")])
        details = [b for b in blocks if b.get("type") == "details"]
        self.assertEqual(len(details), 1)
        self.assertIn("Kohli Jr", flatten(details[0]))


# ── Challenge League fixtures, table and team cards ──────────────────

def _team(team_id, name, **kwargs):
    row = SimpleNamespace(
        id=team_id, name=name, played=4, won=3, lost=1, tied=0, points=6,
        points_adjust=0, points_adjust_note=None, owner_name="Owner",
        owner_tg_id=1, home_pitch="Green", user_tg_id=None, sort_order=team_id,
        max_squad_size=11)
    row._nrr = 0.512
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def _fixture(match_no, team1, team2, status="scheduled", **kwargs):
    return SimpleNamespace(
        match_no=match_no, round_no=1, id=match_no, stage="league",
        team1_id=team1, team2_id=team2, slot1_label=None, slot2_label=None,
        home_team_id=team1, status=status, result_text=None,
        winner_team_id=None, pitch_type="Green", **kwargs)


class ChallengeLeagueViewBlockTests(unittest.TestCase, BlockTreeAssertions):

    def setUp(self):
        self.tour = SimpleNamespace(id=1, name="CL 2026", status="active",
                                    max_teams=4, overs=20, format="League",
                                    league_name="IPL", description=None)
        self.teams = [_team(1, "Royal Challengers Bengaluru"),
                      _team(2, "MI", points_adjust=-2,
                            points_adjust_note="late XI")]

    def test_points_table_is_one_table_with_a_column_per_number(self):
        with patch.object(ctr.tournament_service, "points_table",
                          return_value=self.teams), \
             patch.object(ctr.tournament_service, "league_progress",
                          return_value=(4, 6)), \
             patch.object(ctr.tournament_service, "tournament_champion",
                          return_value=None):
            blocks = ctr.table_blocks(None, self.tour)
        self.assertWellFormed(blocks)
        board = tables(blocks)[0]["cells"]
        self.assertEqual([flatten(c) for c in board[0]],
                         ["#", "TEAM", "P", "W", "L", "T", "PTS", "NRR"])
        # The full name survives: the HTML version truncates it to 14 columns
        # to keep its fake table aligned, which is the whole reason for this one.
        self.assertEqual(flatten(board[1][1]), "Royal Challengers Bengaluru")
        # An adjusted total keeps its star, and the reason is one tap away.
        self.assertEqual(flatten(board[2][1]), "MI*")
        self.assertIn("late XI", flatten(blocks))

    def test_points_table_with_no_teams_renders_no_table(self):
        with patch.object(ctr.tournament_service, "points_table",
                          return_value=[]):
            blocks = ctr.table_blocks(None, self.tour)
        self.assertEqual(tables(blocks), [])
        self.assertIn("No teams have been added", flatten(blocks))

    def _fixtures_blocks(self, fixtures, viewer=None):
        session = SimpleNamespace(query=lambda _model: SimpleNamespace(
            filter_by=lambda **_kw: SimpleNamespace(
                order_by=lambda *_a: SimpleNamespace(
                    all=lambda: fixtures))))
        with patch.object(ctr.V, "teams", return_value=self.teams), \
             patch.object(ctr.tournament_service, "is_team_member",
                          side_effect=lambda tt, tg: tt.id == 1):
            return ctr.fixtures_blocks(session, self.tour, viewer_tg_id=viewer)

    def test_played_fixtures_are_struck_through_and_carry_the_result(self):
        played = _fixture(1, 1, 2, status="completed")
        played.result_text = "RCB won by 8 wickets"
        blocks = self._fixtures_blocks([played, _fixture(2, 2, 1)])
        self.assertWellFormed(blocks)
        rows = tables(blocks)[0]["cells"][1:]
        self.assertEqual(rows[0][1]["text"]["type"], "strikethrough")
        self.assertIn("RCB won by 8 wickets", flatten(rows[0][2]))
        # An unplayed one is not struck through, and names its pitch.
        self.assertNotIsInstance(rows[1][1].get("text"), dict)
        self.assertIn("Green", flatten(rows[1][2]))

    def test_viewer_gets_their_own_matches_in_their_own_table(self):
        fixtures = [_fixture(1, 1, 2), _fixture(2, 2, 2)]
        with_viewer = self._fixtures_blocks(fixtures, viewer=99)
        without = self._fixtures_blocks(fixtures)
        self.assertEqual(len(tables(with_viewer)), 2)
        self.assertEqual(len(tables(without)), 1)
        self.assertIn("Your next matches", flatten(with_viewer))

    def test_no_schedule_says_free_play_rather_than_an_empty_table(self):
        blocks = self._fixtures_blocks([])
        self.assertEqual(tables(blocks), [])
        self.assertIn("free-play", flatten(blocks))

    def test_a_builder_that_blows_up_returns_none_instead_of_raising(self):
        # A builder that blows up must not cost the player the answer: None is
        # how this module says "send the HTML". The guard is on each builder,
        # not only on the dispatcher, because the team cards are called direct.
        with patch.object(ctr.tournament_service, "points_table",
                          side_effect=RuntimeError("boom")):
            self.assertIsNone(ctr.table_blocks(None, self.tour))
            self.assertIsNone(ctr.render_blocks(None, self.tour, "table"))
        with patch.object(ctr.V, "_standing_of",
                          side_effect=RuntimeError("boom")):
            self.assertIsNone(ctr.team_schedule_blocks(
                None, self.tour, self.teams[0]))
            self.assertIsNone(ctr.team_stats_blocks(
                None, self.tour, self.teams[0]))


# ── The live match ───────────────────────────────────────────────────

def _match_state(**overrides):
    state = {
        "match_id": 7, "overs": 20, "innings": 1, "target": None,
        "bat_team_name": "RCB", "bowl_team_name": "MI",
        "batting_order": [
            {"roster_id": 1, "name": "Opener One", "bat_rating": 80},
            {"roster_id": 2, "name": "Opener Two", "bat_rating": 78},
        ],
        "bat_xi": [], "bowl_xi": [],
        "striker_idx": 0, "non_striker_idx": 1,
        "current_bowler": {"roster_id": 9, "name": "Bumrah",
                           "bowl_rating": 90, "bowl_style": "Fast",
                           "bowl_hand": "Right"},
        "bat_stats": {"1": {"runs": 42, "balls": 30, "fours": 5, "sixes": 1},
                      "2": {"runs": 18, "balls": 20, "fours": 2, "sixes": 0}},
        "bowl_stats": {"9": {"overs_done": 3, "this_over_balls": 2, "runs": 24,
                             "wickets": 1}},
        "current_over": 5, "current_ball": 2,
        "total_runs": 60, "total_wickets": 1,
        "partnership_runs": 30, "partnership_balls": 24,
        "timeline": ["1️⃣", "4️⃣", "0️⃣"], "chat_id": -100,
    }
    state.update(overrides)
    return state


class LiveMatchBlockTests(unittest.TestCase, BlockTreeAssertions):

    def test_live_board_carries_the_same_numbers_as_the_html_board(self):
        from services.match_engine import build_live_scorecard
        state = _match_state()
        blocks = match_rich.live_scorecard_blocks(state)
        self.assertWellFormed(blocks)
        flat = flatten(blocks)
        html = build_live_scorecard(state)
        for number in ("60/1", "42", "18", "RCB", "MI", "Bumrah"):
            self.assertIn(number, flat)
            self.assertIn(number, html)

    def test_striker_is_marked_in_the_crease_table(self):
        board = [t for t in tables(match_rich.live_scorecard_blocks(
            _match_state())) if flatten(t.get("caption")) == "🏏 At the crease"]
        self.assertEqual(len(board), 1)
        names = [flatten(row[0]) for row in board[0]["cells"][1:]]
        self.assertEqual(names, ["Opener One *", "Opener Two"])

    def test_a_chase_states_what_is_needed(self):
        state = _match_state(innings=2, target=181, inn1_team="MI",
                             inn1_runs=180, inn1_wickets=5, inn1_overs="20.0")
        blocks = match_rich.live_scorecard_blocks(state)
        self.assertWellFormed(blocks)
        quotes = [b for b in blocks if b.get("type") == "pullquote"]
        self.assertEqual(len(quotes), 1)
        self.assertIn("121", flatten(quotes[0]))   # 181 - 60
        self.assertIn("94", flatten(quotes[0]))    # 20*6 - 26

    def test_a_broken_state_returns_none_rather_than_raising(self):
        self.assertIsNone(match_rich.live_scorecard_blocks({}))

    def test_ball_result_leads_with_the_headline_then_the_board(self):
        blocks = match_rich.ball_result_blocks(
            _match_state(), bowler_name="Bumrah", delivery="Yorker",
            striker_name="Opener One", shot="Lofted Drive",
            headline="6️⃣ SIX! 💥", commentary="Into the second tier!",
            trait_lines=["💎 Power Hitter"])
        self.assertWellFormed(blocks)
        types = [b.get("type") for b in blocks]
        self.assertLess(types.index("pullquote"), types.index("blockquote"))
        self.assertIn("SIX!", flatten(blocks))
        self.assertIn("Into the second tier!", flatten(blocks))
        self.assertIn("Power Hitter", flatten(blocks))

    def test_prompts_mention_the_player_whose_turn_it_is(self):
        state = _match_state()
        for blocks in (
            match_rich.delivery_prompt_blocks(
                state, bowler=state["current_bowler"],
                striker=state["batting_order"][0], phase="Powerplay",
                mention_text="@bowler", mention_tg_id=4242,
                choose_label="🎯 SELECT VARIATION"),
            match_rich.shot_prompt_blocks(
                state, bowler=state["current_bowler"],
                striker=state["batting_order"][0], delivery="Bouncer",
                mention_text="@batter", mention_tg_id=4343),
        ):
            self.assertWellFormed(blocks)
            self.assertIn("tg://user?id=", json.dumps(blocks))

    def test_a_bot_opponent_is_named_but_never_linked(self):
        state = _match_state()
        blocks = match_rich.shot_prompt_blocks(
            state, bowler=state["current_bowler"],
            striker=state["batting_order"][0], delivery="Bouncer",
            mention_text="🤖 Bot", mention_tg_id=None)
        self.assertNotIn("tg://user", json.dumps(blocks))
        self.assertIn("🤖 Bot", flatten(blocks))

    def test_broadcast_scorecard_blocks_show_both_batsmen_and_the_bowler(self):
        state = _match_state()
        state["bat_stats"] = {1: state["bat_stats"]["1"],
                              2: state["bat_stats"]["2"]}
        state["bowl_stats"] = {9: state["bowl_stats"]["9"]}
        blocks = match_broadcast.build_live_scorecard_blocks(
            state, waiting_for_mention="@bowler")
        self.assertWellFormed(blocks)
        captions = [flatten(t.get("caption")) for t in tables(blocks)]
        self.assertIn("🪓 Batsmen", captions)
        self.assertIn("🎳 Bowler", captions)
        self.assertIn("Waiting for @bowler", flatten(blocks))

    def test_broadcast_blocks_survive_a_state_with_nothing_in_it(self):
        self.assertIsNotNone(match_broadcast.build_live_scorecard_blocks({}))

    def test_match_ready_card_keeps_the_mentions_it_was_handed(self):
        blocks = match_broadcast._match_ready_blocks(
            SimpleNamespace(stadium="Wankhede", pitch_type="Green", overs=20),
            "RCB", "MI", '<a href="tg://user?id=11">@alpha</a>', "🤖 Bot",
            None, "RCB chose to bat", None, has_button=True)
        self.assertWellFormed(blocks)
        flat = json.dumps(blocks, ensure_ascii=False)
        # A human captain still gets pinged; the bot opponent has no account to
        # link, and must not end up with a broken one.
        self.assertIn("tg://user?id=11", flat)
        self.assertIn("@alpha", flat)
        self.assertNotIn("<a href", flat)
        self.assertIn("🤖 Bot", flatten(blocks))
        self.assertIn("RCB chose to bat", flatten(blocks))

    def test_match_ready_card_says_when_the_mini_app_link_is_missing(self):
        blocks = match_broadcast._match_ready_blocks(
            SimpleNamespace(stadium=None, pitch_type=None, overs=5),
            "RCB", "MI", "@a", "@b", None, None, None, has_button=False)
        self.assertIn("Mini App link unavailable", flatten(blocks))


class PlayingXiBlockTests(unittest.TestCase, BlockTreeAssertions):

    def _squad(self, n):
        return [SimpleNamespace(id=i, name=f"Player {i}") for i in range(1, n + 1)]

    def test_bench_numbering_continues_the_xi(self):
        squad = self._squad(14)
        blocks = match_rich.playing_xi_blocks(
            "RCB", squad[:11], bench=squad[11:],
            name_of=lambda p: p.name, detail_of=lambda p: "Batsman")
        self.assertWellFormed(blocks)
        xi, bench = tables(blocks)
        self.assertEqual([flatten(r[0]) for r in xi["cells"][1:]],
                         [str(i) for i in range(1, 12)])
        self.assertEqual([flatten(r[0]) for r in bench["cells"][1:]],
                         ["12", "13", "14"])

    def test_bench_is_collapsed_and_absent_when_empty(self):
        squad = self._squad(11)
        with_bench = match_rich.playing_xi_blocks(
            "RCB", squad, bench=[SimpleNamespace(id=12, name="Twelfth")],
            name_of=lambda p: p.name)
        without = match_rich.playing_xi_blocks("RCB", squad,
                                               name_of=lambda p: p.name)
        self.assertTrue(any(b.get("type") == "details" for b in with_bench))
        self.assertFalse(any(b.get("type") == "details" for b in without))


# ── /claim and the messages it leads to ──────────────────────────────

class ClaimBlockTests(unittest.TestCase, BlockTreeAssertions):

    def test_retained_card_puts_the_attributes_behind_a_details(self):
        blocks = claim_handler._retained_blocks(
            "Kohli", "Batsman", 92, 95, 40, "Right", "Off Spin", 15,
            "@manager", 4242)
        self.assertWellFormed(blocks)
        details = [b for b in blocks if b.get("type") == "details"]
        self.assertEqual(len(details), 1)
        flat = flatten(details[0])
        for value in ("Batsman", "92", "95", "40", "Right", "Off Spin"):
            self.assertIn(value, flat)
        self.assertIn("15/", flatten(blocks))
        self.assertIn("tg://user?id=4242", json.dumps(blocks))

    def test_released_card_states_the_coins_only_when_there_are_any(self):
        paid = claim_handler._released_blocks("Kohli", 92, "@m", 1, coins=5000)
        free = claim_handler._released_blocks("Kohli", 92, "@m", 1)
        self.assertIn("+5,000", flatten(paid))
        self.assertNotIn("coins added", flatten(free))

    def test_replace_reads_as_one_swap(self):
        blocks = claim_handler._replaced_blocks("Old Name", "New Name", 15, 2)
        self.assertWellFormed(blocks)
        rows = tables(blocks)[0]["cells"]
        self.assertEqual(flatten(rows[0][2]), "Old Name")
        self.assertEqual(flatten(rows[1][2]), "New Name")
        self.assertIn("2 trait(s) returned", flatten(blocks))

    def test_auto_decide_card_names_the_outcome_and_the_player(self):
        blocks = claim_handler._auto_decide_blocks(
            "Time expired — auto-released", "Kohli", 92, coins=5000,
            note="Your squad was already full.")
        self.assertWellFormed(blocks)
        flat = flatten(blocks)
        self.assertIn("auto-released", flat)
        self.assertIn("Kohli", flat)
        self.assertIn("+5,000", flat)

    def test_a_builder_that_cannot_build_returns_none(self):
        # ``_card_details`` is what a malformed row breaks; the caller must get
        # None and fall back to HTML rather than lose the message.
        with patch.object(claim_handler, "_card_details",
                          side_effect=RuntimeError("boom")):
            self.assertIsNone(claim_handler._retained_blocks(
                "Kohli", "Batsman", 92, 95, 40, "R", "Off", 1, "@m", 1))


# ── A player's own career record, and the scorecard of last resort ──

class CareerRecordBlockTests(unittest.TestCase, BlockTreeAssertions):

    def test_batting_and_bowling_are_separate_tables(self):
        player = SimpleNamespace(name="Kohli", rating=92, category="Batsman")
        gs = SimpleNamespace(
            potm=4, bat_inns=30, runs=1200, fifties=9, hundreds=2, fours=140,
            sixes=44, ducks=1, bat_avg=44.4, bat_sr=138.2, hs_str="112*",
            bowl_inns=3, wickets_taken=2, three_fers=0, five_fers=0,
            hattricks=0, bowl_avg=30.0, bowl_economy=8.1, bowl_sr=22.5,
            bbf_str="1/18")
        blocks = team_handler._career_blocks(player, "🇮🇳", "@manager",
                                             1_250_000, gs)
        self.assertWellFormed(blocks)
        captions = [flatten(t.get("caption")) for t in tables(blocks)]
        self.assertIn("🏏 Batting", captions)
        self.assertIn("🎯 Bowling", captions)
        flat = flatten(blocks)
        self.assertIn("1200", flat)
        self.assertIn("112*", flat)
        self.assertIn("1/18", flat)
        self.assertIn("/gstats Kohli", flat)

    def test_a_missing_field_costs_the_rendering_not_the_message(self):
        self.assertIsNone(team_handler._career_blocks(
            SimpleNamespace(name="X", rating=1, category=None), "", "@m", 0,
            SimpleNamespace()))


class ScorecardFallbackBlockTests(unittest.TestCase, BlockTreeAssertions):

    def _cards(self):
        return [
            {"card_type": scorecard_delivery.CARD_BATTING, "innings": 1,
             "payload": {"team_name": "RCB", "total_runs": 186,
                         "total_wickets": 4, "overs_str": "20.0",
                         "extras_dict": {"total": 9},
                         "batsmen_rows": [
                             {"name": "Kohli", "runs": 104, "balls": 58,
                              "fours": 9, "sixes": 5, "dismissal": "not out"},
                             {"name": "Tailender", "status": "dnb"},
                         ]}},
            {"card_type": scorecard_delivery.CARD_BOWLING, "innings": 1,
             "payload": {"team_name": "MI", "bowlers_rows": [
                 {"name": "Bumrah", "overs": "4", "maidens": 1,
                  "runs_conceded": 22, "wickets": 2}]}},
            {"card_type": scorecard_delivery.CARD_SUMMARY, "innings": 2,
             "payload": {"winner_name": "RCB", "win_margin_text": "by 31 runs",
                         "inn1_team": "RCB", "inn1_runs": 186,
                         "inn1_wickets": 4, "inn1_overs": "20.0",
                         "inn2_team": "MI", "inn2_runs": 155,
                         "inn2_wickets": 9, "inn2_overs": "20.0",
                         "potm_name": "Kohli", "potm_stats": "104* (58)"}},
        ]

    def test_every_innings_card_becomes_a_table(self):
        blocks = scorecard_delivery.scorecard_blocks(self._cards())
        self.assertWellFormed(blocks)
        captions = [flatten(t.get("caption")) for t in tables(blocks)]
        self.assertTrue(any("RCB" in c and "186/4" in c for c in captions))
        self.assertIn("🎳 MI — Bowling", captions)
        self.assertIn("🏆 Result", captions)
        self.assertIn("104* (58)", flatten(blocks))

    def test_a_player_who_did_not_bat_is_left_out_of_both_renderings(self):
        cards = self._cards()
        blocks = scorecard_delivery.scorecard_blocks(cards)
        batting = tables(blocks)[0]["cells"]
        self.assertNotIn("Tailender", flatten(batting))
        self.assertNotIn("Tailender",
                         scorecard_delivery.text_scorecard(cards))

    def test_nothing_to_show_returns_none_in_both_renderings(self):
        self.assertIsNone(scorecard_delivery.scorecard_blocks([]))
        self.assertIsNone(scorecard_delivery.text_scorecard([]))


if __name__ == "__main__":
    unittest.main()
