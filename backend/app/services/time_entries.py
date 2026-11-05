"""Worker clock-in/out: records time worked and moves the job's status.

These are separate from ``dispatch.transition_job`` (PRP task 19) because they
do more than change status: they own the job's TimeEntry and check who is
calling. Races are decided by conditional UPDATEs and ``uq_time_entries_job``,
not by the read-then-check code, which only exists to give precise errors.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, Shift, TimeEntry
from app.models.job import utcnow
from app.services.dispatch import Conflict, NotFound, conditional_update


def _entry_for(db: Session, job_id: int) -> TimeEntry | None:
    return db.scalars(select(TimeEntry).where(TimeEntry.job_id == job_id)).one_or_none()


def _is_on_shift(db: Session, worker_id: int, now: datetime) -> bool:
    return (
        db.scalar(
            select(Shift.id)
            .where(Shift.worker_id == worker_id, Shift.starts_at <= now, Shift.ends_at > now)
            .limit(1)
        )
        is not None
    )


def clock_in(db: Session, job_id: int, worker_id: int, *, now: datetime | None = None) -> TimeEntry:
    now = now or utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Job not found")
    if job.assigned_worker_id != worker_id:
        raise Conflict("Job not assigned to this worker")
    if _entry_for(db, job_id) is not None:
        raise Conflict("Job already has a TimeEntry")
    if job.status is not JobStatus.assigned:
        raise Conflict(f"Cannot clock in to a job that is {job.status.value}")
    if not _is_on_shift(db, worker_id, now):
        raise Conflict("Worker is not on shift")

    claimed = conditional_update(
        db,
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.assigned,
            Job.assigned_worker_id == worker_id,
        )
        .values(status=JobStatus.in_progress),
    )
    if claimed != 1:
        db.rollback()
        raise Conflict("Job status changed concurrently")
    entry = TimeEntry(job_id=job_id, worker_id=worker_id, started_at=now)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # uq_time_entries_job: the database's last word.
        db.rollback()
        raise Conflict("Job already has a TimeEntry") from None
    return entry


def clock_out(
    db: Session, job_id: int, worker_id: int, *, now: datetime | None = None
) -> TimeEntry:
    """End the entry and complete the job.

    The shift is not re-checked: the work was already under way. If a
    dispatcher moved the job on mid-work (say, cancelled it), the entry still
    closes, so the hours get paid, and the job keeps the dispatcher's status.
    """
    now = now or utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Job or TimeEntry not found")
    if job.assigned_worker_id != worker_id:
        raise Conflict("Job not assigned to this worker")
    entry = _entry_for(db, job_id)
    if entry is None:
        raise NotFound("Job or TimeEntry not found")
    if entry.ended_at is not None:
        raise Conflict("TimeEntry already ended")

    closed = conditional_update(
        db,
        update(TimeEntry)
        .where(TimeEntry.id == entry.id, TimeEntry.ended_at.is_(None))
        .values(ended_at=now),
    )
    if closed != 1:
        db.rollback()
        raise Conflict("TimeEntry already ended")
    conditional_update(
        db,
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.in_progress)
        .values(status=JobStatus.completed),
    )
    db.commit()
    db.refresh(entry)
    return entry
