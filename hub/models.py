"""Data model. Every user-owned row carries user_id and every query for it filters on it (see tests/test_isolation.py)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# ----------------------------------------------------------------- vocabularies
OPPORTUNITY_TYPES = {
    "OPPORTUNITY_LEAD": "مؤشر فرصة غير مؤكد",
    "OPEN_TENDER": "منافسة مفتوحة",
    "AWARDED_PROJECT": "مشروع مُرسى (قد تنشأ منه أعمال باطن)",
    "SUBCONTRACT_CONFIRMED": "طلب مقاول باطن مؤكد",
    "CLOSED": "مغلقة",
    "UNKNOWN": "غير معلوم",
}
REVIEW_STATES = {
    "pending_review": "بانتظار المراجعة",
    "approved": "معتمدة للنشر",
    "rejected": "مرفوضة",
    "stale": "تحتاج إعادة تحقق",
}
SOURCE_RIGHTS = {
    "verified_public_reuse": "إعادة استخدام عامة موثقة",
    "licensed_private": "ترخيص خاص",
    "link_only": "الإشارة بالرابط فقط",
    "unknown": "غير متحقق — لا جلب آلي",
    "restricted": "مقيّد — لا جلب آلي",
}
TRADES = ["الجبس والأسقف المستعارة", "البلاط والسيراميك", "الدهانات", "الأبواب", "إعادة تأهيل المباني والمساكن",
          "الصيانة المدنية والمعمارية"]
CITIES = {  # city -> region
    "الرياض": "الرياض", "الخرج": "الرياض", "جدة": "مكة المكرمة", "مكة المكرمة": "مكة المكرمة", "الطائف": "مكة المكرمة",
    "المدينة المنورة": "المدينة المنورة", "ينبع": "المدينة المنورة", "الدمام": "الشرقية", "الخبر": "الشرقية",
    "الجبيل": "الشرقية", "الأحساء": "الشرقية", "بريدة": "القصيم", "أبها": "عسير", "خميس مشيط": "عسير",
    "تبوك": "تبوك", "حائل": "حائل", "جازان": "جازان", "نجران": "نجران", "الباحة": "الباحة", "عرعر": "الحدود الشمالية",
    "سكاكا": "الجوف",
}
PIPELINE_STAGES = {
    "saved": "محفوظة", "studying": "قيد الدراسة", "pricing": "قيد التسعير", "quote_sent": "أُرسل العرض",
    "negotiating": "تفاوض", "won": "تمت الترسية علينا", "executing": "قيد التنفيذ", "lost": "لم نُوفّق", "dropped": "استُبعدت",
}
PLANS = {
    "free": {"title": "مجاني", "price": 0, "radar_contacts": False, "alerts": False, "all_tools": False},
    "radar": {"title": "رادار المقاول", "price": 199, "radar_contacts": True, "alerts": True, "all_tools": False},
    "pro": {"title": "احترافي", "price": 399, "radar_contacts": True, "alerts": True, "all_tools": True},
}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(120))
    company: Mapped[str] = mapped_column(String(160), default="")
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | reviewer | admin
    trades: Mapped[list] = mapped_column(JSON, default=list)
    cities: Mapped[list] = mapped_column(JSON, default=list)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)  # replay protection
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254))  # the address being proven; stale if the account e-mail changes
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(20))
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    file_stem: Mapped[str | None] = mapped_column(String(80), nullable=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    features: Mapped[list] = mapped_column(JSON, default=list)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # before VAT
    status: Mapped[str] = mapped_column(String(30))  # ready | blocked_legal_review | draft
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # product | subscription
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    amount_before_vat: Mapped[float] = mapped_column(Float)
    vat_amount: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | paid | failed | refunded | cancelled
    provider: Mapped[str] = mapped_column(String(20))
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    product: Mapped[Product | None] = relationship()


class PaymentEvent(Base):
    """Every webhook/callback seen, keyed by provider event id -> idempotency."""
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(160))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("provider", "event_id"),)


class Entitlement(Base):
    __tablename__ = "entitlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "product_id", "order_id"),)


class DownloadToken(Base):
    __tablename__ = "download_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class DownloadLog(Base):
    __tablename__ = "download_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ----------------------------------------------------------------- radar
class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    homepage: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))  # manual | json_feed | rss | csv_import
    feed_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rights: Mapped[str] = mapped_column(String(30), default="unknown")
    rights_evidence: Mapped[str] = mapped_column(Text, default="")
    rights_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    trust: Mapped[str] = mapped_column(String(20), default="review_all")  # review_all | auto_publish
    refresh_hours: Mapped[int] = mapped_column(Integer, default=24)
    verify_ttl_days: Mapped[int] = mapped_column(Integer, default=7)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")


class FetchRun(Base):
    __tablename__ = "fetch_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(30))  # success | failed | gated | skipped_not_due
    reason: Mapped[str] = mapped_column(Text, default="")
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    items_to_review: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[Source] = relationship()


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    # "source": text as published by the source (shown only if the source's rights allow republication)
    # "reviewer": written by our reviewer in their own words (always publishable)
    summary_origin: Mapped[str] = mapped_column(String(20), default="source")
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    opportunity_type: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    owner_entity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    main_contractor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    responsible_company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # see radar.contact_ok
    source_url: Mapped[str] = mapped_column(String(800))
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    review_due_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    review_state: Mapped[str] = mapped_column(String(20), default="pending_review")
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{url, checked_at, finding}]
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[Source] = relationship()
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)


class ErrorReport(Base):
    __tablename__ = "error_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[str] = mapped_column(String(40))
    details: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ----------------------------------------------------------------- contractor workspace
class PipelineItem(Base):
    __tablename__ = "pipeline_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(20), default="saved")
    notes: Mapped[str] = mapped_column(Text, default="")
    next_action_at: Mapped[dt.date | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    opportunity: Mapped[Opportunity] = relationship()
    __table_args__ = (UniqueConstraint("user_id", "opportunity_id"),)


class ContactLog(Base):
    __tablename__ = "contact_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipeline_items.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    outcome: Mapped[str] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class QuoteDraft(Base):
    __tablename__ = "quote_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_items.id", ondelete="SET NULL"), nullable=True)
    client: Mapped[str] = mapped_column(String(200), default="")
    project: Mapped[str] = mapped_column(String(300), default="")
    lines: Mapped[list] = mapped_column(JSON, default=list)  # [{desc, unit, qty, unit_price}]
    discount: Mapped[float] = mapped_column(Float, default=0)
    vat_rate: Mapped[float] = mapped_column(Float, default=0.15)
    validity_days: Mapped[int] = mapped_column(Integer, default=15)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    link: Mapped[str] = mapped_column(String(300), default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "kind", "link"),)


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    to: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
