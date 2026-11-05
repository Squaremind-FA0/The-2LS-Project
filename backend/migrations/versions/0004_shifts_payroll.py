"""roster and payroll: shifts, time_entries, payruns, paylines, audit_logs

G4 adds no migration, so this revision follows 0003 whichever of G4 and G5
merges first.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAYRUN_STATUSES = ("draft", "approved", "paid")


def upgrade() -> None:
    op.create_table(
        "shifts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shifts_company_id", "shifts", ["company_id"], unique=False)
    op.create_index(
        "ix_shifts_company_worker_window",
        "shifts",
        ["company_id", "worker_id", "starts_at", "ends_at"],
        unique=False,
    )

    op.create_table(
        "time_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_time_entries_job"),
    )
    op.create_index("ix_time_entries_company_id", "time_entries", ["company_id"], unique=False)
    op.create_index(
        "ix_time_entries_company_job", "time_entries", ["company_id", "job_id"], unique=False
    )
    op.create_index(
        "ix_time_entries_company_worker", "time_entries", ["company_id", "worker_id"], unique=False
    )
    op.create_index(
        "ix_time_entries_company_ended_at", "time_entries", ["company_id", "ended_at"], unique=False
    )

    op.create_table(
        "payruns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_PAYRUN_STATUSES, name="payrun_status", native_enum=False, length=20),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payruns_company_id", "payruns", ["company_id"], unique=False)
    op.create_index("ix_payruns_company_status", "payruns", ["company_id", "status"], unique=False)

    op.create_table(
        "paylines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payrun_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("minutes_worked", sa.Integer(), nullable=False),
        sa.Column("hourly_rate_cents", sa.BigInteger(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payrun_id"], ["payruns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payrun_id", "worker_id", name="uq_paylines_payrun_worker"),
    )
    op.create_index("ix_paylines_company_id", "paylines", ["company_id"], unique=False)
    op.create_index(
        "ix_paylines_company_payrun_worker",
        "paylines",
        ["company_id", "payrun_id", "worker_id"],
        unique=False,
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("old_values", sa.JSON(), nullable=True),
        sa.Column("new_values", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_company_id", "audit_logs", ["company_id"], unique=False)
    op.create_index(
        "ix_audit_logs_company_entity",
        "audit_logs",
        ["company_id", "entity_type", "entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_company_entity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_company_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_paylines_company_payrun_worker", table_name="paylines")
    op.drop_index("ix_paylines_company_id", table_name="paylines")
    op.drop_table("paylines")
    op.drop_index("ix_payruns_company_status", table_name="payruns")
    op.drop_index("ix_payruns_company_id", table_name="payruns")
    op.drop_table("payruns")
    op.drop_index("ix_time_entries_company_ended_at", table_name="time_entries")
    op.drop_index("ix_time_entries_company_worker", table_name="time_entries")
    op.drop_index("ix_time_entries_company_job", table_name="time_entries")
    op.drop_index("ix_time_entries_company_id", table_name="time_entries")
    op.drop_table("time_entries")
    op.drop_index("ix_shifts_company_worker_window", table_name="shifts")
    op.drop_index("ix_shifts_company_id", table_name="shifts")
    op.drop_table("shifts")
