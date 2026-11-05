"""core domain: job lifecycle columns, job_events, worker_profiles reshape

* jobs: title, description, created_by, version; scheduled_for becomes nullable;
  status value ``offering`` is renamed ``dispatching``.
* job_events: the audit trail written by app/domain/job_state.py.
* worker_profiles: user_id becomes the primary key, the rate is stored as whole
  cents, plus is_available and last_lat/last_lng/last_seen_at (backfilled from
  worker_locations).

worker_profiles is rebuilt by hand instead of with batch_alter_table: its unique
constraint on user_id is unnamed, so there is no name to drop it by. Nothing has
a foreign key to worker_profiles, so swapping the table in is safe.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COMPANY_INDEX = "ix_worker_profiles_company_id"


def _common_profile_columns() -> list[sa.Column]:
    return [
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("trades", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    ]


def _swap_in_profiles() -> None:
    op.drop_index(_COMPANY_INDEX, table_name="worker_profiles")
    op.drop_table("worker_profiles")
    op.rename_table("worker_profiles_new", "worker_profiles")
    op.create_index(_COMPANY_INDEX, "worker_profiles", ["company_id"], unique=False)


def upgrade() -> None:
    op.execute("UPDATE jobs SET status = 'dispatching' WHERE status = 'offering'")
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("title", sa.String(length=200), server_default="", nullable=False)
        )
        batch.add_column(sa.Column("description", sa.Text(), server_default="", nullable=False))
        batch.add_column(sa.Column("created_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch.alter_column("scheduled_for", existing_type=sa.DateTime(), nullable=True)
        batch.create_foreign_key(
            "fk_jobs_created_by_users", "users", ["created_by"], ["id"], ondelete="SET NULL"
        )
    op.execute("UPDATE jobs SET title = trade")

    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=False),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_events_company_id", "job_events", ["company_id"], unique=False)
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"], unique=False)

    op.create_table(
        "worker_profiles_new",
        *_common_profile_columns(),
        sa.Column("hourly_rate_cents", sa.BigInteger(), nullable=False),
        sa.Column("is_available", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_lat", sa.Float(), nullable=True),
        sa.Column("last_lng", sa.Float(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", name="pk_worker_profiles"),
    )
    op.execute(
        """
        INSERT INTO worker_profiles_new
            (user_id, company_id, trades, active, hourly_rate_cents,
             last_lat, last_lng, last_seen_at)
        SELECT p.user_id, p.company_id, p.trades, p.active,
               CAST(ROUND(p.hourly_rate * 100) AS BIGINT),
               l.lat, l.lng, l.updated_at
        FROM worker_profiles p
        LEFT JOIN worker_locations l
          ON l.worker_id = p.user_id AND l.company_id = p.company_id
        """
    )
    _swap_in_profiles()


def downgrade() -> None:
    op.create_table(
        "worker_profiles_new",
        sa.Column("id", sa.Integer(), nullable=False),
        *_common_profile_columns(),
        sa.Column("hourly_rate", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.execute(
        """
        INSERT INTO worker_profiles_new (user_id, company_id, trades, active, hourly_rate)
        SELECT user_id, company_id, trades, active, hourly_rate_cents / 100.0
        FROM worker_profiles
        """
    )
    _swap_in_profiles()

    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_index("ix_job_events_company_id", table_name="job_events")
    op.drop_table("job_events")

    # scheduled_for was NOT NULL before this revision.
    op.execute("UPDATE jobs SET scheduled_for = created_at WHERE scheduled_for IS NULL")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("fk_jobs_created_by_users", type_="foreignkey")
        batch.alter_column("scheduled_for", existing_type=sa.DateTime(), nullable=False)
        batch.drop_column("version")
        batch.drop_column("created_by")
        batch.drop_column("description")
        batch.drop_column("title")
    op.execute("UPDATE jobs SET status = 'offering' WHERE status = 'dispatching'")
