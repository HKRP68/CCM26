"""One roster numbering, shared by every surface that shows or takes a position.

A card has two positions and they are not the same number:

  • ``UserRoster.order_position`` is the batting slot — gameplay data.
  • the *display position* is what the user reads: /pxi groups the XI by
    category, so the seventh line on screen is usually not batting slot seven.

``/release N`` and ``/releasemultiple N M`` quote the number off the screen, so
they must resolve against the display position, and /myroster must print that
same number. These tests pin the whole loop: the ordering itself, both release
commands, and the listing they are read from.

The fixture squad is built so the two numberings disagree on purpose — the
guard test below asserts that, so nothing here can pass vacuously.
"""

import asyncio
import itertools
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

_TG_IDS = itertools.count(97_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.roster_service",
                 "services.roster_view", "handlers.release", "handlers.myroster")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class _Msg:
    """Just enough of a telegram Message for the release handlers."""

    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        sent = MagicMock()
        sent.chat_id = 77
        sent.message_id = len(self.replies)
        return sent


# Batting order deliberately interleaves the categories so the display sort
# (batsmen → keepers → all-rounders → pacers → spinners) genuinely reshuffles
# the XI. 14 cards: 11 in the XI, 3 on the bench.
_SQUAD = [
    # (name, category, bowl_style)
    ("Pace Pete", "Bowler", "Fast"),            # batting slot 1
    ("Bat Bailey", "Batsman", "Fast"),          # 2
    ("Keep Kim", "Wicket Keeper", "Fast"),      # 3
    ("Spin Sasha", "Bowler", "Off Spin"),       # 4
    ("Bat Bobby", "Batsman", "Fast"),           # 5
    ("All Alex", "All-rounder", "Fast"),        # 6
    ("Bat Blake", "Batsman", "Fast"),           # 7
    ("Pace Pat", "Bowler", "Fast"),             # 8
    ("Bat Bree", "Batsman", "Fast"),            # 9
    ("Spin Sam", "Bowler", "Leg Spin"),         # 10
    ("Bat Bo", "Batsman", "Fast"),              # 11
    ("Bench Bill", "Batsman", "Fast"),          # 12 — bench
    ("Bench Ben", "Bowler", "Fast"),            # 13 — bench
    ("Bench Bea", "Wicket Keeper", "Fast"),     # 14 — bench
]

# The display order the squad above produces: batsmen, keeper, all-rounder,
# pacers, spinners, then the bench untouched.
_EXPECTED_DISPLAY = [
    "Bat Bailey", "Bat Bobby", "Bat Blake", "Bat Bree", "Bat Bo",   # 1-5
    "Keep Kim",                                                      # 6
    "All Alex",                                                      # 7
    "Pace Pete", "Pace Pat",                                         # 8-9
    "Spin Sasha", "Spin Sam",                                        # 10-11
    "Bench Bill", "Bench Ben", "Bench Bea",                          # 12-14
]


class _SquadFixture(unittest.TestCase):
    """A user holding ``_SQUAD`` in batting order."""

    def setUp(self):
        from database import get_session
        from models import Player, User, UserRoster

        self.session = get_session()
        self.tg_id = next(_TG_IDS)
        self.user = User(telegram_id=self.tg_id, username="ranger",
                         total_coins=0, total_gems=0, roster_count=len(_SQUAD))
        self.session.add(self.user)
        self.session.flush()

        # Ids and names are read out now, while the rows are alive — a released
        # entry can no longer be lazy-loaded through the ORM.
        self.entry_id = {}
        self.suffix = f" {self.user.id}"
        for slot, (name, category, bowl_style) in enumerate(_SQUAD, 1):
            player = Player(name=f"{name}{self.suffix}", version="Base card",
                            rating=80, category=category, country="India",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style=bowl_style, bat_rating=80,
                            bowl_rating=60)
            self.session.add(player)
            self.session.flush()
            entry = UserRoster(user_id=self.user.id, player_id=player.id,
                               order_position=slot)
            self.session.add(entry)
            self.session.flush()
            self.entry_id[name] = entry.id
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def display_name(self, position):
        """The bare name expected at a 1-based display position."""
        return _EXPECTED_DISPLAY[position - 1]

    def full_name(self, bare):
        return f"{bare}{self.suffix}"


