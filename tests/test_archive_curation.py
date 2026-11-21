"""Archive curation: automatic lifecycle (stored dates only), CR-only identity and merge, lifecycle D with two people.

All fixtures are SYNTHETIC (see tests/test_archive.py).
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import func, select

from hub import db as dbm
from hub import jobs
from hub.archive import curation
from hub.archive.curation import CurationError
from hub.archive.importer import Importer
from hub.archive.models import (
    ChangeHistory,
    Company,
    CompanyAlias,
    ProcurementTender,
    SubcontractOpportunity,
    TenderAward,
    TenderBid,
)

from .conftest import csrf_of, login, make_staff, new_client
from .test_archive import TODAY, base_rows, make_package

EVIDENCE = "https://registry.example.test/cr/1010101010"


@pytest.fixture()
def staged(database, tmp_path):
    Importer(dbm.SessionLocal, today=TODAY, chunk_size=2).stage(make_package(tmp_path / "p.zip", *base_rows()))
    return dbm


def company(s, name) -> Company:
    return s.scalars(select(Company).where(Company.display_name == name)).one()


def no_network():
    return httpx.MockTransport(lambda request: httpx.Response(500))


# ------------------------------------------------------------------ lifecycle
def test_lifecycle_closes_only_open_tenders_past_their_own_deadline(staged, session):
    open_t = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000004")).one()
    assert open_t.lifecycle == "A_open" and open_t.submission_deadline == dt.date(2026, 12, 1)
    before = dict(session.execute(select(ProcurementTender.official_reference, ProcurementTender.lifecycle)).all())
    assert curation.refresh_lifecycle(session, dt.date(2026, 12, 1)) == {"archive_closed": 0}  # deadline day still open
    assert curation.refresh_lifecycle(session, dt.date(2026, 12, 2)) == {"archive_closed": 1}
    session.commit()
    after = dict(session.execute(select(ProcurementTender.official_reference, ProcurementTender.lifecycle)).all())
    assert after.pop("900000000004") == "B_closed"
    before.pop("900000000004")
    assert after == before  # awarded and already-closed rows untouched
    h = session.scalars(select(ChangeHistory).where(ChangeHistory.field == "lifecycle")).one()
    assert (h.old_value, h.new_value, h.event_id is not None) == ("A_open", "B_closed", True)
    assert curation.refresh_lifecycle(session, dt.date(2026, 12, 3)) == {"archive_closed": 0}  # idempotent


def test_scheduled_refresh_runs_the_archive_lifecycle(staged, session):
    out = jobs.refresh(session, now=dt.datetime(2026, 12, 5, 6, 0), transport=no_network())
    assert out["archive_closed"] == 1 and out["subcontracts_closed"] == 0


# ------------------------------------------------------------------ identity and merges
def test_identity_needs_valid_number_and_https_evidence(staged, session):
    c = company(session, "شركة الألوان اختبار")
    for kwargs, msg in [({"cr_number": "123"}, "10 أرقام"), ({"unified_number": "1234567890"}, "يبدأ بـ 7"),
                        ({}, "أدخل رقم")]:
        with pytest.raises(CurationError, match=msg):
            curation.verify_identity(session, c.id, evidence_url=EVIDENCE, by="t", **kwargs)
    with pytest.raises(CurationError, match="https"):
        curation.verify_identity(session, c.id, cr_number="1010101010", evidence_url="http://x.test", by="t")
    curation.verify_identity(session, c.id, cr_number="١٠١٠١٠١٠١٠", evidence_url=EVIDENCE, by="t")  # Arabic digits
    session.commit()
    assert (c.cr_number, c.identity_status) == ("1010101010", "registry_verified")
    with pytest.raises(CurationError, match="رقم مختلف"):
        curation.verify_identity(session, c.id, cr_number="2020202020", evidence_url=EVIDENCE, by="t")


def test_similar_names_merge_only_on_the_same_verified_number_and_unmerge_restores(staged, session):
    a, b = company(session, "شركة الملونة اختبار"), company(session, "شركة الملونه اختبار")  # ة/ه variants
    a_bids = {x.id for x in session.scalars(select(TenderBid).where(TenderBid.company_id == a.id))}
    b_bids = {x.id for x in session.scalars(select(TenderBid).where(TenderBid.company_id == b.id))}
    with pytest.raises(CurationError, match="متحققًا"):  # no number anywhere: never merged by name
        curation.merge_companies(session, b.id, a.id, registry_number="1010101010", evidence_url=EVIDENCE, by="t")
    curation.verify_identity(session, a.id, cr_number="1010101010", evidence_url=EVIDENCE, by="t")
    with pytest.raises(CurationError) as conflict:  # same number typed on the other row is a conflict, not a silent merge
        curation.verify_identity(session, b.id, cr_number="1010101010", evidence_url=EVIDENCE, by="t")
    assert conflict.value.conflict_company_id == a.id
    with pytest.raises(CurationError, match="متحققًا"):
        curation.merge_companies(session, b.id, a.id, registry_number="9999999999", evidence_url=EVIDENCE, by="t")
    curation.merge_companies(session, b.id, a.id, registry_number="1010101010", evidence_url=EVIDENCE, by="t")
    session.commit()
    assert {x.id for x in session.scalars(select(TenderBid).where(TenderBid.company_id == a.id))} == a_bids | b_bids
    assert b.merged_into_id == a.id and b.identity_status == "merged_same_registry"
    assert {al.alias_raw for al in session.scalars(select(CompanyAlias).where(CompanyAlias.company_id == a.id))} == {
        "شركة الملونة اختبار", "شركة الملونه اختبار"}
    moved = curation.unmerge_company(session, b.id, by="t")
    session.commit()
    assert moved == len(b_bids)
    assert {x.id for x in session.scalars(select(TenderBid).where(TenderBid.company_id == b.id))} == b_bids
    assert {al.alias_raw for al in session.scalars(select(CompanyAlias).where(CompanyAlias.company_id == a.id))} == {
        "شركة الملونة اختبار"}
    assert b.merged_into_id is None and b.identity_status == "name_only_unverified"


def test_different_numbers_are_never_merged(staged, session):
    a, b = company(session, "شركة الألوان اختبار"), company(session, "شركة الملونة اختبار")
    curation.verify_identity(session, a.id, cr_number="1010101010", evidence_url=EVIDENCE, by="t")
    curation.verify_identity(session, b.id, cr_number="2020202020", evidence_url=EVIDENCE, by="t")
    with pytest.raises(CurationError, match="كيانان مختلفان"):
        curation.merge_companies(session, b.id, a.id, registry_number="1010101010", evidence_url=EVIDENCE, by="t")


def test_reimport_after_merge_attaches_to_survivor_and_rollback_keeps_curated_rows(database, tmp_path, session):
    tenders, bids, companies = base_rows()
    imp = Importer(dbm.SessionLocal, today=TODAY, chunk_size=2)
    imp.stage(make_package(tmp_path / "p1.zip", tenders, bids, companies))
    a, b = company(session, "شركة الملونة اختبار"), company(session, "شركة الملونه اختبار")
    curation.verify_identity(session, a.id, cr_number="1010101010", evidence_url=EVIDENCE, by="t")
    curation.merge_companies(session, b.id, a.id, registry_number="1010101010", evidence_url=EVIDENCE, by="t")
    session.commit()
    new_tender = dict(tenders[1], tender_reference="900000000009", title="ترميم إضافي اختبار")
    new_bid = dict(bids[5], tender_reference="900000000009")  # spelled like the merged row
    r = imp.stage(make_package(tmp_path / "p2.zip", [*tenders, new_tender], [*bids, new_bid], companies))
    session.expire_all()
    t9 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000009")).one()
    assert [x.company_id for x in t9.bids] == [a.id]
    imp.rollback(r["batch_id"])
    session.expire_all()
    assert session.get(Company, a.id) is not None and session.get(Company, b.id) is not None


# ------------------------------------------------------------------ lifecycle D
def test_subcontract_needs_evidence_and_a_second_person(staged, session):
    with pytest.raises(CurationError, match="https"):
        curation.add_subcontract(session, user_id=1, title="دهانات باطن", main_contractor_name="مقاول اختبار",
                                 evidence_kind="company_announcement", evidence_url="")
    with pytest.raises(CurationError, match="نوع الدليل"):
        curation.add_subcontract(session, user_id=1, title="دهانات باطن", main_contractor_name="مقاول اختبار",
                                 evidence_kind="guess", evidence_url=EVIDENCE)
    op = curation.add_subcontract(session, user_id=1, title="دهانات باطن", main_contractor_name="مقاول اختبار",
                                  evidence_kind="company_announcement", evidence_url=EVIDENCE, city="ينبع الصناعية",
                                  response_deadline="2026-12-10")
    assert op.review_status == "pending_second_review" and op.publish_allowed is False and op.region == "المدينة المنورة"
    with pytest.raises(CurationError, match="شخص غير"):
        curation.review_subcontract(session, op.id, user_id=1, decision="confirmed")
    curation.review_subcontract(session, op.id, user_id=2, decision="confirmed")
    with pytest.raises(CurationError, match="مسبقًا"):
        curation.review_subcontract(session, op.id, user_id=3, decision="rejected")
    assert curation.close_due_subcontracts(session, dt.date(2026, 12, 11)) == 1 and op.status == "closed"


def test_awarded_tenders_never_become_subcontract_opportunities(staged, session):
    assert session.scalar(select(func.count(TenderAward.id))) == 2
    curation.refresh_lifecycle(session, dt.date(2027, 1, 1))
    jobs.refresh(session, now=dt.datetime(2027, 1, 1), transport=no_network())
    assert session.scalar(select(func.count(SubcontractOpportunity.id))) == 0
    assert session.scalar(select(func.count(ProcurementTender.id)).where(
        ProcurementTender.lifecycle == "D_subcontract_confirmed")) == 0


# ------------------------------------------------------------------ screens
def test_identity_and_merge_screens_admin_only_with_csrf(app, staged, session):
    a, b = company(session, "شركة الملونة اختبار"), company(session, "شركة الملونه اختبار")
    make_staff(session, "boss@example.sa", "admin")
    make_staff(session, "rev@example.sa", "reviewer")
    rev = new_client(app)
    login(rev, "rev@example.sa", "staff-password-1")
    page = rev.get(f"/admin/archive/companies/{a.id}")
    assert page.status_code == 200 and "identityForm" not in page.text
    token = csrf_of(rev, f"/admin/archive/companies/{a.id}")
    assert rev.post(f"/admin/archive/companies/{a.id}/identity",
                    data={"csrf": token, "cr_number": "1010101010", "evidence_url": EVIDENCE}).status_code in (403, 404)
    c = new_client(app)
    login(c, "boss@example.sa", "staff-password-1")
    assert c.post(f"/admin/archive/companies/{a.id}/identity",
                  data={"cr_number": "1010101010", "evidence_url": EVIDENCE}).status_code in (400, 403)  # no CSRF
    token = csrf_of(c, f"/admin/archive/companies/{a.id}")
    r = c.post(f"/admin/archive/companies/{a.id}/identity", data={"csrf": token, "cr_number": "1010101010", "evidence_url": EVIDENCE})
    assert r.status_code == 303
    r = c.post(f"/admin/archive/companies/{b.id}/identity", data={"csrf": token, "cr_number": "1010101010", "evidence_url": EVIDENCE})
    assert r.status_code == 303 and f"conflict={a.id}" in r.headers["location"]
    page = c.get(r.headers["location"])
    assert "mergeForm" in page.text
    r = c.post(f"/admin/archive/companies/{b.id}/merge",
               data={"csrf": token, "target_id": str(a.id), "registry_number": "1010101010", "evidence_url": EVIDENCE})
    assert r.status_code == 303
    assert "mergedNote" in c.get(f"/admin/archive/companies/{b.id}").text
    assert "mergedFrom" in c.get(f"/admin/archive/companies/{a.id}").text
    assert c.post(f"/admin/archive/companies/{b.id}/unmerge", data={"csrf": token}).status_code == 303
    session.expire_all()
    assert session.get(Company, b.id).merged_into_id is None


def test_subcontract_screens_two_people(app, staged, session):
    make_staff(session, "rev1@example.sa", "reviewer")
    make_staff(session, "rev2@example.sa", "reviewer")
    one, two = new_client(app), new_client(app)
    login(one, "rev1@example.sa", "staff-password-1")
    login(two, "rev2@example.sa", "staff-password-1")
    page = one.get("/admin/archive/subcontracts")
    assert page.status_code == 200 and "dRule" in page.text
    token = csrf_of(one, "/admin/archive/subcontracts")
    bad = one.post("/admin/archive/subcontracts", data={"csrf": token, "title": "أسقف جبسية باطن", "main_contractor_name": "مقاول",
                                                        "evidence_kind": "company_announcement", "evidence_url": "ftp://x"})
    assert bad.status_code == 400 and "formError" in bad.text and "أسقف جبسية باطن" in bad.text
    ok = one.post("/admin/archive/subcontracts", data={"csrf": token, "title": "أسقف جبسية باطن", "main_contractor_name": "مقاول",
                                                       "evidence_kind": "company_announcement", "evidence_url": EVIDENCE,
                                                       "trade": "الجبس والأسقف المستعارة", "city": "جدة"})
    assert ok.status_code == 303
    op = session.scalars(select(SubcontractOpportunity)).one()
    assert "تحتاج مراجعًا آخر" in one.get("/admin/archive/subcontracts").text
    r = one.post(f"/admin/archive/subcontracts/{op.id}/review", data={"csrf": token, "decision": "confirmed"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    session.expire_all()
    assert session.get(SubcontractOpportunity, op.id).review_status == "pending_second_review"
    token2 = csrf_of(two, "/admin/archive/subcontracts")
    assert two.post(f"/admin/archive/subcontracts/{op.id}/review", data={"csrf": token2, "decision": "confirmed"}).status_code == 303
    session.expire_all()
    assert session.get(SubcontractOpportunity, op.id).review_status == "confirmed"
    home = two.get("/admin/archive")
    assert home.status_code == 200
    anon = new_client(app)
    assert anon.get("/admin/archive/subcontracts").status_code in (303, 401, 403)
