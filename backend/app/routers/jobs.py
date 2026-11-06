from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import JobStatus, Role, User
from app.routers.dispatch import raise_http
from app.schemas.jobs import JobCancelRequest, JobCreateRequest, JobOut, JobUpdateRequest
from app.services import dispatch as service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_staff = require_role(Role.dispatcher, Role.company_admin)


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreateRequest,
    user: User = Depends(_staff),
    db: Session = Depends(get_tenant_db),
) -> JobOut:
    return JobOut.model_validate(service.create_job(db, payload, actor_id=user.id))


@router.get("", response_model=list[JobOut], dependencies=[Depends(_staff)])
def list_jobs(
    status_filter: JobStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_tenant_db),
) -> list[JobOut]:
    jobs = service.list_jobs(db, status_filter, limit=limit, offset=offset)
    return [JobOut.model_validate(job) for job in jobs]


@router.get("/{job_id}", response_model=JobOut, dependencies=[Depends(_staff)])
def get_job(job_id: int, db: Session = Depends(get_tenant_db)) -> JobOut:
    try:
        return JobOut.model_validate(service.get_job(db, job_id))
    except service.DispatchError as exc:
        raise_http(exc)


@router.patch("/{job_id}", response_model=JobOut)
def update_job(
    job_id: int,
    payload: JobUpdateRequest,
    user: User = Depends(_staff),
    db: Session = Depends(get_tenant_db),
) -> JobOut:
    try:
        if payload.status is not None:
            job = service.transition_job(
                db,
                job_id,
                JobStatus(payload.status),
                actor_id=user.id,
                expected_version=payload.version,
            )
        else:
            job = service.update_job_details(db, job_id, payload.version, payload.details())
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOut.model_validate(job)


@router.post("/{job_id}/dispatch", response_model=JobOut)
def dispatch_job(
    job_id: int,
    user: User = Depends(_staff),
    db: Session = Depends(get_tenant_db),
) -> JobOut:
    """Alias of PATCH /api/jobs/{job_id} {"status": "dispatching"}, with no
    version check (this endpoint's contract has none). Moves a draft job
    straight to dispatching and offers it to the nearest ring."""
    try:
        job = service.transition_job(db, job_id, JobStatus.dispatching, actor_id=user.id)
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOut.model_validate(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    job_id: int,
    payload: JobCancelRequest,
    user: User = Depends(_staff),
    db: Session = Depends(get_tenant_db),
) -> JobOut:
    try:
        job = service.transition_job(
            db, job_id, JobStatus.cancelled, actor_id=user.id, expected_version=payload.version
        )
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOut.model_validate(job)


@router.delete(
    "/{job_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_staff)]
)
def delete_job(job_id: int, db: Session = Depends(get_tenant_db)) -> Response:
    try:
        service.delete_job(db, job_id)
    except service.DispatchError as exc:
        raise_http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
