from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import ALL_ROLES, get_tenant_db, require_role
from app.models import Role, User
from app.routers.dispatch import raise_http
from app.schemas.jobs import (
    JobOut,
    WorkerAvailabilityUpdate,
    WorkerLocationOut,
    WorkerLocationUpdate,
)
from app.schemas.users import MeResponse, WorkerProfileOut
from app.services import dispatch, time_entries, workers

router = APIRouter(prefix="/api", tags=["me"])

_worker = require_role(Role.worker)


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


@router.patch("/me/location", response_model=WorkerLocationOut)
def update_my_location(
    payload: WorkerLocationUpdate,
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> WorkerLocationOut:
    location = dispatch.upsert_location(db, user.id, payload.lat, payload.lng)
    return WorkerLocationOut.model_validate(location)


@router.patch("/me/availability", response_model=WorkerProfileOut)
def update_my_availability(
    payload: WorkerAvailabilityUpdate,
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> WorkerProfileOut:
    profile = workers.set_availability(db, user.id, payload.is_available)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Worker profile not found")
    return WorkerProfileOut.model_validate(profile)


@router.get("/me/jobs", response_model=list[JobOut])
def list_my_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> list[JobOut]:
    jobs = dispatch.list_worker_jobs(db, user.id, limit=limit, offset=offset)
    return [JobOut.model_validate(job) for job in jobs]


def _own_job(db: Session, job_id: int, worker_id: int):
    """Another worker's job looks missing, like another worker's offer does."""
    job = dispatch.get_job(db, job_id)
    if job.assigned_worker_id != worker_id:
        raise dispatch.NotFound("Job not found")
    return job


@router.post("/me/jobs/{job_id}/start", response_model=JobOut)
def start_my_job(
    job_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> JobOut:
    # clock_in, not a bare transition: the TimeEntry is what payroll pays from.
    try:
        _own_job(db, job_id, user.id)
        time_entries.clock_in(db, job_id, user.id)
        return JobOut.model_validate(dispatch.get_job(db, job_id))
    except dispatch.DispatchError as exc:
        raise_http(exc)


@router.post("/me/jobs/{job_id}/complete", response_model=JobOut)
def complete_my_job(
    job_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> JobOut:
    try:
        _own_job(db, job_id, user.id)
        time_entries.clock_out(db, job_id, user.id)
        return JobOut.model_validate(dispatch.get_job(db, job_id))
    except dispatch.DispatchError as exc:
        raise_http(exc)
