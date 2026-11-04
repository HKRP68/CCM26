"""Receipt revisions upgrade populated databases without changing money."""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


class CorrectionMigrationTests(unittest.TestCase):
    def setUp(self):
        F.setup_module_db()

    def tearDown(self):
        F.teardown_module_db()

    def check_upgrade(self, *, standalone_index=False):
        from database import Base, engine, get_session, _migrate_add_columns
        from models import Tournament, TournamentHonour, TournamentPrizePayout, User
        from sqlalchemy import inspect, text
        from sqlalchemy.exc import IntegrityError

        stamp = datetime(2026, 11, 4, 10, 30)
        session = get_session()
        user = User(telegram_id=F.next_tg(), total_coins=60123, total_gems=72)
        tour = Tournament(name="Previously paid cup", status="completed")
        session.add_all([user, tour])
        session.flush()
        honour = TournamentHonour(
            tournament_id=tour.id, tournament_name=tour.name, award="champion",
            team_name="Previous team", user_id=user.id,
            prize_coins=60000, prize_gems=60, awarded_at=stamp)
        session.add(honour)
        session.flush()
        user_id, tour_id, honour_id = user.id, tour.id, honour.id
        telegram_id = user.telegram_id
        session.commit()
        session.close()

        with engine.begin() as connection:
            connection.execute(text("DROP TABLE tournament_prize_payouts"))
            connection.execute(text("""
                CREATE TABLE tournament_prize_payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    honour_id INTEGER REFERENCES tournament_honours(id) ON DELETE SET NULL,
                    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
                    tournament_name VARCHAR(120) NOT NULL,
                    award VARCHAR(20) NOT NULL,
                    team_name VARCHAR(120),
                    recipient_tg_id BIGINT NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    coins INTEGER NOT NULL,
                    gems INTEGER NOT NULL,
                    share_bps INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    failure_reason VARCHAR(500),
                    balance_coins_after INTEGER,
                    balance_gems_after INTEGER,
                    paid_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                """ + ("" if standalone_index else ", CONSTRAINT uq_tournament_prize_recipient "
                        "UNIQUE (honour_id, recipient_tg_id)") + ")"))
            if standalone_index:
                connection.execute(text("CREATE UNIQUE INDEX legacy_prize_unique ON "
                                        "tournament_prize_payouts(honour_id, recipient_tg_id)"))
            connection.execute(text("CREATE INDEX legacy_prize_unique_status ON "
                                    "tournament_prize_payouts(status)"))
            connection.execute(text("CREATE TABLE prize_migration_audit(status VARCHAR(20))"))
            connection.execute(text("""
                CREATE TRIGGER legacy_prize_unique_status_audit
                AFTER UPDATE OF status ON tournament_prize_payouts
                BEGIN INSERT INTO prize_migration_audit(status) VALUES (NEW.status); END
            """))
            connection.execute(text("""
                INSERT INTO tournament_prize_payouts
                    (id, honour_id, tournament_id, tournament_name, award, team_name,
                     recipient_tg_id, user_id, coins, gems, share_bps, status,
                     balance_coins_after, balance_gems_after, paid_at, created_at, updated_at)
                VALUES (17, :hid, :tid, 'Previously paid cup', 'champion', 'Previous team',
                        :tg, :uid, 60000, 60, 10000, 'paid', 60123, 72, :stamp, :stamp, :stamp)
            """), {"hid": honour_id, "tid": tour_id, "tg": telegram_id,
                   "uid": user_id, "stamp": stamp})
            connection.execute(text("ALTER TABLE tournament_honours DROP COLUMN prize_revision"))
            connection.execute(text("ALTER TABLE tournament_teams DROP COLUMN prize_split_reopened_at"))

        original_columns = [column["name"] for column in inspect(engine).get_columns(
            "tournament_prize_payouts")]
        original_select = ", ".join(original_columns)

        def state():
            with engine.connect() as connection:
                return (
                    connection.execute(text("SELECT total_coins, total_gems FROM users "
                                            "WHERE id=:id"), {"id": user_id}).one(),
                    connection.execute(text(f"SELECT {original_select} "
                                            "FROM tournament_prize_payouts WHERE id=17")).one(),
                )

        before = state()
        for _ in range(2):
            Base.metadata.create_all(bind=engine)
            _migrate_add_columns()
            self.assertEqual(state(), before)

        inspector = inspect(engine)
        self.assertEqual(inspector.get_unique_constraints("tournament_prize_payouts")[0][
            "column_names"], ["honour_id", "revision", "recipient_tg_id"])
        self.assertIn("legacy_prize_unique_status", {
            index["name"] for index in inspector.get_indexes("tournament_prize_payouts")})
        self.assertTrue({"tournament_prize_corrections", "tournament_prize_redistributions"}
                        .issubset(inspector.get_table_names()))
        self.assertIn("prize_split_reopened_at", {column["name"] for column in
                                                 inspector.get_columns("tournament_teams")})
        copy_columns = original_columns[1:] + ["revision"]
        insert_sql = (f"INSERT INTO tournament_prize_payouts ({', '.join(copy_columns)}) "
                      f"SELECT {', '.join(original_columns[1:])}, :revision "
                      "FROM tournament_prize_payouts WHERE id=17")
        with engine.begin() as connection:
            self.assertEqual(connection.execute(text("SELECT revision FROM tournament_prize_payouts "
                                                     "WHERE id=17")).scalar_one(), 0)
            connection.execute(text(insert_sql), {"revision": 1})
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.execute(text(insert_sql), {"revision": 1})
        with engine.begin() as connection:
            connection.execute(text("UPDATE tournament_prize_payouts SET status=status WHERE id=17"))
            self.assertEqual(connection.execute(text("SELECT status FROM prize_migration_audit")).all(),
                             [("paid",)])
        self.assertEqual(state(), before)

    def test_inline_unique_receipt_table_preserves_money_and_allows_new_revision(self):
        self.check_upgrade()

    def test_standalone_unique_index_preserves_custom_name_prefix_objects(self):
        self.check_upgrade(standalone_index=True)
