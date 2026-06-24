from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from anisubio.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if settings.database_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_schema() -> None:
    from anisubio import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("subtitle_assets")
        }
        if "fansubs_id" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE subtitle_assets "
                        "ADD COLUMN fansubs_id INTEGER"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_subtitle_assets_fansubs_id "
                        "ON subtitle_assets (fansubs_id)"
                    )
                )
        if "manual_verified" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE subtitle_assets "
                        "ADD COLUMN manual_verified INTEGER NOT NULL DEFAULT 0"
                    )
                )
        if "mapping_quarantined" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE subtitle_assets "
                        "ADD COLUMN mapping_quarantined INTEGER NOT NULL DEFAULT 0"
                    )
                )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
