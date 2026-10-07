"""Auto-building the pool as IPL-style sets.

Marquee first, then role sets in tiers that rotate Batsmen → Bowlers →
All-rounders → Wicket-keepers — and, because it writes through the sets
import, nobody already signed moves.
"""

import unittest

from tests.test_auction_retention import (  # noqa: F401 — module fixtures
    AuctionCase, setUpModule, tearDownModule)


class AutoSetsCase(AuctionCase):

    def setUp(self):
        super().setUp()
        from models import Player
        from services import auction_auto_sets as AUTO
        self.AUTO = AUTO
        # Every test adds a fresh catalogue to one shared database; only this
        # test's cards should be eligible.
        mine = {p.id for p in self.players}
        for player in self.session.query(Player).all():
            if player.id not in mine:
                player.is_active = False
        self.session.commit()

    def names(self, plan):
        return [(name, [p.name for p in rows]) for name, rows in plan]

    def queue(self):
        return [(self.A.set_label(lot), lot.name)
                for lot in self.A.queued_lots(self.session, self.season)]


class PlanTests(AutoSetsCase):

    def test_marquee_then_roles_in_rotation(self):
        plan = self.AUTO.plan_auto_sets(self.session, self.season,
                                        marquee=2, set_size=2)
        self.assertEqual([
            ("Marquee", ["Virat Kohli", "Jasprit Bumrah"]),
            ("Batsmen 1", ["Tim David", "Rinku Singh"]),
            ("Bowlers 1", ["Mukesh Kumar"]),
            ("All-rounders 1", ["Rashid Khan"]),
            ("Wicket-keepers 1", ["Jos Buttler", "Sanju Samson"]),
        ], self.names(plan))

    def test_tiers_rotate_before_the_second_batch(self):
        plan = self.AUTO.plan_auto_sets(self.session, self.season,
                                        marquee=0, set_size=2)
        self.assertEqual(["Batsmen 1", "Bowlers 1", "All-rounders 1",
                          "Wicket-keepers 1", "Batsmen 2"],
                         [name for name, _ in plan])

    def test_special_editions_only_when_asked(self):
        plain = self.AUTO.plan_auto_sets(self.session, self.season, marquee=1)
        self.assertEqual(97, plain[0][1][0].rating)
        every = self.AUTO.plan_auto_sets(self.session, self.season, marquee=1,
                                         editions=True)
        self.assertEqual(99, every[0][1][0].rating)

    def test_min_rating_and_pool_size(self):
        plan = self.AUTO.plan_auto_sets(self.session, self.season, marquee=0,
                                        min_rating=90)
        self.assertEqual(4, sum(len(rows) for _, rows in plan))
        plan = self.AUTO.plan_auto_sets(self.session, self.season, marquee=0,
                                        pool_size=3)
        self.assertEqual(3, sum(len(rows) for _, rows in plan))

    def test_bad_numbers_are_refused(self):
        with self.assertRaises(self.A.AuctionError):
            self.AUTO.plan_auto_sets(self.session, self.season, set_size=1)
        with self.assertRaises(self.A.AuctionError):
            self.AUTO.plan_auto_sets(self.session, self.season, min_rating=999)

    def icon_of_virat(self):
        base = next(p for p in self.players
                    if p.name == "Virat Kohli" and p.version == "Base")
        icon = next(p for p in self.players if p.version == "Icon")
        icon.parent_player_id = base.id
        self.session.commit()
        return icon

    def test_one_version_only(self):
        icon = self.icon_of_virat()
        plan = self.AUTO.plan_auto_sets(self.session, self.season,
                                        versions=["Icon"])
        self.assertEqual([("Marquee", [icon])],
                         [(n, rows) for n, rows in plan])

    def test_several_versions_keep_one_card_per_cricketer(self):
        icon = self.icon_of_virat()
        plan = self.AUTO.plan_auto_sets(self.session, self.season, marquee=1,
                                        versions="Base, Icon")
        cards = [p for _, rows in plan for p in rows]
        self.assertEqual(icon, cards[0])
        self.assertEqual(1, sum(p.name == "Virat Kohli" for p in cards))
        self.assertEqual(8, len(cards))

    def test_unknown_version_is_refused_by_name(self):
        with self.assertRaises(self.A.AuctionError) as caught:
            self.AUTO.plan_auto_sets(self.session, self.season,
                                     versions=["Nope"])
        self.assertIn("Nope", str(caught.exception))

    def test_role_spellings(self):
        key = self.AUTO.role_key
        self.assertEqual("ar", key("All Rounder"))
        self.assertEqual("ar", key("All-rounder"))
        self.assertEqual("wk", key("Wicket Keeper"))
        self.assertEqual("bowl", key("Bowler"))
        self.assertEqual("bat", key("Batsman"))
        self.assertEqual("bat", key(None))


class BuildTests(AutoSetsCase):

    def test_builds_the_queue_in_planned_order(self):
        result = self.AUTO.auto_build_sets(self.session, self.season,
                                           marquee=2, set_size=2)
        self.session.commit()
        self.assertEqual(8, result["added"])
        self.assertEqual(
            ["Marquee", "Batsmen 1", "Bowlers 1", "All-rounders 1",
             "Wicket-keepers 1"],
            [e["name"] for e in self.A.list_sets(self.session, self.season)])
        self.assertEqual(("Marquee", "Virat Kohli"), self.queue()[0])

    def test_running_twice_changes_nothing(self):
        self.AUTO.auto_build_sets(self.session, self.season, marquee=2,
                                  set_size=2)
        first = self.queue()
        result = self.AUTO.auto_build_sets(self.session, self.season,
                                           marquee=2, set_size=2)
        self.assertEqual(0, result["added"])
        self.assertEqual(first, self.queue())

    def test_signed_players_stay_put_and_skip_marquee(self):
        from models import AuctionLot
        self.build_pool([self.players[0]])
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id).one())
        lot.status = self.A.LOT_SOLD
        self.session.commit()
        self.AUTO.auto_build_sets(self.session, self.season, marquee=1,
                                  set_size=5)
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(("Marquee", "Jasprit Bumrah"), self.queue()[0])

    def test_replace_drops_queued_players_left_out(self):
        self.build_pool()
        self.AUTO.auto_build_sets(self.session, self.season, marquee=0,
                                  min_rating=90, replace=True)
        self.assertEqual(4, len(self.queue()))

    def test_refused_while_live(self):
        self.build_pool()
        self.start()
        with self.assertRaises(self.A.AuctionError):
            self.AUTO.auto_build_sets(self.session, self.season)


if __name__ == "__main__":
    unittest.main()
