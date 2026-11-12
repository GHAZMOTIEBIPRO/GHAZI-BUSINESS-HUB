import datetime as dt

from hub.db import utcnow
from hub.models import Opportunity, Source


def source(session, key="feed", rights="verified_public_reuse", trust="review_all", kind="json_feed",
           feed_url="https://feed.example.sa/items.json", **kw):
    s = Source(key=key, name=f"مصدر {key}", homepage="https://feed.example.sa", kind=kind, feed_url=feed_url,
               rights=rights, trust=trust, **kw)
    session.add(s)
    session.commit()
    return s


def approved_opp(session, title="ترميم مدرسة — أعمال دهانات وبلاط", city="جدة", trades=None, contact=None, **kw):
    src = session.query(Source).filter_by(key="manual-review").one()
    now = utcnow()
    o = Opportunity(source_id=src.id, external_id=title, fingerprint=title, title=title, city=city, region="مكة المكرمة",
                    trades=trades or ["الدهانات", "البلاط والسيراميك"], opportunity_type=kw.pop("opportunity_type", "OPEN_TENDER"),
                    source_url="https://tenders.example.sa/1", verified_at=now, review_due_at=now + dt.timedelta(days=7),
                    review_state="approved", contact=contact, **kw)
    session.add(o)
    session.commit()
    return o


GOOD_CONTACT = {"public_business_name": "إدارة المشتريات", "role": "مشتريات", "public_business_email": "rfq@contractor.example.sa",
                "public_business_phone": None, "source_url": "https://contractor.example.sa/rfq", "verified_at": "2026-11-01T00:00:00",
                "display_allowed": True}
