from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import JobStatus, ReviewItem
from anisubio.services.jobs import (
    claim_next_job,
    complete_job,
    enqueue_sync_job,
    fail_job,
    recover_interrupted_jobs,
)


def test_job_is_single_flight_and_claimable() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        first = enqueue_sync_job(db, 11, 1)
        duplicate = enqueue_sync_job(db, 11, 2)

        assert duplicate.id == first.id
        claimed = claim_next_job(db, "test-worker")
        assert claimed
        assert claimed.status == JobStatus.RUNNING.value
        assert claimed.attempts == 1

        complete_job(db, claimed)
        assert claimed.status == JobStatus.SUCCEEDED.value
        assert claimed.active_key is None

        next_job = enqueue_sync_job(db, 11, 3)
        assert next_job.id != first.id


def test_failed_job_is_retried_then_released() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = enqueue_sync_job(db, 22)
        claimed = claim_next_job(db, "test-worker")
        fail_job(db, claimed, "temporary", max_attempts=2)
        assert claimed.status == JobStatus.PENDING.value
        assert claimed.active_key == "kitsu:22"

        claimed.available_at = claimed.created_at
        db.commit()
        claimed = claim_next_job(db, "test-worker")
        fail_job(db, claimed, "permanent", max_attempts=2)
        assert claimed.status == JobStatus.FAILED.value
        assert claimed.active_key is None
        review = db.query(ReviewItem).one()
        assert review.category == "import_error"
        assert review.sync_job_id == claimed.id


def test_lazy_job_has_priority_over_vacuum() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        enqueue_sync_job(
            db,
            100,
            reason="vacuum",
            fansubs_id=500,
            source_page_url="http://fansubs.ru/base.php?id=500",
        )
        lazy = enqueue_sync_job(db, 11, requested_episode=1, reason="lazy_load")

        claimed = claim_next_job(db, "test-worker")

        assert claimed.id == lazy.id
        assert claimed.kitsu_id == 11


def test_lazy_request_promotes_existing_vacuum_job() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        vacuum = enqueue_sync_job(
            db,
            11,
            reason="vacuum",
            fansubs_id=274,
            source_page_url="http://fansubs.ru/base.php?id=274",
        )
        lazy = enqueue_sync_job(db, 11, requested_episode=1, reason="lazy_load")

        assert lazy.id == vacuum.id
        assert lazy.priority == 0
        assert lazy.reason == "lazy_load"
        assert lazy.source_page_url.endswith("id=274")


def test_lazy_request_outranks_imdb_priority_job() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        imdb_job = enqueue_sync_job(
            db,
            11,
            reason="imdb_priority",
            priority=1,
            fansubs_id=274,
        )

        lazy = enqueue_sync_job(
            db,
            11,
            requested_episode=1,
            reason="lazy_load",
        )

        assert lazy.id == imdb_job.id
        assert lazy.priority == 0
        assert lazy.reason == "lazy_load"


def test_recovers_running_job_after_worker_restart() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        enqueue_sync_job(db, 11)
        running = claim_next_job(db, "dead-worker")

        assert recover_interrupted_jobs(db) == 1
        assert running.status == JobStatus.PENDING.value
        assert running.locked_by is None


def test_workers_claim_separate_priority_queues() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        lazy = enqueue_sync_job(db, 11, reason="lazy_load")
        vacuum = enqueue_sync_job(
            db,
            22,
            reason="vacuum",
            fansubs_id=222,
        )

        assert claim_next_job(db, "vacuum", min_priority=100).id == vacuum.id
        assert claim_next_job(db, "lazy", max_priority=99).id == lazy.id


def test_explicit_priority_promotes_existing_job() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        original = enqueue_sync_job(
            db,
            11,
            reason="vacuum",
            priority=100,
            fansubs_id=274,
        )
        promoted = enqueue_sync_job(
            db,
            11,
            reason="imdb_priority",
            priority=1,
            fansubs_id=274,
        )

        assert promoted.id == original.id
        assert promoted.priority == 1
        assert promoted.reason == "imdb_priority"
