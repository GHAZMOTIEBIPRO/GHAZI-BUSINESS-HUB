"""Calculation, structure and export tests for the shipped workbooks (products/dist).

All scenarios are written to temp copies of the shipped files and recalculated by
LibreOffice in a single batch, then compared with independently computed values.
Includes the 4 regressions found in the Step 0 audit.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "products" / "dist" / "full"
SAMPLE = ROOT / "products" / "dist" / "sample"
NAMES = ["01_finishing_pricing", "02_project_costs", "03_progress_invoices", "04_labor_cost",
         "05_quotation", "06_profitability", "07_execution_tracking"]

pytestmark = pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice required")

TODAY = dt.datetime.combine(dt.date.today(), dt.time())


def day(n):
    return TODAY + dt.timedelta(days=n)


BLANK = object()  # expect empty cell

# scenario id -> (product, {sheet_index: {cell: value}}, {sheet_index: {cell: expected}})
SCENARIOS = {
    "pricing_realistic": ("01_finishing_pricing", {1: {
        "D8": 100, "E8": 10, "F8": 5,
        "D9": 50, "E9": 20,                       # labour missing -> not priced, flagged
        "D10": 10, "E10": 100, "F10": 0, "G10": 5,
    }}, {1: {"H8": 1500, "J8": 2000, "I8": 20, "H9": BLANK, "J9": BLANK, "K9": "ناقص", "K8": "مكتمل",
             "H10": 1050, "J10": 1400, "J39": 2550, "J40": 3400, "J41": 340, "J42": 510, "J43": 510,
             "J44": 3910, "J45": 4, "E3": BLANK}}),
    "pricing_bad_percentages": ("01_finishing_pricing", {1: {"C3": 0.5, "C4": 0.5, "D8": 1, "E8": 1, "F8": 1}},
                                {1: {"H8": 2, "J8": BLANK, "J44": 0,
                                     "E3": "خطأ: مجموع الربح والمصاريف يجب أن يكون أقل من 100%"}}),
    "pricing_zero_qty": ("01_finishing_pricing", {1: {"D8": 0, "E8": 10, "F8": 5}},
                         {1: {"H8": 0, "J8": 0, "I8": BLANK}}),
    "projects": ("02_project_costs", {1: {
        "A9": "P1", "B9": "مشروع 1", "D9": 100000, "E9": 80000, "F9": 90000, "G9": 60000, "H9": 50000,
        "A10": "P2", "B10": "مشروع 2", "D10": 50000, "E10": 40000, "F10": 10000, "G10": 20000,  # nothing collected
        "A11": "P3", "B11": "مشروع 3", "D11": 10000, "G11": 12000, "H11": 15000,
    }}, {1: {"I9": 20000, "J9": -30000, "K9": 1.125, "L9": 10000, "M9": "تجاوز الموازنة",
             "L10": 20000,                      # Step 0 regression (was blank)
             "M10": "ضمن الموازنة", "L11": 0, "M11": "تحصيل يتجاوز المستخلصات",
             "C3": 3, "C4": 160000, "C5": 30000, "C6": 1}}),
    "projects_overbilled": ("02_project_costs", {1: {"B9": "x", "D9": 1000, "G9": 1500, "H9": 0}},
                            {1: {"M9": "مستخلصات تتجاوز العقد", "L9": 1500}}),
    "invoices": ("03_progress_invoices", {1: {
        "C5": 0.10,
        "D10": day(-60), "E10": 100000, "K10": 50000,
        "D11": day(0), "E11": 1000, "K11": 2000,          # overpaid
        "D12": day(-10), "E12": 2000,                     # nothing collected, not yet due
        "D13": day(0), "E13": 1000, "K13": 985,           # exactly collected
        "E14": 1000, "K14": 100,                          # no date, partial
    }}, {1: {"F10": 10000, "G10": 5000, "H10": 90000, "I10": 13500, "J10": 98500, "L10": 48500, "O10": 30,
             "P10": "متأخر", "M11": 1015, "P11": "دفعة زائدة",       # Step 0 regression (was "محصل")
             "L12": 1970, "O12": 0, "P12": "غير محصل", "L13": 0, "P13": "محصل",
             "O14": BLANK, "P14": "محصل جزئيًا", "L14": 885,
             "G5": 48500 + 1970 + 885, "G6": 48500, "G7": 5000 + 50 + 100 + 50 + 50, "L3": 1015}}),
    "invoices_vat_after_retention": ("03_progress_invoices", {1: {"C4": 0.10, "C7": "بعد خصم المحتجزات", "E10": 1000}},
                                     {1: {"G10": 100, "H10": 900, "I10": 135, "J10": 1035}}),
    "labor": ("04_labor_cost", {1: {
        "B8": 4, "C8": 20, "D8": 8, "E8": 150, "F8": 10, "G8": 15, "H8": 5,
        "B9": 2, "C9": 10, "D9": 8, "E9": 200, "F9": 10, "G9": 15,       # 'other' blank
        "B10": 3, "E10": 100,                                           # days missing
        "B11": 2, "C11": 5, "D11": 0, "E11": 100,                       # zero hours
    }}, {1: {"I8": 14400, "J8": 640, "K8": 22.5, "L8": 180,
             "I9": 4500,                        # Step 0 regression (was blank)
             "I10": BLANK, "I11": 1000, "J11": 0, "K11": BLANK, "B3": 19900, "B4": 800, "B5": 19900 / 800}}),
    "quotation": ("05_quotation", {1: {"E4": day(0), "B10": "دهان", "D10": 100, "E10": 26, "B11": "باب", "D11": 2,
                                       "E11": 500, "F36": 600}},
                  {1: {"F10": 2600, "F11": 1000, "F35": 3600, "F37": 3000, "F38": 450, "F39": 3450, "E7": day(15),
                       "B36": BLANK}}),
    "quotation_big_discount": ("05_quotation", {1: {"D10": 1, "E10": 100, "F36": 500}},
                               {1: {"F37": 0, "F38": 0, "F39": 0, "B36": "تنبيه: الخصم أكبر من المجموع"}}),
    "profitability": ("06_profitability", {1: {"B4": 10000, "B5": 16000, "B21": 260000}, 2: {
        "A5": "a", "B5": 100000, "C5": 50000, "D5": 20000,
        "A6": "b", "B6": 100000, "C6": 85000,
        "A7": "c", "B7": 100000, "C7": 120000,
    }}, {1: {"B20": 26000, "B22": 0.1}, 2: {"B3": 0.1, "H5": 70000, "I5": 10000, "J5": 20000, "K5": "20.0% — مقبول",
                                            "K6": "5.0% — هامش منخفض", "K7": "-30.0% — خسارة", "J26": -5000}}),
    "profitability_no_revenue": ("06_profitability", {1: {"B4": 10000}, 2: {"B5": 1000, "C5": 400}},
                                 {1: {"B22": BLANK}, 2: {"I5": BLANK, "J5": 600}}),
    "execution": ("07_execution_tracking", {1: {
        "A9": "a", "B9": 0.5, "C9": day(-10), "D9": day(10), "E9": 0.5,
        "A10": "b", "B10": 0.5, "C10": day(-20), "D10": day(-10), "E10": 0.5,
    }}, {1: {"F9": 0.5, "G9": 0, "H9": "حسب الخطة", "F10": 1, "H10": "متأخر", "B4": 1, "C4": BLANK,
             "B5": 0.5, "B6": 0.75}}),
    "execution_bad_weights": ("07_execution_tracking", {1: {"A9": "a", "B9": 0.6, "C9": day(-1), "D9": day(1), "E9": 1}},
                              {1: {"C4": "تنبيه: مجموع الأوزان يجب أن يساوي 100%", "H9": "منجز"}}),
}


def _soffice_convert(files, out_dir, fmt="xlsx"):
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run(["soffice", f"-env:UserInstallation=file://{prof}", "--headless", "--calc", "--convert-to", fmt,
                        "--outdir", str(out_dir), *map(str, files)], check=True, capture_output=True, timeout=600)


@pytest.fixture(scope="module")
def recalculated(tmp_path_factory):
    work = tmp_path_factory.mktemp("scenarios")
    (work / "out").mkdir()
    files = []
    for sid, (product, inputs, _) in SCENARIOS.items():
        wb = openpyxl.load_workbook(FULL / f"{product}.xlsx")
        for idx, cells in inputs.items():
            ws = wb.worksheets[idx]
            for ref, v in cells.items():
                ws[ref] = v
        path = work / f"{sid}.xlsx"
        wb.save(path)
        files.append(path)
    _soffice_convert(files, work / "out")
    return {sid: openpyxl.load_workbook(work / "out" / f"{sid}.xlsx", data_only=True) for sid in SCENARIOS}


def _norm(v):
    if isinstance(v, dt.datetime):
        return v.date()
    return v


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_scenario(recalculated, sid):
    wb = recalculated[sid]
    failures = []
    for idx, cells in SCENARIOS[sid][2].items():
        ws = wb.worksheets[idx]
        for ref, want in cells.items():
            got = ws[ref].value
            if want is BLANK:
                ok = got in (None, "")
            elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
                ok = abs(got - want) <= 1e-6 * max(1, abs(want))
            else:
                ok = _norm(got) == _norm(want)
            if not ok:
                failures.append(f"{ws.title}!{ref}: got {got!r}, want {want!r}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("folder", [FULL, SAMPLE], ids=["full", "sample"])
def test_structure(name, folder):
    path = folder / f"{name}.xlsx"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert not any("externalLink" in n for n in names), "external links"
        assert not any(n.endswith(".bin") and "vba" in n.lower() for n in names), "macros"
    wb = openpyxl.load_workbook(path)
    wbv = openpyxl.load_workbook(path, data_only=True)
    formula_count = 0
    for ws in wb.worksheets:
        assert ws.sheet_view.rightToLeft, f"{ws.title} not RTL"
        assert ws.protection.sheet, f"{ws.title} not protected"
        assert ws.page_setup.paperSize in (9, "9"), f"{ws.title} not A4"
        assert ws.sheet_properties.pageSetUpPr.fitToPage, f"{ws.title} not fit-to-page"
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    formula_count += 1
                    assert c.protection.locked, f"{ws.title}!{c.coordinate} formula unlocked"
                v = wbv[ws.title][c.coordinate].value
                assert not (isinstance(v, str) and v.startswith("#") and v.endswith(("!", "?", "A"))), \
                    f"{ws.title}!{c.coordinate} error {v}"
    assert formula_count > 0
    main = wb.worksheets[1]
    unlocked = sum(1 for row in main.iter_rows() for c in row if c.protection and not c.protection.locked)
    if folder is FULL:
        assert unlocked > 10, "full version must have input cells"
        assert main.print_area, "print area missing"
    else:
        assert unlocked == 0, "sample must be fully locked"


@pytest.mark.parametrize("name", NAMES)
def test_cached_values_present(name):
    """Previewers (phone, mail, Drive) show cached values; totals must not be empty."""
    wbv = openpyxl.load_workbook(SAMPLE / f"{name}.xlsx", data_only=True)
    wb = openpyxl.load_workbook(SAMPLE / f"{name}.xlsx")
    numeric = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    if isinstance(wbv[ws.title][c.coordinate].value, (int, float)):
                        numeric += 1
    assert numeric > 0, "no cached numeric results in sample"


def test_pdf_export(tmp_path):
    files = [FULL / f"{n}.xlsx" for n in NAMES]
    _soffice_convert(files, tmp_path, "pdf")
    for n in NAMES:
        pdf = tmp_path / f"{n}.pdf"
        assert pdf.exists() and pdf.stat().st_size > 2000, n
        assert pdf.read_bytes()[:4] == b"%PDF"
