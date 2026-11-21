"""Regression tests for the independent review of 2026-11-13.

1. content rights enforced on display / approval / search, not only on automated fetching
2. e-mail goes through an HTTPS API (no SMTP), failures recorded, bad keys stop the batch
3. first admin: one-time secret setup, never promotes an existing account; e-mail confirmation for users
"""
import datetime as dt
import json

import httpx
from sqlalchemy import select

from hub import jobs, mailer, radar
from hub.config import settings
from hub.db import utcnow
from hub.models import EmailOutbox, Opportunity, Source, Subscription, User

from .conftest import csrf_of, latest_link, login, make_staff, new_client, post, register
from .factories import approved_opp, source

SOURCE_TEXT = "نص الإعلان الأصلي المنقول حرفيًا من موقع الجهة"


# ----------------------------------------------------------------- 1. content rights
def _opp_with_source_text(session, rights):
    src = source(session, key=f"s-{rights}", rights=rights, kind="manual", feed_url=None)
    o = approved_opp(session, title=f"فرصة {rights}")
    o.source_id, o.summary, o.summary_origin = src.id, SOURCE_TEXT, "source"
    session.commit()
    return o


def test_restricted_source_text_never_displayed_but_facts_and_link_are(app, session):
    c = new_client(app)
    for rights in ("unknown", "link_only", "restricted"):
        o = _opp_with_source_text(session, rights)
        page = c.get(f"/radar/{o.id}").text
        assert SOURCE_TEXT not in page, rights
        assert 'id="summaryWithheld"' in page and o.source_url in page and o.title in page, rights


def test_licensed_source_text_is_displayed(app, session):
    o = _opp_with_source_text(session, "verified_public_reuse")
    page = new_client(app).get(f"/radar/{o.id}").text
    assert SOURCE_TEXT in page and 'id="oppSummary"' in page


def test_search_does_not_match_withheld_text(app, session):
    o = _opp_with_source_text(session, "unknown")
    t = new_client(app).get("/radar", params={"q": "المنقول حرفيًا"}).text
    assert o.title not in t


def test_ingest_flags_unlicensed_text_and_reviewer_rewrite_publishes(app, session):
    make_staff(session, "rev2@example.sa", "reviewer")
    c = new_client(app)
    login(c, "rev2@example.sa", "staff-password-1")
    manual = session.scalar(select(Source).where(Source.key == "manual-review"))  # rights = link_only
    post(c, "/admin/opportunities/new", {"source_id": str(manual.id), "title": "ترميم مبنى إداري",
                                         "url": "https://agency.example.sa/1", "city": "جدة", "summary": SOURCE_TEXT,
                                         "trades": ["الدهانات"]}, csrf_from="/admin/opportunities/new")
    o = session.scalar(select(Opportunity).where(Opportunity.title == "ترميم مبنى إداري"))
    assert o.summary_origin == "source" and any("غير مرخّص" in r for r in o.review_reasons)
    assert "لن يُنشر" in c.get("/admin/review").text
    # approving without rewriting keeps the text private
    post(c, f"/admin/opportunities/{o.id}/review", {"decision": "approve", "opportunity_type": "OPEN_TENDER"},
         csrf_from="/admin/review")
    session.refresh(o)
    assert o.review_state == "approved"
    assert SOURCE_TEXT not in new_client(app).get(f"/radar/{o.id}").text
    # a reviewer summary in own words becomes the public description
    o.review_state = "pending_review"
    session.commit()
    post(c, f"/admin/opportunities/{o.id}/review", {"decision": "approve", "opportunity_type": "OPEN_TENDER",
                                                     "reviewer_summary": "أعمال دهانات لمبنى إداري في جدة"},
         csrf_from="/admin/review")
    page = new_client(app).get(f"/radar/{o.id}").text
    assert "أعمال دهانات لمبنى إداري في جدة" in page and "ملخص المراجع" in page and SOURCE_TEXT not in page


