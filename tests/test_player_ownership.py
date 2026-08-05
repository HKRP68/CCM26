"""/owners — who holds a card in this group, and how rare it is everywhere.

Three layers, one section each:

  1. Membership — ``services.chat_tracker.record_chat_member`` is the only
     source of "who is in this group" a bot gets, so it has to learn from
     ordinary messages, throttle those writes, and act on joins and leaves.
  2. Queries — ``services.ownership_service`` answers group-scoped and global
     ownership, across every edition of a player.
  3. Rendering — the group view names owners and pages; the DM view falls back
     to a per-group tally.

Everything runs against a real (in-memory SQLite) database, because the whole
feature is queries.
"""

import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import BotCommand, ChatMember, Player, User, UserRoster
from services import chat_tracker
from services import ownership_service as own
import handlers.owners as owners_mod


GROUP_ID = -100777
OTHER_GROUP_ID = -100888

# Columns the schema insists on but this feature never looks at.
_HANDS = {"bat_hand": "Right", "bowl_hand": "Right", "bowl_style": "Medium"}


def _run(coro):
    """Drive one handler call. A fresh loop each time keeps this file
    independent of whatever loop the rest of the suite left behind."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Msg:
    """A Telegram message that records what the handler sent back."""

    def __init__(self, sent):
        self.sent = sent
        self.chat_id = GROUP_ID
        self.message_id = 1

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return self


def _update(tg_id, chat_type="supergroup", chat_id=GROUP_ID, title="Cric Masters"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=tg_id, is_bot=False, username=None,
                                       first_name="Player"),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=title),
        effective_message=None,
        message=None,
    )


class OwnershipTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.close = lambda: None  # handlers close it; the test still reads

        self.base = Player(name="Rohit Sharma", version="Base", rating=92,
                           category="Batsman", country="India", is_active=True,
                           **_HANDS)
        self.db.add(self.base)
        self.db.flush()
        self.gold = Player(name="Rohit Sharma", version="Gold", rating=94,
                           category="Batsman", country="India", is_active=True,
                           parent_player_id=self.base.id, **_HANDS)
        self.other = Player(name="Jasprit Bumrah", version="Base", rating=91,
                            category="Bowler", country="India", is_active=True,
                            **_HANDS)
        self.db.add_all([self.gold, self.other])
        self.db.flush()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()

    def _user(self, tg_id, username=None, first_name="Manager"):
        user = User(telegram_id=tg_id, username=username, first_name=first_name)
        self.db.add(user)
        self.db.flush()
        return user

    def _own(self, user, player, days_ago=0):
        entry = UserRoster(user_id=user.id, player_id=player.id,
                           acquired_date=datetime.utcnow() - timedelta(days=days_ago))
        self.db.add(entry)
        self.db.flush()
        return entry

    def _member(self, user, chat_id=GROUP_ID, active=True):
        row = ChatMember(chat_id=chat_id, user_id=user.id, is_active=active,
                         first_seen_at=datetime.utcnow(),
                         last_seen_at=datetime.utcnow())
        self.db.add(row)
        self.db.flush()
        return row


# ════════════════════════════════════════════════════════════════════
# 1. Membership is learned from activity
# ════════════════════════════════════════════════════════════════════

class MembershipTrackingTests(OwnershipTestBase):
    def setUp(self):
        super().setUp()
        self._real_get_session = chat_tracker.__dict__.get("get_session")
        import database
        self._real_db_get_session = database.get_session
        database.get_session = lambda: self.db
        chat_tracker._MEMBER_SEEN_MEM.clear()

    def tearDown(self):
        import database
        database.get_session = self._real_db_get_session
        chat_tracker._MEMBER_SEEN_MEM.clear()
        super().tearDown()

    def _rows(self):
        return self.db.query(ChatMember).all()

    def test_a_message_in_a_group_records_the_sender(self):
        user = self._user(501)
        self.db.commit()
        chat_tracker.record_chat_member(_update(501))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].chat_id, rows[0].user_id, rows[0].is_active),
                         (GROUP_ID, user.id, True))

    def test_private_chats_are_not_groups(self):
        self._user(501)
        self.db.commit()
        chat_tracker.record_chat_member(_update(501, chat_type="private", chat_id=501))
        self.assertEqual(self._rows(), [])

    def test_users_who_never_debuted_are_not_recorded(self):
        chat_tracker.record_chat_member(_update(999))
        self.assertEqual(self._rows(), [])

    def test_the_second_message_does_not_write_again(self):
        self._user(501)
        self.db.commit()
        chat_tracker.record_chat_member(_update(501))
        first_seen = self._rows()[0].last_seen_at
        chat_tracker.record_chat_member(_update(501))
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self._rows()[0].last_seen_at, first_seen)

    def test_the_throttle_is_per_group(self):
        self._user(501)
        self.db.commit()
        chat_tracker.record_chat_member(_update(501))
        chat_tracker.record_chat_member(_update(501, chat_id=OTHER_GROUP_ID))
        self.assertEqual({r.chat_id for r in self._rows()},
                         {GROUP_ID, OTHER_GROUP_ID})

    def test_a_failed_write_is_retried_rather_than_throttled_away(self):
        """Nothing may be marked "recently seen" unless the row really landed."""
        chat_tracker.record_chat_member(_update(501))  # not debuted → no write
        user = self._user(501)  # they debut a moment later
        self.db.commit()
        chat_tracker.record_chat_member(_update(501))
        rows = self._rows()
        self.assertEqual([r.user_id for r in rows], [user.id])

    def test_joining_is_recorded_immediately(self):
        user = self._user(501)
        self.db.commit()
        update = _update(501)
        update.effective_user = SimpleNamespace(id=42, is_bot=False)  # an admin added them
        update.effective_message = SimpleNamespace(
            new_chat_members=[SimpleNamespace(id=501, is_bot=False)],
            left_chat_member=None)
        chat_tracker.record_chat_member(update)
        rows = self._rows()
        self.assertEqual([(r.user_id, r.is_active) for r in rows], [(user.id, True)])

    def test_bots_that_join_are_ignored(self):
        self._user(501)
        self.db.commit()
        update = _update(501)
        update.effective_message = SimpleNamespace(
            new_chat_members=[SimpleNamespace(id=77, is_bot=True)],
            left_chat_member=None)
        chat_tracker.record_chat_member(update)
        self.assertEqual([r.user_id for r in self._rows()],
                         [self.db.query(User).filter(User.telegram_id == 501).one().id])

    def test_leaving_deactivates_the_row(self):
        user = self._user(501)
        self.db.commit()
        chat_tracker.record_chat_member(_update(501))
        update = _update(501)
        update.effective_user = SimpleNamespace(id=42, is_bot=False)
        update.effective_message = SimpleNamespace(
            new_chat_members=None,
            left_chat_member=SimpleNamespace(id=501, is_bot=False))
        chat_tracker.record_chat_member(update)
        row = self.db.query(ChatMember).filter(ChatMember.user_id == user.id).one()
        self.assertFalse(row.is_active)

    def test_a_leaver_who_was_never_seen_creates_no_row(self):
        self._user(501)
        self.db.commit()
        update = _update(501)
        update.effective_user = SimpleNamespace(id=42, is_bot=False)
        update.effective_message = SimpleNamespace(
            new_chat_members=None,
            left_chat_member=SimpleNamespace(id=501, is_bot=False))
        chat_tracker.record_chat_member(update)
        self.assertEqual(self._rows(), [])


# ════════════════════════════════════════════════════════════════════
# 2. The ownership queries
# ════════════════════════════════════════════════════════════════════

class OwnershipQueryTests(OwnershipTestBase):
    def test_group_owners_exclude_managers_from_other_groups(self):
        inside = self._user(1, username="inside")
        outside = self._user(2, username="outside")
        self._own(inside, self.base)
        self._own(outside, self.base)
        self._member(inside)
        self._member(outside, chat_id=OTHER_GROUP_ID)
        self.db.commit()

        rows = own.group_owner_rows(self.db, GROUP_ID, [self.base.id, self.gold.id])
        self.assertEqual([u.username for u, _e, _p in rows], ["inside"])
        self.assertEqual(own.group_member_count(self.db, GROUP_ID), 1)
        self.assertEqual(own.global_owner_count(self.db, [self.base.id, self.gold.id]), 2)

    def test_every_edition_counts_as_owning_the_player(self):
        base_owner = self._user(1, username="base")
        gold_owner = self._user(2, username="gold")
        self._own(base_owner, self.base)
        self._own(gold_owner, self.gold)
        self._member(base_owner)
        self._member(gold_owner)
        self.db.commit()

        ids = [self.base.id, self.gold.id]
        rows = own.group_owner_rows(self.db, GROUP_ID, ids)
        # Ordered by card rating: the Gold edition leads.
        self.assertEqual([u.username for u, _e, _p in rows], ["gold", "base"])
        self.assertEqual(own.owners_per_version(self.db, ids),
                         {self.base.id: 1, self.gold.id: 1})

    def test_members_who_left_stop_being_counted(self):
        gone = self._user(1, username="gone")
        self._own(gone, self.base)
        self._member(gone, active=False)
        self.db.commit()
        self.assertEqual(own.group_member_count(self.db, GROUP_ID), 0)
        self.assertEqual(own.group_owner_rows(self.db, GROUP_ID, [self.base.id]), [])

    def test_owning_a_different_player_is_not_owning_this_one(self):
        user = self._user(1, username="other")
        self._own(user, self.other)
        self._member(user)
        self.db.commit()
        rows = own.group_owner_rows(self.db, GROUP_ID, [self.base.id, self.gold.id])
        self.assertEqual(rows, [])

    def test_counts_per_group_for_the_dm_view(self):
        here = self._user(1)
        there = self._user(2)
        both = self._user(3)
        for user in (here, there, both):
            self._own(user, self.base)
        self._member(here)
        self._member(there, chat_id=OTHER_GROUP_ID)
        self._member(both)
        self._member(both, chat_id=OTHER_GROUP_ID)
        self.db.commit()

        counts = own.owner_counts_by_group(
            self.db, [self.base.id, self.gold.id], [GROUP_ID, OTHER_GROUP_ID])
        self.assertEqual(counts, {GROUP_ID: 2, OTHER_GROUP_ID: 2})

    def test_a_manager_holding_two_editions_is_still_one_group_owner(self):
        hoarder = self._user(1)
        self._own(hoarder, self.base)
        self._own(hoarder, self.gold)
        self._member(hoarder)
        self.db.commit()
        counts = own.owner_counts_by_group(
            self.db, [self.base.id, self.gold.id], [GROUP_ID])
        self.assertEqual(counts, {GROUP_ID: 1})

    def test_scarcity_reads_as_a_ratio(self):
        self.assertEqual(own.scarcity_line(0, 100), "nobody owns this card yet")
        self.assertEqual(own.scarcity_line(100, 100), "every manager owns one")
        self.assertEqual(own.scarcity_line(10, 100), "1 in every 10 managers")

    def test_empty_inputs_never_query(self):
        self.assertEqual(own.group_owner_rows(self.db, GROUP_ID, []), [])
        self.assertEqual(own.global_owner_count(self.db, []), 0)
        self.assertEqual(own.owner_counts_by_group(self.db, [self.base.id], []), {})


# ════════════════════════════════════════════════════════════════════
# 3. What the command actually says
# ════════════════════════════════════════════════════════════════════

class OwnersHandlerTests(OwnershipTestBase):
    def setUp(self):
        super().setUp()
        self._real = owners_mod.get_session
        owners_mod.get_session = lambda: self.db
        self.sent = []

    def tearDown(self):
        owners_mod.get_session = self._real
        super().tearDown()

    def _call(self, args, chat_type="supergroup", tg_id=1):
        msg = _Msg(self.sent)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=tg_id, is_bot=False),
            effective_chat=SimpleNamespace(id=GROUP_ID, type=chat_type,
                                           title="Cric Masters"),
            message=msg)
        context = SimpleNamespace(args=args, bot_data={})
        _run(owners_mod.owners_handler(update, context))
        return self.sent[-1] if self.sent else ""

    def test_the_group_view_names_the_owners(self):
        caller = self._user(1, username="caller")
        mate = self._user(2, username="mate")
        self._own(caller, self.base, days_ago=5)
        self._own(mate, self.gold, days_ago=40)
        self._member(caller)
        self._member(mate)
        self.db.commit()

        text = self._call(["Rohit", "Sharma"])
        self.assertIn("Cric Masters", text)
        self.assertIn("Owned by <b>2</b> of 2 known members", text)
        self.assertIn("@caller", text)
        self.assertIn("@mate", text)
        self.assertIn("Gold ⭐94", text)
        self.assertIn("5d", text)
        self.assertIn("/trade @user", text)

    def test_a_manager_without_a_username_is_still_reachable(self):
        caller = self._user(1, first_name="Solo")
        self._own(caller, self.base)
        self._member(caller)
        self.db.commit()
        text = self._call(["Rohit Sharma"])
        self.assertIn('<a href="tg://user?id=1">Solo</a>', text)

    def test_nobody_in_the_group_owns_him(self):
        caller = self._user(1, username="caller")
        stranger = self._user(2, username="stranger")
        self._own(stranger, self.base)
        self._member(caller)
        self.db.commit()
        text = self._call(["Rohit Sharma"])
        self.assertIn("Nobody here owns this card yet", text)
        self.assertIn("1 owner", text)  # the global line still reports the truth

    def test_the_group_list_pages_at_ten(self):
        for i in range(1, 13):
            user = self._user(i, username=f"m{i}")
            self._own(user, self.base)
            self._member(user)
        self.db.commit()

        text = self._call(["Rohit Sharma"])
        self.assertIn("page 1/2", text)
        self.assertIn("Owned by <b>12</b> of 12 known members", text)
        self.assertEqual(text.count("\n1. "), 1)
        self.assertNotIn("11. ", text)

        # …and page two continues the numbering rather than restarting.
        page2, pages, total = owners_mod._render_group_page(
            self.db, self.base, [self.base, self.gold], GROUP_ID, "Cric Masters", 2)
        self.assertEqual((pages, total), (2, 12))
        self.assertIn("11. ", page2)

    def test_a_dm_falls_back_to_the_per_group_tally(self):
        caller = self._user(1, username="caller")
        mate = self._user(2, username="mate")
        self._own(mate, self.base)
        self._member(caller)
        self._member(mate)
        self.db.commit()

        text = self._call(["Rohit Sharma"], chat_type="private")
        self.assertIn("Your groups", text)
        self.assertIn("1</b> owner", text)
        self.assertNotIn("@mate", text)  # a DM never names other groups' members

    def test_an_unknown_player_says_so(self):
        self._user(1)
        self.db.commit()
        self.assertIn("not found", self._call(["Nobody At All"]))

    def test_an_admin_can_switch_the_command_off(self):
        """It is in the website's command catalog, so the toggle must bite."""
        self._user(1)
        self.db.add(BotCommand(command_key="owners", display_name="/owners",
                               enabled=False))
        self.db.commit()
        self.assertIn("temporarily disabled", self._call(["Rohit Sharma"]))

    def test_no_argument_explains_the_command(self):
        self._user(1)
        self.db.commit()
        self.assertIn("Usage", self._call([]))

    def test_an_ambiguous_name_asks_for_the_full_one(self):
        self._user(1)
        extra = Player(name="Rohit Kumar", version="Base", rating=80,
                       category="Bowler", country="India", is_active=True,
                       **_HANDS)
        self.db.add(extra)
        self.db.commit()
        text = self._call(["Rohit"])
        self.assertIn("Multiple players found", text)
        self.assertIn("/owners Rohit Sharma", text)

    def test_a_group_title_cannot_inject_html(self):
        caller = self._user(1, username="caller")
        self._own(caller, self.base)
        self._member(caller)
        self.db.commit()
        text, _pages, _total = owners_mod._render_group_page(
            self.db, self.base, [self.base], GROUP_ID, "<b>evil</b>", 1)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", text)


if __name__ == "__main__":
    unittest.main()
