"""Fill a STAGING/dev database with clearly fictional demo records for UI testing and screenshots.

Every record is labelled «مثال تجريبي», uses example.sa URLs, and invents no real company, person or phone.
Refuses to run when HUB_ENV=production.

    python scripts/seed_demo.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub import db as dbm  # noqa: E402
from hub import jobs  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import utcnow  # noqa: E402
from hub.models import CITIES, Opportunity, Source, User  # noqa: E402
from hub.security import hash_password  # noqa: E402

DEMO = [
    ("مثال تجريبي: ترميم وصيانة مبنى مدرسي — دهانات وأسقف جبس", "ينبع", ["الدهانات", "الجبس والأسقف المستعارة"], "OPEN_TENDER", 18),
    ("مثال تجريبي: مشروع سكني مُرسى — قد يحتاج مقاولي بلاط", "جدة", ["البلاط والسيراميك"], "AWARDED_PROJECT", None),
    ("مثال تجريبي: طلب مقاول باطن لأعمال الأبواب الخشبية", "الرياض", ["الأبواب"], "SUBCONTRACT_CONFIRMED", 9),
    ("مثال تجريبي: صيانة مدنية ومعمارية لمبانٍ إدارية", "الدمام", ["الصيانة المدنية والمعمارية"], "OPEN_TENDER", 25),
    ("مثال تجريبي: إعادة تأهيل مساكن — مؤشر غير مؤكد", "المدينة المنورة", ["إعادة تأهيل المباني والمساكن"], "OPPORTUNITY_LEAD", None),
]


def main():
    if settings.env == "production":
        raise SystemExit("refusing to seed demo data in production")
    dbm.init_db()
    with dbm.SessionLocal() as db:
        jobs.seed(db)
        src = db.query(Source).filter_by(key="manual-review").one()
        now = utcnow()
        for i, (title, city, trades, typ, days) in enumerate(DEMO, start=1):
            if db.query(Opportunity).filter_by(title=title).first():
                continue
            ev = [{"url": f"https://demo.example.sa/evidence/{i}", "checked_at": now.isoformat(),
                   "finding": "مثال: إعلان الجهة المسؤولة عن طلب مقاول باطن"}] if typ == "SUBCONTRACT_CONFIRMED" else []
            db.add(Opportunity(
                source_id=src.id, external_id=f"demo-{i}", fingerprint=f"demo-{i}", title=title, city=city,
                region=CITIES[city], trades=trades, opportunity_type=typ,
                owner_entity="جهة مثال (بيانات تجريبية)" if i != 5 else None, main_contractor=None,
                summary="سجل تجريبي لاختبار الواجهة فقط — ليس فرصة حقيقية.",
                source_url=f"https://demo.example.sa/opportunity/{i}", published_at=now - dt.timedelta(days=i),
                deadline_at=now + dt.timedelta(days=days) if days else None, verified_at=now - dt.timedelta(days=i - 1),
                review_due_at=now + dt.timedelta(days=7), review_state="approved", evidence=ev,
                contact={"public_business_name": "قسم المشتريات (مثال)", "role": "مشتريات", "public_business_email": "rfq@demo.example.sa",
                         "public_business_phone": None, "source_url": "https://demo.example.sa/contact",
                         "verified_at": now.isoformat(), "display_allowed": True} if i == 3 else None))
        db.add(Opportunity(source_id=src.id, external_id="demo-pending", fingerprint="demo-pending",
                           title="مثال تجريبي: سجل ينتظر المراجعة", city=None, trades=[], opportunity_type="OPPORTUNITY_LEAD",
                           source_url="https://demo.example.sa/pending", verified_at=now, review_state="pending_review",
                           review_reasons=["المصدر يتطلب مراجعة بشرية لكل سجل", "المدينة غير محددة"]))
        for email, role in (("admin@demo.example.sa", "admin"), ("contractor@demo.example.sa", "user")):
            if not db.query(User).filter_by(email=email).first():
                db.add(User(email=email, name="مدير تجريبي" if role == "admin" else "مؤسسة مثال للتشطيبات", role=role,
                            password_hash=hash_password("demo-password-123"),
                            trades=["الدهانات", "الجبس والأسقف المستعارة"] if role == "user" else [],
                            cities=["ينبع", "جدة"] if role == "user" else []))
        db.commit()
    print("demo data ready (staging only). users: admin@demo.example.sa / contractor@demo.example.sa, password demo-password-123")


if __name__ == "__main__":
    main()
