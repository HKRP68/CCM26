"""Retention is only from last season's squad — and a rename cannot lose it.

Pinned here:

  • **The squad is the whole search space.** A name is looked up among the
    cards this franchise held last season, never the catalogue, so two
    editions of one cricketer are never a question: only one was on the squad.
    Loose matching ("gill") works inside it; a player from another squad, or a
    season following no league, is refused by name.
  • **Which team each franchise was** is resolved by a stored id first, then
    the carried link, then a forgiving name match that is unique both ways.
    A renamed side is tied to its old team by hand, and the link survives any
    later rename. Two franchises can never claim one team.
  • **The commands**: /aretain <team> lists the squad as buttons, a tap posts
    the offer; /retain lists the owner's own squad; /aprevious links a league.
"""

import asyncio
import unittest
from types import SimpleNamespace

from tests._previous_league import link_squads
from tests.test_auction_retention import (  # noqa: F401 — module fixtures
    _PID, ALICE, BOB, CAROL, AuctionCase, setUpModule, tearDownModule)


def _card(session, name, rating=90, version="Base"):
    from models import Player
    player = Player(name=name, rating=rating, category="Batsman",
                    country="India", version=version, bat_hand="Right",
                    bowl_hand="Right", bowl_style="Medium Pacer",
                    bat_rating=rating, bowl_rating=30, is_active=True)
    session.add(player)
    session.flush()
    return player


class SquadCase(AuctionCase):
    max_squad = 8

    def setUp(self):
        super().setUp()
        from services import retention_negotiation as RN
        self.RN = RN
        tag = next(_PID)
        # Two editions of one cricketer in the catalogue — the /aretain bug.
        self.gill = _card(self.session, f"Shubman Gill {tag}", 94)
        self.gill_ipl = _card(self.session, f"Shubman Gill {tag}", 96,
                              version="IPL2026")
        self.pant = _card(self.session, f"Rishabh Pant {tag}", 89)
        self.jadeja = _card(self.session, f"Ravindra Jadeja {tag}", 88)
        self.session.commit()
        self.tag = tag

    def link(self, squads):
        return link_squads(self.session, self.A, self.season, squads)


