"""jobs and dispatch: jobs, worker_locations, job_offers

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_STATUSES = (
    "draft", "offering", "assigned", "in_progress", "completed", "cancelled", "unfilled",
)
_OFFER_STATUSES = ("pending", "accepted", "declined", "expired")
_ONE_ACCEPTED = sa.text("status = 'accepted'")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade", sa.String(length=50), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_JOB_STATUSES, name="job_status", native_enum=False, length=20),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("assigned_worker_id", sa.Integer(), nullable=True),
        sa.Column("offer_round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_worker_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"], unique=False)
    op.create_index("ix_jobs_company_status", "jobs", ["company_id", "status"], unique=False)
    op.create_index(
        "ix_jobs_company_scheduled_for", "jobs", ["company_id", "scheduled_for"], unique=False
    )

    op.create_table(
        "worker_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "worker_id", name="uq_worker_locations_company_worker"
        ),
    )
    op.create_index(
        "ix_worker_locations_company_id", "worker_locations", ["company_id"], unique=False
    )
    op.create_index(
        "ix_worker_locations_worker_id", "worker_locations", ["worker_id"], unique=False
    )
    op.create_index(
        "ix_worker_locations_lat_lng", "worker_locations", ["lat", "lng"], unique=False
    )

    op.create_table(
        "job_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_OFFER_STATUSES, name="offer_status", native_enum=False, length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "worker_id", name="uq_job_offers_job_worker"),
    )
    op.create_index("ix_job_offers_company_id", "job_offers", ["company_id"], unique=False)
    op.create_index("ix_job_offers_job_id", "job_offers", ["job_id"], unique=False)
    op.create_index(
        "ix_job_offers_worker_status", "job_offers", ["worker_id", "status"], unique=False
    )
    op.create_index(
        "ix_job_offers_status_expires_at", "job_offers", ["status", "expires_at"], unique=False
    )
    op.create_index(
        "uq_job_offers_one_accepted_per_job",
        "job_offers",
        ["job_id"],
        unique=True,
        sqlite_where=_ONE_ACCEPTED,
        postgresql_where=_ONE_ACCEPTED,
    )


def downgrade() -> None:
    op.drop_index("uq_job_offers_one_accepted_per_job", table_name="job_offers")
    op.drop_index("ix_job_offers_status_expires_at", table_name="job_offers")
    op.drop_index("ix_job_offers_worker_status", table_name="job_offers")
    op.drop_index("ix_job_offers_job_id", table_name="job_offers")
    op.drop_index("ix_job_offers_company_id", table_name="job_offers")
    op.drop_table("job_offers")
    op.drop_index("ix_worker_locations_lat_lng", table_name="worker_locations")
    op.drop_index("ix_worker_locations_worker_id", table_name="worker_locations")
    op.drop_index("ix_worker_locations_company_id", table_name="worker_locations")
    op.drop_table("worker_locations")
    op.drop_index("ix_jobs_company_scheduled_for", table_name="jobs")
    op.drop_index("ix_jobs_company_status", table_name="jobs")
    op.drop_index("ix_jobs_company_id", table_name="jobs")
    op.drop_table("jobs")
