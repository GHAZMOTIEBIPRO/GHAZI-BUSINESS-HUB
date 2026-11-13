"""Procurement archive (issue #2): import modes, relations, entity rules, rights, resume, rollback, history, search.

All fixtures are SYNTHETIC (names start with «اختبار»). The real 2026-11-13 package is not in this public
repository (its reuse rights are unverified); `test_real_package_counts` runs it only when
HUB_TEST_TENDER_PACKAGE points at a local copy.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from hub import db as dbm
from hub.archive import normalize as N
from hub.archive.importer import Importer, Interrupted, PackageError, load_package
from hub.archive.models import (
    ChangeHistory,
    Company,
    ImportBatch,
    ProcurementTender,
    TenderAward,
    TenderBid,
)
from hub.archive.search import TenderFilters, search_tenders

from .conftest import csrf_of, login, make_staff, new_client, register

TODAY = dt.date(2026, 11, 13)
T_COLS = ["tender_reference", "title", "issuer", "city_or_location", "announced_status", "published_date",
          "submission_deadline", "award_date", "winner_name_as_published", "award_amount_sar_as_published",
          "offer_count_claimed", "bidders_name_count_public", "source_url", "source_publisher", "verification_notes",
          "source_rights_status", "public_publish_allowed", "review_status"]
B_COLS = ["tender_reference", "bidder_name_as_published", "bid_price_sar_as_published", "technical_or_award_result",
          "bid_status", "source_url", "source_rights_status", "review_status"]


def tender(ref, title, *, status="تمت الترسية", city="ينبع", issuer="جهة اختبار أ", pub="2025-03-01", deadline="2025-04-01",
           award="2025-06-01", winner="", amount="", claimed="", public=""):
    return dict(tender_reference=ref, title=title, issuer=issuer, city_or_location=city, announced_status=status,
                published_date=pub, submission_deadline=deadline, award_date=award, winner_name_as_published=winner,
                award_amount_sar_as_published=amount, offer_count_claimed=claimed, bidders_name_count_public=public,
                source_url=f"https://example.test/tender/{ref}", source_publisher="ناشر اختبار", verification_notes="",
                source_rights_status="true", public_publish_allowed="true", review_status="x")


def bid(ref, name, price, result="مطابق", status="متقدم غير فائز"):
    return dict(tender_reference=ref, bidder_name_as_published=name, bid_price_sar_as_published=price,
                technical_or_award_result=result, bid_status=status, source_url=f"https://example.test/tender/{ref}",
                source_rights_status="unverified", review_status="x")


def base_rows():
    tenders = [
        tender("900000000001", "توريد دهانات لمباني اختبار", winner="شركة اختبار الدهانات", amount="1000", claimed="5", public="3",
               pub="2024-02-01", award="2024-05-01", city="ينبع الصناعية"),
        tender("900000000002", "ترميم وتهيئة مبنى اختبار", winner="مؤسسة اختبار الترميم", amount="2500000", claimed="2", public="2",
               city="جدة", issuer="جهة اختبار ب"),
        tender("900000000003", "صيانة مباني اختبار", winner="", amount="", claimed="4", public="2", city="الرياض"),  # awarded, winner unpublished
        tender("900000000004", "تركيب بلاط وأسقف جبسية اختبار", status="معلنة - التقديم بحسب المصدر", award="",
               pub="2026-11-01", deadline="2026-12-01", winner="", public="0"),
        tender("900000000005", "أعمال أبواب اختبار", status="معلنة - التقديم بحسب المصدر", award="", pub="2026-01-01",
               deadline="2026-02-01", public="0"),  # deadline passed: closed, not open
    ]
    bids = [
        bid("900000000001", "شركة اختبار الدهانات", "1000", "فائز", "فائز"),
        bid("900000000001", "شركة اختبار  الدهانات المحدودة", "900"),   # cheaper, NOT the winner
        bid("900000000001", "شركة الألوان اختبار", "1200"),
        bid("900000000002", "مؤسسة اختبار الترميم", "2500000", "فائز", "فائز"),
        bid("900000000002", "شركة الملونة اختبار", "2600000"),
        bid("900000000003", "شركة الملونه اختبار", "50"),   # ة/ه spelling variant of a different row
        bid("900000000003", "شركة الألوان اختبار", "40"),
    ]
    companies = sorted({b["bidder_name_as_published"] for b in bids} | {t["winner_name_as_published"] for t in tenders
                                                                       if t["winner_name_as_published"]})
    return tenders, bids, [{"company_name_as_published": c} for c in companies]


def _csv(rows, cols):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def make_package(path: Path, tenders, bids, companies, *, manifest=None, drop=None) -> Path:
    files = {"import/tenders_staging.csv": _csv(tenders, T_COLS), "import/bids_staging.csv": _csv(bids, B_COLS),
             "import/companies_staging.csv": _csv(companies, ["company_name_as_published"]),
             "manifest.json": json.dumps(manifest or {"tender_records": len(tenders), "bid_records": len(bids),
                                                      "company_names": len(companies)}).encode()}
    if drop:
        files.pop(drop)
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in files.items():
            zf.writestr(name, blob)
    return path


@pytest.fixture()
def pkg(tmp_path):
    return make_package(tmp_path / "pkg.zip", *base_rows())


@pytest.fixture()
def importer(database):
    return Importer(dbm.SessionLocal, today=TODAY, chunk_size=2)


def counts(s):
    return {m.__name__: s.scalar(select(func.count()).select_from(m))
            for m in (ProcurementTender, TenderBid, TenderAward, Company)}


# ------------------------------------------------------------------ import modes and counts
def test_dry_run_reads_and_writes_nothing(importer, pkg, session):
    r = importer.dry_run(pkg)
    assert r["read"] == {"tenders": 5, "bids": 7, "companies": 6} and r["rejected_count"] == 0
    assert r["status_counts"] == {"awarded": 3, "announced": 2}
    assert counts(session) == {"ProcurementTender": 0, "TenderBid": 0, "TenderAward": 0, "Company": 0}
    assert session.scalar(select(func.count(ImportBatch.id))) == 0


def test_staging_counts_relations_and_privacy(importer, pkg, session):
    r = importer.stage(pkg)
    assert counts(session) == {"ProcurementTender": 5, "TenderBid": 7, "TenderAward": 2, "Company": 6}
    assert r["batch_counts"]["tenders_new"] == 5 and r["database"]["archive_totals"]["publicly_visible_tenders"] == 0
    t1 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000001")).one()
    assert {b.bidder_name_raw for b in t1.bids} == {"شركة اختبار الدهانات", "شركة اختبار الدهانات المحدودة", "شركة الألوان اختبار"}
    assert [a.winner_name_raw for a in t1.awards] == ["شركة اختبار الدهانات"]
    assert t1.bidders_known == 3 and t1.offer_count_claimed == 5 and t1.bid_list_complete is False
    assert t1.city == "ينبع الصناعية" and t1.region == "المدينة المنورة" and "الدهانات" in t1.work_types
    for t in session.scalars(select(ProcurementTender)):
        assert t.publish_allowed is False and t.visibility == "private_staging" and t.source_rights_status == "unverified"
    per = {x["ref"]: x for x in r["database"]["per_tender"]}
    assert per["900000000002"]["bid_list_complete"] is True and per["900000000002"]["companies"]


def test_winner_is_never_inferred_from_the_lowest_bid(importer, pkg, session):
    importer.stage(pkg)
    t1 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000001")).one()
    cheapest = min(t1.bids, key=lambda b: b.bid_amount_sar)
    assert cheapest.bidder_name_raw != t1.awards[0].winner_name_raw and cheapest.price_rank_among_published == 1
    t3 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000003")).one()
    assert t3.status == "awarded" and t3.awards == []  # winner not published -> no award, even with bids


def test_running_tenders_have_no_winner_and_lifecycle_is_separate(importer, tmp_path, session):
    tenders, bids, companies = base_rows()
    tenders[3]["winner_name_as_published"] = "شركة اختبار مزعومة"  # a winner on an announced tender is refused
    bids.append(bid("900000000004", "شركة اختبار مزعومة", "10", "فائز", "فائز"))
    r = importer.stage(make_package(tmp_path / "p.zip", tenders, bids, companies))
    t4 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000004")).one()
    assert t4.awards == [] and t4.lifecycle == "A_open"
    assert t4.bids[0].bid_status.startswith("غير محدد")
    t5 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000005")).one()
    assert t5.lifecycle == "B_closed"  # announced at collection time but the deadline has passed
    assert {"C_awarded", "A_open", "B_closed"} == set(r["lifecycle_counts"]) and "D_subcontract_confirmed" not in r["lifecycle_counts"]
    assert any("لمنافسة غير مرساة" in w["reason"] for w in r["warnings"])


def test_rerun_is_idempotent(importer, pkg, session):
    importer.stage(pkg)
    before = counts(session)
    r2 = importer.stage(pkg)
    assert counts(session) == before
    assert r2["batch_counts"].get("tenders_new", 0) == 0 and r2["batch_counts"]["tenders_existing_unchanged"] == 5
    assert r2["batch_counts"]["bids_existing_unchanged"] == 7


def test_validate_mode_is_read_only(importer, pkg, session):
    importer.stage(pkg)
    before = counts(session)
    r = importer.validate_against_db(pkg)
    assert r["database"]["tenders_already_in_archive"] == 5 and r["database"]["tenders_would_be_new"] == 0
    assert counts(session) == before


# ------------------------------------------------------------------ rejects and missing data
def test_rejects_with_reasons_and_partial_lists(importer, tmp_path, session):
    tenders, bids, companies = base_rows()
    tenders.append(tender("", "بلا رقم"))
    tenders.append(tender("900000000001", "مكرر"))                     # duplicate in file
    tenders.append(tender("bad ref; DROP", "رقم غير صالح"))
    tenders.append(tender("900000000009", "تاريخ خاطئ", pub="2025-13-45", amount="abc", winner="شركة اختبار ز"))
    bids.append(bid("900000000077", "شركة يتيمة", "5"))                # orphan bid
    bids.append(bid("900000000002", "", "5"))                          # missing bidder name
    bids.append(bid("900000000002", "مؤسسة اختبار الترميم", "1", "فائز", "فائز"))  # duplicate bid
    r = importer.stage(make_package(tmp_path / "p.zip", tenders, bids, companies))
    reasons = [x["reason"] for x in r["rejected"]]
    assert r["rejected_count"] == 4 and any("مفقود" in x for x in reasons) and any("يتيم" in x for x in reasons)
    assert any("مكرر داخل الملف" in w["reason"] for w in r["warnings"]) and r["duplicates_in_file"] == {"tenders": 1, "bids": 1}
    t9 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000009")).one()
    assert t9.published_date is None and t9.awards[0].award_amount_sar is None   # bad values -> unknown, never guessed
    assert r["missing_fields"]["award_amount"] >= 2
    assert r["bidders"]["claimed_offers_total"] == 5 + 2 + 4


# ------------------------------------------------------------------ entity resolution
def test_name_variants_are_flagged_not_merged(importer, pkg, session):
    r = importer.stage(pkg)
    names = {c.display_name for c in session.scalars(select(Company))}
    assert "شركة الملونة اختبار" in names and "شركة الملونه اختبار" in names           # two rows, not merged
    assert "شركة اختبار  الدهانات المحدودة" in names or "شركة اختبار الدهانات المحدودة" in names
    groups = r["database"]["possible_same_entity_groups"]
    assert ["شركة الملونة اختبار", "شركة الملونه اختبار"] in groups
    assert all(c.identity_status == "name_only_unverified" and c.cr_number is None for c in session.scalars(select(Company)))
    # whitespace-only differences are the same published name
    assert N.name_key("شركة  اختبار") == N.name_key("شركة اختبار") and N.name_key("شركة ـاختبار") == N.name_key("شركة اختبار")
    assert N.name_key("شركة الملونة") != N.name_key("شركة الملونه") and N.fold_key("شركة الملونة") == N.fold_key("شركة الملونه")


# ------------------------------------------------------------------ resume, history, rollback, commit
def test_resume_after_interruption_gives_the_same_result(database, pkg, session):
    crashed = Importer(dbm.SessionLocal, today=TODAY, chunk_size=2, fail_after_chunks=2)
    with pytest.raises(Interrupted):
        crashed.stage(pkg)
    batch = session.scalars(select(ImportBatch)).one()
    assert batch.status == "in_progress" and batch.checkpoint["next"] == 4
    assert session.scalar(select(func.count(ProcurementTender.id))) == 4
    with pytest.raises(PackageError):  # a second fresh run must not start beside the unfinished one
        Importer(dbm.SessionLocal, today=TODAY).stage(pkg)
    r = Importer(dbm.SessionLocal, today=TODAY, chunk_size=2).stage(pkg, resume_batch_id=batch.id)
    session.expire_all()
    assert counts(session) == {"ProcurementTender": 5, "TenderBid": 7, "TenderAward": 2, "Company": 6}
    assert r["batch_counts"]["tenders_new"] == 5 and r["batch_counts"]["bids_new"] == 7


def test_resume_refuses_a_different_file(database, pkg, tmp_path):
    with pytest.raises(Interrupted):
        Importer(dbm.SessionLocal, today=TODAY, chunk_size=1, fail_after_chunks=1).stage(pkg)
    tenders, bids, companies = base_rows()
    tenders[0]["title"] = "مختلف"
    other = make_package(tmp_path / "other.zip", tenders, bids, companies)
    with pytest.raises(PackageError, match="بصمة"):
        Importer(dbm.SessionLocal, today=TODAY).stage(other, resume_batch_id=1)


def test_status_change_is_kept_in_history_and_rollback_restores_it(importer, tmp_path, session):
    first = importer.stage(make_package(tmp_path / "a.zip", *base_rows()))
    tenders, bids, companies = base_rows()
    tenders[3].update(announced_status="تمت الترسية", award_date="2026-12-20", winner_name_as_published="شركة اختبار جديدة",
                      award_amount_sar_as_published="777")
    bids[0]["bid_price_sar_as_published"] = "1001"
    companies.append({"company_name_as_published": "شركة اختبار جديدة"})
    second = importer.stage(make_package(tmp_path / "b.zip", tenders, bids, companies))
    t4 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000004")).one()
    assert t4.status == "awarded" and t4.awards[0].award_amount_sar == 777
    hist = {(h.field, h.old_value, h.new_value) for h in session.scalars(select(ChangeHistory).where(
        ChangeHistory.batch_id == second["batch_id"]))}
    assert ("status", "announced", "awarded") in hist and ("bid_amount_sar", "1000.0", "1001.0") in hist

    importer.rollback(second["batch_id"])
    session.expire_all()
    t4 = session.get(ProcurementTender, t4.id)
    assert t4.status == "announced" and t4.awards == [] and t4.lifecycle == "A_open"
    assert session.scalars(select(TenderBid).where(TenderBid.bidder_name_raw == "شركة اختبار الدهانات")).one().bid_amount_sar == 1000
    assert not session.scalars(select(Company).where(Company.display_name == "شركة اختبار جديدة")).first()

    importer.rollback(first["batch_id"])
    assert counts(session) == {"ProcurementTender": 0, "TenderBid": 0, "TenderAward": 0, "Company": 0}


def test_commit_moves_to_private_archive_only(importer, pkg, session):
    r = importer.stage(pkg)
    out = importer.commit(r["batch_id"])
    assert out["tenders_moved_to_private_archive"] == 5 and out["publicly_visible"] == 0
    session.expire_all()
    assert {(t.visibility, t.publish_allowed) for t in session.scalars(select(ProcurementTender))} == {("archive_private", False)}
    with pytest.raises(PackageError):
        importer.commit(r["batch_id"])  # already committed


# ------------------------------------------------------------------ bad packages
def test_broken_packages_fail_cleanly(importer, tmp_path):
    with pytest.raises(PackageError, match="غير موجود"):
        load_package(make_package(tmp_path / "x.zip", *base_rows(), drop="import/bids_staging.csv"))
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("import/tenders_staging.csv", "a,b\n1,2\n")
        zf.writestr("import/bids_staging.csv", "a\n")
        zf.writestr("import/companies_staging.csv", "company_name_as_published\n")
    with pytest.raises(PackageError, match="أعمدة ناقصة"):
        importer.dry_run(bad)
    (tmp_path / "notzip.zip").write_text("hello")
    with pytest.raises(PackageError):
        importer.dry_run(tmp_path / "notzip.zip")


# ------------------------------------------------------------------ search and injection
def test_search_filters(importer, pkg, session):
    importer.stage(pkg)

    def refs(**kw):
        rows, total = search_tenders(session, TenderFilters(**kw))
        return {t.official_reference for t in rows}
    assert refs(year=2024) == {"900000000001"}
    assert refs(city="جدة") == {"900000000002"}
    assert refs(trade="الدهانات") == {"900000000001"}
    assert refs(company="الملونه") == {"900000000003"}
    assert refs(company="الألوان") == {"900000000001", "900000000003"}
    assert refs(min_value=1_000_000) == {"900000000002"}
    assert refs(issuer="جهة اختبار ب") == {"900000000002"}
    assert refs(lifecycle="A_open") == {"900000000004"}


def test_sql_injection_and_wildcards_are_inert(importer, pkg, session):
    importer.stage(pkg)
    for q in ("'; DROP TABLE procurement_tenders; --", "%", "_", "\\", "' OR 1=1 --"):
        rows, total = search_tenders(session, TenderFilters(q=q, company=q, issuer=q))
        assert total == 0, q
    assert session.scalar(select(func.count(ProcurementTender.id))) == 5


# ------------------------------------------------------------------ web: access and screens
def test_archive_is_staff_only(app, importer, pkg, session):
    importer.stage(pkg)
    anon = new_client(app)
    assert anon.get("/admin/archive").status_code in (303, 401, 403, 404)
    user = new_client(app)
    register(user, "member@example.sa")
    for path in ("/admin/archive", "/admin/archive/companies", "/admin/archive/sources", "/admin/archive/import",
                 "/admin/archive/tenders/1", "/admin/archive/companies/1"):
        r = user.get(path)
        assert r.status_code in (303, 403, 404), path
        assert "900000000001" not in r.text
    # nothing from the archive leaks into the public radar
    assert "اختبار الدهانات" not in anon.get("/radar").text


def test_admin_screens_and_upload(app, database, pkg, session):
    make_staff(session, "boss@example.sa", "admin")
    c = new_client(app)
    login(c, "boss@example.sa", "staff-password-1")
    token = csrf_of(c, "/admin/archive/import")
    blob = Path(pkg).read_bytes()
    r = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging"},
               files={"file": ("pkg.zip", blob, "application/zip")})
    assert r.status_code == 200 and "importReport" in r.text, r.text[:400]
    home = c.get("/admin/archive")
    assert home.status_code == 200 and "900000000001" in home.text and 'id="publicCount">0<' in home.text
    t1 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "900000000001")).one()
    page = c.get(f"/admin/archive/tenders/{t1.id}")
    assert "partialNote" in page.text and "شركة اختبار الدهانات" in page.text
    company = session.scalars(select(Company).where(Company.display_name == "شركة الملونة اختبار")).one()
    cp = c.get(f"/admin/archive/companies/{company.id}")
    assert cp.status_code == 200 and "similarNote" in cp.text and "identityBadge" in cp.text
    src = c.get("/admin/archive/sources")
    assert src.status_code == 200 and "مصادر آلية مفعلة: 0" in src.text and "src-etimad" in src.text
    batch = session.scalars(select(ImportBatch)).one()
    token = csrf_of(c, f"/admin/archive/batches/{batch.id}")
    assert c.post(f"/admin/archive/batches/{batch.id}/rollback", data={"csrf": token}).status_code == 303
    assert session.scalar(select(func.count(ProcurementTender.id))) == 0
    # reviewers can read but not import
    make_staff(session, "rev@example.sa", "reviewer")
    rc = new_client(app)
    login(rc, "rev@example.sa", "staff-password-1")
    assert rc.get("/admin/archive").status_code == 200 and rc.get("/admin/archive/import").status_code in (403, 404)


def test_upload_requires_csrf(app, database, pkg, session):
    make_staff(session, "boss2@example.sa", "admin")
    c = new_client(app)
    login(c, "boss2@example.sa", "staff-password-1")
    r = c.post("/admin/archive/import", data={"mode": "staging"}, files={"file": ("pkg.zip", Path(pkg).read_bytes())})
    assert r.status_code in (400, 403)
    assert session.scalar(select(func.count(ImportBatch.id))) == 0


# ------------------------------------------------------------------ migrations
def test_migration_downgrade_and_upgrade(database):
    from hub.archive.migrate import downgrade, upgrade
    downgrade(dbm.engine, "base")
    with dbm.engine.connect() as conn:
        assert "procurement_tenders" not in dbm.engine.dialect.get_table_names(conn)
    upgrade(dbm.engine)
    with dbm.engine.connect() as conn:
        tables = set(dbm.engine.dialect.get_table_names(conn))
        assert {"procurement_tenders", "procurement_issuers", "companies", "company_aliases", "tender_bids", "tender_awards",
                "procurement_sources", "source_licenses", "import_batches", "import_rejects", "verification_events",
                "change_history", "subcontract_opportunities"} <= tables
        assert conn.execute(text("select version_num from alembic_version_archive")).scalar() == "0002"


# ------------------------------------------------------------------ the real package (local only)
@pytest.mark.skipif(not os.environ.get("HUB_TEST_TENDER_PACKAGE"), reason="real package is not in the public repo")
def test_real_package_counts(database, session):
    imp = Importer(dbm.SessionLocal, today=TODAY, chunk_size=10)
    r = imp.stage(os.environ["HUB_TEST_TENDER_PACKAGE"])
    assert r["read"] == {"tenders": 38, "bids": 138, "companies": 121} and r["rejected_count"] == 0
    assert all(v["match"] for v in r["manifest_check"].values())
    assert counts(session) == {"ProcurementTender": 38, "TenderBid": 138, "TenderAward": 30, "Company": 121}
    assert r["status_counts"] == {"awarded": 30, "announced": 8}
    assert r["database"]["archive_totals"]["publicly_visible_tenders"] == 0
    assert r["companies"]["csv_names_not_linked"] == [] and r["companies"]["linked_names_not_in_csv"] == []
