"""Open-data tables (XLSX / CSV with the publisher's own headers) into the archive. Fixtures are SYNTHETIC."""
from __future__ import annotations

import datetime as dt

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from hub import db as dbm
from hub.archive import open_data
from hub.archive.importer import Importer, PackageError
from hub.archive.models import ImportBatch, ProcurementTender, TenderAward

from .conftest import csrf_of, login, make_staff, new_client

TODAY = dt.date(2026, 11, 21)


def xlsx(path, headers, rows, title_rows=0):
    wb = Workbook()
    ws = wb.active
    for _ in range(title_rows):
        ws.append(["قائمة اختبار"])  # a banner line above the header, as some publishers do
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


CONTRACT_HEADERS = ["رقم العقد", "اسم العقد", "الجهة", "المدينة", "اسم المورد", "قيمة العقد", "تاريخ توقيع العقد"]
CONTRACT_ROWS = [
    ["C-2023-001", "صيانة مباني اختبار", "جهة اختبار", "ينبع", "مؤسسة اختبار للصيانة", 1250000.5, dt.datetime(2023, 3, 1)],
    ["C-2023-002", "توريد دهانات اختبار", "جهة اختبار", "جدة", "شركة اختبار الدهانات", "980,000", "15/06/2023"],
    ["C-2023-003", "ترميم مبنى اختبار", "جهة اختبار", "الرياض", "", "", 45000],  # no supplier: no award; Excel serial
]


def test_detect_mapping_handles_variants_and_spacing():
    m = open_data.detect_mapping(["  رقم المنافسه ", "اسم_المنافسة", "الجهة الحكومية", "آخر موعد لتقديم العروض",
                                  "Status", "Supplier Name", "غير ذي صلة"])
    assert m == {"tender_reference": "  رقم المنافسه ", "title": "اسم_المنافسة", "issuer": "الجهة الحكومية",
                 "submission_deadline": "آخر موعد لتقديم العروض", "announced_status": "Status",
                 "winner_name_as_published": "Supplier Name"}


def test_contract_list_creates_awards_only_for_named_suppliers(database, tmp_path, session):
    path = xlsx(tmp_path / "contracts.xlsx", CONTRACT_HEADERS, CONTRACT_ROWS, title_rows=1)
    pkg = open_data.to_package(path, publisher="جهة اختبار (بيانات مفتوحة)", source_url="https://example.test/dataset")
    assert pkg.manifest["mapping"]["winner_name_as_published"] == "اسم المورد"
    by_ref = {t["tender_reference"]: t for t in pkg.tenders}
    assert by_ref["C-2023-001"]["award_date"] == "2023-03-01" and by_ref["C-2023-002"]["award_date"] == "2023-06-15"
    assert by_ref["C-2023-003"]["award_date"] == "2023-03-15"  # Excel serial 45000
    assert by_ref["C-2023-001"]["award_amount_sar_as_published"] == "1250000.5"
    report = Importer(dbm.SessionLocal, source_key="saudi_open_data", today=TODAY).stage(pkg)
    assert report["batch_counts"]["tenders_new"] == 3 and report["batch_counts"]["awards_new"] == 2
    awards = dict(session.execute(select(TenderAward.winner_name_raw, TenderAward.award_amount_sar)).all())
    assert awards == {"مؤسسة اختبار للصيانة": 1250000.5, "شركة اختبار الدهانات": 980000.0}
    t3 = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "C-2023-003")).one()
    assert t3.status == "unknown" and not t3.awards  # nothing guessed for a row without a supplier
    assert session.scalar(select(func.count(ProcurementTender.id)).where(ProcurementTender.publish_allowed.is_(True))) == 0
    assert {t.source_publisher for t in session.scalars(select(ProcurementTender))} == {"جهة اختبار (بيانات مفتوحة)"}


def test_tender_list_without_supplier_never_creates_a_winner(database, tmp_path, session):
    path = xlsx(tmp_path / "tenders.xlsx", ["رقم المنافسة", "اسم المنافسة", "الجهة", "حالة المنافسة", "تاريخ النشر",
                                            "آخر موعد لتقديم العروض"],
                [["900100", "أعمال أسقف جبسية اختبار", "جهة اختبار", "منشورة", "2026-11-01", "2026-12-01"],
                 ["900101", "أعمال بلاط اختبار", "جهة اختبار", "تم الترسية", "2026-01-01", "2026-02-01"]])
    Importer(dbm.SessionLocal, source_key="saudi_open_data", today=TODAY).stage(
        open_data.to_package(path, publisher="اختبار"))
    rows = {t.official_reference: (t.status, t.lifecycle) for t in session.scalars(select(ProcurementTender))}
    assert rows == {"900100": ("announced", "A_open"), "900101": ("awarded", "C_awarded")}
    assert session.scalar(select(func.count(TenderAward.id))) == 0


