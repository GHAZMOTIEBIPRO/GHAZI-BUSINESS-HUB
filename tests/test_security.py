"""Authentication, CSRF, headers, rate limiting and user isolation (IDOR)."""
import re

from sqlalchemy import select

from hub.models import Order, PipelineItem, QuoteDraft, User

from .conftest import csrf_of, login, make_staff, new_client, post, register
from .factories import approved_opp


def test_register_login_logout_and_password_hashing(app, session):
    c = new_client(app)
    register(c, "a@example.sa")
    assert c.get("/dashboard").status_code == 200
    u = session.scalar(select(User).where(User.email == "a@example.sa"))
    assert u.password_hash.startswith("scrypt$") and "correct-horse-1" not in u.password_hash
    r = post(c, "/logout")
    assert r.status_code == 303
    assert c.get("/dashboard").headers["location"].startswith("/login")
    login(c, "a@example.sa", "correct-horse-1")
    assert c.get("/dashboard").status_code == 200


def test_wrong_password_and_duplicate_email(app):
    c = new_client(app)
    register(c, "b@example.sa")
    c2 = new_client(app)
    r = c2.post("/login", data={"csrf": csrf_of(c2, "/login"), "email": "b@example.sa", "password": "wrong-password"})
    assert r.status_code == 400
    r = c2.post("/register", data={"csrf": csrf_of(c2, "/register"), "name": "x", "email": "b@example.sa",
                                   "password": "another-pass-1", "agree": "1"})
    assert r.status_code == 400 and "يوجد حساب" in r.text


def test_csrf_required(app):
    c = new_client(app)
    register(c, "c@example.sa")
    assert c.post("/logout", data={}).status_code == 403
    assert c.post("/logout", data={"csrf": "forged"}).status_code == 403


def test_login_rate_limited(app):
    c = new_client(app)
    token = csrf_of(c, "/login")
    codes = [c.post("/login", data={"csrf": token, "email": "x@example.sa", "password": "nope-nope-1"}).status_code
             for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429


def test_security_headers_and_no_inline_script(app):
    c = new_client(app)
    r = c.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert not re.search(r"<script(?![^>]*\bsrc=)", r.text), "inline <script> would be blocked by CSP"
    assert ' style="' not in r.text, "inline style attribute would be blocked by CSP"


def test_open_redirect_blocked(app):
    c = new_client(app)
    register(c, "d@example.sa")
    post(c, "/logout")
    token = csrf_of(c, "/login")
    r = c.post("/login", data={"csrf": token, "email": "d@example.sa", "password": "correct-horse-1",
                               "next": "//evil.example.com/x"})
    assert r.headers["location"] == "/dashboard"


def test_user_a_cannot_read_or_change_user_b_data(app, session):
    opp = approved_opp(session)
    a, b = new_client(app), new_client(app)
    register(a, "owner@example.sa")
    register(b, "intruder@example.sa")
    post(a, f"/radar/{opp.id}/save")
    pipe = session.scalar(select(PipelineItem))
    post(a, f"/pipeline/{pipe.id}/quote")
    quote = session.scalar(select(QuoteDraft))
    post(a, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    order = session.scalar(select(Order))

    for path in (f"/pipeline/{pipe.id}", f"/quotes/{quote.id}", f"/quotes/{quote.id}/xlsx", f"/orders/{order.id}",
                 f"/checkout/{order.id}"):
        assert a.get(path).status_code == 200, path
        assert b.get(path).status_code == 404, path
    for path, data in ((f"/pipeline/{pipe.id}/update", {"stage": "lost"}), (f"/pipeline/{pipe.id}/log",
                       {"channel": "phone", "outcome": "x"}), (f"/quotes/{quote.id}", {"client": "hacked"}),
                       (f"/checkout/{order.id}/sandbox", {"result": "paid"})):
        assert post(b, path, data).status_code == 404, path
    session.expire_all()
    assert session.get(PipelineItem, pipe.id).stage != "lost"
    assert session.get(QuoteDraft, quote.id).client != "hacked"
    assert session.get(Order, order.id).status == "pending"
    # B's own lists never show A's rows
    assert "owner" not in b.get("/pipeline").text and f"/orders/{order.id}" not in b.get("/account/orders").text
    export = b.get("/account/export").json()
    assert export["pipeline"] == [] and export["orders"] == []


def test_roles(app, session):
    make_staff(session, "rev@example.sa", "reviewer")
    user = new_client(app)
    register(user, "plain@example.sa")
    assert user.get("/admin").status_code == 403
    assert user.get("/admin/review").status_code == 403
    rev = new_client(app)
    login(rev, "rev@example.sa", "staff-password-1")
    assert rev.get("/admin").status_code == 200
    assert rev.get("/admin/orders").status_code == 403  # money is admin-only
    assert post(rev, "/admin/orders/1/refund", csrf_from="/admin").status_code == 403


def test_account_export_and_delete(app, session):
    c = new_client(app)
    register(c, "del@example.sa")
    data = c.get("/account/export").json()
    assert data["profile"]["email"] == "del@example.sa"
    assert post(c, "/account/delete", {"password": "bad-password"}).headers["location"].startswith("/account/profile")
    r = post(c, "/account/delete", {"password": "correct-horse-1"})
    assert r.headers["location"].startswith("/")
    assert session.scalar(select(User).where(User.email == "del@example.sa")) is None


def test_internal_refresh_requires_token(app):
    c = new_client(app)
    assert c.post("/internal/jobs/refresh").status_code == 401
    assert c.post("/internal/jobs/refresh", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/internal/jobs/refresh", headers={"Authorization": "Bearer jobs-token-for-tests"})
    assert r.status_code == 200 and "runs" in r.json()


def test_private_files_not_reachable_statically(app):
    c = new_client(app)
    for path in ("/static/../products/dist/full/04_labor_cost.xlsx", "/static/%2e%2e/products/catalog.json",
                 "/products/dist/full/04_labor_cost.xlsx"):
        assert c.get(path).status_code == 404, path
