from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.tenancy import install_tenant_listeners


def _build_engine(url: str):
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url)

    kwargs = {"connect_args": {"check_same_thread": False}}
    if parsed.database in (None, "", ":memory:"):
        # An in-memory database exists per connection; share one connection
        # across threads (e.g. TestClient) or each thread sees an empty DB.
        kwargs["poolclass"] = StaticPool
    sqlite_engine = create_engine(url, **kwargs)

    # SQLAlchemy's pysqlite SAVEPOINT recipe: take transaction control away
    # from the sqlite3 driver (which defers BEGIN and skips it for DDL) and
    # emit BEGIN ourselves, so SAVEPOINT / ROLLBACK TO behave correctly.
    # Required for transactional_session()'s create_savepoint mode.
    @event.listens_for(sqlite_engine, "connect")
    def _disable_pysqlite_begin(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(sqlite_engine, "begin")
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    return sqlite_engine


engine = _build_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

install_tenant_listeners()


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transactional_session():
    """Yield a Session bound to a connection whose outer transaction is
    rolled back on exit, so writes never outlive the caller.

    The session runs in ``create_savepoint`` mode: its commit() releases a
    SAVEPOINT and its rollback() rolls back to one, so code under test may
    commit or roll back freely without touching the outer transaction.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
