"""Plans, orders, payments, entitlements and protected downloads.

Payment flow (provider-agnostic):
  create_order -> provider checkout -> signed event -> handle_payment_event (idempotent, amount-checked) -> fulfil
Real money is impossible unless HUB_PAYMENTS_LIVE_ENABLED=true AND a live provider key is configured.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .models import PLANS, AuditLog, DownloadLog, DownloadToken, Entitlement, Order, PaymentEvent, Product, Subscription, User

SUB_DAYS = 30


class StoreError(Exception):
    pass


# ----------------------------------------------------------------- plans & entitlements
def active_subscription(db: Session, user: User, now: dt.datetime | None = None) -> Subscription | None:
    now = now or utcnow()
    return db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.revoked.is_(False),
                                                Subscription.starts_at <= now, Subscription.ends_at > now)
                     .order_by(Subscription.ends_at.desc()))


def active_plan(db: Session, user: User | None) -> dict:
    if user is None:
        return {"key": "anon", **PLANS["free"], "title": "زائر"}
    sub = active_subscription(db, user)
    key = sub.plan if sub else "free"
    return {"key": key, **PLANS[key], "ends_at": sub.ends_at if sub else None}


def owns_product(db: Session, user: User, product: Product) -> bool:
    return db.scalar(select(func.count(Entitlement.id)).join(Order, Order.id == Entitlement.order_id).where(
        Entitlement.user_id == user.id, Entitlement.product_id == product.id, Entitlement.revoked.is_(False),
        Order.status == "paid")) > 0


def can_download(db: Session, user: User, product: Product) -> bool:
    if product.status != "ready" or not product.file_stem:
        return False
    return owns_product(db, user, product) or active_plan(db, user)["all_tools"]


# ----------------------------------------------------------------- pricing
def price_breakdown(amount_before_vat: float) -> tuple[float, float, float]:
    vat = round(amount_before_vat * settings.vat_rate, 2) if settings.vat_registered else 0.0
    return round(amount_before_vat, 2), vat, round(amount_before_vat + vat, 2)


def create_order(db: Session, user: User, *, product: Product | None = None, plan: str | None = None) -> Order:
    if product is not None:
        if product.status != "ready" or product.price is None:
            raise StoreError("المنتج غير متاح للبيع")
        if owns_product(db, user, product):
            raise StoreError("تملك هذا المنتج مسبقًا")
        base, kind = product.price, "product"
    elif plan in ("radar", "pro"):
        base, kind = PLANS[plan]["price"], "subscription"
    else:
        raise StoreError("طلب غير صالح")
    b, v, t = price_breakdown(base)
    order = Order(user_id=user.id, kind=kind, product_id=product.id if product else None, plan=plan,
                  amount_before_vat=b, vat_amount=v, total=t, provider=settings.payment_provider)
    db.add(order)
    db.flush()
    return order


# ----------------------------------------------------------------- providers
@dataclass
class VerifiedPayment:
    event_id: str
    payment_id: str
    order_id: int
    status: str  # paid | failed | refunded
    amount_halalas: int
    currency: str


def sandbox_sign(body: bytes) -> str:
    return hmac.new(settings.sandbox_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def sandbox_event(order: Order, status: str = "paid", amount: float | None = None, event_id: str | None = None) -> bytes:
    return json.dumps({"event_id": event_id or f"evt_{secrets.token_hex(8)}", "payment_id": f"sbx_{order.id}",
                       "order_id": order.id, "status": status,
                       "amount": round((order.total if amount is None else amount) * 100), "currency": "SAR"}).encode()


def verify_sandbox(body: bytes, signature: str) -> VerifiedPayment:
    if settings.env == "production":
        raise StoreError("sandbox disabled in production")
    if not signature or not hmac.compare_digest(sandbox_sign(body), signature):
        raise StoreError("توقيع غير صالح")
    d = json.loads(body)
    return VerifiedPayment(str(d["event_id"]), str(d["payment_id"]), int(d["order_id"]), str(d["status"]),
                           int(d["amount"]), str(d["currency"]))


MOYASAR_API = "https://api.moyasar.com/v1"


def moyasar_fetch_payment(payment_id: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Never trust a callback body: re-read the payment from Moyasar with the secret key."""
    key = settings.moyasar_secret_key
    if not key:
        raise StoreError("Moyasar غير مهيأ")
    if key.startswith("sk_live_") and not settings.payments_live_enabled:
        raise StoreError("مفتاح حي مرفوض: المدفوعات الحقيقية غير مفعلة")
    with httpx.Client(timeout=15, transport=transport, auth=(key, "")) as c:
        r = c.get(f"{MOYASAR_API}/payments/{payment_id}")
    if r.status_code != 200:
        raise StoreError(f"تعذر التحقق من الدفع ({r.status_code})")
    return r.json()


def verify_moyasar(payment_id: str, event_id: str, transport=None) -> VerifiedPayment:
    p = moyasar_fetch_payment(payment_id, transport)
    meta = p.get("metadata") or {}
    status = {"paid": "paid", "failed": "failed", "refunded": "refunded"}.get(p.get("status"), "other")
    return VerifiedPayment(event_id, str(p["id"]), int(meta.get("order_id", 0)), status, int(p.get("amount", 0)),
                           str(p.get("currency", "")))