def test_contract_status_column_with_a_supplier_is_an_award(tmp_path):
    path = xlsx(tmp_path / "c.xlsx", ["رقم العقد", "اسم العقد", "حالة العقد", "اسم المتعاقد"],
                [["K-1", "صيانة اختبار", "ساري", "مقاول اختبار"], ["K-2", "نظافة اختبار", "ملغى", "مقاول اختبار"]])
    by_ref = {t["tender_reference"]: t for t in open_data.to_package(path, publisher="x").tenders}
    assert by_ref["K-1"]["announced_status"] == open_data.AWARDED_WITHOUT_STATUS
    assert by_ref["K-2"]["announced_status"] == "ملغى"


def test_cp1256_csv_and_reimport_is_idempotent(database, tmp_path, session):
    text = "رقم المنافسة,اسم المنافسة,المدينة\n700001,صيانة اختبار,ينبع الصناعية\n700002,ترميم اختبار,جدة\n"
    path = tmp_path / "list.csv"
    path.write_bytes(text.encode("cp1256"))
    imp = Importer(dbm.SessionLocal, source_key="saudi_open_data", today=TODAY)
    first = imp.stage(open_data.to_package(path, publisher="اختبار"))
    again = imp.stage(open_data.to_package(path, publisher="اختبار"))
    assert first["batch_counts"]["tenders_new"] == 2 and again["batch_counts"].get("tenders_new", 0) == 0
    assert session.scalar(select(func.count(ProcurementTender.id))) == 2
    assert session.scalars(select(ProcurementTender.region).where(ProcurementTender.official_reference == "700001")).one() == "المدينة المنورة"


def test_rows_without_a_number_get_a_labelled_internal_key_and_keep_their_columns(database, tmp_path, session):
    # the shape described by the Etimad library: name, description, sector, entity, start date — no tender number
    headers = ["Tender Name", "Tender Description", "Sector", "Entity", "Start Date", "Number of bids that passed evaluation"]
    rows = [["صيانة مدارس اختبار", "وصف", "التعليم", "جهة اختبار", dt.datetime(2024, 5, 1), 3],
            ["ترميم مبنى اختبار", "وصف", "البلديات", "جهة اختبار", dt.datetime(2024, 6, 1), 0]]
    pkg = open_data.to_package(xlsx(tmp_path / "tenders2024.xlsx", headers, rows), publisher="المركز الوطني لنظم الموارد الحكومية")
    refs = [t["tender_reference"] for t in pkg.tenders]
    assert all(r.startswith("OD-") and len(r) == 15 for r in refs) and len(set(refs)) == 2
    again = open_data.to_package(tmp_path / "tenders2024.xlsx", publisher="المركز الوطني لنظم الموارد الحكومية")
    assert [t["tender_reference"] for t in again.tenders] == refs  # stable across re-imports
    first = pkg.tenders[0]
    assert first["offer_count_claimed"] == ""  # "passed evaluation" is not the number of offers submitted
    assert first["src:Sector"] == "التعليم" and first["src:Number of bids that passed evaluation"] == "3"
    assert "مشتق" in first["verification_notes"]
    Importer(dbm.SessionLocal, source_key="etimad_open_data", today=TODAY).stage(pkg)
    t = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == refs[0])).one()
    assert t.raw["src:Sector"] == "التعليم" and t.published_date == dt.date(2024, 5, 1) and t.issuer.name == "جهة اختبار"
    assert "المدارس" in t.title or "مدارس" in t.title


MONTHLY_HEADERS = ["Entity Name", "Sector", "Tender Title", "Status", "Supplier Name", "Tender Value", "Creation Date",
                   "Award Date"]
