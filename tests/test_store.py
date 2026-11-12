"""Purchases, sandbox payments (signatures, idempotency, amounts), entitlements and protected downloads."""
import datetime as dt
import json
import re
import threading

import httpx
import openpyxl
import pytest
from sqlalchemy import func, select

from hub import store
from hub.config import settings
from hub.db import SessionLocal
from hub.models import DownloadToken, Entitlement, Order, Product, Subscription, User

from .conftest import new_client, post, register


def buy(c, slug="labor-cost", result="paid"):
    r = post(c, f"/buy/product/{slug}", csrf_from=f"/store/{slug}")
    assert r.status_code == 303 and r.headers["location"].startswith("/checkout/"), r.headers.get("location")
    oid = int(r.headers["location"].rsplit("/", 1)[1])
    r = post(c, f"/checkout/{oid}/sandbox", {"result": result}, csrf_from=f"/checkout/{oid}")
    assert r.headers["location"] == f"/orders/{oid}"
    return oid


def download(c, slug="labor-cost"):
    r = post(c, f"/downloads/{slug}", csrf_from="/account/orders")
    assert r.status_code == 303
    return r.headers["location"]


def test_purchase_then_download_exact_file(app, session):
    c = new_client(app)
    register(c, "buyer@example.sa")
    oid = buy(c)
    assert "مدفوع" in c.get(f"/orders/{oid}").text
    order = session.get(Order, oid)
    assert order.invoice_number and order.invoice_number.startswith("GH-")
    loc = download(c)
    assert loc.startswith("/dl/")
    r = c.get(loc)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.content == (settings.product_dir / "04_labor_cost.xlsx").read_bytes()
    assert c.get(loc).status_code == 403, "download link must be single use"


def test_no_purchase_no_download(app):
    c = new_client(app)
    register(c, "free@example.sa")
    r = post(c, "/downloads/labor-cost", csrf_from="/account/orders")
    assert "لا تملك" in r.headers["location"] or "%D9%84%D8%A7" in r.headers["location"]
    assert c.get("/dl/made-up-token").status_code == 403


def test_failed_payment_grants_nothing(app, session):
    c = new_client(app)
    register(c, "fail@example.sa")
    oid = buy(c, result="failed")
    assert session.get(Order, oid).status == "failed"
    assert session.scalar(select(func.count(Entitlement.id))) == 0


def test_token_bound_to_user_and_expiry(app, session):
    a, b = new_client(app), new_client(app)
    register(a, "a1@example.sa")
    register(b, "b1@example.sa")
    buy(a)
    loc = download(a)
    assert b.get(loc).status_code == 403, "another user must not redeem the link"
    loc2 = download(a)
    session.execute(DownloadToken.__table__.update().values(expires_at=dt.datetime(2000, 1, 1)))
    session.commit()
    assert a.get(loc2).status_code == 403
    assert a.get(loc).status_code == 403


def test_download_limit(app, monkeypatch):
    monkeypatch.setattr(settings, "download_limit_per_product", 2)
    c = new_client(app)
    register(c, "lim@example.sa")
    buy(c)
    assert c.get(download(c)).status_code == 200
    assert c.get(download(c)).status_code == 200
    r = post(c, "/downloads/labor-cost", csrf_from="/account/orders")
    assert r.headers["location"].startswith("/account/orders?msg=")


def test_refund_revokes_access(app, session):
    c = new_client(app)
    register(c, "ref@example.sa")
    oid = buy(c)
    loc = download(c)
    order = session.get(Order, oid)
    assert store.refund(session, order, None) == "refunded"
    session.commit()
    assert c.get(loc).status_code == 403, "issued link must die after refund"
    r = post(c, "/downloads/labor-cost", csrf_from="/account/orders")
    assert r.headers["location"].startswith("/account/orders?msg=")


def test_cannot_buy_twice_or_buy_blocked_product(app, session):
    c = new_client(app)
    register(c, "twice@example.sa")
    buy(c)
    r = post(c, "/buy/product/labor-cost", csrf_from="/store/labor-cost")
    assert r.headers["location"].startswith("/store/labor-cost?msg=")
    r = post(c, "/buy/product/contract-templates", csrf_from="/store/contract-templates")
    assert r.headers["location"].startswith("/store/contract-templates?msg=")
    assert session.scalar(select(func.count(Order.id))) == 1


def _pending_order(session, email="wh@example.sa"):
    u = User(email=email, name="x", password_hash="!", trades=[], cities=[])
    session.add(u)
    session.flush()
    o = store.create_order(session, u, product=session.scalar(select(Product).where(Product.slug == "quotation")))
    session.commit()
    return u, o


def test_webhook_signature_duplicates_and_amount(app, session):
    u, o = _pending_order(session)
    c = new_client(app)
    body = store.sandbox_event(o, event_id="evt_1")
    assert c.post("/payments/webhook/sandbox", content=body, headers={"x-signature": "bad"}).status_code == 400
    tampered = store.sandbox_event(o, amount=1.0, event_id="evt_2")
    r = c.post("/payments/webhook/sandbox", content=tampered, headers={"x-signature": store.sandbox_sign(tampered)})
    assert r.json()["outcome"] == "amount_mismatch"
    for expected in ("fulfilled", "duplicate_event", "duplicate_event"):
        r = c.post("/payments/webhook/sandbox", content=body, headers={"x-signature": store.sandbox_sign(body)})
        assert r.json()["outcome"] == expected
    other = store.sandbox_event(o, event_id="evt_3")  # a different event for an already-paid order
    r = c.post("/payments/webhook/sandbox", content=other, headers={"x-signature": store.sandbox_sign(other)})
    assert r.json()["outcome"] == "already_processed"
    session.expire_all()
    assert session.scalar(select(func.count(Entitlement.id)).where(Entitlement.user_id == u.id)) == 1


