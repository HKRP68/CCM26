"""Telegram: an owner's /arelease and the public /seasons.

  • a non-admin owner releases his own kept player by name or by button, and
    nobody else's; the button asks before it releases;
  • /seasons shows the competition, a season, the all-time boards and a
    player season by season, and its buttons answer only the person who asked.

Reuses ``tests/test_league_seasons.py``'s Season-1-played fixture.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_league_seasons import (  # noqa: E402,F401 — module fixtures
    ALICE, BOB, NOW, LeagueCase, setUpModule, tearDownModule)


class HandlerCase(LeagueCase):

    def setUp(self):
        super().setUp()
        self.replies, self.markups, self.answers, self.edits = [], [], [], []

    def update(self, user_id, *, chat_id=None, chat_type="private", data=None):
        async def reply_text(text, **kwargs):
            self.replies.append(text)
            self.markups.append(kwargs.get("reply_markup"))
            return SimpleNamespace(message_id=5)

        async def answer(text=None, **kwargs):
            self.answers.append(text)

        async def edit_message_text(text, **kwargs):
            self.edits.append(text)
            self.markups.append(kwargs.get("reply_markup"))

        message = SimpleNamespace(reply_text=reply_text, message_id=7,
                                  chat_id=chat_id or user_id)
        user = SimpleNamespace(id=user_id, username="u", first_name="U")
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id or user_id, type=chat_type),
            effective_user=user, effective_message=message,
            callback_query=SimpleNamespace(data=data, answer=answer,
                                           message=message, from_user=user,
                                           edit_message_text=edit_message_text))

    def call(self, handler, user_id, args=(), **kw):
        previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = "999999"
        try:
            asyncio.run(handler(self.update(user_id, **kw),
                                SimpleNamespace(args=list(args),
                                                bot=SimpleNamespace())))
        finally:
            if previous is None:
                os.environ.pop("BOT_ADMIN_IDS", None)
            else:
                os.environ["BOT_ADMIN_IDS"] = previous

    def buttons(self):
        markup = self.markups[-1]
        return [b for row in (markup.inline_keyboard if markup else ()) for b in row]


class OwnerReleaseTests(HandlerCase):

    def setUp(self):
        super().setUp()
        _season, self.next_auction, _ = self.LS.start_mini_auction(
            self.session, self.series, purse_lakh=1100, now=NOW)
        # Earlier tests left Alice owning teams in other live auctions; a DM
        # would rightly ask which one. Only this one is still running here.
        from models import AuctionSeason
        for other in self.session.query(AuctionSeason).filter(
                AuctionSeason.id != self.next_auction.id,
                AuctionSeason.status.in_(self.A.STATUS_ACTIVE)).all():
            other.status = self.A.STATUS_CANCELLED
        self.session.commit()
        self.mumbai = next(f for f in self.A.franchises(self.session, self.next_auction.id)
                           if f.name == "Mumbai")

    def test_an_owner_releases_by_name_in_a_dm(self):
        from handlers import auction as H
        lot = self.A.retained(self.session, self.mumbai.id)[0]
        self.call(H.aunretain_handler, ALICE, lot.name.split()[0:2])
        self.assertIn("released", self.replies[-1])
        self.session.expire_all()
        self.assertEqual(self.A.LOT_QUEUED, self.session.get(type(lot), lot.id).status)

    def test_another_owner_cannot_name_your_player(self):
        from handlers import auction as H
        lot = self.A.retained(self.session, self.mumbai.id)[0]
        self.call(H.aunretain_handler, BOB, lot.name.split())
        self.assertIn("not one of your kept players", self.replies[-1])

    def test_bare_lists_buttons_and_a_tap_asks_first(self):
        from handlers import auction as H
        self.call(H.aunretain_handler, ALICE)
        buttons = self.buttons()
        self.assertTrue(buttons)
        first = buttons[0].callback_data
        self.assertTrue(first.startswith("au_rel_p_"))
        lot_id = int(first.rsplit("_", 1)[1])
        self.call(H.release_callback, BOB, data=first)
        self.assertIn("Only Mumbai", self.answers[-1])
        self.call(H.release_callback, ALICE, data=first)
        self.assertIn("Release", self.edits[-1])
        self.session.expire_all()
        from models import AuctionLot
        self.assertEqual(self.A.LOT_SOLD, self.session.get(AuctionLot, lot_id).status)
        self.call(H.release_callback, ALICE, data=f"au_rel_y_{lot_id}")
        self.session.expire_all()
        self.assertEqual(self.A.LOT_QUEUED, self.session.get(AuctionLot, lot_id).status)

    def test_a_stranger_is_told_no(self):
        from handlers import auction as H
        self.call(H.aunretain_handler, 424242)
        self.assertIn("franchise", self.replies[-1])


class SeasonsCommandTests(HandlerCase):

    def setUp(self):
        super().setUp()
        # One competition shown, so /seasons opens it rather than a picker.
        for series in self.LS.series_list(self.session):
            series.is_active = series.id == self.series.id
        self.session.commit()

    def test_several_competitions_get_a_picker(self):
        from handlers import league_seasons as H
        other = self.LS.create_series(self.session, f"Other {self.tag}")
        self.session.commit()
        self.call(H.seasons_handler, ALICE)
        self.assertIn("Which competition", self.replies[-1])
        other.is_active = False
        self.session.commit()

    def test_overview_season_alltime_and_player(self):
        from handlers import league_seasons as H
        self.call(H.seasons_handler, ALICE)
        self.assertIn("every season", self.replies[-1])
        self.assertIn("Mumbai", self.replies[-1])
        data = {b.text: b.callback_data for b in self.buttons()}
        self.assertIn("S1", data)

        self.call(H.seasons_callback, ALICE, data=data["S1"])
        self.assertIn("Top batters", self.edits[-1])
        self.call(H.seasons_callback, ALICE, data=data["📊 All-time"])
        self.assertIn("all-time", self.edits[-1])
        wkts = next(b.callback_data for b in self.buttons() if "Wickets" in b.text)
        self.call(H.seasons_callback, ALICE, data=wkts)
        self.assertIn("Wickets", self.edits[-1])

        self.call(H.seasons_handler, ALICE, args=[self.series.name, "|",
                                                  self.players[3].name])
        self.assertIn("Season by season", self.replies[-1])

    def test_buttons_answer_only_the_opener(self):
        from handlers import league_seasons as H
        self.call(H.seasons_handler, ALICE)
        data = self.buttons()[0].callback_data
        self.call(H.seasons_callback, BOB, data=data)
        self.assertIn("Only the person", self.answers[-1])

    def test_a_player_by_name_alone(self):
        from handlers import league_seasons as H
        self.call(H.seasons_handler, ALICE, args=self.players[0].name.split())
        self.assertIn(self.players[0].name, self.replies[-1])


if __name__ == "__main__":
    unittest.main()
