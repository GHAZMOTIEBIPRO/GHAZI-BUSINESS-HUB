"""Archive insights (counts over the sample, no rates) and the private staff CSV export. Fixtures are SYNTHETIC."""
from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import select

from hub import db as dbm
from hub.archive.importer import Importer
from hub.archive.search import TenderFilters, _csv_cell, export_rows, insights
from hub.models import AuditLog

from .conftest import login, make_staff, new_client, register
from .test_archive import TODAY, base_rows, make_package, tender


@pytest.fixture()
def staged(database, tmp_path):
    Importer(dbm.SessionLocal, today=TODAY, chunk_size=2).stage(make_package(tmp_path / "p.zip", *base_rows()))
    return dbm


def test_insights_counts_match_the_fixture(staged, session):
    ins = insights(session, TenderFilters())
    assert ins["tenders"] == 5 and ins["by_lifecycle"] == {"C_awarded": 3, "A_open": 1, "B_closed": 1}
    assert (ins["awards_with_amount"], ins["award_min"], ins["award_median"], ins["award_max"]) == (2, 1000.0, 1250500.0, 2500000.0)
    assert ins["awarded_with_partial_bid_list"] == 2  # 5 claimed/3 known and 4 claimed/2 known
    top = {r["company"].display_name: (r["bids"], r["wins"]) for r in ins["companies"]}
    assert top["شركة اختبار الدهانات"] == (1, 1) and top["شركة الألوان اختبار"] == (2, 0)
    assert top["شركة اختبار الدهانات المحدودة"] == (1, 0)  # cheaper bid, still no win
    assert [r["wins"] for r in ins["companies"]] == sorted((r["wins"] for r in ins["companies"]), reverse=True)
    assert dict(ins["issuers"]) == {"جهة اختبار أ": 4, "جهة اختبار ب": 1}


def test_insights_follow_the_filters(staged, session):
    yanbu = insights(session, TenderFilters(city="ينبع الصناعية"))
    assert yanbu["tenders"] == 1 and yanbu["award_median"] == 1000.0 and len(yanbu["companies"]) == 3
    paint = insights(session, TenderFilters(trade="الدهانات"))
    assert paint["tenders"] == 1
    none = insights(session, TenderFilters(city="لا توجد"))
    assert none["tenders"] == 0 and none["award_median"] is None and none["companies"] == []


def test_export_rows_and_formula_cells_are_neutralised(database, tmp_path, session):
    tenders, bids, companies = base_rows()
    tenders.append(tender("900000000007", "=HYPERLINK(\"https://evil.test\")", winner="", public="0"))
    Importer(dbm.SessionLocal, today=TODAY).stage(make_package(tmp_path / "p.zip", tenders, bids, companies))
    rows = export_rows(session, TenderFilters())
    assert len(rows) == 7 and rows[0][0] == "المرجع"
    by_ref = {r[0]: r for r in rows[1:]}
    assert by_ref["900000000007"][1].startswith("'=")
    assert by_ref["900000000001"][10] == "شركة اختبار الدهانات" and by_ref["900000000001"][11] == "1000.0"
    assert [_csv_cell(v) for v in ("+1", "-1", "@x", "آمن", None, 5)] == ["'+1", "'-1", "'@x", "آمن", "", "5"]
    assert len(export_rows(session, TenderFilters(city="جدة"))) == 2


def test_insights_and_export_are_staff_only_and_logged(app, staged, session):
    anon = new_client(app)
    assert anon.get("/admin/archive/insights").status_code in (303, 401, 403)
    assert anon.get("/admin/archive/export.csv").status_code in (303, 401, 403)
    user = new_client(app)
    register(user, "user@example.sa")
    assert user.get("/admin/archive/export.csv").status_code in (303, 403, 404)
    make_staff(session, "rev@example.sa", "reviewer")
    c = new_client(app)
    login(c, "rev@example.sa", "staff-password-1")
    page = c.get("/admin/archive/insights?city=" + "جدة")
    assert page.status_code == 200 and "insightsCaveat" in page.text and 'id="insTenders">1<' in page.text
    assert "exportCsv" in c.get("/admin/archive").text
    r = c.get("/admin/archive/export.csv?city=" + "جدة")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "no-store" in r.headers["cache-control"] and "attachment" in r.headers["content-disposition"]
    parsed = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert "غير مرخص لإعادة النشر" in parsed[0][0] and parsed[1][0] == "المرجع"
    assert [row[0] for row in parsed[2:]] == ["900000000002"]
    log = session.scalars(select(AuditLog).where(AuditLog.action == "archive.export_csv")).one()
    assert log.target == "rows=1" and log.actor_id is not None