def test_manual_entry_own_words_attestation(app, session):
    make_staff(session, "rev3@example.sa", "reviewer")
    c = new_client(app)
    login(c, "rev3@example.sa", "staff-password-1")
    manual = session.scalar(select(Source).where(Source.key == "manual-review"))
    post(c, "/admin/opportunities/new", {"source_id": str(manual.id), "title": "صيانة مدرسة", "summary_own_words": "1",
                                         "url": "https://agency.example.sa/2", "summary": "ملخص كتبه المراجع"},
         csrf_from="/admin/opportunities/new")
    o = session.scalar(select(Opportunity).where(Opportunity.title == "صيانة مدرسة"))
    assert o.summary_origin == "reviewer" and radar.public_summary(o) == "ملخص كتبه المراجع"


def test_csv_summary_defaults_to_source_text(app, session):
    make_staff(session, "imp2@example.sa", "reviewer")
    c = new_client(app)
    login(c, "imp2@example.sa", "staff-password-1")
    manual = session.scalar(select(Source).where(Source.key == "manual-review"))
    body = ("title,url,summary,summary_is_own_words\n"
            f"أ,https://a.example.sa/1,{SOURCE_TEXT},\n"
            "ب,https://a.example.sa/2,ملخص بصياغتنا,yes\n").encode("utf-8-sig")
    c.post("/admin/import", data={"csrf": csrf_of(c, "/admin/import"), "source_id": str(manual.id)},
           files={"file": ("x.csv", body, "text/csv")})
    by = {o.title: o for o in session.scalars(select(Opportunity))}
    assert by["أ"].summary_origin == "source" and by["ب"].summary_origin == "reviewer"


# ----------------------------------------------------------------- 2. HTTPS e-mail
def _mail_env(monkeypatch, provider, handler):
    monkeypatch.setenv("HUB_MAIL_PROVIDER", provider)
    monkeypatch.setenv("HUB_MAIL_API_KEY", "k-test")
    monkeypatch.setenv("HUB_MAIL_FROM", "no-reply@example.sa")
    monkeypatch.setattr(mailer, "TRANSPORT", httpx.MockTransport(handler))


def test_mailer_has_no_smtp():
    import inspect
    src = inspect.getsource(mailer)
    assert "smtplib" not in src and "starttls" not in src


def test_resend_payload_and_brevo_auth_failure_stops_batch(session, monkeypatch):
    calls = []

    def resend(request):
        calls.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"id": "re_1"})
    _mail_env(monkeypatch, "resend", resend)
    session.add(EmailOutbox(to="a@example.sa", subject="s", body="b"))
    session.commit()
    assert mailer.send_pending(session)["sent"] == 1
    url, auth, payload = calls[0]
    assert url == "https://api.resend.com/emails" and auth == "Bearer k-test" and payload["to"] == ["a@example.sa"]
    assert session.scalar(select(EmailOutbox.provider_message_id)) == "re_1"

    def brevo_401(request):
        calls.append(str(request.url))
        return httpx.Response(401, json={"code": "unauthorized", "message": "Key not found"})
    _mail_env(monkeypatch, "brevo", brevo_401)
    session.add_all([EmailOutbox(to=f"x{i}@example.sa", subject="s", body="b") for i in range(3)])
    session.commit()
    n_before = len(calls)
    res = mailer.send_pending(session)
    assert res["sent"] == 0 and len(calls) == n_before + 1, "a bad key must not be retried for every message"
    row = session.scalar(select(EmailOutbox).where(EmailOutbox.to == "x0@example.sa"))
    assert row.sent_at is None and row.attempts == 1 and "401" in row.last_error and "k-test" not in row.last_error


def test_unconfigured_mail_stays_queued(session, monkeypatch):
    monkeypatch.delenv("HUB_MAIL_PROVIDER", raising=False)
    assert mailer.send_pending(session) == {"sent": 0, "skipped": "mail_provider_not_configured"}


