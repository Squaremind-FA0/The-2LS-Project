from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import ALL_ROLES, get_tenant_db, require_role
from app.models import User
from app.schemas.users import MeResponse, WorkerProfileOut

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=MeResponse)
def read_me(
    user: User = Depends(require_role(*ALL_ROLES)),
    db: Session = Depends(get_tenant_db),
) -> MeResponse:
    # Build the response here, while the tenant context is still set, so the
    # worker_profile / company lazy loads run scoped.
    profile = user.worker_profile
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        company_id=user.company_id,
        company_name=user.company.name,
        worker_profile=WorkerProfileOut.model_validate(profile) if profile else None,
    )
