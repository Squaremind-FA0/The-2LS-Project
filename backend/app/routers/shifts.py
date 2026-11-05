from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import Role, User
from app.routers.dispatch import raise_http
from app.schemas.shifts import ShiftCreate, ShiftResponse
from app.services import shifts as service
from app.services.dispatch import DispatchError

router = APIRouter(prefix="/api/shifts", tags=["shifts"])

_worker = require_role(Role.worker)


@router.post("", response_model=ShiftResponse, status_code=status.HTTP_201_CREATED)
def create_shift(
    payload: ShiftCreate, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> ShiftResponse:
    try:
        shift = service.create_shift(db, user.id, payload.starts_at, payload.ends_at)
    except DispatchError as exc:
        raise_http(exc)
    return ShiftResponse.model_validate(shift)


@router.get("", response_model=list[ShiftResponse])
def list_shifts(
    worker_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> list[ShiftResponse]:
    if worker_id is not None and worker_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Workers can only list own shifts")
    shifts = service.list_shifts(db, user.id, limit=limit, offset=offset)
    return [ShiftResponse.model_validate(shift) for shift in shifts]
