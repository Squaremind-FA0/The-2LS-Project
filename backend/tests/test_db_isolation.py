from sqlalchemy import text

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
