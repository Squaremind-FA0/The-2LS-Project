"""The job state machine: every (from, to) pair, the audit row, the version guards.

LEGAL is spelled out here rather than imported, so editing the table in
app/domain/job_state.py cannot quietly change what these tests expect.
"""

import itertools
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.domain.job_state import (
    TERMINAL,
    IllegalTransition,
    StaleJob,
    transition,
    update_details,
)
from app.models import Job, JobEvent, JobOffer, JobStatus, OfferStatus, Shift
from app.models.job import utcnow
from app.services import dispatch, time_entries
from tests.factories import tenant_context

S = JobStatus
LEGAL = {
    (S.draft, S.dispatching),
    (S.dispatching, S.assigned),
    (S.assigned, S.in_progress),
    (S.in_progress, S.completed),
    (S.dispatching, S.unfilled),
    (S.draft, S.cancelled),
    (S.dispatching, S.cancelled),
    (S.assigned, S.cancelled),
    (S.in_progress, S.cancelled),
    (S.dispatching, S.dispatching),
}
ALL_PAIRS = list(itertools.product(JobStatus, JobStatus))
ILLEGAL = [pair for pair in ALL_PAIRS if pair not in LEGAL]


def _id(pair) -> str:
    return f"{pair[0].value}->{pair[1].value}"


def _job(db, company_id, status=S.draft, **fields) -> Job:
    job = Job(
        title="Fix tap", trade="plumbing", address="1 George St", lat=-33.86, lng=151.2,
        status=status, company_id=company_id, **fields,
    )
    db.add(job)
    db.flush()
    return job


def _events(db, job_id) -> list[JobEvent]:
    return db.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)).all()


def test_pairs_cover_the_whole_status_space():
    assert len(LEGAL) + len(ILLEGAL) == len(JobStatus) ** 2
    assert TERMINAL == {S.completed, S.cancelled, S.unfilled}


@pytest.mark.parametrize("pair", sorted(LEGAL, key=_id), ids=_id)
def test_legal_transition_moves_the_job_and_writes_one_event(
    transactional_db, two_companies, pair
):
    start, target = pair
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, start)
        before = job.version

        event = transition(job, target, a.dispatcher.id)

        assert job.status is target
        assert job.version == before + 1
        [stored] = _events(transactional_db, job.id)
        assert stored is event
        assert (stored.job_id, stored.company_id) == (job.id, a.id)
        assert (stored.from_status, stored.to_status) == (start, target)
        assert stored.actor_id == a.dispatcher.id
        assert stored.at is not None


@pytest.mark.parametrize("pair", ILLEGAL, ids=_id)
def test_illegal_transition_raises_and_writes_nothing(transactional_db, two_companies, pair):
    start, target = pair
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, start)
        before = job.version

        with pytest.raises(IllegalTransition):
            transition(job, target, a.dispatcher.id)

        transactional_db.refresh(job)
        assert job.status is start
        assert job.version == before
        assert _events(transactional_db, job.id) == []


def test_transition_accepts_no_actor_for_system_moves(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, S.dispatching)
        assert transition(job, S.unfilled, None).actor_id is None


def test_transition_applies_extra_values(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, S.draft)
        transition(job, S.dispatching, a.dispatcher.id, offer_round=1)
        assert job.offer_round == 1


def test_transition_refuses_a_stale_expected_version(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, S.draft)

        with pytest.raises(StaleJob):
            transition(job, S.cancelled, a.dispatcher.id, expected_version=job.version + 1)

        transactional_db.refresh(job)
        assert job.status is S.draft
        assert _events(transactional_db, job.id) == []


def test_transition_loses_to_a_status_change_after_the_read(transactional_db, two_companies):
    # Deterministic stand-in for a race: SQLite serialises whole requests, so a
    # concurrent test would never reach this branch.
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, S.dispatching)
        transactional_db.execute(
            update(Job).where(Job.id == job.id).values(status=S.cancelled)
            .execution_options(synchronize_session=False)
        )
        assert job.status is S.dispatching  # the read we are acting on is out of date

        with pytest.raises(StaleJob):
            transition(job, S.assigned, a.worker.id)

        assert _events(transactional_db, job.id) == []


