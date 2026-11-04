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


G3_TABLES = {"jobs", "worker_locations", "job_offers"}


def test_0003_creates_dispatch_schema_and_downgrades_to_0002(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'g3.db'}"
    engine = create_engine(db_url)
    try:
        _alembic(db_url, "upgrade", "head")
        insp = inspect(engine)
        assert G3_TABLES <= set(insp.get_table_names())

        def index_cols(table):
            return {ix["name"]: (tuple(ix["column_names"]), ix["unique"])
                    for ix in insp.get_indexes(table)}

        jobs = index_cols("jobs")
        assert jobs["ix_jobs_company_status"] == (("company_id", "status"), False)
        assert jobs["ix_jobs_company_scheduled_for"] == (("company_id", "scheduled_for"), False)
        locations = index_cols("worker_locations")
        assert locations["ix_worker_locations_worker_id"] == (("worker_id",), False)
        assert locations["ix_worker_locations_lat_lng"] == (("lat", "lng"), False)
        offers = index_cols("job_offers")
        assert offers["ix_job_offers_job_id"] == (("job_id",), False)
        assert offers["ix_job_offers_worker_status"] == (("worker_id", "status"), False)
        assert offers["ix_job_offers_status_expires_at"] == (("status", "expires_at"), False)
        # DB-level race guard: at most one accepted offer per job.
        assert offers["uq_job_offers_one_accepted_per_job"] == (("job_id",), True)

        uniques = {
            t: {tuple(u["column_names"]) for u in insp.get_unique_constraints(t)}
            for t in ("worker_locations", "job_offers")
        }
        assert ("company_id", "worker_id") in uniques["worker_locations"]
        assert ("job_id", "worker_id") in uniques["job_offers"]

        _alembic(db_url, "check")

        _alembic(db_url, "downgrade", "0002")
        engine.dispose()
        remaining = set(inspect(engine).get_table_names())
        assert not G3_TABLES & remaining
        assert TABLES <= remaining

        _alembic(db_url, "upgrade", "head")
        engine.dispose()
        assert G3_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
