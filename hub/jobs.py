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
from .models import EmailOutbox, Notification, Opportunity, Product, Source, User
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


def bootstrap_admin(db: Session) -> str:
    """Create the first admin from HUB_BOOTSTRAP_ADMIN_EMAIL/PASSWORD once, only if no admin exists.
    Hosts without shell access (free tiers) need this; remove the variables after first login."""
    import os

    from .security import hash_password

    email = os.environ.get("HUB_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("HUB_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return "not_configured"
    if db.scalar(select(User).where(User.role == "admin")):
        return "admin_exists"
    if len(password) < 14:
        return "password_too_short"
    user = db.scalar(select(User).where(User.email == email))
    if user:
        user.role = "admin"
    else:
        db.add(User(email=email, name="المدير", role="admin", password_hash=hash_password(password), trades=[], cities=[]))
    db.commit()
    return "created"


def seed(db: Session):
    sync_catalog(db)
    sync_sources(db)
    db.commit()
    bootstrap_admin(db)


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
                if active_plan(db, u)["alerts"]:
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


def main(argv):
    dbm.init_db()
    with dbm.SessionLocal() as db:
        if argv[:1] == ["seed"]:
            seed(db)
            print("seeded")
        elif argv[:1] == ["refresh"]:
            seed(db)
            print(json.dumps(refresh(db), ensure_ascii=False, indent=1))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
