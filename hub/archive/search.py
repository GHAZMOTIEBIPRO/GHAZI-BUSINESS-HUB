"""Archive queries (staff screens). Every filter is a bound parameter; LIKE wildcards in user text are escaped."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import Select, and_, extract, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .models import Company, CompanyAlias, ProcurementIssuer, ProcurementTender, TenderAward, TenderBid

PER_PAGE = 50


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass
class TenderFilters:
    q: str = ""
    year: int | None = None
    city: str = ""
    region: str = ""
    issuer: str = ""
    trade: str = ""
    company: str = ""
    status: str = ""
    lifecycle: str = ""
    min_value: float | None = None
    max_value: float | None = None
    include_synthetic: bool = False
    page: int = 1


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse_filters(params) -> TenderFilters:
    year = params.get("year", "")
    page = params.get("page", "1")
    return TenderFilters(q=(params.get("q") or "")[:120].strip(), year=int(year) if str(year).isdigit() else None,
                         city=(params.get("city") or "")[:60], region=(params.get("region") or "")[:60],
                         issuer=(params.get("issuer") or "")[:120].strip(), trade=(params.get("trade") or "")[:60],
                         company=(params.get("company") or "")[:120].strip(), status=(params.get("status") or "")[:20],
                         lifecycle=(params.get("lifecycle") or "")[:30], min_value=_num(params.get("min_value")),
                         max_value=_num(params.get("max_value")), page=max(1, int(page) if str(page).isdigit() else 1))


def tender_query(f: TenderFilters) -> Select:
    stmt = select(ProcurementTender)
    conds = []
    if not f.include_synthetic:
        conds.append(ProcurementTender.is_synthetic.is_(False))
    if f.q:
        conds.append(or_(ProcurementTender.title.ilike(_like(f.q), escape="\\"),
                         ProcurementTender.official_reference == f.q))
    if f.year:
        conds.append(or_(extract("year", ProcurementTender.published_date) == f.year,
                         extract("year", ProcurementTender.award_date) == f.year))
    if f.city:
        conds.append(ProcurementTender.city == f.city)
    if f.region:
        conds.append(ProcurementTender.region == f.region)
    if f.status:
        conds.append(ProcurementTender.status == f.status)
    if f.lifecycle:
        conds.append(ProcurementTender.lifecycle == f.lifecycle)
    if f.issuer:
        conds.append(ProcurementTender.issuer_id.in_(select(ProcurementIssuer.id).where(
            ProcurementIssuer.name.ilike(_like(f.issuer), escape="\\"))))
    if f.trade:
        conds.append(ProcurementTender.work_types_text.ilike(_like(f.trade), escape="\\"))
    if f.company:
        # companies first (small), then their tenders through the indexed company_id columns
        names = select(CompanyAlias.company_id).where(CompanyAlias.alias_raw.ilike(_like(f.company), escape="\\"))
        tender_ids = select(TenderBid.tender_id).where(TenderBid.company_id.in_(names)).union(
            select(TenderAward.tender_id).where(TenderAward.company_id.in_(names)))
        conds.append(ProcurementTender.id.in_(tender_ids))
    if f.min_value is not None or f.max_value is not None:
        amt = []
        if f.min_value is not None:
            amt.append(TenderAward.award_amount_sar >= f.min_value)
        if f.max_value is not None:
            amt.append(TenderAward.award_amount_sar <= f.max_value)
        conds.append(ProcurementTender.awards.any(and_(*amt)))
    if conds:
        stmt = stmt.where(and_(*conds))
    return stmt


def search_tenders(s: Session, f: TenderFilters) -> tuple[list[ProcurementTender], int]:
    base = tender_query(f)
    total = s.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
    rows = s.scalars(base.options(selectinload(ProcurementTender.issuer), selectinload(ProcurementTender.awards))
                     .order_by(ProcurementTender.published_date.desc().nulls_last(), ProcurementTender.id.desc())
                     .limit(PER_PAGE).offset((f.page - 1) * PER_PAGE)).all()
    return rows, total


def facets(s: Session) -> dict:
    real = ProcurementTender.is_synthetic.is_(False)
    years = sorted({y for (y,) in s.execute(select(extract("year", ProcurementTender.published_date)).where(real).distinct())
                    if y is not None}, reverse=True)
    cities = [c for (c,) in s.execute(select(ProcurementTender.city).where(real, ProcurementTender.city.is_not(None))
                                      .distinct().order_by(ProcurementTender.city))]
    regions = [r for (r,) in s.execute(select(ProcurementTender.region).where(real, ProcurementTender.region.is_not(None))
                                       .distinct().order_by(ProcurementTender.region))]
    return {"years": [int(y) for y in years], "cities": cities, "regions": regions}


def company_profile(s: Session, company_id: int) -> dict | None:
    c = s.get(Company, company_id)
    if c is None:
        return None
    bids = s.scalars(select(TenderBid).where(TenderBid.company_id == c.id)
                     .options(selectinload(TenderBid.tender).selectinload(ProcurementTender.issuer))).all()
    awards = s.scalars(select(TenderAward).where(TenderAward.company_id == c.id)
                       .options(selectinload(TenderAward.tender).selectinload(ProcurementTender.issuer))).all()
    tenders = {b.tender_id: b.tender for b in bids} | {a.tender_id: a.tender for a in awards}
    issuers = sorted({t.issuer.name for t in tenders.values() if t.issuer})
    by_year: dict[int, dict] = {}
    for t in tenders.values():
        d = t.award_date or t.published_date
        if d:
            y = by_year.setdefault(d.year, {"bids": 0, "wins": 0, "award_total": 0.0})
            y["bids"] += sum(1 for b in bids if b.tender_id == t.id)
            y["wins"] += sum(1 for a in awards if a.tender_id == t.id)
            y["award_total"] += sum(a.award_amount_sar or 0 for a in awards if a.tender_id == t.id)
    similar = s.scalars(select(Company).where(Company.fold_key == c.fold_key, Company.id != c.id)).all()
    latest = sorted(tenders.values(), key=lambda t: t.award_date or t.published_date or dt.date.min, reverse=True)[:10]
    return {"company": c, "bids": sorted(bids, key=lambda b: b.tender.published_date or dt.date.min, reverse=True),
            "awards": awards, "issuers": issuers, "by_year": dict(sorted(by_year.items(), reverse=True)),
            "similar": similar, "latest": latest}


def search_companies(s: Session, q: str, limit: int = 100) -> list[tuple[Company, int, int]]:
    stmt = select(Company)
    if q:
        stmt = stmt.where(Company.id.in_(select(CompanyAlias.company_id).where(
            CompanyAlias.alias_raw.ilike(_like(q[:120]), escape="\\"))))
    companies = s.scalars(stmt.order_by(Company.display_name).limit(limit)).all()
    ids = [c.id for c in companies]
    bids = dict(s.execute(select(TenderBid.company_id, func.count()).where(TenderBid.company_id.in_(ids)).group_by(TenderBid.company_id)).all()) if ids else {}
    wins = dict(s.execute(select(TenderAward.company_id, func.count()).where(TenderAward.company_id.in_(ids)).group_by(TenderAward.company_id)).all()) if ids else {}
    return [(c, bids.get(c.id, 0), wins.get(c.id, 0)) for c in companies]
