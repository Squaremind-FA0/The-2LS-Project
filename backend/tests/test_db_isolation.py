import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.models import Company

_CREATE_SCRATCH = "CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, value TEXT)"


def test_transactional_db_write_is_visible_within_test(transactional_db):
    transactional_db.execute(text(_CREATE_SCRATCH))
    transactional_db.execute(text("INSERT INTO scratch (value) VALUES ('leftover')"))
    transactional_db.flush()

    rows = transactional_db.execute(text("SELECT value FROM scratch")).fetchall()
    assert len(rows) == 1


def test_transactional_db_rolls_back_between_tests(transactional_db):
    transactional_db.execute(text(_CREATE_SCRATCH))

    rows = transactional_db.execute(text("SELECT value FROM scratch")).fetchall()
    assert rows == []


def test_committed_rows_are_visible_within_test(transactional_db):
    # Application code commits; the fixture must turn that into a SAVEPOINT
    # release so the outer rollback still discards the row.
    transactional_db.add(Company(name="committed-in-test"))
    transactional_db.commit()

    assert transactional_db.scalar(select(func.count(Company.id))) == 1


def test_committed_rows_are_rolled_back_between_tests(transactional_db):
    assert transactional_db.scalar(select(func.count(Company.id))) == 0


def test_in_memory_sqlite_uses_one_shared_connection(transactional_db):
    # TestClient runs the app in another thread; with :memory: SQLite that only
    # works if the pool hands every thread the same connection. The login and
    # worker API tests prove the app thread sees fixture rows end to end.
    assert isinstance(transactional_db.bind.engine.pool, StaticPool)


def test_rollback_after_failed_flush_keeps_earlier_rows(transactional_db):
    # A route that hits IntegrityError rolls back its session. That must undo
    # only its own work, not the test's whole outer transaction / fixtures.
    transactional_db.add(Company(name="kept"))
    transactional_db.commit()

    transactional_db.add(Company(name=None))
    with pytest.raises(IntegrityError):
        transactional_db.flush()
    transactional_db.rollback()

    assert transactional_db.scalars(select(Company.name)).all() == ["kept"]
