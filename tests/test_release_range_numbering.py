"""``/releasemultiple <from> <to>`` counts positions the way /myroster does.

The bot has two roster numberings: /myroster lists players in plain roster
order, while /pxi re-sorts the top 11 by category. A range release is a range
the user reads off /myroster, so both the preview and the confirmed delete have
to resolve against that order — otherwise typing "2 3" sells whichever cards
happen to sit second and third in the *categorised* order, which is a different
pair of players on almost every roster.

The rosters below are built so the two orders disagree on purpose.
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
                 "handlers.release")


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


# Roster order deliberately interleaves the categories, so the /pxi display
# order (batsmen → keepers → all-rounders → pacers → spinners) shuffles it.
_SQUAD = [
    ("Pace Pete", "Bowler", "Fast"),
    ("Bat Bailey", "Batsman", "Fast"),
    ("Keep Kim", "Wicket Keeper", "Fast"),
    ("Bat Bobby", "Batsman", "Fast"),
    ("Spin Sasha", "Bowler", "Off Spin"),
    ("Bat Blake", "Batsman", "Fast"),
]


class RangeUsesMyRosterNumberingTests(unittest.TestCase):
    def setUp(self):
        import handlers.release as rl
        from database import get_session
        from models import Player, User, UserRoster

        self.rl = rl
        rl._open_prompts.clear()

        self.session = get_session()
        self.tg_id = next(_TG_IDS)
        self.user = User(telegram_id=self.tg_id, username="ranger",
                         total_coins=0, total_gems=0,
                         roster_count=len(_SQUAD))
        self.session.add(self.user)
        self.session.flush()

        # Names and ids are read out now, while the rows are still alive — a
        # released entry can no longer be lazy-loaded through the ORM.
        self.by_position = {}
        self.entry_id = {}
        self.player_name = {}
        for position, (name, category, bowl_style) in enumerate(_SQUAD, 1):
            player = Player(name=f"{name} {self.user.id}", version="Base card",
                            rating=80, category=category, country="India",
                            bat_hand="Right", bowl_hand="Right",
                            bowl_style=bowl_style, bat_rating=80,
                            bowl_rating=60)
            self.session.add(player)
            self.session.flush()
            entry = UserRoster(user_id=self.user.id, player_id=player.id,
                               order_position=position)
            self.session.add(entry)
            self.session.flush()
            self.by_position[position] = (entry, player)
            self.entry_id[position] = entry.id
            self.player_name[position] = player.name
        self.session.commit()

    def tearDown(self):
        self.rl._open_prompts.clear()
        self.session.rollback()
        self.session.close()

    def _update(self):
        update = MagicMock()
        update.message = _Msg()
        update.effective_user = MagicMock()
        update.effective_user.id = self.tg_id
        return update

    def _preview(self, pos_from, pos_to):
        update = self._update()
        context = MagicMock()
        context.args = [str(pos_from), str(pos_to)]
        asyncio.run(self.rl.releasemultiple_handler(update, context))
        self.assertEqual(len(update.message.replies), 1)
        return update.message.replies[0]

    def _confirm(self, pos_from, pos_to):
        query = MagicMock()
        query.data = f"rlm_{self.tg_id}_{pos_from}_{pos_to}"
        query.from_user.id = self.tg_id
        query.message.chat_id = 77
        query.message.message_id = next(_TG_IDS)   # a fresh dedup key each time
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        asyncio.run(self.rl.releasemultiple_confirm_callback(update, MagicMock()))
        return query.edit_message_text.call_args.args[0]

    def test_the_myroster_order_is_not_the_pxi_order(self):
        """Guards the fixture: without this the test would pass either way."""
        from handlers.lineup import _build_display_order
        from services.roster_service import get_roster_ordered

        ordered = get_roster_ordered(self.session, self.user.id)
        display = _build_display_order(ordered)
        self.assertNotEqual([p.name for _, p in ordered],
                            [p.name for _, p in display])

    def test_the_preview_lists_the_myroster_players(self):
        preview = self._preview(2, 3)
        self.assertIn(self.player_name[2], preview)
        self.assertIn(self.player_name[3], preview)
        for position in (1, 4, 5, 6):
            self.assertNotIn(self.player_name[position], preview,
                             f"position {position} is outside the range")

    def test_the_preview_numbers_the_lines_as_the_user_typed_them(self):
        preview = self._preview(2, 3)
        self.assertIn(f"#2. {self.player_name[2]}", preview)
        self.assertIn(f"#3. {self.player_name[3]}", preview)

    def test_confirming_releases_exactly_those_players(self):
        from models import UserRoster

        self._preview(2, 3)
        text = self._confirm(2, 3)
        self.assertIn("RELEASED 2 PLAYERS", text)
        self.assertIn(self.player_name[2], text)
        self.assertIn(self.player_name[3], text)

        self.session.expire_all()
        survivors = {e.id for e in self.session.query(UserRoster)
                     .filter(UserRoster.user_id == self.user.id).all()}
        self.assertNotIn(self.entry_id[2], survivors)
        self.assertNotIn(self.entry_id[3], survivors)
        for position in (1, 4, 5, 6):
            self.assertIn(self.entry_id[position], survivors,
                          f"position {position} should have been left alone")

    def test_a_range_past_the_end_is_refused(self):
        update = self._update()
        context = MagicMock()
        context.args = ["5", "9"]
        asyncio.run(self.rl.releasemultiple_handler(update, context))
        self.assertIn("only have 6 players", update.message.replies[0])

    def test_the_usage_text_points_at_myroster(self):
        update = self._update()
        context = MagicMock()
        context.args = []
        asyncio.run(self.rl.releasemultiple_handler(update, context))
        self.assertIn("/myroster", update.message.replies[0])


class PagingMatchesTheOrderedRosterTests(unittest.TestCase):
    """/myroster's pages are slices of the very list the range resolves against."""

    def setUp(self):
        from database import get_session
        from models import Player, User, UserRoster

        self.session = get_session()
        self.user = User(telegram_id=next(_TG_IDS), username="pager",
                         total_coins=0, total_gems=0, roster_count=len(_SQUAD))
        self.session.add(self.user)
        self.session.flush()
        for position, (name, category, bowl_style) in enumerate(_SQUAD, 1):
            player = Player(name=f"{name} pager {self.user.id}",
                            version="Base card", rating=80, category=category,
                            country="India", bat_hand="Right",
                            bowl_hand="Right", bowl_style=bowl_style,
                            bat_rating=80, bowl_rating=60)
            self.session.add(player)
            self.session.flush()
            self.session.add(UserRoster(user_id=self.user.id,
                                        player_id=player.id,
                                        order_position=position))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_each_page_is_the_matching_slice(self):
        from services.roster_service import get_roster_ordered, get_user_roster

        ordered = get_roster_ordered(self.session, self.user.id)
        seen = []
        for page in (1, 2, 3):
            entries, total, total_pages = get_user_roster(
                self.session, self.user.id, page=page, page_size=2)
            self.assertEqual(total, len(_SQUAD))
            self.assertEqual(total_pages, 3)
            seen.extend(entries)
        self.assertEqual([e.id for e, _ in seen], [e.id for e, _ in ordered])

    def test_a_page_past_the_end_clamps_to_the_last_one(self):
        from services.roster_service import get_user_roster

        entries, _total, total_pages = get_user_roster(
            self.session, self.user.id, page=99, page_size=2)
        self.assertEqual(total_pages, 3)
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
