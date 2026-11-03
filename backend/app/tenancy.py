"""Company tenancy.

Every tenant-owned table inherits ``TenantScoped``, which declares a
non-null, indexed ``company_id`` foreign key to ``companies.id``.
"""

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class TenantScoped:
    @declared_attr
    def company_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
        )