def test_concurrent_deliveries_fulfil_once(app, session):
    u, o = _pending_order(session, "conc@example.sa")
    outcomes = []

    def deliver(i):
        with SessionLocal() as s:
            body = store.sandbox_event(o, event_id=f"evt_c{i}")
            try:
                outcomes.append(store.handle_payment_event(s, "sandbox", store.verify_sandbox(body, store.sandbox_sign(body))))
            except Exception as e:  # sqlite may report "database is locked": the delivery is retried by the provider
                outcomes.append(type(e).__name__)
    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    session.expire_all()
    assert outcomes.count("fulfilled") == 1, outcomes
    assert session.scalar(select(func.count(Entitlement.id)).where(Entitlement.user_id == u.id)) == 1


def test_pro_subscription_unlocks_all_tools_and_extends(app, session):
    c = new_client(app)
    register(c, "pro@example.sa")
    r = post(c, "/buy/plan/pro", csrf_from="/pricing")
    oid = int(r.headers["location"].rsplit("/", 1)[1])
    post(c, f"/checkout/{oid}/sandbox", {"result": "paid"}, csrf_from=f"/checkout/{oid}")
    page = c.get("/account/orders").text
    assert len(re.findall(r'data-slug="', page)) == 7
    assert c.get(download(c, "progress-invoices")).status_code == 200
    r = post(c, "/buy/plan/pro", csrf_from="/pricing")
    oid2 = int(r.headers["location"].rsplit("/", 1)[1])
    post(c, f"/checkout/{oid2}/sandbox", {"result": "paid"}, csrf_from=f"/checkout/{oid2}")
    subs = session.scalars(select(Subscription).order_by(Subscription.id)).all()
    assert len(subs) == 2 and subs[1].starts_at == subs[0].ends_at, "renewal must extend, not overlap"


def test_sample_is_public_and_locked(app):
    c = new_client(app)
    r = c.get("/samples/labor-cost.xlsx")
    assert r.status_code == 200
    assert r.content == (settings.sample_dir / "04_labor_cost.xlsx").read_bytes()
    assert r.content != (settings.product_dir / "04_labor_cost.xlsx").read_bytes()
    assert c.get("/samples/contract-templates.xlsx").status_code == 404


def test_moyasar_verification_rereads_payment(session, monkeypatch):
    u, o = _pending_order(session, "my@example.sa")
    monkeypatch.setattr(settings, "moyasar_secret_key", "sk_test_dummy")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"id": "pay_1", "status": "paid", "amount": round(o.total * 100),
                                         "currency": "SAR", "metadata": {"order_id": str(o.id)}})
    vp = store.verify_moyasar("pay_1", "evt_m1", transport=httpx.MockTransport(handler))
    assert seen["auth"].startswith("Basic ")
    assert store.handle_payment_event(session, "moyasar", vp) == "fulfilled"


def test_moyasar_live_key_refused_until_enabled(monkeypatch):
    monkeypatch.setattr(settings, "moyasar_secret_key", "sk_live_dummy")
    monkeypatch.setattr(settings, "payments_live_enabled", False)
    with pytest.raises(store.StoreError):
        store.moyasar_fetch_payment("pay_1", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def test_moyasar_webhook_token(app):
    c = new_client(app)
    r = c.post("/payments/webhook/moyasar", content=json.dumps({"secret_token": "wrong", "data": {"id": "x"}}))
    assert r.status_code == 401


def test_quote_from_opportunity_and_xlsx(app, session):
    from .factories import approved_opp
    opp = approved_opp(session)
    c = new_client(app)
    register(c, "q@example.sa")
    r = post(c, f"/radar/{opp.id}/save")
    pipe_loc = r.headers["location"].split("?")[0]
    r = post(c, f"{pipe_loc}/quote", csrf_from=pipe_loc)
    qloc = r.headers["location"].split("?")[0]
    page = c.get(qloc).text
    assert "دهان" in page and "بلاط" in page, "starter lines follow the opportunity trades"
    data = {"client": "مقاول رئيسي", "project": opp.title, "validity_days": "20", "vat_rate": "15", "discount": "100",
            "desc": ["دهان", "بلاط"], "unit": ["م²", "م²"], "qty": ["100", "50"], "unit_price": ["26", "60"]}
    post(c, qloc, data, csrf_from=qloc)
    page = c.get(qloc).text
    assert "5,600.00" in page and "5,500.00" in page and "6,325.00" in page  # 2600+3000=5600, -100, +15%
    x = c.get(f"{qloc}/xlsx")
    assert x.status_code == 200
    import io
    ws = openpyxl.load_workbook(io.BytesIO(x.content)).worksheets[1]
    assert ws["B6"].value == "مقاول رئيسي" and ws["D10"].value == 100 and ws["E11"].value == 60 and ws["F36"].value == 100
