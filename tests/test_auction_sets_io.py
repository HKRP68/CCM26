"""Download every auction set as a file, and load it back.

The round trip is the promise: a pool exported from one season and imported
into a fresh one comes back as the same sets in the same running order. And an
import only ever rearranges QUEUED players — anybody already signed stays put.
"""

import json
import unittest

from tests.test_auction_retention import (  # noqa: F401 — module fixtures
    AuctionCase, setUpModule, tearDownModule)


class SetsFileCase(AuctionCase):

    def setUp(self):
        super().setUp()
        from services import auction_sets_io as SIO
        self.SIO = SIO
        base = [p for p in self.players if p.version == "Base"]
        self.stars, self.rest = base[:3], base[3:]
        self.A.add_players_to_pool(self.session, self.season, self.stars,
                                   set_name="Marquee")
        self.A.add_players_to_pool(self.session, self.season, self.rest,
                                   set_name="Capped")
        self.session.commit()

    def fresh_season(self):
        season = self.A.create_season(self.session,
                                      f"Copy of {self.season.name}")
        self.session.flush()
        return season

    def order(self, season):
        return [(self.A.set_label(lot), lot.player_id)
                for lot in self.A.queued_lots(self.session, season)]


class ExportTests(SetsFileCase):

    def test_json_lists_the_sets_in_running_order(self):
        payload = self.SIO.export_sets(self.session, self.season)
        self.assertEqual(self.SIO.SETS_FILE_FORMAT, payload["format"])
        self.assertEqual(["Marquee", "Capped"],
                         [s["name"] for s in payload["sets"]])
        self.assertEqual([p.id for p in self.stars],
                         [p["player_id"] for p in payload["sets"][0]["players"]])

    def test_csv_has_one_row_per_player(self):
        text = self.SIO.export_sets_csv(self.session, self.season).decode(
            "utf-8-sig")
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertEqual(",".join(self.SIO.CSV_FIELDS), lines[0])
        self.assertEqual(len(self.stars) + len(self.rest), len(lines) - 1)


class ImportTests(SetsFileCase):

    def test_json_round_trip_into_a_fresh_season(self):
        blob = self.SIO.export_sets_json(self.session, self.season)
        target = self.fresh_season()
        groups = self.SIO.parse_sets_file(blob)
        result = self.SIO.import_sets(self.session, target, groups)
        self.assertEqual(len(self.stars) + len(self.rest), result["added"])
        self.assertEqual(self.order(self.season), self.order(target))

    def test_csv_round_trip(self):
        blob = self.SIO.export_sets_csv(self.session, self.season)
        target = self.fresh_season()
        groups = self.SIO.parse_sets_file(blob, "csv")
        self.SIO.import_sets(self.session, target, groups)
        self.assertEqual(self.order(self.season), self.order(target))

    def test_an_import_refiles_and_reorders_queued_players(self):
        payload = {"sets": [
            {"name": "Openers", "players": [{"player_id": self.rest[0].id,
                                             "base_price_lakh": 250}]},
            {"name": "Marquee", "players": [{"player_id": p.id}
                                            for p in self.stars]},
        ]}
        groups = self.SIO.parse_sets_file(json.dumps(payload).encode())
        result = self.SIO.import_sets(self.session, self.season, groups)
        self.assertEqual(4, result["moved"])
        first = self.A.queued_lots(self.session, self.season)[0]
        self.assertEqual(("Openers", self.rest[0].id, 250),
                         (first.set_name, first.player_id,
                          first.base_price_lakh))

    def test_replace_drops_queued_players_the_file_leaves_out(self):
        payload = {"sets": [{"name": "Marquee",
                             "players": [{"player_id": p.id}
                                         for p in self.stars]}]}
        groups = self.SIO.parse_sets_file(json.dumps(payload).encode())
        result = self.SIO.import_sets(self.session, self.season, groups,
                                      replace=True)
        self.assertEqual(len(self.rest), result["removed"])
        self.assertEqual(len(self.stars),
                         len(self.A.queued_lots(self.session, self.season)))

    def test_signed_players_are_left_alone(self):
        from models import AuctionLot
        self.season.max_retentions = 2
        self.A.retain(self.session, self.season, self.mumbai, self.stars[0],
                      1800)
        payload = {"sets": [{"name": "Somewhere else",
                             "players": [{"player_id": self.stars[0].id}]}]}
        groups = self.SIO.parse_sets_file(json.dumps(payload).encode())
        result = self.SIO.import_sets(self.session, self.season, groups,
                                      replace=True)
        self.assertEqual(1, result["kept"])
        lot = (self.session.query(AuctionLot)
               .filter(AuctionLot.season_id == self.season.id,
                       AuctionLot.player_id == self.stars[0].id).one())
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual("Marquee", lot.set_name)

    def test_unknown_and_ambiguous_players_are_reported(self):
        payload = {"sets": [{"name": "X", "players": [
            {"name": "Nobody At All"},
            {"player_id": 99999999, "name": "Also Nobody"},
        ]}]}
        groups = self.SIO.parse_sets_file(json.dumps(payload).encode())
        result = self.SIO.import_sets(self.session, self.season, groups)
        self.assertEqual(2, len(result["unmatched"]))
        self.assertEqual(0, result["added"])

    def test_an_id_with_someone_elses_name_is_not_trusted(self):
        payload = {"sets": [{"name": "X", "players": [
            {"player_id": self.stars[0].id, "name": "Somebody Else Entirely"},
        ]}]}
        groups = self.SIO.parse_sets_file(json.dumps(payload).encode())
        result = self.SIO.import_sets(self.session, self.season, groups)
        self.assertEqual(1, len(result["unmatched"]))

    def test_bad_files_say_why(self):
        for blob in (b"", b"{not json", b'{"nothing": 1}', b"a,b\n1,2\n"):
            with self.assertRaises(self.A.AuctionError):
                self.SIO.parse_sets_file(blob)

    def test_refused_while_the_auction_is_live(self):
        self.season.status = self.A.STATUS_LIVE
        groups = [("X", [{"player_id": self.stars[0].id}])]
        with self.assertRaises(self.A.AuctionError):
            self.SIO.import_sets(self.session, self.season, groups)


if __name__ == "__main__":
    unittest.main()
