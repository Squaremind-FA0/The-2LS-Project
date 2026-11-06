"""The job lifecycle: the only code that writes ``Job.status`` or ``Job.version``.

Writes are conditional UPDATEs checked by rowcount, like the rest of the dispatch
code, so the database decides races rather than the read that preceded them.
Nothing here commits: the caller owns the transaction, which is what makes a
status change and its JobEvent atomic.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, update
from sqlalchemy.orm import Session, object_session

from app.models.job import Job, JobStatus
from app.models.job_event import JobEvent

S = JobStatus

TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    S.draft: frozenset({S.dispatching, S.cancelled}),
    S.dispatching: frozenset({S.assigned, S.unfilled, S.cancelled, S.dispatching}),
    S.assigned: frozenset({S.in_progress, S.cancelled}),
    S.in_progress: frozenset({S.completed, S.cancelled}),
    S.completed: frozenset(),
    S.cancelled: frozenset(),
    S.unfilled: frozenset(),
}
TERMINAL: frozenset[JobStatus] = frozenset(s for s, targets in TRANSITIONS.items() if not targets)

_LIFECYCLE_COLUMNS = frozenset(
    {"id", "company_id", "status", "version", "assigned_worker_id", "offer_round", "created_by"}
)


class IllegalTransition(Exception):
    """The table forbids this move. Nothing was written."""

    def __init__(self, current: JobStatus, target: JobStatus):
        super().__init__(f"Cannot change job status from {current.value} to {target.value}")


class StaleJob(Exception):
    """The row no longer matches what the caller read. Nothing was written."""


def _session(job: Job) -> Session:
    session = object_session(job)
    if session is None:
        raise ValueError("job is not attached to a session")
    return session


def _apply(session: Session, job: Job, conditions: Iterable[ColumnElement], values: dict) -> None:
    updated = session.execute(
        update(Job)
        .where(Job.id == job.id, *conditions)
        .values(version=Job.version + 1, **values)
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        raise StaleJob(f"Job {job.id} changed since it was read")


def transition(
    job: Job,
    to: JobStatus,
    actor: int | None,
    *,
    expected_version: int | None = None,
    guards: Iterable[ColumnElement] = (),
    **values: Any,
) -> JobEvent:
    """Move ``job`` to ``to`` and record who did it (``actor`` is a user id, None
    for a system move such as the sweep).

    The move is conditional on the status that was read, and on
    ``expected_version`` when given. Callers acting on behalf of someone holding
    an old copy of the job pass ``expected_version``. Internal moves leave it out:
    an unrelated title edit must not fail a worker's accept.
    """
    current = job.status
    if to not in TRANSITIONS[current]:
        raise IllegalTransition(current, to)

    session = _session(job)
    conditions = [Job.status == current, *guards]
    if expected_version is not None:
        conditions.append(Job.version == expected_version)
    _apply(session, job, conditions, {"status": to, **values})

    event = JobEvent(
        company_id=job.company_id, job_id=job.id, from_status=current, to_status=to, actor_id=actor
    )
    session.add(event)
    session.flush()
    session.refresh(job)
    return event


def update_details(job: Job, expected_version: int, **fields: Any) -> None:
    """Edit descriptive fields, conditional on ``expected_version``."""
    smuggled = _LIFECYCLE_COLUMNS & fields.keys()
    if smuggled:
        raise ValueError(f"Not editable here: {sorted(smuggled)}")
    session = _session(job)
    _apply(session, job, [Job.version == expected_version], fields)
    session.refresh(job)
