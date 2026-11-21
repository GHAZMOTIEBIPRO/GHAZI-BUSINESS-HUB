"""Public tender and company intelligence.

Only explicitly publishable, nonsynthetic records from the internal archive may
appear publicly. Daall public MCP queries are user-triggered, transient, credited
to their provider and never written into our database.
"""
from __future__ import annotations

import os
import re

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select, union
from sqlalchemy.orm import Session, selectinload

from .archive.models import Company, CompanyAlias, ProcurementTender, TenderAward, TenderBid
from .daall_mcp import ProviderError, lookup, safe_source_url, tender_link
from .security import rate_limit, rate_limit_key

DIGITS = re.compile(r"^[0-9]{6,30}$")
SOURCE_URL = "https://mnaqasat.daall.sa/help/ai"
MAX_INPUT = 120
PER_PAGE = 20


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _public():
    return (ProcurementTender.visibility == "public",
            ProcurementTender.publish_allowed.is_(True),
            ProcurementTender.is_synthetic.is_(False))


def local_tenders(db: Session, query: str, limit: int = PER_PAGE) -> list[ProcurementTender]:
    if not query:
        return []
    cond = or_(ProcurementTender.official_reference == query,
               ProcurementTender.title.ilike(_like(query), escape="\\"))
    return db.scalars(select(ProcurementTender).where(*_public(), cond)
                      .options(selectinload(ProcurementTender.issuer))
                      .order_by(ProcurementTender.published_date.desc().nulls_last(), ProcurementTender.id.desc())
                      .limit(limit)).all()


def local_companies(db: Session, query: str) -> list[Company]:
    """No name/CR leaks for companies that only appear in private or synthetic tenders."""
    if not query:
        return []
    awards = select(TenderAward.company_id).join(
        ProcurementTender, TenderAward.tender_id == ProcurementTender.id).where(*_public())
    bids = select(TenderBid.company_id).join(
        ProcurementTender, TenderBid.tender_id == ProcurementTender.id).where(*_public())
    eligible = union(awards, bids).subquery()
    names = select(CompanyAlias.company_id).where(
        CompanyAlias.alias_raw.ilike(_like(query), escape="\\"))
    return db.scalars(select(Company).where(
        Company.id.in_(select(eligible.c.company_id)),
        or_(Company.cr_number == query, Company.unified_number == query,
            Company.display_name.ilike(_like(query), escape="\\"),
            Company.id.in_(names)))
        .order_by(Company.display_name).limit(PER_PAGE)).all()


def local_company_projects(db: Session, company_id: int):
    company = db.get(Company, company_id)
    if company is None or company.merged_into_id:
        return None
    awards = db.scalars(select(TenderAward).join(ProcurementTender).where(
        TenderAward.company_id == company_id, *_public()).options(
            selectinload(TenderAward.tender).selectinload(ProcurementTender.issuer))
        .order_by(TenderAward.award_date.desc().nulls_last(), TenderAward.id.desc()).limit(100)).all()
    bids = db.scalars(select(TenderBid).join(ProcurementTender).where(
        TenderBid.company_id == company_id, *_public()).options(
            selectinload(TenderBid.tender).selectinload(ProcurementTender.issuer))
        .order_by(TenderBid.id.desc()).limit(100)).all()
    if not awards and not bids:
        return None
    return {"company": company, "awards": awards, "bids": bids,
            "total_awards_value": sum(a.award_amount_sar for a in awards if a.award_amount_sar is not None),
            "amounts_count": sum(a.award_amount_sar is not None for a in awards)}


def _short(value, maxlen=250):
    return str(value)[:maxlen] if value is not None else ""


def _tender(record):
    if not isinstance(record, dict):
        return None
    ref = _short(record.get("reference_no") or record.get("reference"), 30)
    url = safe_source_url(record.get("url")) or tender_link(ref)
    return {k: _short(record.get(k), 300) for k in (
        "reference_no", "name", "agency", "activity", "tender_type", "status",
        "region", "city", "contract_duration", "publish_date", "last_submission",
        "closes_at", "days_left", "booklet_total_sar")} | {
        "reference_no": ref, "url": url,
        "etimad_url": safe_source_url(record.get("etimad_url")),
        "is_open": record.get("is_open") is True,
        "is_awarded": record.get("is_awarded") is True,
        "winners": [
            {"name": _short(w.get("name") or w.get("winner") or w.get("company"), 180),
             "value": _short(w.get("value_sar") or w.get("amount_sar") or w.get("amount"), 100)}
            for w in record.get("winners", [])[:20] if isinstance(w, dict)
        ] if isinstance(record.get("winners"), list) else [],
    }