MONTHLY_ROWS = [
    ["جهة اختبار للتعليم", "التعليم", "صيانة وترميم مدارس اختبار", "Awarded", "مؤسسة اختبار للمقاولات", 2350000,
     dt.datetime(2025, 1, 5), dt.datetime(2025, 2, 10)],
    ["جهة اختبار بلدية", "البلديات", "توريد وتركيب أسقف جبسية اختبار", "Under Evaluation", "", 0,
     dt.datetime(2025, 2, 1), None],
    ["جهة اختبار بلدية", "البلديات", "أعمال دهانات اختبار", "Cancelled", "", "", dt.datetime(2025, 2, 3), None],
]


def test_catalog_lists_the_official_datasets():
    from hub.archive import catalog
    data = catalog.catalog()
    assert len(data["datasets"]) == 34 and data["by_id"]["0409048941305"]["priority"] == 1
    assert all(d["url"].startswith("https://open.data.gov.sa/") for d in data["datasets"])
    assert len({d["id"] for d in data["datasets"]}) == 34


def test_upload_by_catalog_dataset_records_publisher_link_and_awards(app, database, tmp_path, session):
    path = xlsx(tmp_path / "feb2025.xlsx", MONTHLY_HEADERS, MONTHLY_ROWS)
    make_staff(session, "boss@example.sa", "admin")
    c = new_client(app)
    login(c, "boss@example.sa", "staff-password-1")
    assert "datasetSelect" in c.get("/admin/archive/import").text
    sources = c.get("/admin/archive/sources")
    assert "catalogTable" in sources.text and 'id="ds-0409048941305"' in sources.text and "لم يُرفع" in sources.text
    token = csrf_of(c, "/admin/archive/import")
    zip_try = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging", "dataset": "0409048941305"},
                     files={"file": ("x.zip", b"PK", "application/zip")})
    assert zip_try.status_code == 400
    ok = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging", "dataset": "0409048941305",
                                               "source": "research_package_2026_11_13"},
                files={"file": ("download.xlsx", path.read_bytes(), "application/octet-stream")})
    assert ok.status_code == 200 and "importReport" in ok.text, ok.text[:400]
    tenders = {t.title: t for t in session.scalars(select(ProcurementTender))}
    awarded = tenders["صيانة وترميم مدارس اختبار"]
    assert (awarded.status, awarded.lifecycle, awarded.award_date) == ("awarded", "C_awarded", dt.date(2025, 2, 10))
    assert [(a.winner_name_raw, a.award_amount_sar) for a in awarded.awards] == [("مؤسسة اختبار للمقاولات", 2350000.0)]
    assert awarded.source_url == "https://open.data.gov.sa/en/datasets/view/9e99c257-76a7-4f77-b0d5-9f15dfd1800c"
    assert awarded.source.key == "etimad_open_data" and "اعتماد" in awarded.source_publisher
    assert awarded.official_reference.startswith("OD-") and awarded.raw["src:Sector"] == "التعليم"
    assert tenders["توريد وتركيب أسقف جبسية اختبار"].status == "closed"
    assert tenders["أعمال دهانات اختبار"].status == "cancelled"
    assert session.scalar(select(func.count(TenderAward.id))) == 1 and not any(t.publish_allowed for t in tenders.values())
    batch = session.scalars(select(ImportBatch)).one()
    assert batch.label == "0409048941305 — Tenders Data by Feb 2025"
    after = c.get("/admin/archive/sources").text
    assert f'href="/admin/archive/batches/{batch.id}"' in after


PO_HEADERS = ["وحدة التشغيل", "رقم امر الشراء", "وصف امر الشراء", "السجل التجتري للمورد", "اسم المورد",
              "مبلغ التعميد", "سنة التعميد", "تاريخ الترسية"]


