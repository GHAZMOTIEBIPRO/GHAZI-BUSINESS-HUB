"""Consistent SQLite backup / verified restore.

    python scripts/backup.py backup  <db_path> <backup_dir>   -> prints backup file path
    python scripts/backup.py restore <backup_file> <db_path>  -> verifies integrity, keeps the old DB as *.before-restore

For PostgreSQL use pg_dump/pg_restore (see docs/OPERATIONS.md). Encrypt backups at rest and copy them off the host.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path


def backup(db_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"hub-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)  # online, consistent snapshot even while the app writes
    src.close()
    dst.close()
    verify(target)
    return target


def verify(path: Path) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if ok != "ok":
        raise SystemExit(f"integrity check failed: {ok}")
    missing = {"users", "orders", "opportunities", "entitlements"} - tables
    if missing:
        raise SystemExit(f"backup is missing tables: {sorted(missing)}")


def restore(backup_file: Path, db_path: Path) -> None:
    verify(backup_file)
    if db_path.exists():
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".before-restore"))
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    verify(db_path)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in {"backup", "restore"}:
        raise SystemExit(__doc__)
    if sys.argv[1] == "backup":
        print(backup(Path(sys.argv[2]), Path(sys.argv[3])))
    else:
        restore(Path(sys.argv[2]), Path(sys.argv[3]))
        print("restored")
