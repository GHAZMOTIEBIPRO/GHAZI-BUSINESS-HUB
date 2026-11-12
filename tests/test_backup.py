import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select

from hub import db as dbm
from hub.models import User

ROOT = Path(__file__).resolve().parents[1]


def test_backup_then_restore_brings_data_back(database, tmp_path):
    db_path = Path(str(dbm.engine.url.database))
    with dbm.SessionLocal() as s:
        s.add(User(email="keep@example.sa", name="k", password_hash="!", trades=[], cities=[]))
        s.commit()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), "backup", str(db_path), str(tmp_path / "bk")],
                         check=True, capture_output=True, text=True)
    backup_file = Path(out.stdout.strip())
    assert backup_file.exists()
    with dbm.SessionLocal() as s:  # disaster: data deleted
        s.query(User).delete()
        s.commit()
        assert s.scalar(select(func.count(User.id))) == 0
    dbm.engine.dispose()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), "restore", str(backup_file), str(db_path)],
                   check=True, capture_output=True, text=True)
    with dbm.SessionLocal() as s:
        assert s.scalar(select(User.email)) == "keep@example.sa"
    assert Path(str(db_path) + ".before-restore").exists()


def test_restore_rejects_corrupt_backup(database, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), "restore", str(bad), str(tmp_path / "x.db")],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert not (tmp_path / "x.db").exists() or (tmp_path / "x.db").stat().st_size == 0