def test_transition_honours_extra_guards(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id, S.dispatching, assigned_worker_id=a.worker.id)
        with pytest.raises(StaleJob):
            transition(
                job, S.assigned, a.worker.id, guards=(Job.assigned_worker_id.is_(None),)
            )


def test_update_details_bumps_version_and_refuses_a_stale_one(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id)
        update_details(job, 1, title="New title")
        assert (job.title, job.version) == ("New title", 2)
        assert _events(transactional_db, job.id) == []

        with pytest.raises(StaleJob):
            update_details(job, 1, title="Lost update")
        transactional_db.refresh(job)
        assert job.title == "New title"


def test_update_details_cannot_change_lifecycle_columns(transactional_db, two_companies):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        job = _job(transactional_db, a.id)
        for smuggled in ("status", "version", "assigned_worker_id", "company_id"):
            with pytest.raises(ValueError):
                update_details(job, 1, **{smuggled: S.completed})


# --- every path that moves a job in the running app --------------------------------------


def test_no_status_write_bypasses_the_state_machine():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if path.name != "job_state.py" and "update(Job)" in path.read_text()
    ]
    assert offenders == []


def _trail(db, job_id) -> list[tuple]:
    return [(e.from_status, e.to_status, e.actor_id) for e in _events(db, job_id)]


def test_accept_clock_in_and_clock_out_each_write_an_event(transactional_db, two_companies):
    a, _ = two_companies
    db = transactional_db
    worker = a.worker.id
    with tenant_context(db, a.id):
        job = _job(db, a.id, S.dispatching, scheduled_for=utcnow())
        job_id = job.id
        now = utcnow()
        offer = JobOffer(
            job_id=job_id, worker_id=worker, status=OfferStatus.pending,
            expires_at=now + timedelta(minutes=5),
        )
        db.add_all([
            offer,
            Shift(
                worker_id=worker, starts_at=now - timedelta(hours=1),
                ends_at=now + timedelta(hours=1),
            ),
        ])
        db.flush()

        dispatch.accept_offer(db, offer.id, worker, now=now)
        time_entries.clock_in(db, job_id, worker, now=now)
        time_entries.clock_out(db, job_id, worker, now=now + timedelta(minutes=30))

        job = db.get(Job, job_id)
        assert job.status is S.completed
        assert job.version == 4
        assert _trail(db, job_id) == [
            (S.dispatching, S.assigned, worker),
            (S.assigned, S.in_progress, worker),
            (S.in_progress, S.completed, worker),
        ]


def test_dispatcher_transition_records_who_and_sweep_records_the_system(
    transactional_db, two_companies
):
    a, _ = two_companies
    db = transactional_db
    with tenant_context(db, a.id):
        job_id = _job(db, a.id, S.draft).id
        dispatch.transition_job(db, job_id, S.dispatching, actor_id=a.dispatcher.id)
        # Nobody is in range, so the sweep's re-offer closes the job.
        dispatch.sweep(db, a.id)

        assert db.get(Job, job_id).status is S.unfilled
        assert _trail(db, job_id) == [
            (S.draft, S.dispatching, a.dispatcher.id),
            (S.dispatching, S.unfilled, None),
        ]


def test_a_rolled_back_transition_leaves_no_event(transactional_db, two_companies):
    a, _ = two_companies
    db = transactional_db
    with tenant_context(db, a.id):
        job_id = _job(db, a.id, S.draft).id
        db.commit()
        transition(db.get(Job, job_id), S.cancelled, a.dispatcher.id)
        db.rollback()

        assert db.get(Job, job_id).status is S.draft
        assert _events(db, job_id) == []
