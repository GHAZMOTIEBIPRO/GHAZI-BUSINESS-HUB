"""Opportunity radar: source gating, fetching, normalisation, de-duplication, review routing, staleness, matching.

Principles (enforced here, tested in tests/test_radar.py):
  * a source is fetched automatically only if its reuse rights are documented
    (verified_public_reuse | licensed_private) AND robots.txt allows the feed URL;
  * 401/403/451 is a refusal: recorded, never retried or worked around;
  * nothing becomes SUBCONTRACT_CONFIRMED without evidence and a human approval;
  * a job finishing is not evidence the data is right: freshness is per record (verified_at / review_due_at).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import defusedxml.ElementTree as ET
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .models import CITIES, TRADES, FetchRun, Opportunity, Source

USER_AGENT = "GhaziBusinessHubBot/1.0 (+{}/sources)"
AUTOMATABLE_RIGHTS = {"verified_public_reuse", "licensed_private"}
REFUSALS = {401, 403, 451}

TRADE_KEYWORDS = {
    "الجبس والأسقف المستعارة": ["جبس", "أسقف", "اسقف", "سقف مستعار", "جبسوم", "gypsum", "ceiling"],
    "البلاط والسيراميك": ["بلاط", "سيراميك", "بورسلان", "رخام", "tiles", "tiling", "ceramic"],
    "الدهانات": ["دهان", "دهانات", "طلاء", "بوية", "paint"],
    "الأبواب": ["أبواب", "ابواب", "باب", "door"],
    "إعادة تأهيل المباني والمساكن": ["تأهيل", "ترميم", "إعادة تأهيل", "تجديد", "rehabilitation", "renovation"],
    "الصيانة المدنية والمعمارية": ["صيانة", "تشغيل وصيانة", "maintenance"],
}
TYPE_KEYWORDS = [("AWARDED_PROJECT", ["ترسية", "تمت الترسية", "awarded"]),
                 ("OPEN_TENDER", ["منافسة", "مناقصة", "tender", "rfq", "طلب عروض"])]


# ----------------------------------------------------------------- helpers
def norm_text(s: str | None) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[ًٌٍَُِّْـ]", "", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s)


def fingerprint(title: str, city: str | None, owner: str | None) -> str:
    return hashlib.sha256("|".join(norm_text(x) for x in (title, city, owner)).encode()).hexdigest()


def classify_trades(text: str) -> list[str]:
    t = norm_text(text)
    out = []
    for trade, words in TRADE_KEYWORDS.items():
        if any(norm_text(w) in t for w in words):
            out.append(trade)
    return out


def classify_type(text: str, declared: str | None) -> str:
    if declared in {"OPEN_TENDER", "AWARDED_PROJECT", "OPPORTUNITY_LEAD", "CLOSED", "SUBCONTRACT_CONFIRMED"}:
        return declared
    t = norm_text(text)
    for typ, words in TYPE_KEYWORDS:
        if any(norm_text(w) in t for w in words):
            return typ
    return "OPPORTUNITY_LEAD"


def parse_dt(v) -> dt.datetime | None:
    if not v:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(dt.UTC).replace(tzinfo=None)
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.astimezone(dt.UTC).replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    return None


def https_url(u: str | None) -> str | None:
    if not u:
        return None
    p = urlparse(str(u).strip())
    return p.geturl() if p.scheme == "https" and p.netloc else None


REPUBLISHABLE_RIGHTS = AUTOMATABLE_RIGHTS  # rights that allow republishing the source's own text


def summary_publishable(summary: str | None, origin: str | None, source_rights: str | None) -> bool:
    """Facts and links are always shown; descriptive text only if we wrote it or the source licenses reuse."""
    if not (summary or "").strip():
        return False
    return origin == "reviewer" or source_rights in REPUBLISHABLE_RIGHTS


def public_summary(opp) -> str | None:
    return opp.summary if summary_publishable(opp.summary, opp.summary_origin, opp.source.rights) else None


def contact_ok(contact: dict | None) -> bool:
    """A contact may be displayed only if it is a business channel, has its own source URL and display is allowed."""
    if not contact:
        return False
    return bool(contact.get("display_allowed")) and bool(https_url(contact.get("source_url"))) and bool(
        contact.get("verified_at")) and any(contact.get(k) for k in ("public_business_email", "public_business_phone"))


# ----------------------------------------------------------------- fetch layer
@dataclass
class FetchResult:
    ok: bool
    outcome: str
    reason: str = ""
    items: list[dict] = field(default_factory=list)


def robots_allows(client: httpx.Client, url: str) -> tuple[bool, str]:
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = client.get(robots_url)
    except httpx.HTTPError as e:
        return False, f"تعذر قراءة robots.txt: {type(e).__name__}"
    if r.status_code in REFUSALS:
        return False, f"robots.txt مرفوض ({r.status_code})"
    if r.status_code >= 400:
        return True, "لا يوجد robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    ua = USER_AGENT.split("/")[0]
    return (rp.can_fetch(ua, url), "robots.txt يمنع المسار" if not rp.can_fetch(ua, url) else "robots.txt يسمح")


def parse_feed(kind: str, text: str) -> list[dict]:
    if kind == "json_feed":
        data = json.loads(text)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("JSON feed must contain an items list")
        return [i for i in items if isinstance(i, dict)]
    if kind == "rss":
        root = ET.fromstring(text)
        out = []
        for it in root.iter("item"):
            g = {tag: (it.findtext(tag) or "").strip() for tag in ("guid", "link", "title", "description", "pubDate")}
            out.append({"external_id": g["guid"] or g["link"], "title": g["title"], "url": g["link"],
                        "summary": g["description"], "published_at": g["pubDate"]})
        return out
    raise ValueError(f"unsupported kind {kind}")


def fetch_source(source: Source, transport: httpx.BaseTransport | None = None) -> FetchResult:
    if source.kind in ("manual", "csv_import"):
        return FetchResult(False, "skipped", "مصدر يدوي: الإدخال عبر لوحة المراجعة")
    if source.rights not in AUTOMATABLE_RIGHTS:
        return FetchResult(False, "gated", f"حقوق إعادة الاستخدام غير موثقة ({source.rights}) — لا جلب آلي")
    url = https_url(source.feed_url)
    if not url:
        return FetchResult(False, "failed", "رابط الخلاصة غير صالح أو ليس HTTPS")
    headers = {"User-Agent": USER_AGENT.format(settings.base_url), "Accept": "application/json, application/rss+xml, */*"}
    with httpx.Client(timeout=20, headers=headers, transport=transport, follow_redirects=False) as client:
        allowed, why = robots_allows(client, url)
        if not allowed:
            return FetchResult(False, "gated", why)
        try:
            r = client.get(url)
        except httpx.HTTPError as e:
            return FetchResult(False, "failed", f"خطأ شبكة: {type(e).__name__}")
        if r.status_code in REFUSALS:
            return FetchResult(False, "refused", f"المصدر رفض الطلب ({r.status_code}) — لن يُعاد المحاولة آليًا")
        if r.status_code != 200:
            return FetchResult(False, "failed", f"HTTP {r.status_code}")
        try:
            items = parse_feed(source.kind, r.text)
        except (ValueError, ET.ParseError) as e:
            return FetchResult(False, "failed", f"تعذر تحليل البيانات: {e}"[:300])
    return FetchResult(True, "success", f"{len(items)} عنصر", items)


# ----------------------------------------------------------------- normalise + upsert
def normalise(raw: dict) -> dict:
    title = str(raw.get("title") or "").strip()[:300]
    summary = str(raw.get("summary") or "").strip()[:4000]
    city = str(raw.get("city") or "").strip() or None
    if city and city not in CITIES:
        city_n = norm_text(city)
        city = next((c for c in CITIES if norm_text(c) == city_n), city)
    text = f"{title} {summary} {raw.get('scope') or ''}"
    declared_trades = [t for t in (raw.get("trades") or []) if t in TRADES]
    contact = raw.get("contact") if isinstance(raw.get("contact"), dict) else None
    return {
        "external_id": str(raw.get("external_id") or raw.get("id") or "").strip()[:200] or None,
        "title": title,
        "summary": summary,
        "summary_origin": "reviewer" if raw.get("summary_origin") == "reviewer" else "source",
        "city": city,
        "region": CITIES.get(city or "") or (str(raw.get("region")).strip() if raw.get("region") else None),
        "trades": declared_trades or classify_trades(text),
        "opportunity_type": classify_type(text, raw.get("opportunity_type")),
        "owner_entity": (str(raw.get("owner_entity")).strip() or None) if raw.get("owner_entity") else None,
        "main_contractor": (str(raw.get("main_contractor")).strip() or None) if raw.get("main_contractor") else None,
        "responsible_company": (str(raw.get("responsible_company")).strip() or None) if raw.get("responsible_company") else None,
        "contact": contact,
        "source_url": https_url(raw.get("url") or raw.get("source_url")),
        "published_at": parse_dt(raw.get("published_at")),
        "deadline_at": parse_dt(raw.get("deadline_at")),
        "evidence": [e for e in (raw.get("evidence") or []) if isinstance(e, dict) and https_url(e.get("url"))],
    }


def review_reasons(n: dict, source: Source, now: dt.datetime) -> list[str]:
    r = []
    if source.trust != "auto_publish":
        r.append("المصدر يتطلب مراجعة بشرية لكل سجل")
    if not n["source_url"]:
        r.append("لا يوجد رابط مصدر HTTPS")
    if n["opportunity_type"] == "SUBCONTRACT_CONFIRMED":
        r.append("باطن مؤكد: يتطلب دليلًا مباشرًا واعتمادًا بشريًا")
        if not n["evidence"]:
            r.append("لا يوجد دليل مرفق لتأكيد الباطن")
    if n["contact"] and not contact_ok(n["contact"]):
        r.append("جهة اتصال بلا مصدر/تاريخ تحقق/إذن عرض — لن تُعرض")
    if not n["city"]:
        r.append("المدينة غير محددة")
    if not n["trades"]:
        r.append("لم يُتعرف على تخصص مستهدف")
    if n["deadline_at"] and n["deadline_at"] < now:
        r.append("موعد التقديم منتهٍ")
    if not n["title"]:
        r.append("لا يوجد عنوان")
    if n["summary"] and not summary_publishable(n["summary"], n["summary_origin"], source.rights):
        r.append("الوصف منقول من مصدر غير مرخّص لإعادة النشر — لن يُعرض للعامة؛ اكتب ملخصًا بصياغتك إن لزم")
    return r


@dataclass
class UpsertStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    to_review: int = 0
    skipped: int = 0


SIGNIFICANT = ("opportunity_type", "deadline_at", "main_contractor", "owner_entity", "contact")


def upsert(db: Session, source: Source, raws: list[dict], now: dt.datetime | None = None) -> UpsertStats:
    now = now or utcnow()
    st = UpsertStats()
    for raw in raws:
        st.seen += 1
        n = normalise(raw)
        if not n["title"] or not n["source_url"]:
            st.skipped += 1
            continue
        fp = fingerprint(n["title"], n["city"], n["owner_entity"])
        existing = None
        if n["external_id"]:
            existing = db.scalar(select(Opportunity).where(Opportunity.source_id == source.id,
                                                           Opportunity.external_id == n["external_id"]))
        if existing is None:
            existing = db.scalar(select(Opportunity).where(Opportunity.source_id == source.id, Opportunity.fingerprint == fp))
        reasons = review_reasons(n, source, now)
        ttl = dt.timedelta(days=source.verify_ttl_days)
        if existing:
            # never erase known information with an empty value; trades accumulate
            n["trades"] = sorted(set(existing.trades or []) | set(n["trades"] or []), key=TRADES.index)
            for k in list(n):
                if n[k] in (None, "", []) and getattr(existing, k) not in (None, "", []):
                    n[k] = getattr(existing, k)
            changed = [k for k in n if k != "evidence" and getattr(existing, k) != n[k]]
            for k in changed:
                setattr(existing, k, n[k])
            existing.evidence = _merge_evidence(existing.evidence, n["evidence"])
            existing.fingerprint = fp
            existing.last_seen_at = now
            existing.verified_at = now
            existing.review_due_at = now + ttl
            if changed:
                existing.updated_at = now
                st.updated += 1
                if any(k in SIGNIFICANT for k in changed) and existing.review_state == "approved":
                    existing.review_state = "pending_review"
                    existing.review_reasons = ["تغيّر جوهري في: " + "، ".join(k for k in changed if k in SIGNIFICANT)]
                    st.to_review += 1
            if existing.review_state == "stale":
                existing.review_state = "pending_review"
                existing.review_reasons = ["عاد السجل للظهور في المصدر بعد انقطاع"]
                st.to_review += 1
            continue
        dup = db.scalar(select(Opportunity).where(Opportunity.fingerprint == fp, Opportunity.source_id != source.id,
                                                  Opportunity.duplicate_of_id.is_(None)))
        opp = Opportunity(source_id=source.id, fingerprint=fp, first_seen_at=now, last_seen_at=now, verified_at=now,
                          review_due_at=now + ttl, updated_at=now, **n)
        if dup:
            opp.duplicate_of_id = dup.id
            reasons.append(f"مكرر محتمل للسجل #{dup.id} من مصدر آخر")
            dup.evidence = _merge_evidence(dup.evidence, [{"url": n["source_url"], "checked_at": now.isoformat(),
                                                           "finding": f"ظهر أيضًا في {source.name}"}])
        opp.review_reasons = reasons
        opp.review_state = "approved" if not reasons else "pending_review"
        db.add(opp)
        st.new += 1
        st.to_review += bool(reasons)
    db.flush()
    return st


def _merge_evidence(old: list, new: list) -> list:
    seen = {e.get("url") for e in old or []}
    return list(old or []) + [e for e in new or [] if e.get("url") not in seen]


# ----------------------------------------------------------------- run + health + staleness
def source_is_due(source: Source, now: dt.datetime) -> bool:
    return source.last_run_at is None or now - source.last_run_at >= dt.timedelta(hours=source.refresh_hours)


def run_source(db: Session, source: Source, now: dt.datetime | None = None, transport=None, force=False) -> FetchRun:
    now = now or utcnow()
    run = FetchRun(source_id=source.id, started_at=now, outcome="skipped_not_due")
    if not source.enabled:
        run.outcome, run.reason = "skipped", "المصدر معطل"
    elif not force and not source_is_due(source, now):
        run.reason = "لم يحن موعد التحديث"
    else:
        res = fetch_source(source, transport=transport)
        run.outcome, run.reason = res.outcome, res.reason
        source.last_run_at = now
        if res.ok:
            st = upsert(db, source, res.items, now)
            run.items_seen, run.items_new, run.items_updated, run.items_to_review = st.seen, st.new, st.updated, st.to_review
            if st.skipped:
                run.reason += f"؛ {st.skipped} عنصر بلا عنوان أو رابط HTTPS تم تجاهله"
            source.last_success_at = now
            source.consecutive_failures = 0
            source.last_error = ""
        elif res.outcome in ("failed", "refused"):
            source.consecutive_failures += 1
            source.last_error = res.reason
    run.finished_at = utcnow()
    db.add(run)
    db.flush()
    return run


def source_health(source: Source, now: dt.datetime) -> str:
    if not source.enabled:
        return "disabled"
    if source.kind in ("manual", "csv_import"):
        return "manual"
    if source.rights not in AUTOMATABLE_RIGHTS:
        return "gated"
    if source.consecutive_failures >= 2:
        return "down"
    if source.last_success_at is None:
        return "never_succeeded" if source.last_run_at else "pending"
    if now - source.last_success_at > dt.timedelta(hours=source.refresh_hours * 2 + 1):
        return "late"
    return "ok"


HEALTH_LABELS = {"ok": "يعمل", "late": "متأخر", "down": "متوقف", "gated": "مقيّد بالحقوق", "manual": "يدوي",
                 "disabled": "معطل", "pending": "لم يُشغَّل", "never_succeeded": "لم ينجح بعد"}


def mark_stale_and_closed(db: Session, now: dt.datetime | None = None) -> dict:
    now = now or utcnow()
    closed = stale = 0
    for o in db.scalars(select(Opportunity).where(Opportunity.review_state.in_(["approved", "pending_review"]))):
        if o.deadline_at and o.deadline_at < now and o.opportunity_type not in ("CLOSED", "AWARDED_PROJECT"):
            o.opportunity_type = "CLOSED"
            o.updated_at = now
            closed += 1
        if o.review_state == "approved" and o.review_due_at and o.review_due_at < now:
            o.review_state = "stale"
            o.review_reasons = ["لم يُعد التحقق منه خلال المهلة — مخفي من النتائج الحديثة"]
            stale += 1
    db.flush()
    return {"closed": closed, "stale": stale}


# ----------------------------------------------------------------- matching
def match(opp: Opportunity, trades: list[str], cities: list[str]) -> tuple[str, list[str]]:
    """Explainable match level (not a fake percentage)."""
    reasons = []
    t = sorted(set(opp.trades or []) & set(trades or []))
    if t:
        reasons.append("تخصص: " + "، ".join(t))
    city_hit = opp.city and opp.city in (cities or [])
    region_hit = (not city_hit) and opp.region and opp.region in {CITIES.get(c) for c in cities or []}
    if city_hit:
        reasons.append(f"مدينتك: {opp.city}")
    elif region_hit:
        reasons.append(f"منطقتك: {opp.region}")
    if t and (city_hit or region_hit):
        return "high", reasons
    if t:
        return "medium", reasons
    if city_hit or region_hit:
        return "low", reasons
    return "none", reasons


MATCH_LABELS = {"high": "مطابقة عالية", "medium": "مطابقة في التخصص", "low": "في منطقتك فقط", "none": "خارج تفضيلاتك"}
