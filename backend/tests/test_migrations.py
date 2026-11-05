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


G5_TABLES = {"shifts", "time_entries", "payruns", "paylines", "audit_logs"}


def test_0004_creates_roster_payroll_schema_and_downgrades_to_0003(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'g5.db'}"
    engine = create_engine(db_url)
    try:
        _alembic(db_url, "upgrade", "head")
        insp = inspect(engine)
        assert G5_TABLES <= set(insp.get_table_names())

        def index_cols(table):
            return {ix["name"]: tuple(ix["column_names"]) for ix in insp.get_indexes(table)}

        assert index_cols("shifts")["ix_shifts_company_worker_window"] == (
            "company_id", "worker_id", "starts_at", "ends_at",
        )
        entries = index_cols("time_entries")
        assert entries["ix_time_entries_company_job"] == ("company_id", "job_id")
        assert entries["ix_time_entries_company_worker"] == ("company_id", "worker_id")
        assert index_cols("payruns")["ix_payruns_company_status"] == ("company_id", "status")
        assert index_cols("paylines")["ix_paylines_company_payrun_worker"] == (
            "company_id", "payrun_id", "worker_id",
        )
        assert index_cols("audit_logs")["ix_audit_logs_company_entity"] == (
            "company_id", "entity_type", "entity_id",
        )

        def uniques(table):
            return {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}

        assert ("job_id",) in uniques("time_entries")
        assert ("payrun_id", "worker_id") in uniques("paylines")

        # Money is whole cents and whole minutes, never a float or Numeric.
        cols = {c["name"]: c["type"] for c in insp.get_columns("paylines")}
        for name in ("minutes_worked", "hourly_rate_cents", "amount_cents"):
            assert cols[name].python_type is int

        _alembic(db_url, "check")

        _alembic(db_url, "downgrade", "0003")
        engine.dispose()
        remaining = set(inspect(engine).get_table_names())
        assert not G5_TABLES & remaining
        assert G3_TABLES <= remaining

        _alembic(db_url, "upgrade", "head")
        engine.dispose()
        assert G5_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _sql(engine, *statements: str) -> None:
    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


def _rows(engine, query: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.exec_driver_sql(query).all()]


def test_0005_migrates_real_rows_both_ways(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'm3.db'}"
    engine = create_engine(db_url)
    try:
        _alembic(db_url, "upgrade", "0004")
        _sql(
            engine,
            "INSERT INTO companies (id, name, created_at) VALUES (1, 'Co', '2026-01-01 00:00:00')",
            "INSERT INTO users (id, company_id, email, password_hash, full_name, role, is_active,"
            " token_version, created_at) VALUES (7, 1, 'w@co.test', 'x', 'W', 'worker', 1, 0,"
            " '2026-01-01 00:00:00'), (8, 1, 'n@co.test', 'x', 'N', 'worker', 1, 0,"
            " '2026-01-01 00:00:00')",
            "INSERT INTO worker_profiles (id, user_id, company_id, trades, hourly_rate, active)"
            " VALUES (50, 7, 1, '[\"gas\"]', 19.99, 1), (51, 8, 1, '[]', 55.10, 0)",
            "INSERT INTO worker_locations (company_id, worker_id, lat, lng, updated_at)"
            " VALUES (1, 7, -33.5, 151.25, '2026-02-02 03:04:05')",
            "INSERT INTO jobs (id, company_id, trade, address, lat, lng, scheduled_for, status,"
            " offer_round, created_at, updated_at) VALUES"
            " (1, 1, 'gas', 'A', 1.0, 2.0, '2026-03-01 09:00:00', 'offering', 1,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00'),"
            " (2, 1, 'gas', 'B', 1.0, 2.0, '2026-03-02 09:00:00', 'draft', 0,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            "INSERT INTO job_offers (id, company_id, job_id, worker_id, status, expires_at,"
            " created_at, updated_at) VALUES (1, 1, 1, 7, 'pending', '2026-03-01 09:00:00',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        )

        _alembic(db_url, "upgrade", "head")
        engine.dispose()
        assert _rows(
            engine,
            "SELECT id, status, title, description, version, created_by FROM jobs ORDER BY id",
        ) == [(1, "dispatching", "gas", "", 1, None), (2, "draft", "gas", "", 1, None)]
        profiles = _rows(
            engine,
            "SELECT user_id, company_id, trades, hourly_rate_cents, active, is_available,"
            " last_lat, last_lng, last_seen_at FROM worker_profiles ORDER BY user_id",
        )
        assert [row[:2] + row[3:8] for row in profiles] == [
            (7, 1, 1999, 1, 1, -33.5, 151.25),
            (8, 1, 5510, 0, 1, None, None),
        ]
        assert profiles[0][8].startswith("2026-02-02 03:04:05")
        assert profiles[1][8] is None

        insp = inspect(engine)
        assert insp.get_pk_constraint("worker_profiles")["constrained_columns"] == ["user_id"]
        assert "ix_worker_profiles_company_id" in {
            ix["name"] for ix in insp.get_indexes("worker_profiles")
        }
        assert "job_events" in insp.get_table_names()
        scheduled = {c["name"]: c for c in insp.get_columns("jobs")}["scheduled_for"]
        assert scheduled["nullable"] is True
        # The rebuild of jobs must not lose its dependants.
        assert _rows(engine, "SELECT job_id, worker_id FROM job_offers") == [(1, 7)]
        _alembic(db_url, "check")

        # Rows only the new schema allows must survive the way back.
        _sql(
            engine,
            "UPDATE jobs SET scheduled_for = NULL WHERE id = 2",
            "INSERT INTO job_events (company_id, job_id, from_status, to_status, at)"
            " VALUES (1, 1, 'draft', 'dispatching', '2026-03-01 00:00:00')",
        )
        _alembic(db_url, "downgrade", "0004")
        engine.dispose()
        assert _rows(engine, "SELECT id, status FROM jobs ORDER BY id") == [
            (1, "offering"), (2, "draft"),
        ]
        insp = inspect(engine)
        assert "job_events" not in insp.get_table_names()
        [(backfilled,)] = _rows(engine, "SELECT scheduled_for FROM jobs WHERE id = 2")
        assert backfilled.startswith("2026-01-01 00:00:00")
        assert insp.get_pk_constraint("worker_profiles")["constrained_columns"] == ["id"]
        assert _rows(
            engine, "SELECT user_id, trades, active FROM worker_profiles ORDER BY user_id"
        ) == [(7, '["gas"]', 1), (8, "[]", 0)]
        rates = _rows(engine, "SELECT hourly_rate FROM worker_profiles ORDER BY user_id")
        assert [float(rate) for (rate,) in rates] == [19.99, 55.10]
        assert ("user_id",) in {
            tuple(u["column_names"]) for u in insp.get_unique_constraints("worker_profiles")
        }
        assert {c["name"] for c in insp.get_columns("jobs")}.isdisjoint(
            {"title", "description", "created_by", "version"}
        )
        assert _rows(engine, "SELECT job_id, worker_id FROM job_offers") == [(1, 7)]

        _alembic(db_url, "upgrade", "head")
        _alembic(db_url, "check")
    finally:
        engine.dispose()
