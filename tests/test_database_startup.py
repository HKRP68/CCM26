"""Warm schema checks stay bounded without skipping fresh tables or migrations."""
from sqlalchemy import create_engine,event,inspect,text
from sqlalchemy.orm import sessionmaker

import pytest


@pytest.fixture
def database(tmp_path,monkeypatch):
    import database
    import models  # noqa: F401
    engine=create_engine('sqlite:///'+str(tmp_path/'boot.db'))
    monkeypatch.setattr(database,'engine',engine)
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=engine))
    yield database
    engine.dispose()


def test_warm_start_uses_table_inventory_and_preserves_existing_data(database):
    from services import auction_service as A
    database._create_missing_tables()
    with database.SessionLocal() as db:
        season=A.create_season(db,'Keep this auction')
        sid=season.id
        db.commit()
    queries=[]
    def record(conn,cursor,statement,parameters,context,many):
        queries.append(statement)
    event.listen(database.engine,'before_cursor_execute',record)
    database.Base.metadata.create_all(database.engine)
    old_count=len(queries)
    queries.clear()
    database._create_missing_tables()
    assert len(queries)<old_count/10
    assert not any('CREATE TABLE' in q.upper() or 'DROP TABLE' in q.upper() for q in queries)
    with database.engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM auction_seasons WHERE id=:id'),{'id':sid}).scalar_one()=='Keep this auction'


def test_new_tables_are_created_in_an_existing_shared_database(database):
    from models import FranchiseDraftAdmin
    database._create_missing_tables()
    FranchiseDraftAdmin.__table__.drop(database.engine)
    database._create_missing_tables()
    assert set(database.Base.metadata.tables)<=set(inspect(database.engine).get_table_names())


def test_existing_table_columns_still_receive_normal_migrations(database,monkeypatch):
    database._create_missing_tables()
    with database.engine.begin() as conn:
        conn.execute(text('ALTER TABLE franchise_drafts DROP COLUMN pick_minutes'))
    monkeypatch.setattr(database,'_seed_traits',lambda:None)
    monkeypatch.setattr(database,'_seed_competition_templates',lambda:None)
    monkeypatch.setattr(database,'_restore_uploaded_assets',lambda:None)
    database.init_db()
    columns={col['name'] for col in inspect(database.engine).get_columns('franchise_drafts')}
    assert 'pick_minutes' in columns


def test_inventory_failure_falls_back_to_complete_schema_checks(database,monkeypatch):
    import sqlalchemy
    def unavailable(*args,**kwargs):
        raise RuntimeError('Inventory temporarily unavailable')
    monkeypatch.setattr(sqlalchemy,'inspect',unavailable)
    database._create_missing_tables()
    with database.engine.connect() as conn:
        names={row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert set(database.Base.metadata.tables)<=names
