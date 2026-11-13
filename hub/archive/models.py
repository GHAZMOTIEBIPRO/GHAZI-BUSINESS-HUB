"""Archive schema (Alembic-managed; see migrations/versions/)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..db import utcnow

NAMING = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
          "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
          "pk": "pk_%(table_name)s"}


class ArchiveBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# ------------------------------------------------------------------ vocabularies
TENDER_STATUS = {
    "announced": "معلنة (حالة وقت الجمع)",
    "open": "مفتوحة",
    "closed": "مغلقة",
    "awarded": "تمت الترسية",
    "cancelled": "ملغاة",
    "unknown": "غير معلوم",
}
# A/B/C/D separation required by the owner; D is never set by an import.
LIFECYCLE = {
    "A_open": "منافسة مفتوحة",
    "B_closed": "منافسة مغلقة",
    "C_awarded": "مشروع مُرسى (أرشيف)",
    "D_subcontract_confirmed": "فرصة باطن مؤكدة",
}
RIGHTS = {
    "verified_open_license": "ترخيص بيانات مفتوحة موثق",
    "licensed_private": "ترخيص خاص موقّع",
    "unverified": "غير متحقق — خاص بالمراجعة",
    "restricted": "مقيد — بالرابط فقط",
    "synthetic_test": "بيانات اختبار اصطناعية",
}
VISIBILITY = {
    "private_staging": "منطقة استيراد خاصة",
    "archive_private": "أرشيف خاص (فريق المنصة)",
    "public": "منشور للعموم",
}
IDENTITY = {
    "name_only_unverified": "اسم كما نُشر فقط — الهوية غير متحقق منها",
    "registry_verified": "متحقق بالسجل التجاري / الرقم الموحد",
}


class ProcurementSource(ArchiveBase):
    __tablename__ = "procurement_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    access_method: Mapped[str] = mapped_column(String(40))  # api | file_download | licensed_upload | manual | link_only | research_package | synthetic
    official_api: Mapped[str] = mapped_column(String(40), default="unknown")  # yes | no | coming_soon | unknown
    export_available: Mapped[str] = mapped_column(String(40), default="unknown")
    commercial_reuse: Mapped[str] = mapped_column(String(40), default="unknown")  # verified | restricted | requires_agreement | unknown
    requires_subscription: Mapped[str] = mapped_column(String(40), default="unknown")
    record_types: Mapped[str] = mapped_column(String(300), default="")
    years_covered: Mapped[str] = mapped_column(String(100), default="")
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_status: Mapped[str] = mapped_column(String(40), default="unverified")
    evidence_url: Mapped[str] = mapped_column(String(500), default="")
    last_probe_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_probe_result: Mapped[str] = mapped_column(String(500), default="never probed")
    notes: Mapped[str] = mapped_column(Text, default="")
    licenses: Mapped[list[SourceLicense]] = relationship(back_populates="source")


class SourceLicense(ArchiveBase):
    __tablename__ = "source_licenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("procurement_sources.id", ondelete="CASCADE"), index=True)
    license_name: Mapped[str] = mapped_column(String(200))
    license_url: Mapped[str] = mapped_column(String(500), default="")
    allows_storage: Mapped[str] = mapped_column(String(20), default="unknown")
    allows_public_display: Mapped[str] = mapped_column(String(20), default="unknown")
    allows_commercial: Mapped[str] = mapped_column(String(20), default="unknown")
    status: Mapped[str] = mapped_column(String(40), default="unverified")  # unverified | verified | rejected | requested
    evidence: Mapped[str] = mapped_column(Text, default="")
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    verified_by: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[ProcurementSource] = relationship(back_populates="licenses")


class ImportBatch(ArchiveBase):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("procurement_sources.id"), index=True)
    package_sha256: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="in_progress")  # in_progress | staged | committed | rolled_back | failed
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(200), default="cli")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)


class ImportReject(ArchiveBase):
    __tablename__ = "import_rejects"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    file: Mapped[str] = mapped_column(String(100))
    row_number: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(10))  # reject | warning
    reason: Mapped[str] = mapped_column(String(300))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class ProcurementIssuer(ArchiveBase):
    __tablename__ = "procurement_issuers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    name_key: Mapped[str] = mapped_column(String(300), unique=True)
    sector: Mapped[str] = mapped_column(String(100), default="")
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))


class Company(ArchiveBase):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(300))
    name_key: Mapped[str] = mapped_column(String(300), unique=True)  # exact name after whitespace/diacritic cleanup only
    fold_key: Mapped[str] = mapped_column(String(300), index=True)  # looser key: flags *possible* duplicates, never merges
    cr_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    unified_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    identity_status: Mapped[str] = mapped_column(String(30), default="name_only_unverified")
    specialties: Mapped[list] = mapped_column(JSON, default=list)
    cities: Mapped[list] = mapped_column(JSON, default=list)
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    aliases: Mapped[list[CompanyAlias]] = relationship(back_populates="company", cascade="all, delete-orphan")
    bids: Mapped[list[TenderBid]] = relationship(back_populates="company")
    awards: Mapped[list[TenderAward]] = relationship(back_populates="company")


class CompanyAlias(ArchiveBase):
    __tablename__ = "company_aliases"
    __table_args__ = (UniqueConstraint("company_id", "alias_raw"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    alias_raw: Mapped[str] = mapped_column(String(300))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("procurement_sources.id", ondelete="SET NULL"))
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))
    company: Mapped[Company] = relationship(back_populates="aliases")


class ProcurementTender(ArchiveBase):
    __tablename__ = "procurement_tenders"
    __table_args__ = (
        Index("ix_tender_status_year", "status", "published_date"),
        Index("ix_tender_city", "city"),
        Index("ix_tender_award_date", "award_date"),
        Index("ix_tender_visibility", "visibility", "is_synthetic"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    official_reference: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("procurement_sources.id"), index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("procurement_issuers.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    sector: Mapped[str] = mapped_column(String(100), default="")
    city: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(100))
    work_types: Mapped[list] = mapped_column(JSON, default=list)
    # same trades as plain text ("|"-joined): JSON stores Arabic as \\u escapes, which LIKE cannot match
    work_types_text: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    status_as_published: Mapped[str] = mapped_column(String(100), default="")
    lifecycle: Mapped[str] = mapped_column(String(30), default="B_closed")
    published_date: Mapped[dt.date | None] = mapped_column(Date)
    submission_deadline: Mapped[dt.date | None] = mapped_column(Date)
    award_date: Mapped[dt.date | None] = mapped_column(Date)
    estimated_value_sar: Mapped[float | None] = mapped_column(Float)
    offer_count_claimed: Mapped[int | None] = mapped_column(Integer)
    bidders_known: Mapped[int] = mapped_column(Integer, default=0)
    bid_list_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    completeness: Mapped[float] = mapped_column(Float, default=0.0)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    source_publisher: Mapped[str] = mapped_column(String(200), default="")
    source_rights_status: Mapped[str] = mapped_column(String(30), default="unverified")
    publish_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility: Mapped[str] = mapped_column(String(20), default="private_staging")
    review_status: Mapped[str] = mapped_column(String(30), default="pending_rights_review")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_notes: Mapped[str] = mapped_column(Text, default="")
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"), index=True)
    last_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    issuer: Mapped[ProcurementIssuer | None] = relationship()
    source: Mapped[ProcurementSource] = relationship()
    bids: Mapped[list[TenderBid]] = relationship(back_populates="tender", cascade="all, delete-orphan")
    awards: Mapped[list[TenderAward]] = relationship(back_populates="tender", cascade="all, delete-orphan")


class TenderBid(ArchiveBase):
    __tablename__ = "tender_bids"
    __table_args__ = (UniqueConstraint("tender_id", "bidder_name_raw"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("procurement_tenders.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    bidder_name_raw: Mapped[str] = mapped_column(String(300))
    bid_amount_sar: Mapped[float | None] = mapped_column(Float)
    bid_amount_raw: Mapped[str] = mapped_column(String(60), default="")
    technical_result: Mapped[str] = mapped_column(String(100), default="")
    bid_status: Mapped[str] = mapped_column(String(100), default="")
    # rank by price among the *published* bids only (1 = lowest); not a claim about the full field or the winner
    price_rank_among_published: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    tender: Mapped[ProcurementTender] = relationship(back_populates="bids")
    company: Mapped[Company] = relationship(back_populates="bids")


class TenderAward(ArchiveBase):
    __tablename__ = "tender_awards"
    __table_args__ = (UniqueConstraint("tender_id", "winner_name_raw"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("procurement_tenders.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    winner_name_raw: Mapped[str] = mapped_column(String(300))
    award_amount_sar: Mapped[float | None] = mapped_column(Float)
    award_amount_raw: Mapped[str] = mapped_column(String(60), default="")
    award_date: Mapped[dt.date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(40), default="secondary_source_unverified")
    source_url: Mapped[str] = mapped_column(String(500), default="")
    first_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    tender: Mapped[ProcurementTender] = relationship(back_populates="awards")
    company: Mapped[Company] = relationship(back_populates="awards")


class VerificationEvent(ArchiveBase):
    __tablename__ = "verification_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    event: Mapped[str] = mapped_column(String(60))
    result: Mapped[str] = mapped_column(String(300), default="")
    source_url: Mapped[str] = mapped_column(String(500), default="")
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ChangeHistory(ArchiveBase):
    """Every value an import overwrites is kept here (status changes, re-published amounts...)."""
    __tablename__ = "change_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    field: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