def _company(record):
    if not isinstance(record, dict):
        return None
    def count(k):
        val = record.get(k)
        return val if type(val) is int and val >= 0 else None
    def group(k):
        vals = record.get(k)
        return [_short(v.get("name") or v.get("agency") or v.get("title"), 170)
                if isinstance(v, dict) else _short(v, 170)
                for v in vals[:8]] if isinstance(vals, list) else []
    wins = record.get("recent_wins")
    return {"name": _short(record.get("name"), 250),
            "id": count("id"), "participations": count("participations"),
            "wins": count("wins"), "win_rate_pct": count("win_rate_pct"),
            "won_value_sar": record.get("won_value_sar") if isinstance(
                record.get("won_value_sar"), (int, float)) else None,
            "last_seen": _short(record.get("last_seen"), 40),
            "top_agencies": group("top_agencies"), "top_activities": group("top_activities"),
            "recent_wins": [
                {"name": _short(v.get("name") or v.get("title"), 220),
                 "reference_no": _short(v.get("reference_no"), 30),
                 "amount": _short(v.get("award_amount_sar") or v.get("value_sar"), 80),
                 "url": safe_source_url(v.get("url"))}
                for v in wins[:10] if isinstance(v, dict)] if isinstance(wins, list) else [],
            "url": safe_source_url(record.get("url")), "listed": record.get("listed") is True}


def _external(tool: str, request: Request, **kwargs):
    """One interactive lookup per submitted query, never a scheduled/bulk import."""
    if os.environ.get("HUB_MCP_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return None, "الاستعلام الخارجي غير مفعل حاليًا؛ تظهر أدناه البيانات المحلية المصرح بنشرها."
    rate_limit(request, "public-mcp", 12, 60)
    rate_limit_key("public-mcp-all-users", 30, 60)
    try:
        return lookup(tool, **kwargs), ""
    except (ProviderError, ValueError) as exc:
        return None, str(exc)


def register(app, *, render, get_db, current_user_optional):
    @app.get("/intelligence", response_class=HTMLResponse)
    def intelligence(request: Request, q: str = "", kind: str = "tender", region: str = "",
                     open_only: bool = False, db: Session = Depends(get_db),
                     user=Depends(current_user_optional)):
        query = q.strip()[:MAX_INPUT]
        mode = kind if kind in {"tender", "company"} else "tender"
        results = local_tenders(db, query) if mode == "tender" else []
        companies = local_companies(db, query) if mode == "company" else []
        external = None
        error = ""
        if query:
            if mode == "tender" and DIGITS.fullmatch(query):
                raw, error = _external("get_tender", request, reference_no=query)
                external = _tender(raw)
            elif mode == "tender":
                raw, error = _external("search_tenders", request, query=query, region=region[:60],
                                       open_only=open_only, limit=15)
                if isinstance(raw, dict):
                    external = {
                        "total": raw.get("total") if isinstance(raw.get("total"), int) else None,
                        "url": safe_source_url(raw.get("url")),
                        "items": [item for item in (_tender(x) for x in raw.get("items", [])[:15])
                                  if item] if isinstance(raw.get("items"), list) else []}
            elif DIGITS.fullmatch(query):
                error = ("رقم السجل يُطابَق مع سجلات موثقة داخل المنصة. "
                         "مصدر الاستعلام الخارجي يقبل اسم الشركة أو معرّفها الخاص، "
                         "وليس هناك إثبات أن المعرّف يساوي السجل التجاري؛ ابحث بالاسم.")
            else:
                raw, error = _external("company_summary", request, name_or_id=query)
                external = _company(raw)
        return render(request, "intelligence.html", db, user, q=query, kind=mode,
                      region=region[:60], open_only=open_only, local_tenders=results,
                      local_companies=companies, remote=external, error=error,
                      source_url=SOURCE_URL)

    @app.get("/intelligence/tender/{reference}", response_class=HTMLResponse)
    def intelligence_tender(reference: str, request: Request, db: Session = Depends(get_db),
                            user=Depends(current_user_optional)):
        if not DIGITS.fullmatch(reference):
            raise HTTPException(404)
        record = db.scalars(select(ProcurementTender).where(
            ProcurementTender.official_reference == reference, *_public()).options(
                selectinload(ProcurementTender.issuer), selectinload(ProcurementTender.awards),
                selectinload(ProcurementTender.bids))).first()
        raw, error = _external("get_tender", request, reference_no=reference)
        remote = _tender(raw)
        return render(request, "intelligence_tender.html", db, user,
                      reference=reference, record=record, remote=remote, error=error)

    @app.get("/intelligence/company/{company_id}", response_class=HTMLResponse)
    def intelligence_company(company_id: int, request: Request, db: Session = Depends(get_db),
                             user=Depends(current_user_optional)):
        data = local_company_projects(db, company_id)
        if data is None:
            raise HTTPException(404)
        return render(request, "intelligence_company.html", db, user, p=data)
