"""Two-factor login, staff 2FA enforcement, password reset via HTTPS e-mail API, CSV import."""
import json
import re
import time

import httpx
from sqlalchemy import func, select

from hub import mailer, totp
from hub.config import settings
from hub.models import AuthSession, EmailOutbox, Opportunity, Source, User

from .conftest import csrf_of, login, make_staff, new_client, post, register


def test_totp_rfc6238_vector():
    # RFC 6238 appendix B, SHA1, T=59s -> 94287082 (8 digits); our 6-digit code is its last 6 digits
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
    assert totp.now_code(secret, at=59) == "287082"
    assert totp.verify(secret, "287082", at=59) == 1
    assert totp.verify(secret, "287082", last_step=1, at=59) is None, "replayed code must be refused"
    assert totp.verify(secret, "000000", at=59) is None


def test_enable_2fa_then_login_requires_code(app, session):
    c = new_client(app)
    register(c, "tf@example.sa")
    page = c.get("/account/2fa").text
    secret = re.search(r'id="totpSecret">([A-Z2-7]+)<', page).group(1)
    r = post(c, "/account/2fa", {"code": "000000"}, csrf_from="/account/2fa")
    assert r.status_code == 400
    r = post(c, "/account/2fa", {"code": totp.now_code(secret)}, csrf_from="/account/2fa")
    assert r.status_code == 303
    u = session.scalar(select(User).where(User.email == "tf@example.sa"))
    session.refresh(u)
    assert u.totp_secret == secret
    c2 = new_client(app)
    token = csrf_of(c2, "/login")
    r = c2.post("/login", data={"csrf": token, "email": "tf@example.sa", "password": "correct-horse-1"})
    assert r.status_code == 400, "password alone is not enough"
    r = c2.post("/login", data={"csrf": token, "email": "tf@example.sa", "password": "correct-horse-1",
                                "otp": totp.now_code(secret, at=time.time() + 30)})
    assert r.status_code == 303


def test_staff_must_enable_2fa_when_required(app, session, monkeypatch):
    monkeypatch.setattr(settings, "require_staff_2fa", True)
    make_staff(session, "boss@example.sa", "admin")
    c = new_client(app)
    login(c, "boss@example.sa", "staff-password-1")
    r = c.get("/admin")
    assert r.status_code == 303 and r.headers["location"].startswith("/account/2fa")
    u = session.scalar(select(User).where(User.email == "boss@example.sa"))
    u.totp_secret = totp.new_secret()
    session.commit()
    assert c.get("/admin").status_code == 200


def brevo_capture(monkeypatch, status=201):
    """Route the mailer to a fake Brevo endpoint over HTTPS API and record what it would send."""
    sent = []

    def handler(request: httpx.Request):
        assert request.url == httpx.URL("https://api.brevo.com/v3/smtp/email")
        assert request.headers["api-key"] == "test-brevo-key"
        sent.append(json.loads(request.content))
        return httpx.Response(status, json={"messageId": f"<m{len(sent)}@brevo>"} if status < 300 else {"code": "unauthorized"})
    monkeypatch.setenv("HUB_MAIL_PROVIDER", "brevo")
    monkeypatch.setenv("HUB_MAIL_API_KEY", "test-brevo-key")
    monkeypatch.setenv("HUB_MAIL_FROM", "no-reply@example.sa")
    monkeypatch.setattr(mailer, "TRANSPORT", httpx.MockTransport(handler))
    return sent


def test_password_reset_flow_no_enumeration_via_https_api(app, session, monkeypatch):
    sent = brevo_capture(monkeypatch)
    c = new_client(app)
    register(c, "reset@example.sa")
    sent.clear()  # ignore the verification mail
    other = new_client(app)
    r1 = other.post("/forgot", data={"csrf": csrf_of(other, "/forgot"), "email": "nobody@example.sa"})
    r2 = other.post("/forgot", data={"csrf": csrf_of(other, "/forgot"), "email": "reset@example.sa"})
    assert r1.status_code == r2.status_code == 200 and "إن كان البريد" in r1.text and "إن كان البريد" in r2.text
    assert len(sent) == 1 and sent[0]["to"] == [{"email": "reset@example.sa"}]
    link = re.search(r"/reset/[\w-]+", sent[0]["textContent"]).group(0)
    assert session.scalar(select(EmailOutbox.sent_at).where(EmailOutbox.to == "reset@example.sa")) is not None
    r = other.post(link, data={"csrf": csrf_of(other, link), "password": "short"})
    assert r.status_code == 400
    r = other.post(link, data={"csrf": csrf_of(other, link), "password": "brand-new-pass-9"})
    assert r.status_code == 303
    assert other.get(link).status_code == 400, "reset link is single use"
    assert session.scalar(select(func.count(AuthSession.id))) == 0, "all sessions revoked"
    assert c.get("/dashboard").status_code == 303
    login(c, "reset@example.sa", "brand-new-pass-9")


def test_public_staging_sandbox_is_staff_only(app, session, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_checkout_open", False)
    c = new_client(app)
    register(c, "visitor@example.sa")
    r = post(c, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    oid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "محاكاة دفع ناجح" not in c.get(f"/checkout/{oid}").text
    assert post(c, f"/checkout/{oid}/sandbox", {"result": "paid"}, csrf_from=f"/checkout/{oid}").status_code == 403
    make_staff(session, "qa@example.sa", "reviewer")
    s = new_client(app)
    login(s, "qa@example.sa", "staff-password-1")
    r = post(s, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    oid2 = int(r.headers["location"].rsplit("/", 1)[1])
    r = post(s, f"/checkout/{oid2}/sandbox", {"result": "paid"}, csrf_from=f"/checkout/{oid2}")
    assert r.headers["location"] == f"/orders/{oid2}"


def test_csv_import_goes_to_review(app, session):
    make_staff(session, "imp@example.sa", "reviewer")
    c = new_client(app)
    login(c, "imp@example.sa", "staff-password-1")
    manual = session.scalar(select(Source).where(Source.key == "manual-review"))
    body = ("title,url,city,trades,opportunity_type,deadline_at\n"
            "منافسة صيانة مبنى,https://agency.example.sa/t/1,جدة,الصيانة المدنية والمعمارية|الدهانات,OPEN_TENDER,2030-01-01\n"
            "بلا رابط آمن,http://insecure.example.sa/x,جدة,,,\n"
            "منافسة صيانة مبنى,https://agency.example.sa/t/1,جدة,,OPEN_TENDER,2030-01-01\n").encode("utf-8-sig")
    token = csrf_of(c, "/admin/import")
    r = c.post("/admin/import", data={"csrf": token, "source_id": str(manual.id)},
               files={"file": ("ops.csv", body, "text/csv")})
    assert r.status_code == 200 and 'id="importResult"' in r.text
    rows = session.scalars(select(Opportunity)).all()
    assert len(rows) == 1 and rows[0].review_state == "pending_review"
    assert set(rows[0].trades) == {"الصيانة المدنية والمعمارية", "الدهانات"}
    r = c.post("/admin/import", data={"csrf": token, "source_id": str(manual.id)},
               files={"file": ("bad.csv", b"name,link\nx,y\n", "text/csv")})
    assert r.status_code == 400
    user = new_client(app)
    register(user, "nonstaff@example.sa")
    assert user.get("/admin/import").status_code == 403
