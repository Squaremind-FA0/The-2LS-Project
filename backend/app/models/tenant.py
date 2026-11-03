import enum

from app.tenancy import TenantScoped


class Role(str, enum.Enum):
    company_admin = "company_admin"
    dispatcher = "dispatcher"
    worker = "worker"


__all__ = ["Role", "TenantScoped"]
