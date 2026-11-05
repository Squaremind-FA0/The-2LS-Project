from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import Role, User
from app.routers.dispatch import raise_http
from app.schemas.shifts import TimeEntryResponse
from app.services import time_entries as service
from app.services.dispatch import DispatchError

router = APIRouter(prefix="/api/jobs", tags=["time-entries"])

_worker = require_role(Role.worker)


@router.post("/{job_id}/clock-in", response_model=TimeEntryResponse)
def clock_in(
    job_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> TimeEntryResponse:
    try:
        entry = service.clock_in(db, job_id, user.id)
    except DispatchError as exc:
        raise_http(exc)
    return TimeEntryResponse.model_validate(entry)


@router.post("/{job_id}/clock-out", response_model=TimeEntryResponse)
def clock_out(
    job_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> TimeEntryResponse:
    try:
        entry = service.clock_out(db, job_id, user.id)
    except DispatchError as exc:
        raise_http(exc)
    return TimeEntryResponse.model_validate(entry)
