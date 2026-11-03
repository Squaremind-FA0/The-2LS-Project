from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import Role, User
from app.schemas.users import WorkerCreateRequest, WorkerOut
from app.services import workers as service

router = APIRouter(prefix="/api/workers", tags=["workers"])

_admin_only = require_role(Role.company_admin)
_staff = require_role(Role.company_admin, Role.dispatcher)


def _out(user: User) -> WorkerOut:
    return WorkerOut.model_validate(user)


@router.post(
    "", response_model=WorkerOut, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_admin_only)],
)
def create_worker(
    payload: WorkerCreateRequest, db: Session = Depends(get_tenant_db)
) -> WorkerOut:
    try:
        user = service.create_worker(db, payload)
    except service.EmailAlreadyRegistered:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered") from None
    return _out(user)


@router.get("", response_model=list[WorkerOut], dependencies=[Depends(_staff)])
def list_workers(db: Session = Depends(get_tenant_db)) -> list[WorkerOut]:
    return [_out(user) for user in service.list_workers(db)]


@router.get("/{user_id}", response_model=WorkerOut, dependencies=[Depends(_staff)])
def get_worker(user_id: int, db: Session = Depends(get_tenant_db)) -> WorkerOut:
    # Another company's worker is filtered out by the tenant listener, so it
    # is indistinguishable from a nonexistent id: 404, not 403.
    user = service.get_worker(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Worker not found")
    return _out(user)
