"""Scheduled work. Run by cron / a worker / the protected /internal/jobs/refresh endpoint.

    python -m hub.jobs refresh       # due sources + staleness + alerts
    python -m hub.jobs seed          # catalogue + source registry (idempotent)
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import db as dbm
from . import mailer
from .config import ROOT, settings
from .db import utcnow
from .models import EmailOutbox, JobRun, Notification, Opportunity, Product, Source, User
from .radar import HEALTH_LABELS, mark_stale_and_closed, match, run_source, source_health
from .store import active_plan

# Registry of real channels. rights=unknown => link-only, never fetched automatically
# until the owner documents permission (terms page / written licence) in rights_evidence.
SOURCE_REGISTRY = [
    {"key": "manual-review", "name": "إدخال المراجعين (مصادر موثقة يدويًا)", "homepage": "",
     "kind": "manual", "rights": "link_only", "trust": "review_all",
     "notes": "كل سجل يُدخله مراجع مع رابط المصدر وتاريخ التحقق."},
    {"key": "etimad", "name": "منصة اعتماد — المنافسات الحكومية", "homepage": "https://monafasat.etimad.sa/tenders/index",
     "kind": "manual", "rights": "unknown", "trust": "review_all",
     "notes": "لا جلب آلي: لم تُوثَّق شروط إعادة الاستخدام، وبعض التفاصيل تتطلب حسابًا أو رسومًا. الإشارة بالرابط فقط."},
    {"key": "muqawil-market", "name": "منصة مقاول — سوق المشاريع", "homepage": "https://muqawil.sa/ar/market",
     "kind": "manual", "rights": "unknown", "trust": "review_all",
     "notes": "لا جلب آلي حتى توثيق الإذن. يُطلب من الهيئة السعودية للمقاولين واجهة أو ترخيص."},
    {"key": "muqawil-consortium", "name": "منصة مقاول — التحالفات", "homepage": "https://muqawil.sa/ar/consortium/landing",
     "kind": "manual", "rights": "unknown", "trust": "review_all", "notes": "قناة شراكات؛ ليست دليلًا على فرصة باطن بعينها."},
]


def sync_catalog(db: Session) -> int:
    data = json.loads((ROOT / "products" / "catalog.json").read_text(encoding="utf-8"))
    n = 0
    for p in data["products"]:
        row = db.scalar(select(Product).where(Product.slug == p["slug"])) or Product(slug=p["slug"])
        row.file_stem, row.title, row.category = p["file"], p["title"], p["category"]
        row.summary, row.features, row.trades = p["summary"], p["features"], p["trades"]
        row.price, row.status = p["price"], p["status"]
        db.add(row)
        n += 1
    db.flush()
    return n


def sync_sources(db: Session) -> int:
    for s in SOURCE_REGISTRY:
        row = db.scalar(select(Source).where(Source.key == s["key"]))
        if row is None:
            db.add(Source(**s))
    db.flush()
    return len(SOURCE_REGISTRY)


def seed(db: Session):
    sync_catalog(db)
    sync_sources(db)
    db.commit()


def _notify(db: Session, user_id: int, kind: str, title: str, link: str) -> bool:
    try:
        with db.begin_nested():
            db.add(Notification(user_id=user_id, kind=kind, title=title, link=link))
        return True
    except IntegrityError:
        return False  # already notified (unique user/kind/link)


def send_user_alerts(db: Session) -> int:
    opps = db.scalars(select(Opportunity).where(Opportunity.review_state == "approved", Opportunity.alerted.is_(False),
                                                Opportunity.duplicate_of_id.is_(None),
                                                Opportunity.opportunity_type != "CLOSED")).all()
    if not opps:
        return 0
    users = db.scalars(select(User).where(User.is_active.is_(True), User.alerts_enabled.is_(True))).all()
    sent = 0
    for o in opps:
        for u in users:
            level, _ = match(o, u.trades, u.cities)
            if level not in ("high", "medium"):
                continue
            link = f"/radar/{o.id}"
            if _notify(db, u.id, "match", f"فرصة مطابقة: {o.title}", link):
                sent += 1
                if active_plan(db, u)["alerts"] and u.email_verified_at:  # never mail an unproven address
                    db.add(EmailOutbox(to=u.email, subject=f"فرصة مطابقة لتخصصك: {o.title}"[:300],
                                       body=f"{o.title}\n{settings.base_url}{link}\nتحقق من المصدر قبل التواصل."))
        o.alerted = True
    db.flush()
    return sent


def alert_admins_on_health(db: Session, now: dt.datetime) -> int:
    admins = db.scalars(select(User).where(User.role == "admin")).all()
    n = 0
    for s in db.scalars(select(Source)).all():
        h = source_health(s, now)
        if h in ("down", "late", "never_succeeded"):
            day_key = now.strftime("%Y-%m-%d")
            for a in admins:
                if _notify(db, a.id, "source_health", f"المصدر «{s.name}»: {HEALTH_LABELS[h]} — {s.last_error[:120]}",
                           f"/admin/sources#{s.key}-{day_key}"):
                    n += 1
            if settings.admin_alert_email and n:
                db.add(EmailOutbox(to=settings.admin_alert_email, subject=f"تنبيه مصدر: {s.name} {HEALTH_LABELS[h]}",
                                   body=s.last_error or HEALTH_LABELS[h]))
    db.flush()
    return n


def refresh(db: Session, now: dt.datetime | None = None, transport=None) -> dict:
    now = now or utcnow()
    automated = select(Source).where(Source.kind.not_in(["manual", "csv_import"]))  # manual sources have nothing to fetch
    runs = [run_source(db, s, now, transport=transport) for s in db.scalars(automated).all()]
    stale = mark_stale_and_closed(db, now)
    alerts = send_user_alerts(db)
    admin_alerts = alert_admins_on_health(db, now)
    db.commit()
    mail = mailer.send_pending(db)
    return {"runs": [{"source": r.source_id, "outcome": r.outcome, "reason": r.reason, "new": r.items_new,
                      "updated": r.items_updated, "review": r.items_to_review} for r in runs],
            **stale, "user_alerts": alerts, "admin_alerts": admin_alerts, "mail": mail}


def recorded_refresh(db: Session, trigger: str, now: dt.datetime | None = None, transport=None) -> dict:
    """refresh() plus a JobRun row, committed even when the refresh fails, so a stopped scheduler is visible."""
    run = JobRun(job="refresh", trigger=trigger[:40], started_at=utcnow())
    db.add(run)
    db.commit()
    run_id = run.id
    try:
        result = refresh(db, now=now, transport=transport)
    except Exception as e:
        db.rollback()
        run = db.get(JobRun, run_id)
        run.finished_at, run.ok, run.error = utcnow(), False, f"{type(e).__name__}: {e}"[:500]
        db.commit()
        raise
    run = db.get(JobRun, run_id)
    run.finished_at, run.ok = utcnow(), True
    run.summary = {"runs": len(result["runs"]), "user_alerts": result["user_alerts"],
                   "admin_alerts": result["admin_alerts"], "mail_sent": result["mail"].get("sent", 0),
                   **{k: v for k, v in result.items() if k in ("stale", "closed")}}
    db.commit()
    return {**result, "job_run_id": run_id}


def last_job_runs(db: Session, limit: int = 5) -> list[JobRun]:
    return list(db.scalars(select(JobRun).where(JobRun.job == "refresh").order_by(JobRun.id.desc()).limit(limit)))


def main(argv):
    dbm.init_db()
    with dbm.SessionLocal() as db:
        if argv[:1] == ["seed"]:
            seed(db)
            print("seeded")
        elif argv[:1] == ["refresh"]:
            seed(db)
            print(json.dumps(recorded_refresh(db, "cli"), ensure_ascii=False, indent=1, default=str))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
