from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import PayRunStatus, Role, User
from app.routers.dispatch import raise_http
from app.schemas.payruns import PayLineResponse, PayRunCreate, PayRunResponse, PayRunUpdate
from app.services import payruns as service
from app.services.dispatch import DispatchError

router = APIRouter(prefix="/api/payruns", tags=["payruns"])

_admin = require_role(Role.company_admin)
_worker = require_role(Role.worker)


@router.get("", response_model=list[PayRunResponse], dependencies=[Depends(_admin)])
def list_payruns(
    status_filter: PayRunStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_tenant_db),
) -> list[PayRunResponse]:
    payruns = service.list_payruns(db, status_filter, limit=limit, offset=offset)
    return [PayRunResponse.model_validate(payrun) for payrun in payruns]


@router.post("", response_model=PayRunResponse, status_code=status.HTTP_201_CREATED)
def create_payrun(
    payload: PayRunCreate, user: User = Depends(_admin), db: Session = Depends(get_tenant_db)
) -> PayRunResponse:
    try:
        payrun = service.create_payrun(
            db, user.company_id, payload.period_start, payload.period_end
        )
    except DispatchError as exc:
        raise_http(exc)
    return PayRunResponse.model_validate(payrun)


@router.patch("/{payrun_id}", response_model=PayRunResponse)
def update_payrun_status(
    payrun_id: int,
    payload: PayRunUpdate,
    user: User = Depends(_admin),
    db: Session = Depends(get_tenant_db),
) -> PayRunResponse:
    try:
        payrun = service.transition_payrun(db, payrun_id, PayRunStatus(payload.status), user.id)
    except DispatchError as exc:
        raise_http(exc)
    return PayRunResponse.model_validate(payrun)


@router.get("/{payrun_id}/paylines", response_model=list[PayLineResponse])
def list_paylines(
    payrun_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> list[PayLineResponse]:
    try:
        lines = service.list_own_paylines(db, payrun_id, user.id)
    except DispatchError as exc:
        raise_http(exc)
    return [PayLineResponse.model_validate(line) for line in lines]


@router.get("/{payrun_id}/export", dependencies=[Depends(_admin)])
def export_payrun(
    payrun_id: int,
    format: Literal["csv"] = "csv",
    db: Session = Depends(get_tenant_db),
) -> StreamingResponse:
    try:
        # Read everything now: the tenant session is gone by the time it streams.
        rows = service.export_rows(db, payrun_id)
    except DispatchError as exc:
        raise_http(exc)
    return StreamingResponse(
        service.stream_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="payrun-{payrun_id}.csv"'},
    )