# ----------------------------------------------------------------- 3a. e-mail confirmation
def test_unverified_user_cannot_buy_until_link_followed(app, session):
    c = new_client(app)
    register(c, "nv@example.sa", verify=False)
    assert "verifyBanner" in c.get("/dashboard").text
    r = post(c, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    assert r.headers["location"].startswith("/account/verify")
    link = latest_link("nv@example.sa", "/verify/")
    assert c.get(link).status_code == 303
    assert c.get(link).status_code == 400, "verification link is single use"
    r = post(c, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    assert r.headers["location"].startswith("/checkout/")


def test_verification_link_dies_if_email_changes(app, session):
    c = new_client(app)
    register(c, "old@example.sa", verify=False)
    link = latest_link("old@example.sa", "/verify/")
    u = session.scalar(select(User).where(User.email == "old@example.sa"))
    u.email = "new@example.sa"
    session.commit()
    assert c.get(link).status_code == 400
    session.refresh(u)
    assert u.email_verified_at is None


def test_alert_emails_only_to_verified_addresses(session):
    s = source(session, trust="auto_publish")
    for email, verified in (("v@example.sa", True), ("nv2@example.sa", False)):
        u = User(email=email, name="x", password_hash="!", trades=["الدهانات"], cities=["ينبع"],
                 email_verified_at=utcnow() if verified else None)
        session.add(u)
        session.flush()
        session.add(Subscription(user_id=u.id, plan="radar", starts_at=utcnow() - dt.timedelta(days=1),
                                 ends_at=utcnow() + dt.timedelta(days=29)))
    session.commit()
    from .test_radar import feed_transport, item
    jobs.refresh(session, transport=feed_transport([item(1)]))
    assert [e.to for e in session.scalars(select(EmailOutbox).where(EmailOutbox.subject.like("فرصة%")))] == ["v@example.sa"]
    _ = s


# ----------------------------------------------------------------- 3b. first admin
def test_setup_closed_without_token(app, monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "")
    assert new_client(app).get("/setup").status_code == 404


def test_setup_never_promotes_existing_account_and_closes_after_use(app, session, monkeypatch):
    code = "s" * 40
    monkeypatch.setattr(settings, "setup_token", code)
    # a mail provider is configured (mocked network): the admin must prove the address by e-mail
    import httpx

    from hub import mailer
    monkeypatch.setenv("HUB_MAIL_PROVIDER", "brevo")
    monkeypatch.setenv("HUB_MAIL_FROM", "noreply@example.sa")
    monkeypatch.setenv("HUB_MAIL_API_KEY", "test-key")
    monkeypatch.setattr(mailer, "TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(201, json={"messageId": "m"})))
    victim = new_client(app)
    register(victim, "existing@example.sa")
    c = new_client(app)
    r = c.post("/setup", data={"csrf": csrf_of(c, "/setup"), "setup_code": "wrong" * 8, "name": "x",
                               "email": "boss@example.sa", "password": "a-very-long-password"})
    assert r.status_code == 403
    r = c.post("/setup", data={"csrf": csrf_of(c, "/setup"), "setup_code": code, "name": "x",
                               "email": "existing@example.sa", "password": "a-very-long-password"})
    assert r.status_code == 400
    assert session.scalar(select(User.role).where(User.email == "existing@example.sa")) == "user", "no promotion"
    r = c.post("/setup", data={"csrf": csrf_of(c, "/setup"), "setup_code": code, "name": "المالك",
                               "email": "boss@example.sa", "password": "a-very-long-password"})
    assert r.status_code == 303
    admin = session.scalar(select(User).where(User.email == "boss@example.sa"))
    assert admin.role == "admin" and admin.email_verified_at is None
    # admin pages stay locked until the e-mail is proven
    assert c.get("/admin").headers["location"].startswith("/account/verify")
    c.get(latest_link("boss@example.sa", "/verify/"))
    assert c.get("/admin").status_code == 200  # 2FA not required in the test environment
    # setup is gone for good
    assert c.get("/setup").status_code == 404
    other = new_client(app)
    r = other.post("/setup", data={"csrf": csrf_of(other, "/login"), "setup_code": code, "name": "y",
                                   "email": "second@example.sa", "password": "a-very-long-password"})
    assert r.status_code == 404


def test_old_env_bootstrap_is_gone(monkeypatch, session):
    monkeypatch.setenv("HUB_BOOTSTRAP_ADMIN_EMAIL", "existing2@example.sa")
    monkeypatch.setenv("HUB_BOOTSTRAP_ADMIN_PASSWORD", "a-very-long-password")
    session.add(User(email="existing2@example.sa", name="x", password_hash="!", trades=[], cities=[]))
    session.commit()
    jobs.seed(session)
    assert session.scalar(select(User.role).where(User.email == "existing2@example.sa")) == "user"
    assert not hasattr(jobs, "bootstrap_admin")
