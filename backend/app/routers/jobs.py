from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import JobStatus, Role
from app.routers.dispatch import raise_http
from app.schemas.jobs import JobCreateRequest, JobOut, JobStatusUpdate
from app.services import dispatch as service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_dispatcher = require_role(Role.dispatcher)
_staff = require_role(Role.dispatcher, Role.company_admin)


@router.post(
    "", response_model=JobOut, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_dispatcher)],
)
def create_job(payload: JobCreateRequest, db: Session = Depends(get_tenant_db)) -> JobOut:
    return JobOut.model_validate(service.create_job(db, payload))


@router.get("", response_model=list[JobOut], dependencies=[Depends(_staff)])
def list_jobs(
    status_filter: JobStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_tenant_db),
) -> list[JobOut]:
    jobs = service.list_jobs(db, status_filter, limit=limit, offset=offset)
    return [JobOut.model_validate(job) for job in jobs]


@router.patch("/{job_id}", response_model=JobOut, dependencies=[Depends(_dispatcher)])
def update_job_status(
    job_id: int, payload: JobStatusUpdate, db: Session = Depends(get_tenant_db)
) -> JobOut:
    try:
        job = service.transition_job(db, job_id, JobStatus(payload.status))
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOut.model_validate(job)
