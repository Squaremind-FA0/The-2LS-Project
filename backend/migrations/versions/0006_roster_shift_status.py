"""roster: shift notes, status, and a DB-level window check

Adds the columns the M5 brief's ``Shift`` model needs on top of the existing
G5 ``shifts`` table: ``notes`` (free text) and ``status`` (``scheduled`` /
``cancelled``, defaulting existing rows to ``scheduled``). Also adds the
``ends_at > starts_at`` CHECK that was previously only enforced in
application code.

No new index: ``ix_shifts_company_worker_window`` (``company_id``,
``worker_id``, ``starts_at``, ``ends_at``) already covers the brief's
``(company_id, worker_id, starts_at)`` as a left-prefix.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SHIFT_STATUSES = ("scheduled", "cancelled")


def upgrade() -> None:
    with op.batch_alter_table("shifts") as batch:
        batch.add_column(sa.Column("notes", sa.String(length=500), nullable=True))
        batch.add_column(
            sa.Column(
                "status",
                sa.Enum(*_SHIFT_STATUSES, name="shift_status", native_enum=False, length=20),
                server_default="scheduled",
                nullable=False,
            )
        )
        batch.create_check_constraint("ck_shifts_ends_after_starts", "ends_at > starts_at")


def downgrade() -> None:
    with op.batch_alter_table("shifts") as batch:
        batch.drop_constraint("ck_shifts_ends_after_starts", type_="check")
        batch.drop_column("status")
        batch.drop_column("notes")
