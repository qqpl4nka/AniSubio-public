import sqlite3
from pathlib import Path

from anisubio.backup import _snapshot_sqlite


def test_snapshot_sqlite_is_consistent(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE values_table (value TEXT)")
        db.execute("INSERT INTO values_table VALUES ('saved')")
        db.commit()

    _snapshot_sqlite(source, destination)

    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT value FROM values_table").fetchone() == ("saved",)
