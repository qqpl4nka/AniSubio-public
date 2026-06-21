from __future__ import annotations

from datetime import timedelta
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from anisubio.models import JobStatus, SyncJob, utcnow
from anisubio.services.review_queue import add_failed_job


LAZY_LOAD_PRIORITY = 0


def enqueue_sync_job(
    db: Session,
    kitsu_id: int,
    requested_episode: int | None = None,
    reason: str = "lazy_load",
    priority: int | None = None,
    fansubs_id: int | None = None,
    source_page_url: str | None = None,
    resolved_mal_id: int | None = None,
    resolved_title: str | None = None,
) -> SyncJob:
    if reason == "lazy_load":
        existing_for_kitsu = db.scalar(
            select(SyncJob)
            .where(
                SyncJob.kitsu_id == kitsu_id,
                SyncJob.status.in_(
                    [JobStatus.PENDING.value, JobStatus.RUNNING.value]
                ),
            )
            .order_by(SyncJob.priority, SyncJob.created_at)
        )
        if existing_for_kitsu:
            if existing_for_kitsu.status == JobStatus.PENDING.value:
                existing_for_kitsu.priority = min(
                    existing_for_kitsu.priority,
                    LAZY_LOAD_PRIORITY,
                )
                existing_for_kitsu.reason = "lazy_load"
                existing_for_kitsu.requested_episode = requested_episode
                db.commit()
            return existing_for_kitsu

    active_key = (
        f"fansubs:{fansubs_id}"
        if reason in {"vacuum", "imdb_priority"} and fansubs_id is not None
        else f"kitsu:{kitsu_id}"
    )
    existing = db.scalar(
        select(SyncJob).where(SyncJob.active_key == active_key)
    )
    if existing:
        if (
            existing.status == JobStatus.PENDING.value
            and priority is not None
            and priority < existing.priority
        ):
            existing.priority = priority
            existing.reason = reason
            existing.resolved_mal_id = resolved_mal_id or existing.resolved_mal_id
            existing.resolved_title = resolved_title or existing.resolved_title
            db.commit()
        return existing
    job = SyncJob(
        kitsu_id=kitsu_id,
        requested_episode=requested_episode,
        reason=reason,
        priority=(
            priority
            if priority is not None
            else (LAZY_LOAD_PRIORITY if reason == "lazy_load" else 100)
        ),
        fansubs_id=fansubs_id,
        source_page_url=source_page_url,
        resolved_mal_id=resolved_mal_id,
        resolved_title=resolved_title,
        status=JobStatus.PENDING.value,
        active_key=active_key,
    )
    for attempt in range(4):
        db.add(job)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(SyncJob).where(SyncJob.active_key == active_key)
            )
            if existing:
                return existing
            raise
        except OperationalError as exc:
            db.rollback()
            if "database is locked" not in str(exc).casefold() or attempt == 3:
                raise
            time.sleep(0.1 * (2**attempt))
    db.refresh(job)
    return job


def claim_next_job(
    db: Session,
    worker_id: str,
    *,
    min_priority: int | None = None,
    max_priority: int | None = None,
) -> SyncJob | None:
    now = utcnow()
    query = select(SyncJob).where(
            SyncJob.status == JobStatus.PENDING.value,
            SyncJob.available_at <= now,
        )
    if min_priority is not None:
        query = query.where(SyncJob.priority >= min_priority)
    if max_priority is not None:
        query = query.where(SyncJob.priority <= max_priority)
    job = db.scalar(
        query
        .order_by(SyncJob.priority, SyncJob.created_at)
        .limit(1)
    )
    if not job:
        return None
    job.status = JobStatus.RUNNING.value
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    db.commit()
    db.refresh(job)
    return job


def recover_interrupted_jobs(
    db: Session,
    *,
    min_priority: int | None = None,
    max_priority: int | None = None,
) -> int:
    query = select(SyncJob).where(SyncJob.status == JobStatus.RUNNING.value)
    if min_priority is not None:
        query = query.where(SyncJob.priority >= min_priority)
    if max_priority is not None:
        query = query.where(SyncJob.priority <= max_priority)
    jobs = db.scalars(query).all()
    for job in jobs:
        job.status = JobStatus.PENDING.value
        job.locked_at = None
        job.locked_by = None
        job.available_at = utcnow()
        job.error = "Recovered after worker restart"
    db.commit()
    return len(jobs)


def complete_job(db: Session, job: SyncJob) -> None:
    job.status = JobStatus.SUCCEEDED.value
    job.completed_at = utcnow()
    job.active_key = None
    job.error = None
    db.commit()


def fail_job(
    db: Session,
    job: SyncJob,
    error: str,
    max_attempts: int,
) -> None:
    job.error = error
    job.locked_at = None
    job.locked_by = None
    if job.attempts >= max_attempts:
        job.status = JobStatus.FAILED.value
        job.completed_at = utcnow()
        job.active_key = None
        db.flush()
        add_failed_job(db, job)
    else:
        job.status = JobStatus.PENDING.value
        delay = min(15 * (2 ** (job.attempts - 1)), 15 * 60)
        job.available_at = utcnow() + timedelta(seconds=delay)
    db.commit()
