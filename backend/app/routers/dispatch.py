from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import get_tenant_db, require_role
from app.models import Role, User
from app.schemas.jobs import (
    JobOfferOut,
    PendingOfferOut,
    SweepResult,
    WorkerLocationOut,
    WorkerLocationUpdate,
)
from app.services import dispatch as service

router = APIRouter(tags=["dispatch"])

_worker = require_role(Role.worker)
_admin = require_role(Role.company_admin)

_STATUS_FOR = {
    service.NotFound: status.HTTP_404_NOT_FOUND,
    service.InvalidTransition: status.HTTP_400_BAD_REQUEST,
    service.Conflict: status.HTTP_409_CONFLICT,
    service.Gone: status.HTTP_410_GONE,
}


def raise_http(exc: service.DispatchError) -> NoReturn:
    raise HTTPException(_STATUS_FOR[type(exc)], detail=exc.detail) from None


@router.put("/api/workers/me/location", response_model=WorkerLocationOut)
def update_my_location(
    payload: WorkerLocationUpdate,
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> WorkerLocationOut:
    location = service.upsert_location(db, user.id, payload.lat, payload.lng)
    return WorkerLocationOut.model_validate(location)


@router.get("/api/offers/pending", response_model=list[PendingOfferOut])
def pending_offers(
    limit: int = Query(200, ge=1, le=200),
    user: User = Depends(_worker),
    db: Session = Depends(get_tenant_db),
) -> list[PendingOfferOut]:
    """The caller's open offers. Call after every (re)connect's auth.ok."""
    return service.list_pending_offers(db, user.id, limit=limit)


@router.post("/api/offers/{offer_id}/accept", response_model=JobOfferOut)
def accept_offer(
    offer_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> JobOfferOut:
    try:
        offer = service.accept_offer(db, offer_id, user.id)
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOfferOut.model_validate(offer)


@router.post("/api/offers/{offer_id}/decline", response_model=JobOfferOut)
def decline_offer(
    offer_id: int, user: User = Depends(_worker), db: Session = Depends(get_tenant_db)
) -> JobOfferOut:
    try:
        offer = service.decline_offer(db, offer_id, user.id)
    except service.DispatchError as exc:
        raise_http(exc)
    return JobOfferOut.model_validate(offer)


@router.post("/api/dispatch/sweep", response_model=SweepResult)
def sweep_offers(
    user: User = Depends(_admin), db: Session = Depends(get_tenant_db)
) -> SweepResult:
    """One bounded expiry + re-offer pass for the caller's company. Meant to
    be called by cron or an admin; there is no background loop."""
    summary = service.sweep(db, user.company_id)
    return SweepResult(
        expired_count=summary.expired_count,
        rejobs_count=summary.rejobs_count,
        unfilled_count=summary.unfilled_count,
    )