class DisplayOrderTests(_SquadFixture):
    def test_the_display_order_is_the_xi_by_category_then_the_bench(self):
        from services.roster_view import get_display_roster

        display = get_display_roster(self.session, self.user.id)
        self.assertEqual([p.name for _, p in display],
                         [self.full_name(n) for n in _EXPECTED_DISPLAY])

    def test_it_disagrees_with_the_batting_order(self):
        """Guards every other test here: if these matched, nothing would prove
        which numbering the commands actually use."""
        from services.roster_service import get_roster_ordered
        from services.roster_view import get_display_roster

        batting = [p.name for _, p in get_roster_ordered(self.session, self.user.id)]
        display = [p.name for _, p in get_display_roster(self.session, self.user.id)]
        self.assertNotEqual(batting, display)

    def test_the_bench_keeps_its_batting_order(self):
        from services.roster_view import get_display_roster, split_xi_bench

        _xi, bench = split_xi_bench(get_display_roster(self.session, self.user.id))
        self.assertEqual([p.name for _, p in bench],
                         [self.full_name(n) for n in
                          ("Bench Bill", "Bench Ben", "Bench Bea")])

    def test_reading_the_roster_never_rewrites_the_batting_order(self):
        """The display sort is a view. If it wrote back, merely opening
        /myroster would reshuffle who opens the batting."""
        from models import UserRoster
        from services.roster_view import get_display_roster

        before = {e.id: e.order_position for e in self.session.query(UserRoster)
                  .filter(UserRoster.user_id == self.user.id).all()}
        get_display_roster(self.session, self.user.id)
        self.session.expire_all()
        after = {e.id: e.order_position for e in self.session.query(UserRoster)
                 .filter(UserRoster.user_id == self.user.id).all()}
        self.assertEqual(before, after)

    def test_the_xi_bench_split_lands_on_eleven(self):
        from services.roster_view import is_in_xi, zone_of, XI_SIZE

        self.assertEqual(XI_SIZE, 11)
        self.assertTrue(is_in_xi(11))
        self.assertFalse(is_in_xi(12))
        self.assertEqual(zone_of(11), "XI")
        self.assertEqual(zone_of(12), "Bench")

    def test_find_position_round_trips(self):
        from services.roster_view import find_position, get_display_roster

        display = get_display_roster(self.session, self.user.id)
        for position in (1, 6, 7, 11, 12, 14):
            entry, _player = display[position - 1]
            self.assertEqual(find_position(display, entry.id), position)

    def test_find_position_is_none_for_a_stranger(self):
        from services.roster_view import find_position, get_display_roster

        display = get_display_roster(self.session, self.user.id)
        self.assertIsNone(find_position(display, 9_999_999))


class _HandlerFixture(_SquadFixture):
    def setUp(self):
        import handlers.release as rl
        super().setUp()
        self.rl = rl
        rl._open_prompts.clear()

    def tearDown(self):
        self.rl._open_prompts.clear()
        super().tearDown()

    def _update(self):
        update = MagicMock()
        update.message = _Msg()
        update.effective_user = MagicMock()
        update.effective_user.id = self.tg_id
        return update

    def _run(self, handler, *args):
        update = self._update()
        context = MagicMock()
        context.args = [str(a) for a in args]
        asyncio.run(handler(update, context))
        self.assertTrue(update.message.replies, "handler sent nothing")
        return update.message.replies[0]


class ReleaseSinglePositionTests(_HandlerFixture):
    def _prompt(self, position):
        return self._run(self.rl.releasepl_handler, position)

    def test_a_position_resolves_through_the_display_order(self):
        for position in (1, 6, 7, 12, 14):
            self.rl._open_prompts.clear()
            prompt = self._prompt(position)
            self.assertIn(self.full_name(self.display_name(position)), prompt,
                          f"position {position} picked the wrong card")

    def test_it_is_not_the_batting_order(self):
        """Display 1 is a batsman; batting slot 1 is the pacer who opens."""
        prompt = self._prompt(1)
        self.assertIn(self.full_name("Bat Bailey"), prompt)
        self.assertNotIn(self.full_name("Pace Pete"), prompt)

    def test_the_prompt_shows_the_number_that_was_typed(self):
        self.assertIn("#7.", self._prompt(7))

    def test_the_prompt_says_whether_the_card_is_xi_or_bench(self):
        self.assertIn("XI", self._prompt(7))
        self.rl._open_prompts.clear()
        self.assertIn("Bench", self._prompt(12))

    def test_a_position_past_the_end_is_refused(self):
        self.assertIn("No match", self._prompt(15))


