from datetime import datetime, timezone

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from anisubio.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StorageObject(Base):
    """One immutable file stored outside the application server."""

    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("backend", "object_id", name="uq_storage_backend_object"),
        Index("ix_storage_checksum", "checksum"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)

    # Telegram-specific recovery metadata. Bot API file IDs and MTProto document
    # IDs are different namespaces, so all identifiers are stored explicitly.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    telegram_file_id: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    telegram_file_unique_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    subtitle_assets: Mapped[list["SubtitleAsset"]] = relationship(
        back_populates="storage_object"
    )


class SubtitleAsset(Base):
    __tablename__ = "subtitle_assets"
    __table_args__ = (
        UniqueConstraint(
            "kitsu_id",
            "episode",
            "storage_object_id",
            name="uq_subtitle_episode_object",
        ),
        Index("ix_subtitle_lookup", "kitsu_id", "episode", "language"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kitsu_id: Mapped[int] = mapped_column(Integer, nullable=False)
    fansubs_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    # Explicitly reviewed mappings may legitimately target another Kitsu ID
    # when one legacy fansubs card contains several seasons or OVAs.
    manual_verified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mapping_quarantined: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="rus")
    display_name: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(64))
    checksum: Mapped[str] = mapped_column(String(64))
    storage_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Temporary compatibility field for assets imported before remote storage.
    stored_filename: Mapped[str | None] = mapped_column(
        String(512), unique=True, nullable=True
    )
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    storage_object: Mapped[StorageObject | None] = relationship(
        back_populates="subtitle_assets"
    )


class UnresolvedSubtitle(Base):
    """A safely stored subtitle whose episode could not be inferred."""

    __tablename__ = "unresolved_subtitles"
    __table_args__ = (
        UniqueConstraint(
            "kitsu_id",
            "checksum",
            "fansubs_archive_id",
            name="uq_unresolved_source_file",
        ),
        Index("ix_unresolved_review", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kitsu_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    fansubs_title_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fansubs_archive_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_object_id: Mapped[int] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(255),
        default="episode_not_detected",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending_review",
        nullable=False,
    )
    resolved_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ReviewItem(Base):
    """Unified dead-letter queue for cases deferred until the first pass ends."""

    __tablename__ = "review_queue"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_review_queue_dedupe"),
        Index("ix_review_queue_status_category", "status", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending_review", nullable=False
    )
    kitsu_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sync_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="SET NULL"), nullable=True
    )
    unresolved_subtitle_id: Mapped[int | None] = mapped_column(
        ForeignKey("unresolved_subtitles.id", ondelete="SET NULL"), nullable=True
    )
    fansubs_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ReviewAnalysis(Base):
    """Non-destructive analysis result for one review queue item."""

    __tablename__ = "review_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_item_id: Mapped[int] = mapped_column(
        ForeignKey("review_queue.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    analyzer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ExternalIdMapping(Base):
    """Cached Stremio/Cinemeta series ID to Kitsu season mapping."""

    __tablename__ = "external_id_mappings"
    __table_args__ = (
        UniqueConstraint(
            "external_id",
            "season",
            name="uq_external_id_season",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kitsu_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SyncRecord(Base):
    __tablename__ = "sync_records"

    kitsu_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    russian_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fansubs_page_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SyncJob(Base):
    """Durable single-flight job created by a cache miss or vacuum scheduler."""

    __tablename__ = "sync_jobs"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_sync_job_active_key"),
        Index("ix_sync_jobs_claim", "status", "available_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kitsu_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    active_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    fansubs_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_page_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    resolved_mal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    requested_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(32), default="lazy_load")
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.PENDING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DatabaseBackup(Base):
    __tablename__ = "database_backups"

    id: Mapped[int] = mapped_column(primary_key=True)
    storage_object_id: Mapped[int] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class FansubsCatalogItem(Base):
    __tablename__ = "fansubs_catalog_items"
    __table_args__ = (
        Index("ix_fansubs_resolution", "resolution_status", "updated_at"),
    )

    fansubs_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_title: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    media_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    episode_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_letter: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kitsu_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resolution_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    resolution_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class VacuumState(Base):
    __tablename__ = "vacuum_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
