from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ANISUBIO_",
        extra="ignore",
    )

    public_base_url: str = "http://localhost:8000"
    admin_key: str = "change-me"
    database_url: str = "sqlite:///./data/anisubio.db"
    storage_dir: Path = Path("./data/subtitles")
    download_dir: Path = Path("./data/downloads")
    allowed_source_hosts: tuple[str, ...] = ("www.fansubs.ru", "fansubs.ru")
    http_user_agent: str = (
        "Mozilla/5.0 (compatible; AniSubio/0.1; "
        "+https://github.com/qqpl4nka/AniSubio)"
    )
    request_timeout_seconds: float = 30.0
    max_archive_bytes: int = 100 * 1024 * 1024
    max_extracted_bytes: int = 500 * 1024 * 1024
    max_archive_files: int = 5000
    manifest_cache_seconds: int = 3600
    subtitle_list_cache_seconds: int = 1800
    sync_kitsu_ids: tuple[int, ...] = ()
    sync_interval_seconds: int = 6 * 60 * 60
    sync_min_request_delay_seconds: float = 5.0
    sync_max_request_delay_seconds: float = 5.0
    telegram_storage_channel_id: int | None = None
    telegram_backup_channel_id: int | None = None
    local_cache_dir: Path = Path("./data/cache")
    local_cache_max_bytes: int = 2 * 1024 * 1024 * 1024
    telegram_bot_token: str = ""
    telegram_proxy_url: str = "socks5://127.0.0.1:10990"
    telegram_api_base: str = "https://api.telegram.org"
    telegram_upload_interval_seconds: float = 1.0
    job_poll_interval_seconds: float = 3.0
    job_max_attempts: int = 3
    backup_interval_seconds: int = 24 * 60 * 60
    temp_dir: Path = Path("/tmp/anisubio")
    lazy_wait_seconds: float = 0.0
    catalog_scan_interval_seconds: int = 24 * 60 * 60
    catalog_resolve_batch_size: int = 20
    metadata_proxy_url: str = ""
    sync_request_retries: int = 3
    http_rate_limit_file: Path = Path("/tmp/anisubio/http-rate-limit")
    telegram_rate_limit_file: Path = Path(
        "/tmp/anisubio/telegram-upload-rate-limit"
    )
    review_batch_size: int = 25
    review_poll_interval_seconds: float = 60.0

    @field_validator("public_base_url")
    @classmethod
    def trim_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("allowed_source_hosts", mode="before")
    @classmethod
    def parse_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip().lower() for part in value.split(",") if part.strip())
        return value

    @field_validator("sync_kitsu_ids", mode="before")
    @classmethod
    def parse_kitsu_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(
                int(part.strip()) for part in value.split(",") if part.strip()
            )
        return value

    def ensure_directories(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.local_cache_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.http_rate_limit_file.parent.mkdir(parents=True, exist_ok=True)
        self.telegram_rate_limit_file.parent.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(
                parents=True, exist_ok=True
            )

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Операция поддерживается только для SQLite")
        return Path(self.database_url.removeprefix("sqlite:///")).resolve()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
