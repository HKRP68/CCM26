"""Sold players reach their real owner, and the room tags people.

What is pinned here:

  • **The sale lands on the right franchise**: a co-owner's winning bid sells
    to the franchise they bid for, its purse pays, its squad grows.
  • **The owner follows the players after the auction**: a tournament built on
    a league the auction published inherits each franchise's owner and
    co-owners — added later or already existing at publish time — and an
    admin's own assignment is never overwritten.
  • **Tags**: the SOLD card tags the buyer's owner (and the co-owner who bid),
    the warning tags the leader, and a refused /bid reply tags the sender.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_auction_bidding import (  # noqa: E402,F401  (module fixtures)
    ALICE, BOB, CAROL, NOW, AuctionCase, setUpModule, tearDownModule)


class OwnerCase(AuctionCase):

    def setUp(self):
        super().setUp()
        self.A.set_co_owners(self.session, self.mumbai, [CAROL])
        self.season.auto_accelerated = 0
        self.session.commit()
        self.build_pool()
        self.lot = self.start()
        self.session.commit()

    def fresh(self, lot_id):
        from models import AuctionLot
        self.session.expire_all()
        return (self.session.query(AuctionLot)
                .filter(AuctionLot.id == lot_id).first())

    def carol_buys_the_first_lot(self):
        """Carol (Mumbai's co-owner) outbids Bob and wins."""
        lot = self.lot
        self.A.place_bid(self.session, self.season, lot, self.chennai,
                         lot.base_price_lakh, now=NOW, by_tg_id=BOB)
        lot = self.fresh(lot.id)
        self.A.place_bid(self.session, self.season, lot, self.mumbai,
                         self.A.next_min_bid(self.season, lot), now=NOW,
                         by_tg_id=CAROL)
        lot = self.A.sell_lot(self.session, self.season, self.fresh(lot.id),
                              now=NOW)
        self.session.commit()
        return self.fresh(lot.id)

    def finish(self):
        guard = 0
        while self.season.status == self.A.STATUS_LIVE and guard < 50:
            guard += 1
            lot = self.A.current_lot(self.session, self.season)
            if lot is None:
                if self.A.next_queued(self.session, self.season.id) is None:
                    self.A.complete_if_done(self.session, self.season)
                    break
                self.A.open_next_lot(self.session, self.season, now=NOW)
                continue
            self.A.pass_lot(self.session, self.season, lot)
        self.session.commit()


class SaleTests(OwnerCase):

    def test_a_co_owners_winning_bid_sells_to_their_franchise(self):
        before = int(self.mumbai.purse_remaining_lakh)
        lot = self.carol_buys_the_first_lot()
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(self.mumbai.id, lot.sold_to_id)
        self.session.refresh(self.mumbai)
        self.assertEqual(before - lot.sold_price_lakh,
                         int(self.mumbai.purse_remaining_lakh))
        self.assertEqual([lot.id], [l.id for l in
                                    self.A.squad(self.session, self.mumbai.id)])
        self.assertEqual(CAROL, self.A.winning_bidder(self.session, lot))
        self.assert_ledger_agrees()


class OwnershipAfterPublishTests(OwnerCase):

    def publish(self):
        self.carol_buys_the_first_lot()
        self.finish()
        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        return league

    def test_the_league_knows_each_teams_owner(self):
        from services import tournament_service as ts
        league = self.publish()
        owner, name, extras = ts.league_owner_for_team(self.session, league.id,
                                                       "Mumbai")
        self.assertEqual(ALICE, owner)
        self.assertEqual("Alice", name)
        self.assertEqual([CAROL], extras)
        self.assertEqual(BOB, ts.draft_owner_for_team(
            self.session, league.id, "Chennai")[0])
        self.assertEqual((None, None, []), ts.league_owner_for_team(
            self.session, league.id, "Nobody"))

    def test_an_existing_tournament_gets_owners_on_publish(self):
        from models import ChallengeTeam, Tournament, TournamentTeam
        self.carol_buys_the_first_lot()
        self.finish()
        league = self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        tour = Tournament(name="Cup", league_id=league.id)
        self.session.add(tour)
        self.session.flush()
        teams = {t.name: t for t in self.session.query(ChallengeTeam)
                 .filter(ChallengeTeam.league_id == league.id).all()}
        self.session.add(TournamentTeam(tournament_id=tour.id,
                                        challenge_team_id=teams["Mumbai"].id,
                                        name="Mumbai"))
        # An admin already chose Chennai's owner by hand — that must stand.
        self.session.add(TournamentTeam(tournament_id=tour.id,
                                        challenge_team_id=teams["Chennai"].id,
                                        name="Chennai", owner_tg_id=999))
        self.session.commit()
        # A correction is republished; the tournament's teams catch up.
        self.A.publish_to_league(self.session, self.season)
        self.session.commit()
        rows = {t.name: t for t in self.session.query(TournamentTeam)
                .filter(TournamentTeam.tournament_id == tour.id).all()}
        self.assertEqual(ALICE, rows["Mumbai"].owner_tg_id)
        self.assertIn(str(CAROL), rows["Mumbai"].co_owner_ids_json or "")
        self.assertEqual(999, rows["Chennai"].owner_tg_id)

    def test_the_published_card_names_every_owner(self):
        from services import auction_rich as AR
        self.publish()
        event = [e for e in self.A.pending_events(self.session, self.season,
                                                  limit=500)
                 if e.kind == "published"][-1]
        text = AR.event_html(self.session, self.season, event)
        self.assertIn(f"tg://user?id={ALICE}", text)
        self.assertIn(f"tg://user?id={BOB}", text)


class TagTests(OwnerCase):

    def test_the_sold_card_tags_the_owner_and_the_co_owner_who_bid(self):
        from services import auction_rich as AR
        self.carol_buys_the_first_lot()
        event = [e for e in self.A.pending_events(self.session, self.season,
                                                  limit=500)
                 if e.kind == "lot_sold"][-1]
        text = AR.event_html(self.session, self.season, event)
        self.assertIn(f"tg://user?id={ALICE}", text)
        self.assertIn("bid by", text)
        self.assertIn(f"tg://user?id={CAROL}", text)

    def test_an_owner_bidding_themselves_is_tagged_once(self):
        tag = self.A.owner_tag(self.session, self.mumbai, by_tg_id=ALICE)
        self.assertEqual(1, tag.count("tg://user?id="))
        self.assertNotIn("bid by", tag)

    def test_the_warning_tags_the_leader(self):
        from services import auction_rich as AR
        self.A.place_bid(self.session, self.season, self.lot, self.chennai,
                         self.lot.base_price_lakh, now=NOW, by_tg_id=BOB)
        self.session.commit()
        text = AR.warning_html(self.session, self.season, self.fresh(self.lot.id),
                               1, 19)
        self.assertIn("Selling", text)
        self.assertIn(f"tg://user?id={BOB}", text)

    def test_a_refused_bid_reply_tags_the_sender(self):
        from handlers import auction as H
        from services import auction_antispam as SPAM
        SPAM.reset()
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)
            return SimpleNamespace(message_id=1)

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=9999, username="x",
                                           first_name="Stranger"),
            effective_message=SimpleNamespace(reply_text=reply_text,
                                              message_id=7))
        asyncio.run(H.bid_handler(update, SimpleNamespace(args=[], bot=None)))
        self.assertTrue(replies[-1].startswith(
            '<a href="tg://user?id=9999">Stranger</a>, '))
        self.assertIn("owner or a co-owner", replies[-1])


if __name__ == "__main__":
    unittest.main()
