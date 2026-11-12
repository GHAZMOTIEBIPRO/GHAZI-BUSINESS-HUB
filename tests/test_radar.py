"""Source gating, fetching, normalisation, de-duplication, review, staleness, health alerts and user alerts."""
import datetime as dt
import json

import httpx
from sqlalchemy import func, select

from hub import jobs, radar
from hub.db import utcnow
from hub.models import EmailOutbox, FetchRun, Notification, Opportunity, Subscription, User

from .conftest import login, make_staff, new_client, post, register
from .factories import GOOD_CONTACT, approved_opp, source

ROBOTS_OK = "User-agent: *\nAllow: /\n"


def feed_transport(items, robots=ROBOTS_OK, status=200, calls=None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(status, text=json.dumps({"items": items}))
    return httpx.MockTransport(handler)


def item(i=1, **kw):
    d = {"external_id": f"T-{i}", "title": f"منافسة ترميم وصيانة مدرسة رقم {i} — أعمال دهانات وجبس", "city": "ينبع",
         "owner_entity": "جهة حكومية مثال", "url": f"https://feed.example.sa/t/{i}", "published_at": "2026-11-01",
         "deadline_at": (utcnow() + dt.timedelta(days=20)).strftime("%Y-%m-%d")}
    d.update(kw)
    return d


def test_unverified_rights_never_fetched(session):
    s = source(session, rights="unknown")
    calls = []
    run = radar.run_source(session, s, transport=feed_transport([item()], calls=calls))
    assert run.outcome == "gated" and calls == []
    assert radar.source_health(s, utcnow()) == "gated"


def test_seeded_official_sources_are_link_only(session):
    for key in ("etimad", "muqawil-market", "muqawil-consortium"):
        s = session.scalar(select(radar.Source).where(radar.Source.key == key))
        assert s.rights == "unknown" and s.kind == "manual"
        assert radar.fetch_source(s).outcome == "skipped"


def test_robots_disallow_is_respected(session):
    s = source(session)
    calls = []
    run = radar.run_source(session, s, transport=feed_transport([item()], robots="User-agent: *\nDisallow: /\n", calls=calls))
    assert run.outcome == "gated" and calls == ["/robots.txt"]


def test_refusal_is_recorded_not_retried(session):
    s = source(session)
    calls = []
    run = radar.run_source(session, s, transport=feed_transport([], status=403, calls=calls))
    assert run.outcome == "refused" and calls.count("/items.json") == 1
    assert s.consecutive_failures == 1 and "403" in s.last_error


def test_ingest_classify_review_and_idempotent_rerun(session):
    s = source(session)
    tr = feed_transport([item(1), item(2, title="", url="https://feed.example.sa/t/2"),
                         item(3, url="http://insecure.example.sa/x")])
    run = radar.run_source(session, s, transport=tr)
    assert (run.outcome, run.items_seen, run.items_new) == ("success", 3, 1)
    assert "تجاهله" in run.reason
    o = session.scalar(select(Opportunity))
    assert o.opportunity_type == "OPEN_TENDER" and o.region == "المدينة المنورة"
    assert set(o.trades) >= {"الدهانات", "الجبس والأسقف المستعارة", "إعادة تأهيل المباني والمساكن"}
    assert o.review_state == "pending_review" and "مراجعة بشرية" in o.review_reasons[0]
    run2 = radar.run_source(session, s, transport=tr, force=True)
    assert (run2.items_new, run2.items_updated) == (0, 0)
    assert session.scalar(select(func.count(Opportunity.id))) == 1


def test_auto_publish_but_subcontract_claim_always_reviewed(session):
    s = source(session, trust="auto_publish")
    radar.run_source(session, s, transport=feed_transport([
        item(1), item(2, opportunity_type="SUBCONTRACT_CONFIRMED", title="مطلوب مقاول باطن بلاط"),
        item(3, contact={"public_business_phone": "0500000000"})]))
    by = {o.external_id: o for o in session.scalars(select(Opportunity))}
    assert by["T-1"].review_state == "approved"
    assert by["T-2"].review_state == "pending_review" and any("دليل" in r for r in by["T-2"].review_reasons)
    assert by["T-3"].review_state == "pending_review" and any("جهة اتصال" in r for r in by["T-3"].review_reasons)


def test_significant_change_returns_to_review(session):
    s = source(session, trust="auto_publish")
    radar.run_source(session, s, transport=feed_transport([item(1)]))
    o = session.scalar(select(Opportunity))
    assert o.review_state == "approved"
    new_deadline = (utcnow() + dt.timedelta(days=40)).strftime("%Y-%m-%d")
    run = radar.run_source(session, s, transport=feed_transport([item(1, deadline_at=new_deadline)]), force=True)
    assert run.items_updated == 1 and o.review_state == "pending_review" and "deadline_at" in o.review_reasons[0]


def test_cross_source_duplicate_detected(session):
    a, b = source(session, key="a"), source(session, key="b", feed_url="https://other.example.sa/f.json")
    radar.run_source(session, a, transport=feed_transport([item(1)]))
    radar.run_source(session, b, transport=feed_transport([item(9, title=item(1)["title"], url="https://other.example.sa/9")]))
    first, dup = session.scalars(select(Opportunity).order_by(Opportunity.id)).all()
    assert dup.duplicate_of_id == first.id and any("مكرر" in r for r in dup.review_reasons)
    assert any(e["url"] == "https://other.example.sa/9" for e in first.evidence)


def test_stale_and_closed_marking(session):
    o = approved_opp(session, deadline_at=utcnow() - dt.timedelta(days=1))
    o2 = approved_opp(session, title="ترميم مبنى آخر")
    o2.review_due_at = utcnow() - dt.timedelta(hours=1)
    session.commit()
    res = radar.mark_stale_and_closed(session)
    assert res == {"closed": 1, "stale": 1}
    assert o.opportunity_type == "CLOSED" and o2.review_state == "stale"


def test_failing_source_alerts_admin_then_recovers(session):
    make_staff(session, "admin@example.sa", "admin")
    s = source(session)
    t0 = utcnow()
    for i in range(2):
        jobs.refresh(session, now=t0 + dt.timedelta(hours=25 * i), transport=feed_transport([], status=500))
    assert radar.source_health(s, t0 + dt.timedelta(hours=26)) == "down"
    titles = [n.title for n in session.scalars(select(Notification).where(Notification.kind == "source_health"))]
    assert "لم ينجح بعد" in titles[0] and any("متوقف" in t for t in titles), titles
    jobs.refresh(session, now=t0 + dt.timedelta(hours=50), transport=feed_transport([item(1)]))
    assert radar.source_health(s, t0 + dt.timedelta(hours=50)) == "ok" and s.consecutive_failures == 0
    outcomes = [r.outcome for r in session.scalars(select(FetchRun).where(FetchRun.source_id == s.id).order_by(FetchRun.id))]
    assert outcomes == ["failed", "failed", "success"]


def test_not_due_source_is_skipped(session):
    s = source(session)
    t0 = utcnow()
    radar.run_source(session, s, now=t0, transport=feed_transport([item(1)]))
    run = radar.run_source(session, s, now=t0 + dt.timedelta(hours=1), transport=feed_transport([item(1)]))
    assert run.outcome == "skipped_not_due"


def test_user_alerts_match_once_and_email_only_for_paid(session):
    paint = User(email="p@example.sa", name="p", password_hash="!", trades=["الدهانات"], cities=["ينبع"])
    doors = User(email="d@example.sa", name="d", password_hash="!", trades=["الأبواب"], cities=["الرياض"])
    paid = User(email="paid@example.sa", name="x", password_hash="!", trades=["الجبس والأسقف المستعارة"], cities=[])
    session.add_all([paint, doors, paid])
    session.flush()
    session.add(Subscription(user_id=paid.id, plan="radar", starts_at=utcnow() - dt.timedelta(days=1),
                             ends_at=utcnow() + dt.timedelta(days=29)))
    s = source(session, trust="auto_publish")
    session.commit()
    jobs.refresh(session, transport=feed_transport([item(1)]))
    notes = {n.user_id for n in session.scalars(select(Notification).where(Notification.kind == "match"))}
    assert notes == {paint.id, paid.id}
    assert [e.to for e in session.scalars(select(EmailOutbox))] == ["paid@example.sa"]
    jobs.refresh(session, now=utcnow() + dt.timedelta(hours=30), transport=feed_transport([item(1)]))
    assert session.scalar(select(func.count(Notification.id)).where(Notification.kind == "match")) == 2
    _ = s


def test_public_views_hide_unreviewed_and_gate_contacts(app, session):
    approved = approved_opp(session, contact=GOOD_CONTACT)
    pending = approved_opp(session, title="سجل غير معتمد")
    pending.review_state = "pending_review"
    session.commit()
    c = new_client(app)
    listing = c.get("/radar").text
    assert approved.title in listing and "سجل غير معتمد" not in listing
    assert c.get(f"/radar/{pending.id}").status_code == 404
    page = c.get(f"/radar/{approved.id}").text
    assert "rfq@contractor.example.sa" not in page and "متاحة لمشتركي الرادار" in page
    register(c, "sub@example.sa")
    u = session.scalar(select(User).where(User.email == "sub@example.sa"))
    session.add(Subscription(user_id=u.id, plan="radar", starts_at=utcnow() - dt.timedelta(minutes=1),
                             ends_at=utcnow() + dt.timedelta(days=30)))
    session.commit()
    page = c.get(f"/radar/{approved.id}").text
    assert "rfq@contractor.example.sa" in page and "contractor.example.sa/rfq" in page
    bad = dict(GOOD_CONTACT, display_allowed=False, public_business_email="hidden@x.example.sa")
    o3 = approved_opp(session, title="جهة بلا إذن عرض", contact=bad)
    assert "hidden@x.example.sa" not in c.get(f"/radar/{o3.id}").text


def test_missing_fields_shown_as_unknown(app, session):
    o = approved_opp(session, owner_entity=None, main_contractor=None)
    page = new_client(app).get(f"/radar/{o.id}").text
    assert page.count("غير معلوم") >= 2


def test_filters(app, session):
    approved_opp(session, title="دهانات في جدة", city="جدة", trades=["الدهانات"])
    approved_opp(session, title="أبواب في الرياض", city="الرياض", trades=["الأبواب"], opportunity_type="AWARDED_PROJECT")
    approved_opp(session, title="منافسة مغلقة", city="جدة", trades=["الدهانات"], opportunity_type="CLOSED")
    c = new_client(app)
    t = c.get("/radar", params={"city": "الرياض"}).text
    assert "أبواب في الرياض" in t and "دهانات في جدة" not in t
    t = c.get("/radar", params={"trade": "الدهانات"}).text
    assert "دهانات في جدة" in t and "أبواب في الرياض" not in t and "منافسة مغلقة" not in t
    assert "منافسة مغلقة" in c.get("/radar", params={"show": "all"}).text
    t = c.get("/radar", params={"type": "AWARDED_PROJECT"}).text
    assert "أبواب في الرياض" in t and "دهانات في جدة" not in t
    t = c.get("/radar", params={"q": "أبواب"}).text
    assert "أبواب في الرياض" in t and "دهانات في جدة" not in t


def test_reviewer_workflow_and_subcontract_evidence_rule(app, session):
    make_staff(session, "rev@example.sa", "reviewer")
    c = new_client(app)
    login(c, "rev@example.sa", "staff-password-1")
    manual = session.scalar(select(radar.Source).where(radar.Source.key == "manual-review"))
    r = post(c, "/admin/opportunities/new", {"source_id": str(manual.id), "title": "مطلوب مقاول باطن جبس",
                                             "url": "https://contractor.example.sa/post/1", "city": "ينبع",
                                             "opportunity_type": "SUBCONTRACT_CONFIRMED", "trades": ["الجبس والأسقف المستعارة"]},
             csrf_from="/admin/opportunities/new")
    assert r.status_code == 303
    o = session.scalar(select(Opportunity).where(Opportunity.title == "مطلوب مقاول باطن جبس"))
    assert o.review_state == "pending_review"
    r = post(c, f"/admin/opportunities/{o.id}/review", {"decision": "approve", "opportunity_type": "SUBCONTRACT_CONFIRMED"},
             csrf_from="/admin/review")
    session.refresh(o)
    assert o.review_state == "pending_review", "no evidence -> cannot approve as confirmed subcontract"
    post(c, f"/admin/opportunities/{o.id}/review", {"decision": "approve", "opportunity_type": "OPPORTUNITY_LEAD"},
         csrf_from="/admin/review")
    session.refresh(o)
    assert o.review_state == "approved" and o.opportunity_type == "OPPORTUNITY_LEAD" and o.reviewer_id
    r = post(c, f"/radar/{o.id}/report", {"category": "closed", "details": "أُغلقت"}, csrf_from=f"/radar/{o.id}")
    assert "أُرسل البلاغ" in c.get(r.headers["location"]).text
    assert "أُغلقت" in c.get("/admin/review").text


def test_admin_dashboard_shows_source_status(app, session):
    make_staff(session, "adm@example.sa", "admin")
    s = source(session)
    radar.run_source(session, s, transport=feed_transport([], status=500))
    session.commit()
    c = new_client(app)
    login(c, "adm@example.sa", "staff-password-1")
    page = c.get("/admin").text
    assert "HTTP 500" in page and "لم ينجح بعد" in page
    assert "منصة اعتماد" in page and "يدوي" in page
