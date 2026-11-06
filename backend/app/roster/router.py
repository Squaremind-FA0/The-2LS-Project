from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import Role, User
from app.roster import service
from app.routers.dispatch import raise_http
from app.schemas.shifts import RosterShiftCreate, RosterShiftUpdate, ShiftResponse
from app.services.dispatch import DispatchError

router = APIRouter(prefix="/api/roster", tags=["roster"])

_admin = require_role(Role.dispatcher, Role.company_admin)


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


@router.get("", response_model=list[ShiftResponse])
def week_view(
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    worker_id: int | None = None,
    user: User = Depends(_admin),
    db: Session = Depends(get_tenant_db),
) -> list[ShiftResponse]:
    shifts = service.list_week(db, _naive_utc(from_), _naive_utc(to), worker_id=worker_id)
    return [ShiftResponse.model_validate(shift) for shift in shifts]


@router.post("/shifts", response_model=ShiftResponse, status_code=status.HTTP_201_CREATED)
def create_shift(
    payload: RosterShiftCreate, user: User = Depends(_admin), db: Session = Depends(get_tenant_db)
) -> ShiftResponse:
    try:
        shift = service.create_shift(
            db, payload.worker_id, payload.starts_at, payload.ends_at, payload.notes
        )
    except DispatchError as exc:
        raise_http(exc)
    return ShiftResponse.model_validate(shift)


@router.get("/shifts/{shift_id}", response_model=ShiftResponse)
def get_shift(
    shift_id: int, user: User = Depends(_admin), db: Session = Depends(get_tenant_db)
) -> ShiftResponse:
    try:
        shift = service.get_shift(db, shift_id)
    except DispatchError as exc:
        raise_http(exc)
    return ShiftResponse.model_validate(shift)


@router.patch("/shifts/{shift_id}", response_model=ShiftResponse)
def update_shift(
    shift_id: int,
    payload: RosterShiftUpdate,
    user: User = Depends(_admin),
    db: Session = Depends(get_tenant_db),
) -> ShiftResponse:
    try:
        shift = service.update_shift(db, shift_id, **payload.model_dump(exclude_unset=True))
    except DispatchError as exc:
        raise_http(exc)
    return ShiftResponse.model_validate(shift)


@router.delete("/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(
    shift_id: int, user: User = Depends(_admin), db: Session = Depends(get_tenant_db)
) -> None:
    try:
        service.delete_shift(db, shift_id)
    except DispatchError as exc:
        raise_http(exc)