# ----------------------------------------------------------------- event handling
def handle_payment_event(db: Session, provider: str, vp: VerifiedPayment) -> str:
    try:
        with db.begin_nested():
            evt = PaymentEvent(provider=provider, event_id=vp.event_id, order_id=vp.order_id, outcome="processing")
            db.add(evt)
            db.flush()
    except IntegrityError:
        return "duplicate_event"
    order = db.get(Order, vp.order_id)
    if order is None:
        evt.outcome = "unknown_order"
        db.commit()
        return evt.outcome
    if vp.currency != "SAR" or vp.amount_halalas != round(order.total * 100):
        evt.outcome = "amount_mismatch"
        db.add(AuditLog(action="payment.amount_mismatch", target=f"order:{order.id}",
                        detail=f"{vp.amount_halalas} {vp.currency} vs {order.total}"))
        db.commit()
        return evt.outcome
    if vp.status == "paid":
        evt.outcome = fulfil(db, order, vp.payment_id)
    elif vp.status == "failed":
        db.execute(update(Order).where(Order.id == order.id, Order.status == "pending").values(status="failed"))
        evt.outcome = "failed"
    elif vp.status == "refunded":
        evt.outcome = refund(db, order, actor=None)
    else:
        evt.outcome = "ignored"
    db.commit()
    return evt.outcome


def _next_invoice_number(db: Session) -> str:
    n = db.scalar(select(func.count(Order.id)).where(Order.invoice_number.is_not(None))) + 1
    return f"GH-{utcnow():%Y}-{n:06d}"


def fulfil(db: Session, order: Order, payment_id: str) -> str:
    # conditional update = only one concurrent delivery can move pending -> paid
    res = db.execute(update(Order).where(Order.id == order.id, Order.status.in_(["pending", "failed"]))
                     .values(status="paid", paid_at=utcnow(), provider_payment_id=payment_id))
    if res.rowcount == 0:
        return "already_processed"
    db.refresh(order)
    for _ in range(3):
        try:
            with db.begin_nested():
                order.invoice_number = _next_invoice_number(db)
                db.flush()
            break
        except IntegrityError:
            continue
    if order.kind == "product":
        db.add(Entitlement(user_id=order.user_id, product_id=order.product_id, order_id=order.id))
    else:
        user = db.get(User, order.user_id)
        cur = active_subscription(db, user)
        start = max(utcnow(), cur.ends_at) if cur else utcnow()
        db.add(Subscription(user_id=order.user_id, plan=order.plan, starts_at=start,
                            ends_at=start + dt.timedelta(days=SUB_DAYS), order_id=order.id))
    db.add(AuditLog(action="order.paid", target=f"order:{order.id}", detail=payment_id))
    db.flush()
    return "fulfilled"


def refund(db: Session, order: Order, actor: User | None) -> str:
    if order.status != "paid":
        return "not_paid"
    order.status = "refunded"
    db.execute(update(Entitlement).where(Entitlement.order_id == order.id).values(revoked=True))
    db.execute(update(Subscription).where(Subscription.order_id == order.id).values(revoked=True))
    db.add(AuditLog(actor_id=actor.id if actor else None, action="order.refunded", target=f"order:{order.id}"))
    db.flush()
    return "refunded"


# ----------------------------------------------------------------- downloads
def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def downloads_used(db: Session, user: User, product: Product) -> int:
    return db.scalar(select(func.count(DownloadLog.id)).where(DownloadLog.user_id == user.id,
                                                             DownloadLog.product_id == product.id))


def issue_download_token(db: Session, user: User, product: Product) -> str:
    if not can_download(db, user, product):
        raise StoreError("لا تملك صلاحية تنزيل هذا الملف")
    if downloads_used(db, user, product) >= settings.download_limit_per_product:
        raise StoreError("تجاوزت حد التنزيلات لهذا المنتج؛ تواصل مع الدعم")
    t = secrets.token_urlsafe(32)
    db.add(DownloadToken(token_hash=_h(t), user_id=user.id, product_id=product.id,
                         expires_at=utcnow() + dt.timedelta(seconds=settings.download_token_ttl_seconds)))
    db.flush()
    return t


def redeem_download_token(db: Session, user: User, token: str) -> Product:
    row = db.scalar(select(DownloadToken).where(DownloadToken.token_hash == _h(token)))
    if row is None or row.user_id != user.id:
        raise StoreError("رابط غير صالح")
    if row.expires_at < utcnow():
        raise StoreError("انتهت صلاحية الرابط")
    res = db.execute(update(DownloadToken).where(DownloadToken.id == row.id, DownloadToken.used_at.is_(None))
                     .values(used_at=utcnow()))
    if res.rowcount == 0:
        raise StoreError("استُخدم هذا الرابط مسبقًا")
    product = db.get(Product, row.product_id)
    if not can_download(db, user, product):  # refund / expiry between issue and redeem
        raise StoreError("لم تعد تملك صلاحية هذا الملف")
    db.add(DownloadLog(user_id=user.id, product_id=product.id))
    db.flush()
    return product
