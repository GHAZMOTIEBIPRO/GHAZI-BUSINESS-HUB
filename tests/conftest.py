import os
import re

os.environ.setdefault("HUB_ENV", "test")
os.environ.setdefault("HUB_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("HUB_JOBS_TOKEN", "jobs-token-for-tests")
os.environ.setdefault("MOYASAR_WEBHOOK_SECRET_TOKEN", "moyasar-test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hub import db as dbm  # noqa: E402
from hub import jobs  # noqa: E402
from hub.security import hash_password, limiter  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


@pytest.fixture()
def database(tmp_path):
    dbm.configure(f"sqlite:///{tmp_path / 'test.db'}")
    dbm.init_db()
    with dbm.SessionLocal() as s:
        jobs.seed(s)
    limiter.reset()
    yield dbm
    dbm.engine.dispose()


@pytest.fixture()
def session(database):
    with dbm.SessionLocal() as s:
        yield s


@pytest.fixture()
def app(database):
    from hub.app import app as fastapi_app
    return fastapi_app


def new_client(app):
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def csrf_of(client, path):
    r = client.get(path)
    m = CSRF_RE.search(r.text)
    assert m, f"no csrf on {path}: {r.status_code}"
    return m.group(1)


def register(client, email, password="correct-horse-1", name="مقاول"):
    token = csrf_of(client, "/register")
    r = client.post("/register", data={"csrf": token, "name": name, "email": email, "password": password, "agree": "1"})
    assert r.status_code == 303, r.text[:500]
    return r


def post(client, path, data=None, csrf_from="/dashboard"):
    token = csrf_of(client, csrf_from)
    return client.post(path, data={"csrf": token, **(data or {})})


def make_staff(session, email, role):
    from hub.models import User
    u = User(email=email, name=role, password_hash=hash_password("staff-password-1"), role=role, trades=[], cities=[])
    session.add(u)
    session.commit()
    return u


def login(client, email, password):
    token = csrf_of(client, "/login")
    r = client.post("/login", data={"csrf": token, "email": email, "password": password})
    assert r.status_code == 303, r.text[:300]