class FindPlayerTests(SquadCase):

    def setUp(self):
        super().setUp()
        self.link({"Mumbai": [self.gill, self.pant],
                   "Chennai": [self.gill_ipl, self.jadeja]})

    def find(self, name, franchise=None):
        return self.RN.find_retention_player(
            self.session, self.season, franchise or self.mumbai, name)

    def test_two_editions_resolve_to_the_card_on_the_squad(self):
        self.assertEqual(self.gill.id, self.find(self.gill.name).id)
        self.assertEqual(self.gill_ipl.id,
                         self.find(self.gill.name, self.chennai).id)

    def test_loose_names_work_inside_the_squad(self):
        self.assertEqual(self.gill.id, self.find("gill").id)
        self.assertEqual(self.gill.id, self.find("shubman").id)
        self.assertEqual(self.pant.id, self.find("PANT").id)

    def test_a_player_from_another_squad_is_refused_with_the_list(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.find("jadeja")
        text = str(caught.exception)
        self.assertIn("wasn't on Mumbai's squad", text)
        self.assertIn(self.pant.name, text)

    def test_an_ambiguous_word_asks_for_more(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.find(str(self.tag))
        self.assertIn("type more of the name", str(caught.exception))

    def test_the_candidates_say_who_is_already_kept(self):
        self.season.max_retentions = 2
        self.A.retain(self.session, self.season, self.mumbai, self.gill, 1800)
        states = {p.id: state for p, state, _n in self.RN.retention_candidates(
            self.session, self.season, self.mumbai)}
        self.assertEqual({self.gill.id: "kept", self.pant.id: "free"}, states)

    def test_check_previous_holder(self):
        self.RN.check_previous_holder(self.session, self.season, self.mumbai,
                                      self.gill)
        with self.assertRaises(self.A.AuctionError):
            self.RN.check_previous_holder(self.session, self.season,
                                          self.mumbai, self.jadeja)


class UnlinkedTests(SquadCase):

    def test_a_season_following_no_league_is_refused(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.RN.find_retention_player(self.session, self.season,
                                          self.mumbai, "gill")
        self.assertIn("/aprevious", str(caught.exception))


class RenamedTeamTests(SquadCase):

    def test_filler_words_and_case_still_match(self):
        self.link({"MUMBAI FC": [self.gill], "The Chennai XI": [self.pant]})
        links = self.A.previous_team_links(self.session, self.season)
        self.assertEqual("MUMBAI FC", links[self.mumbai.id][1].name)
        self.assertEqual("The Chennai XI", links[self.chennai.id][1].name)

    def test_a_renamed_side_is_tied_by_hand_and_stays_tied(self):
        self.link({"Kochi Tuskers": [self.gill], "Chennai": [self.pant]})
        links = self.A.previous_team_links(self.session, self.season)
        self.assertNotIn(self.mumbai.id, links, "Mumbai was never 'Kochi'")
        self.A.set_previous_team(self.session, self.season, self.mumbai,
                                 "kochi tuskers")
        self.assertEqual(self.gill.id, self.RN.find_retention_player(
            self.session, self.season, self.mumbai, "gill").id)
        # Renaming either side afterwards changes nothing.
        self.mumbai.name = "Mumbai Mavericks"
        self.session.flush()
        held = self.A.previous_squad_map(self.session, self.season)
        self.assertEqual(self.mumbai.id, held[self.gill.id].id)

    def test_the_stored_link_beats_a_name_match(self):
        self.link({"Mumbai": [self.gill], "Chennai": [self.pant]})
        self.A.set_previous_team(self.session, self.season, self.chennai,
                                 "none")
        self.chennai.previous_team_id = None
        self.mumbai.previous_team_id = None
        self.session.flush()
        self.A.set_previous_team(self.session, self.season, self.chennai,
                                 "Mumbai")
        held = self.A.previous_squad_map(self.session, self.season)
        self.assertEqual(self.chennai.id, held[self.gill.id].id)
        self.assertNotIn(self.pant.id, held,
                         "Mumbai lost its name match to the explicit link")

    def test_one_team_cannot_be_claimed_twice(self):
        self.link({"Mumbai": [self.gill], "Chennai": [self.pant]})
        with self.assertRaises(self.A.AuctionError) as caught:
            self.A.set_previous_team(self.session, self.season, self.chennai,
                                     "Mumbai")
        self.assertIn("already linked to Mumbai", str(caught.exception))

    def test_linking_a_league_pins_every_match_and_stamps_rtm(self):
        league = self.link({"Kochi Tuskers": [self.gill],
                            "Chennai": [self.pant]})
        self.A.set_previous_team(self.session, self.season, self.mumbai,
                                 "Kochi Tuskers")
        self.A.add_players_to_pool(self.session, self.season,
                                   [self.gill, self.pant])
        self.A.link_previous_season(self.session, self.season, league.id)
        by_player = {lot.player_id: lot.previous_franchise_id
                     for lot in self.A.lots(self.session, self.season.id)}
        self.assertEqual(self.mumbai.id, by_player[self.gill.id])
        self.assertEqual(self.chennai.id, by_player[self.pant.id])
        self.assertIsNotNone(self.chennai.previous_team_id, "pinned by id")


class CommandTests(SquadCase):

    def setUp(self):
        super().setUp()
        self.season.max_retentions = 3
        self.session.commit()
        self.link({"Mumbai": [self.gill, self.pant],
                   "Chennai": [self.gill_ipl, self.jadeja]})
        self.replies, self.markups = [], []

    def update(self, user_id, answers=None, edits=None, data=None):
        async def reply_text(text, **kwargs):
            self.replies.append(text)
            self.markups.append(kwargs.get("reply_markup"))
            return SimpleNamespace(message_id=5)

        async def answer(text=None, **kwargs):
            (answers if answers is not None else []).append(text)

        async def edit_message_text(text, **kwargs):
            (edits if edits is not None else []).append(text)

        message = SimpleNamespace(reply_text=reply_text, message_id=7)
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=self.season.chat_id,
                                           type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username="u",
                                           first_name="U"),
            effective_message=message,
            callback_query=SimpleNamespace(
                data=data, answer=answer, message=message,
                edit_message_text=edit_message_text))

    def call(self, handler, user_id, args=(), **kw):
        import os
        previous = os.environ.get("BOT_ADMIN_IDS")
        os.environ["BOT_ADMIN_IDS"] = str(CAROL)
        try:
            asyncio.run(handler(self.update(user_id, **kw),
                                SimpleNamespace(args=list(args),
                                                bot=SimpleNamespace())))
        finally:
            if previous is None:
                os.environ.pop("BOT_ADMIN_IDS", None)
            else:
                os.environ["BOT_ADMIN_IDS"] = previous

    def test_aretain_with_a_surname_posts_the_offer(self):
        from handlers import auction as H
        self.call(H.aretain_handler, CAROL, ("Mumbai", "|", "gill", "|", "20"))
        self.assertIn("Retention offer", self.replies[-1])
        pending = self.A.pending_retention_offers(self.session, self.season.id)
        self.assertEqual([self.gill.id], [o.player_id for o in pending])

    def test_bare_aretain_lists_the_squad_and_a_tap_offers(self):
        from handlers import auction as H
        self.call(H.aretain_handler, CAROL, ("Mumbai",))
        self.assertIn("last season's squad", self.replies[-1])
        data = [b.callback_data for row in self.markups[-1].inline_keyboard
                for b in row]
        self.assertEqual({f"au_rpk_o_{self.mumbai.id}_{p.id}"
                          for p in (self.gill, self.pant)}, set(data))
        answers = []
        self.call(H.retention_pick_callback, CAROL, answers=answers,
                 data=f"au_rpk_o_{self.mumbai.id}_{self.pant.id}")
        self.session.expire_all()
        pending = self.A.pending_retention_offers(self.session, self.season.id)
        self.assertEqual([self.pant.id], [o.player_id for o in pending])

    def test_a_tap_from_a_non_admin_is_refused(self):
        from handlers import auction as H
        answers = []
        self.call(H.retention_pick_callback, ALICE, answers=answers,
                 data=f"au_rpk_f_{self.mumbai.id}_{self.pant.id}")
        self.assertEqual([], self.A.retained(self.session, self.mumbai.id))

    def test_aretainforce_by_surname(self):
        from handlers import auction as H
        self.call(H.aretainforce_handler, CAROL, ("Mumbai", "|", "pant"))
        self.session.expire_all()
        self.assertEqual([self.pant.id], [l.player_id for l in
                                          self.A.retained(self.session,
                                                          self.mumbai.id)])

    def test_bare_retain_lists_the_owners_own_squad(self):
        from handlers import auction as H
        from services import retention_negotiation as RN
        RN.set_mode(self.session, self.season, RN.MODE_DYNAMIC)
        self.session.commit()
        self.call(H.retain_handler, BOB)
        self.assertIn("Chennai", self.replies[-1])
        self.assertIn(self.jadeja.name, self.replies[-1])
        self.assertNotIn(self.pant.name, self.replies[-1])

    def test_aprevious_links_and_aprevteam_fixes_a_rename(self):
        from handlers import auction as H
        from services import auction_service as A
        league = link_squads(self.session, A, self.season,
                             {"Kochi Tuskers": [self.gill],
                              "Chennai": [self.pant]})
        self.season.previous_league_id = None
        self.session.commit()
        self.call(H.aprevious_handler, CAROL, (str(league.id),))
        self.assertIn("Linked to", self.replies[-1])
        self.assertIn("❓", self.replies[-1], "Mumbai matched nothing")
        self.call(H.aprevteam_handler, CAROL,
                 ("Mumbai", "|", "Kochi", "Tuskers"))
        self.assertIn("was <b>Kochi Tuskers</b>", self.replies[-1])
        self.session.expire_all()
        self.assertEqual(league.id, self.season.previous_league_id)
        self.assertIsNotNone(self.mumbai.previous_team_id,
                             "the renamed side is pinned by id")


if __name__ == "__main__":
    unittest.main()
