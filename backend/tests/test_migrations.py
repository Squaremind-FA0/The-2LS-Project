"""Alembic round trip against a scratch SQLite file.

migrations/env.py always takes its URL from app.config.settings, which this
test process has already pinned to :memory:, so alembic runs in a subprocess
with its own DATABASE_URL.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parents[1]
TABLES = {"companies", "users", "worker_profiles"}


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_upgrade_creates_tenancy_schema_and_downgrade_removes_it(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'migrate.db'}"
    engine = create_engine(db_url)
    try:
        _alembic(db_url, "upgrade", "head")
        insp = inspect(engine)
        assert TABLES <= set(insp.get_table_names())
        assert "ix_users_company_id" in {ix["name"] for ix in insp.get_indexes("users")}
        assert "ix_worker_profiles_company_id" in {
            ix["name"] for ix in insp.get_indexes("worker_profiles")
        }
        email_ix = {ix["name"]: ix for ix in insp.get_indexes("users")}["ix_users_email"]
        assert email_ix["unique"]

        # The hand-written migration matches the ORM models (no autogenerate drift).
        _alembic(db_url, "check")

        _alembic(db_url, "downgrade", "base")
        engine.dispose()
        assert not TABLES & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_revision_0002_follows_baseline():
    source = (BACKEND / "migrations" / "versions" / "0002_auth_tenancy.py").read_text()
    assert 'revision: str = "0002"' in source
    assert 'down_revision: Union[str, None] = "0001"' in source