class ReleaseMultipleRangeTests(_HandlerFixture):
    def _preview(self, pos_from, pos_to):
        return self._run(self.rl.releasemultiple_handler, pos_from, pos_to)

    def _confirm(self, pos_from, pos_to):
        query = MagicMock()
        query.data = f"rlm_{self.tg_id}_{pos_from}_{pos_to}"
        query.from_user.id = self.tg_id
        query.message.chat_id = 77
        query.message.message_id = next(_TG_IDS)   # fresh dedup key each time
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        asyncio.run(self.rl.releasemultiple_confirm_callback(update, MagicMock()))
        return query.edit_message_text.call_args.args[0]

    def test_the_range_covers_the_display_positions(self):
        preview = self._preview(6, 8)
        for position in (6, 7, 8):
            self.assertIn(self.full_name(self.display_name(position)), preview)

    def test_players_outside_the_range_are_untouched_in_the_preview(self):
        preview = self._preview(6, 8)
        for position in (1, 5, 9, 14):
            self.assertNotIn(self.full_name(self.display_name(position)), preview,
                             f"position {position} is outside 6→8")

    def test_the_preview_numbers_the_lines_as_the_user_typed_them(self):
        preview = self._preview(6, 8)
        for position in (6, 7, 8):
            self.assertIn(f"#{position}. {self.full_name(self.display_name(position))}",
                          preview)

    def test_a_bench_only_range_says_so(self):
        preview = self._preview(12, 14)
        self.assertIn("bench", preview.lower())
        self.assertNotIn("from your XI", preview)

    def test_a_range_straddling_the_split_reports_both_sides(self):
        preview = self._preview(10, 13)
        self.assertIn("2 from your XI", preview)
        self.assertIn("2 from the bench", preview)

    def test_an_xi_only_range_is_called_out(self):
        preview = self._preview(1, 3)
        self.assertIn("Playing XI", preview)

    def test_gutting_the_squad_warns_about_the_xi(self):
        preview = self._preview(1, 5)
        self.assertIn("short", preview)
        self.assertIn("Playing XI", preview)

    def test_a_release_that_keeps_eleven_does_not_warn(self):
        preview = self._preview(12, 14)
        self.assertNotIn("short of the", preview)

    def test_confirming_releases_exactly_the_displayed_cards(self):
        from models import UserRoster

        self._preview(6, 8)
        text = self._confirm(6, 8)
        self.assertIn("RELEASED 3 PLAYERS", text)

        self.session.expire_all()
        survivors = {e.id for e in self.session.query(UserRoster)
                     .filter(UserRoster.user_id == self.user.id).all()}
        for position in (6, 7, 8):
            self.assertNotIn(self.entry_id[self.display_name(position)], survivors,
                             f"display position {position} should be gone")
        for position in (1, 5, 9, 14):
            self.assertIn(self.entry_id[self.display_name(position)], survivors,
                          f"display position {position} should have survived")

    def test_a_range_past_the_end_is_refused(self):
        self.assertIn("only have 14 players", self._preview(12, 20))

    def test_the_usage_text_names_both_listings_and_the_split(self):
        usage = self._run(self.rl.releasemultiple_handler)
        self.assertIn("/myroster", usage)
        self.assertIn("/pxi", usage)
        self.assertIn("bench", usage.lower())


class BothCommandsAgreeTests(_HandlerFixture):
    """The whole point of the unification: one number, one card."""

    def test_release_n_and_releasemultiple_n_n_pick_the_same_player(self):
        for position in (2, 7, 11, 13):
            self.rl._open_prompts.clear()
            single = self._run(self.rl.releasepl_handler, position)
            self.rl._open_prompts.clear()
            ranged = self._run(self.rl.releasemultiple_handler, position, position)
            expected = self.full_name(self.display_name(position))
            self.assertIn(expected, single)
            self.assertIn(expected, ranged)