def po_workbook(path, sheets):
    wb = Workbook()
    summary = wb.active
    summary.title = "الاحصائيات"  # a totals sheet without a title column: skipped
    summary.append(["وحدة التشغيل", "2019", "2020", "المجموع"])
    summary.append(["الديوان", 10, 12, 22])
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(PO_HEADERS)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def test_purchase_orders_across_sheets_attach_supplier_cr_safely(database, tmp_path, session):
    from hub.archive.models import Company, VerificationEvent
    path = po_workbook(tmp_path / "po.xlsx", {
        "الديوان": [["الديوان", "4500000001", "صيانة مبنى اختبار", "1010101010", "مؤسسة اختبار للصيانة", 120000.0, "2020",
                     "2020-03-01"],
                    ["الديوان", "4500000002", "دهانات اختبار", "١٠١٠١٠١٠١٠", "مؤسسة اختبار  للصيانة", 50000.0, "2021",
                     "2021-01-10"]],  # same CR, spelled with two spaces: same name_key -> same company
        "المدينة المنورة": [["فرع المدينة", "4500000003", "توريد بلاط اختبار", "2020202020", "شركة اختبار للبلاط", 75000.0,
                             "2019", "2019-11-20"],
                            ["فرع المدينة", "4500000004", "ترميم اختبار", "1010101010", "مؤسسة الاختبار للصيانة والترميم",
                             90000.0, "2019", "2019-12-01"],  # another name with an already-held CR: review only
                            ["فرع المدينة", "4500000005", "أبواب اختبار", "12345", "شركة اختبار الأبواب", 1000.0, "2019",
                             "2019-12-05"]]})  # invalid CR: warning, no number stored
    pkg = open_data.to_package(path, publisher="بيانات مفتوحة (اختبار)", source_url="https://example.test/po")
    assert pkg.manifest["sheets_used"] == ["الديوان", "المدينة المنورة"] and pkg.manifest["sheets_skipped"] == ["الاحصائيات"]
    imp = Importer(dbm.SessionLocal, source_key="saudi_open_data", today=TODAY)
    report = imp.stage(pkg)
    counts = report["batch_counts"]
    assert counts["tenders_new"] == 5 and counts["awards_new"] == 5 and counts["companies_cr_verified"] == 2
    assert counts["cr_shared_with_other_name"] == 1
    assert any("ليس 10 أرقام" in w["reason"] for w in report["warnings"])
    verified = {c.display_name: (c.cr_number, c.identity_status) for c in session.scalars(select(Company))}
    assert verified["مؤسسة اختبار للصيانة"] == ("1010101010", "registry_verified")
    assert verified["شركة اختبار للبلاط"] == ("2020202020", "registry_verified")
    assert verified["مؤسسة الاختبار للصيانة والترميم"] == (None, "name_only_unverified")
    assert verified["شركة اختبار الأبواب"] == (None, "name_only_unverified")
    review = session.scalars(select(VerificationEvent).where(VerificationEvent.event == "same_cr_as_company")).one()
    assert "1010101010" in review.result
    awards = session.scalars(select(TenderAward)).all()
    assert {a.verification_status for a in awards} == {"official_open_data"}
    t = session.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == "4500000003")).one()
    assert t.issuer.name == "فرع المدينة" and t.raw["src:sheet"] == "المدينة المنورة" and t.status == "awarded"
    imp.rollback(report["batch_id"])
    session.expire_all()
    assert session.scalar(select(func.count(Company.id))) == 0  # CRs restored, then the batch's own rows removed


def test_unrecognised_headers_are_refused_with_what_was_found(tmp_path):
    path = xlsx(tmp_path / "x.xlsx", ["العمود الأول", "العمود الثاني"], [["a", "b"]])
    with pytest.raises(PackageError, match="title.*العمود الأول"):
        open_data.to_package(path, publisher="x")
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(PackageError, match="XLSX أو XLS أو CSV"):
        open_data.to_package(tmp_path / "x.pdf", publisher="x")


def test_admin_uploads_an_open_data_xlsx(app, database, tmp_path, session):
    path = xlsx(tmp_path / "contracts.xlsx", CONTRACT_HEADERS, CONTRACT_ROWS)
    make_staff(session, "boss@example.sa", "admin")
    c = new_client(app)
    login(c, "boss@example.sa", "staff-password-1")
    page = c.get("/admin/archive/import")
    assert "openDataFields" in page.text and 'value="saudi_open_data"' in page.text
    token = csrf_of(c, "/admin/archive/import")
    files = {"file": ("moh_contracts.xlsx", path.read_bytes(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    no_pub = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging", "source": "saudi_open_data"},
                    files=files)
    assert no_pub.status_code == 400 and "الجهة المالكة" in no_pub.text
    bad_src = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging", "source": "etimad",
                                                    "publisher": "x"}, files=files)
    assert bad_src.status_code == 400  # a link-only source cannot receive uploads
    ok = c.post("/admin/archive/import", data={"csrf": token, "mode": "staging", "source": "saudi_open_data",
                                               "publisher": "جهة اختبار", "source_url": "https://example.test/ds"},
                files=files)
    assert ok.status_code == 200 and "importReport" in ok.text, ok.text[:300]
    assert session.scalar(select(func.count(TenderAward.id))) == 2
    assert session.scalars(select(ImportBatch.label)).one() == "moh_contracts.xlsx"
