"""Staging launch: PostgreSQL enforced, health facts, recorded refresh, GitHub OIDC job auth, setup without e-mail."""
import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from hub import config, oidc
from hub.config import settings
from hub.models import AuditLog, JobRun, Opportunity, Source, User

from .conftest import csrf_of, new_client

REPO = "GHAZMOTIEBIPRO/GHAZI-BUSINESS-HUB"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def gh_token(key=KEY, **over):
    now = dt.datetime.now(dt.UTC)
    claims = {"iss": oidc.ISSUER, "aud": "ghazi-business-hub", "iat": now, "nbf": now, "exp": now + dt.timedelta(minutes=5),
              "repository": REPO, "ref": "refs/heads/main", "event_name": "schedule",
              "workflow_ref": f"{REPO}/.github/workflows/refresh.yml@refs/heads/main", "sub": f"repo:{REPO}:ref:refs/heads/main"}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


@pytest.fixture()
def gh_keys(monkeypatch):
    import hub.app as appmod
    monkeypatch.setattr(appmod, "OIDC_KEY_RESOLVER", lambda token: KEY.public_key())


# ---------------------------------------------------------------- configuration
def test_staging_refuses_sqlite(monkeypatch):
    monkeypatch.setenv("HUB_ENV", "staging")
    monkeypatch.setenv("HUB_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("HUB_DATABASE_URL", "sqlite:///lost-on-restart.db")
    monkeypatch.delenv("HUB_ALLOW_SQLITE", raising=False)
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        config.Settings()
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require")
    assert config.Settings().database_url.startswith("postgresql://")


def test_render_external_url_is_the_default_base_url(monkeypatch):
    monkeypatch.delenv("HUB_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://ghazi-business-hub-dd3h.onrender.com/")
    s = config.Settings()
    assert s.base_url == "https://ghazi-business-hub-dd3h.onrender.com" and s.secure_cookies


def test_healthz_reports_engine_and_start_time_without_secrets(app):
    body = new_client(app).get("/healthz").json()
    assert body["ok"] is True and body["db"] in ("sqlite", "postgresql")
    assert body["started_at"].endswith("Z")
    assert not any(k in str(body).lower() for k in ("password", "secret", "@"))


# ---------------------------------------------------------------- scheduled refresh
def test_refresh_is_recorded_and_shown_on_sources(app, session, gh_keys):
    c = new_client(app)
    r = c.post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {gh_token()}"})
    assert r.status_code == 200, r.text
    run = session.get(JobRun, r.json()["job_run_id"])
    assert run.ok and run.finished_at and run.trigger == "github_oidc:schedule"
    page = c.get("/sources").text
    assert 'id="jobRuns"' in page and "جدولة GitHub" in page and "نجح" in page


def test_refresh_closes_expired_and_does_not_realert(app, session, gh_keys):
    past = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=2)
    src = session.scalar(select(Source.id).order_by(Source.id))
    session.add(Opportunity(source_id=src, title="منافسة ترميم منتهية", source_url="https://example.sa/t/1", fingerprint="fp-expired",
                            opportunity_type="OPEN_TENDER", review_state="approved", deadline_at=past,
                            trades=["renovation"], city="ينبع"))
    session.commit()
    c = new_client(app)
    first = c.post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {gh_token()}"}).json()
    assert first["closed"] >= 1
    second = c.post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {gh_token()}"}).json()
    assert second["user_alerts"] == 0 and second["closed"] == 0


@pytest.mark.parametrize("over,why", [
    ({"repository": "someone/fork"}, "repository"),
    ({"workflow_ref": f"{REPO}/.github/workflows/ci.yml@refs/heads/main"}, "workflow"),
    ({"ref": "refs/heads/feature"}, "ref"),
    ({"event_name": "pull_request"}, "event"),
    ({"aud": "someone-else"}, "InvalidAudienceError"),
    ({"iss": "https://evil.example"}, "InvalidIssuerError"),
    ({"exp": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)}, "ExpiredSignatureError"),
])
def test_refresh_refuses_foreign_or_tampered_tokens(app, session, gh_keys, over, why):
    r = new_client(app).post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {gh_token(**over)}"})
    assert r.status_code == 401
    assert session.scalar(select(AuditLog.detail).where(AuditLog.action == "jobs.oidc_rejected")) == why
    assert session.scalar(select(JobRun.id)) is None


def test_refresh_refuses_token_signed_by_another_key(app, gh_keys):
    r = new_client(app).post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {gh_token(key=OTHER_KEY)}"})
    assert r.status_code == 401


def test_refresh_still_accepts_the_host_token_and_refuses_nothing(app):
    c = new_client(app)
    assert c.post("/internal/jobs/refresh").status_code == 401
    assert c.post("/internal/jobs/refresh", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/internal/jobs/refresh", headers={"Authorization": f"Bearer {settings.jobs_token}"})
    assert r.status_code == 200 and r.json()["job_run_id"]


# ---------------------------------------------------------------- first admin without an e-mail service
def test_setup_without_mail_provider_verifies_by_setup_code_and_audits(app, session, monkeypatch):
    monkeypatch.delenv("HUB_MAIL_PROVIDER", raising=False)
    code = "k" * 40
    monkeypatch.setattr(settings, "setup_token", code)
    c = new_client(app)
    r = c.post("/setup", data={"csrf": csrf_of(c, "/setup"), "setup_code": code, "name": "المالك",
                               "email": "owner@example.sa", "password": "a-very-long-password"})
    assert r.status_code == 303 and r.headers["location"].startswith("/account/2fa")
    admin = session.scalar(select(User).where(User.email == "owner@example.sa"))
    assert admin.role == "admin" and admin.email_verified_at is not None
    assert session.scalar(select(AuditLog.id).where(AuditLog.action == "setup.verified_by_setup_code"))
    assert c.get("/setup").status_code == 404