class MyRosterShowsTheSameNumbersTests(_SquadFixture):
    def _page(self, page):
        from config import ROSTER_PAGE_SIZE
        from handlers.myroster import _build_roster_message
        from services.roster_service import get_user_roster, get_roster_stats

        entries, total, total_pages = get_user_roster(
            self.session, self.user.id, page, ROSTER_PAGE_SIZE)
        stats = get_roster_stats(self.session, self.user.id)
        text, _kb = _build_roster_message(self.user, entries, stats, page,
                                          total, total_pages)
        return text

    def test_it_lists_in_display_order(self):
        from services.roster_view import get_display_roster

        display = get_display_roster(self.session, self.user.id)
        pages = [self._page(1), self._page(2)]
        for position, (_entry, player) in enumerate(display, 1):
            page = pages[0] if position <= 10 else pages[1]
            self.assertIn(f"{position}. {player.name}", page,
                          f"position {position} is mislabelled")

    def test_the_first_page_is_all_xi(self):
        page = self._page(1)
        self.assertIn("PLAYING XI", page)
        self.assertNotIn("BENCH", page)

    def test_a_page_straddling_the_split_labels_both_sections(self):
        """Page size is 10 and the XI is 11, so page 2 opens on the last XI
        card and then crosses onto the bench."""
        page = self._page(2)
        self.assertIn("PLAYING XI", page)
        self.assertIn("BENCH", page)

    def test_it_points_at_the_commands_that_take_these_numbers(self):
        page = self._page(1)
        self.assertIn("/pxi", page)
        self.assertIn("/release", page)

    def test_paging_covers_the_display_roster_exactly_once(self):
        from config import ROSTER_PAGE_SIZE
        from services.roster_service import get_user_roster
        from services.roster_view import get_display_roster

        seen = []
        for page in (1, 2):
            entries, total, total_pages = get_user_roster(
                self.session, self.user.id, page, ROSTER_PAGE_SIZE)
            self.assertEqual(total, len(_SQUAD))
            self.assertEqual(total_pages, 2)
            seen.extend(entries)
        self.assertEqual([e.id for e, _ in seen],
                         [e.id for e, _ in get_display_roster(self.session, self.user.id)])

    def test_a_page_past_the_end_clamps_to_the_last_one(self):
        from services.roster_service import get_user_roster

        entries, _total, total_pages = get_user_roster(
            self.session, self.user.id, page=99, page_size=10)
        self.assertEqual(total_pages, 2)
        self.assertEqual(len(entries), 4)


class PxiBenchNumberingTests(_SquadFixture):
    """/pxi's bench used to print order_position; it must print the display
    position, which is what /release accepts."""

    def test_the_bench_continues_the_xi_serial(self):
        from handlers.lineup import format_bench_text
        from services.fancy_text import circled, small_caps
        from services.roster_service import get_roster_ordered

        text = format_bench_text(get_roster_ordered(self.session, self.user.id))
        for position, bare in ((12, "Bench Bill"), (13, "Bench Ben"),
                               (14, "Bench Bea")):
            self.assertIn(f"{circled(position)} {small_caps(self.full_name(bare))}",
                          text)

    def test_a_drifted_order_position_does_not_shift_the_bench_numbers(self):
        """The numbers come from the list, not from the column, so a gap in
        order_position can't make /pxi advertise a position /release rejects."""
        from models import UserRoster
        from handlers.lineup import format_bench_text
        from services.roster_service import get_roster_ordered

        # Push every row's order_position up by 100, preserving their order.
        for entry in (self.session.query(UserRoster)
                      .filter(UserRoster.user_id == self.user.id)
                      .order_by(UserRoster.order_position).all()):
            entry.order_position += 100
        self.session.flush()

        from services.fancy_text import circled

        text = format_bench_text(get_roster_ordered(self.session, self.user.id))
        self.assertIn(circled(12), text)
        self.assertNotIn(circled(112), text)


class HowtoDocumentsTheRealSyntaxTests(unittest.TestCase):
    """/howto has to teach a command line the handler actually parses."""

    def _releasing_text(self):
        from handlers.howto import SECTIONS
        return SECTIONS["squad"]["body"]

    def test_it_documents_two_space_separated_positions(self):
        self.assertIn("/releasemultiple &lt;from&gt; &lt;to&gt;", self._releasing_text())

    def test_it_no_longer_teaches_the_hyphenated_form(self):
        self.assertNotIn("/releasemultiple A-B", self._releasing_text())

    def test_it_names_the_numbering(self):
        self.assertIn("/pxi", self._releasing_text())


if __name__ == "__main__":
    unittest.main()
