"""Manual curation of the archive and the one automation that needs no outside source.

* refresh_lifecycle: an open tender whose own published deadline has passed becomes B_closed. Nothing is fetched;
  the rule only uses dates already stored. Every change goes to change_history under one verification event.
* verify_identity: an admin records a CR / unified number with an evidence link. A name is never enough.
* merge_companies: two rows become one ONLY when the number typed by the admin equals the number already verified
  on the target. Every moved bid, award and alias is recorded, so unmerge_company restores the previous state.
* subcontract opportunities (lifecycle D): entered with evidence by one staff member, confirmed by another one.
  Nothing here reads tenders or awards to create them.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from . import normalize as N
from .models import (
    EVIDENCE_KINDS,
    ChangeHistory,
    Company,
    CompanyAlias,
    ProcurementTender,
    SubcontractOpportunity,
    TenderAward,
    TenderBid,
    VerificationEvent,
)

CR_RE = re.compile(r"^\d{10}$")             # Saudi commercial registration: 10 digits
UNIFIED_RE = re.compile(r"^7\d{9}$")        # unified national number: 10 digits starting with 7


class CurationError(ValueError):
    def __init__(self, message: str, conflict_company_id: int | None = None):
        super().__init__(message)
        self.conflict_company_id = conflict_company_id


def _digits(value: str | None) -> str:
    return N.clean_text(value or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace(" ", "")


def _https(url: str | None) -> str:
    url = (url or "").strip()
    if not url.startswith("https://") or len(url) > 500 or any(c.isspace() for c in url):
        raise CurationError("رابط الدليل مطلوب ويجب أن يبدأ بـ https://")
    return url


def _event(s: Session, entity_type: str, entity_id: int, event: str, result: str = "", url: str = "") -> VerificationEvent:
    ev = VerificationEvent(entity_type=entity_type, entity_id=entity_id, event=event, result=result[:300], source_url=url[:500])
    s.add(ev)
    s.flush()
    return ev


def _history(s: Session, ev: VerificationEvent, entity_type: str, entity_id: int, field: str, old, new) -> None:
    s.add(ChangeHistory(entity_type=entity_type, entity_id=entity_id, field=field, event_id=ev.id,
                        old_value=None if old is None else str(old), new_value=None if new is None else str(new)))


# ------------------------------------------------------------------ lifecycle (automatic, no outside source)
def refresh_lifecycle(s: Session, today: dt.date) -> dict:
    """A_open tenders past their own deadline become B_closed. Awarded/closed rows and D are never touched."""
    due = s.scalars(select(ProcurementTender).where(ProcurementTender.lifecycle == "A_open",
                                                    ProcurementTender.submission_deadline < today)).all()
    if not due:
        return {"archive_closed": 0}
    ev = _event(s, "archive", 0, "lifecycle_deadline_passed", f"{len(due)} tenders, today={today.isoformat()}")
    for t in due:
        _history(s, ev, "tender", t.id, "lifecycle", t.lifecycle, "B_closed")
        t.lifecycle = "B_closed"
    s.flush()
    return {"archive_closed": len(due)}


# ------------------------------------------------------------------ identity
def verify_identity(s: Session, company_id: int, *, cr_number: str = "", unified_number: str = "",
                    evidence_url: str, by: str) -> Company:
    c = s.get(Company, company_id)
    if c is None:
        raise CurationError("الشركة غير موجودة")
    if c.merged_into_id:
        raise CurationError("هذا السجل مدموج في كيان آخر؛ عدّل الكيان الأصلي")
    cr, uni = _digits(cr_number), _digits(unified_number)
    if not cr and not uni:
        raise CurationError("أدخل رقم السجل التجاري أو الرقم الموحد")
    if cr and not CR_RE.match(cr):
        raise CurationError("رقم السجل التجاري يتكون من 10 أرقام")
    if uni and not UNIFIED_RE.match(uni):
        raise CurationError("الرقم الموحد يتكون من 10 أرقام ويبدأ بـ 7")
    url = _https(evidence_url)
    for column, value in (("cr_number", cr), ("unified_number", uni)):
        if not value:
            continue
        other = s.scalars(select(Company).where(getattr(Company, column) == value, Company.id != c.id)).first()
        if other is not None:
            raise CurationError("هذا الرقم مسجل لكيان آخر. إن كان الكيان نفسه فاستخدم الدمج بنفس الرقم.", other.id)
        current = getattr(c, column)
        if current and current != value:
            raise CurationError("لهذه الشركة رقم مختلف مسجل مسبقًا؛ لا يُستبدل رقم متحقق منه من هذه الشاشة")
    ev = _event(s, "company", c.id, "identity_verified", f"by {by}", url)
    for column, value in (("cr_number", cr), ("unified_number", uni)):
        if value and getattr(c, column) != value:
            _history(s, ev, "company", c.id, column, getattr(c, column), value)
            setattr(c, column, value)
    _history(s, ev, "company", c.id, "identity_status", c.identity_status, "registry_verified")
    c.identity_status = "registry_verified"
    s.flush()
    return c


def merge_companies(s: Session, source_id: int, target_id: int, *, registry_number: str, evidence_url: str,
                    by: str) -> VerificationEvent:
    """Move bids, awards and aliases of `source` to `target`. Allowed only on an identical verified number."""
    if source_id == target_id:
        raise CurationError("لا يمكن دمج الكيان في نفسه")
    src, tgt = s.get(Company, source_id), s.get(Company, target_id)
    if src is None or tgt is None:
        raise CurationError("الشركة غير موجودة")
    if src.merged_into_id or tgt.merged_into_id:
        raise CurationError("أحد السجلين مدموج مسبقًا")
    number = _digits(registry_number)
    target_numbers = {n for n in (tgt.cr_number, tgt.unified_number) if n}
    if tgt.identity_status != "registry_verified" or number not in target_numbers:
        raise CurationError("الدمج يتطلب أن يكون الرقم نفسه متحققًا منه على الكيان الهدف")
    for own in (src.cr_number, src.unified_number):
        if own and own not in target_numbers:
            raise CurationError("للسجل المصدر رقم مختلف؛ هذان كيانان مختلفان ولا يُدمجان")
    url = _https(evidence_url)
    ev = _event(s, "company", src.id, "merged_into", f"target={tgt.id} by {by}", url)
    for model, kind in ((TenderBid, "tender_bid"), (TenderAward, "tender_award")):
        for row in s.scalars(select(model).where(model.company_id == src.id)):
            _history(s, ev, kind, row.id, "company_id", src.id, tgt.id)
            row.company_id = tgt.id
    existing = {a.alias_raw for a in tgt.aliases}
    for alias in list(src.aliases):
        if alias.alias_raw in existing:
            continue
        s.add(CompanyAlias(company_id=tgt.id, alias_raw=alias.alias_raw, source_id=alias.source_id,
                           first_batch_id=alias.first_batch_id))
        _history(s, ev, "company_alias_added", tgt.id, "alias_raw", None, alias.alias_raw)
    _history(s, ev, "company", src.id, "identity_status", src.identity_status, "merged_same_registry")
    src.identity_status, src.merged_into_id = "merged_same_registry", tgt.id
    tgt.specialties = sorted(set(tgt.specialties or []) | set(src.specialties or []))
    tgt.cities = sorted(set(tgt.cities or []) | set(src.cities or []))
    s.flush()
    return ev


def unmerge_company(s: Session, source_id: int, *, by: str) -> int:
    """Undo the latest merge of `source`: move back exactly the rows that merge moved."""
    src = s.get(Company, source_id)
    if src is None or not src.merged_into_id:
        raise CurationError("هذا السجل غير مدموج")
    merge = s.scalars(select(VerificationEvent).where(VerificationEvent.entity_type == "company",
                                                      VerificationEvent.entity_id == src.id,
                                                      VerificationEvent.event == "merged_into")
                      .order_by(VerificationEvent.id.desc())).first()
    if merge is None:
        raise CurationError("لا يوجد سجل دمج يمكن التراجع عنه")
    rows = s.scalars(select(ChangeHistory).where(ChangeHistory.event_id == merge.id)).all()
    moved = 0
    for h in rows:
        if h.entity_type in ("tender_bid", "tender_award"):
            model = TenderBid if h.entity_type == "tender_bid" else TenderAward
            row = s.get(model, h.entity_id)
            if row is not None and str(row.company_id) == h.new_value:
                row.company_id = int(h.old_value)
                moved += 1
        elif h.entity_type == "company_alias_added":
            alias = s.scalars(select(CompanyAlias).where(CompanyAlias.company_id == h.entity_id,
                                                         CompanyAlias.alias_raw == h.new_value)).first()
            if alias is not None:
                s.delete(alias)
        elif h.entity_type == "company" and h.field == "identity_status":
            src.identity_status = h.old_value or "name_only_unverified"
    target = src.merged_into_id
    src.merged_into_id = None
    _event(s, "company", src.id, "unmerged", f"from target={target} by {by}; rows={moved}")
    s.flush()
    return moved


# ------------------------------------------------------------------ lifecycle D
def _date(value: str | None) -> dt.date | None:
    parsed, problem = N.parse_date(value)
    if problem:
        raise CurationError("تاريخ غير صالح (استخدم YYYY-MM-DD)")
    return parsed


def add_subcontract(s: Session, *, user_id: int, title: str, main_contractor_name: str, evidence_kind: str,
                    evidence_url: str, trade: str = "", city: str = "", evidence_note: str = "", published_date: str = "",
                    response_deadline: str = "", main_contractor_company_id: int | None = None,
                    related_tender_id: int | None = None) -> SubcontractOpportunity:
    title, contractor = N.clean_text(title)[:300], N.clean_text(main_contractor_name)[:300]
    if not title or not contractor:
        raise CurationError("العنوان واسم المقاول الرئيسي مطلوبان")
    if evidence_kind not in EVIDENCE_KINDS:
        raise CurationError("اختر نوع الدليل")
    if main_contractor_company_id and s.get(Company, main_contractor_company_id) is None:
        raise CurationError("الشركة المختارة غير موجودة")
    if related_tender_id and s.get(ProcurementTender, related_tender_id) is None:
        raise CurationError("المنافسة المرتبطة غير موجودة")
    city = N.clean_text(city)[:100] or None
    op = SubcontractOpportunity(
        title=title, trade=N.clean_text(trade)[:60], city=city, region=N.city_region(city)[1] if city else None,
        main_contractor_name=contractor, main_contractor_company_id=main_contractor_company_id or None,
        related_tender_id=related_tender_id or None, evidence_kind=evidence_kind, evidence_url=_https(evidence_url),
        evidence_note=N.clean_text(evidence_note)[:2000], published_date=_date(published_date),
        response_deadline=_date(response_deadline), created_by_user_id=user_id)
    s.add(op)
    s.flush()
    _event(s, "subcontract", op.id, "entered_with_evidence", f"user={user_id}", op.evidence_url)
    return op


def review_subcontract(s: Session, op_id: int, *, user_id: int, decision: str, note: str = "") -> SubcontractOpportunity:
    op = s.get(SubcontractOpportunity, op_id)
    if op is None:
        raise CurationError("الفرصة غير موجودة")
    if op.review_status != "pending_second_review":
        raise CurationError("تمت مراجعة هذه الفرصة مسبقًا")
    if op.created_by_user_id == user_id:
        raise CurationError("المراجعة يجب أن تكون من شخص غير الذي أدخل الفرصة")
    if decision not in ("confirmed", "rejected"):
        raise CurationError("قرار غير معروف")
    op.review_status, op.reviewed_by_user_id, op.reviewed_at = decision, user_id, utcnow()
    op.review_note = N.clean_text(note)[:2000]
    _event(s, "subcontract", op.id, f"review_{decision}", f"user={user_id}")
    s.flush()
    return op


def close_due_subcontracts(s: Session, today: dt.date) -> int:
    due = s.scalars(select(SubcontractOpportunity).where(SubcontractOpportunity.status == "open",
                                                         SubcontractOpportunity.response_deadline < today)).all()
    for op in due:
        op.status = "closed"
    return len(due)
