"""An existing paid prize survives the additive ledger upgrade unchanged."""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class PrizeMigrationTests(unittest.TestCase):
    def test_old_database_upgrades_idempotently_without_reminting_paid_prizes(self):
        from sqlalchemy import inspect, text
        from database import Base, engine, get_session, _migrate_add_columns
        from models import (ActivityLog, Tournament, TournamentHonour,
                            TournamentPrizePayout, TournamentTeam, User)
        from services import tournament_awards as A

        stamp = datetime(2026, 10, 1, 12, 30)
        session = get_session()
        owner = User(telegram_id=F.next_tg(), username="legacy_owner",
                     total_coins=60123, total_gems=77)
        coowner = User(telegram_id=F.next_tg(), username="new_coowner",
                       total_coins=456, total_gems=8)
        tour = Tournament(name="Legacy Prize Cup", status="completed",
                          awards_given_at=stamp,
                          prizes_json='{"champion":{"coins":90000,"gems":90}}')
        session.add_all([owner, coowner, tour])
        session.flush()
        team = TournamentTeam(tournament_id=tour.id, name="Legacy Team",
                              owner_tg_id=owner.telegram_id,
                              co_owner_ids_json=f"[{coowner.telegram_id}]")
        session.add(team)
        session.commit()
        owner_id, coowner_id, tour_id = owner.id, coowner.id, tour.id
        owner_tg = owner.telegram_id
        session.close()

        # Recreate the previous release's honour table (before the new team
        # foreign key), and remove the other additive fields from the fixture.
        # All historical columns and constraints match the earlier model.
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE tournament_prize_payouts"))
            connection.execute(text(
                "DROP INDEX IF EXISTS ix_tournament_honours_tournament_team_id"))
            connection.execute(text("DROP TABLE tournament_honours"))
            connection.execute(text("""
                CREATE TABLE tournament_honours (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
                    tournament_name VARCHAR(120) NOT NULL,
                    award VARCHAR(20) NOT NULL,
                    team_name VARCHAR(120),
                    player_name VARCHAR(150),
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    value VARCHAR(60),
                    prize_coins INTEGER NOT NULL,
                    prize_gems INTEGER NOT NULL,
                    awarded_at DATETIME NOT NULL,
                    announced_at DATETIME
                )
            """))
            for name in ("prize_split_json", "prize_split_snapshot_json",
                         "prize_split_locked_at"):
                connection.execute(text(f"ALTER TABLE tournament_teams DROP COLUMN {name}"))
            connection.execute(text("""
                INSERT INTO tournament_honours
                    (id, tournament_id, tournament_name, award, team_name, user_id,
                     value, prize_coins, prize_gems, awarded_at, announced_at)
                VALUES
                    (1, :tid, 'Legacy Prize Cup', 'champion', 'Legacy Team', :uid,
                     'Previously credited', 60000, 50, :stamp, :stamp)
            """), {"tid": tour_id, "uid": owner_id, "stamp": stamp})

        old_honour_columns = {column["name"] for column in
                              inspect(engine).get_columns("tournament_honours")}
        self.assertNotIn("tournament_team_id", old_honour_columns)
        self.assertNotIn("prize_snapshot_coins", old_honour_columns)
        self.assertNotIn("tournament_prize_payouts", inspect(engine).get_table_names())

        def historical_state():
            with engine.connect() as connection:
                balances = connection.execute(text(
                    "SELECT id, total_coins, total_gems FROM users ORDER BY id")).all()
                honour = connection.execute(text(
                    "SELECT id, tournament_id, tournament_name, award, team_name, "
                    "user_id, value, prize_coins, prize_gems, awarded_at, announced_at "
                    "FROM tournament_honours WHERE id=1")).one()
                return balances, honour

        before_upgrade = historical_state()
        # Production boot creates new tables first, then alters existing tables.
        Base.metadata.create_all(bind=engine)
        _migrate_add_columns()
        self.assertEqual(historical_state(), before_upgrade)
        self.assertIn("tournament_prize_payouts", inspect(engine).get_table_names())
        self.assertTrue({"prize_split_json", "prize_split_snapshot_json",
                         "prize_split_locked_at"}.issubset(
            {column["name"] for column in inspect(engine).get_columns("tournament_teams")}))
        self.assertTrue({"tournament_team_id", "prize_snapshot_coins",
                         "prize_snapshot_gems"}.issubset(
            {column["name"] for column in inspect(engine).get_columns("tournament_honours")}))

        Base.metadata.create_all(bind=engine)
        _migrate_add_columns()
        self.assertEqual(historical_state(), before_upgrade)

        session = get_session()
        try:
            for _ in range(2):
                self.assertEqual(A.finalize(session, session.get(Tournament, tour_id)), [])
                session.commit()
            self.assertEqual(historical_state(), before_upgrade)
            self.assertEqual((session.get(User, owner_id).total_coins,
                              session.get(User, owner_id).total_gems), (60123, 77))
            self.assertEqual((session.get(User, coowner_id).total_coins,
                              session.get(User, coowner_id).total_gems), (456, 8))
            receipt = session.query(TournamentPrizePayout).one()
            self.assertEqual((receipt.honour_id, receipt.recipient_tg_id,
                              receipt.coins, receipt.gems, receipt.status),
                             (1, owner_tg, 60000, 50, "paid"))
            honour = session.get(TournamentHonour, 1)
            self.assertEqual((honour.prize_snapshot_coins, honour.prize_snapshot_gems),
                             (60000, 50))
            self.assertEqual(session.query(ActivityLog).filter_by(
                action="tournament_prize").count(), 0)
        finally:
            session.close()
