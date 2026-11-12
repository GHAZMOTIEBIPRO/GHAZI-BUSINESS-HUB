"""Build the GHAZI contractor workbooks from code.

Every formula lives here so it can be reviewed and tested. Output:
  products/build/raw/<file>.xlsx        openpyxl output (no cached values)
  products/dist/full/<file>.xlsx        recalculated by LibreOffice (cached values present)
  products/dist/sample/<file>.xlsx      5-row sample with example data, fully locked

usage: python products/build_workbooks.py [--no-recalc]
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.worksheet.datavalidation import DataValidation

HERE = Path(__file__).resolve().parent
RAW = HERE / "build" / "raw"
DIST = HERE / "dist"
VERSION = "1.0.0"

NAVY = "0A1E31"
TEAL = "0F8C7E"
INPUT_FILL = PatternFill("solid", fgColor="FFF7DB")
CALC_FILL = PatternFill("solid", fgColor="EEF2F6")
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
TOTAL_FILL = PatternFill("solid", fgColor="DDF3EF")
THIN = Side(style="thin", color="C9D3DD")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FONT = "Arial"
MONEY = '#,##0.00'
PCT = '0.0%'
DATE = 'yyyy-mm-dd'

SAMPLE_BANNER = "نسخة عينة للتجربة — 5 صفوف فقط، والأرقام أمثلة توضيحية وليست أسعار سوق. النسخة الكاملة غير مقفلة الحقول."


# ---------------------------------------------------------------- helpers
class Sheet:
    """Thin wrapper that applies the house style and records input/formula cells."""

    def __init__(self, ws, widths: dict[str, float], landscape=True):
        self.ws = ws
        ws.sheet_view.rightToLeft = True
        ws.sheet_view.showGridLines = False
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        ps = ws.page_setup
        ps.paperSize = ws.PAPERSIZE_A4
        ps.orientation = "landscape" if landscape else "portrait"
        ps.fitToWidth = 1
        ps.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.horizontalCentered = True
        ws.page_margins.left = ws.page_margins.right = 0.4
        ws.page_margins.top = ws.page_margins.bottom = 0.5
        ws.oddFooter.center.text = "صفحة &P من &N"
        ws.oddFooter.right.text = f"غازي للأعمال — الإصدار {VERSION}"
        ws.protection.sheet = True
        ws.protection.formatColumns = False  # allow resizing columns
        ws.protection.formatRows = False
        ws.protection.selectLockedCells = False
        ws.protection.selectUnlockedCells = False

    def title(self, text, cols="A1:H1", sub=None):
        ws = self.ws
        ws.merge_cells(cols)
        c = ws[cols.split(":")[0]]
        c.value = text
        c.font = Font(name=FONT, size=15, bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = Alignment(horizontal="right", vertical="center")
        ws.row_dimensions[1].height = 30
        if sub:
            s = ws["A2"]
            s.value = sub
            s.font = Font(name=FONT, size=9, italic=True, color="6B7785")

    def label(self, ref, text, bold=True):
        c = self.ws[ref]
        c.value = text
        c.font = Font(name=FONT, bold=bold, color=NAVY)
        c.alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)
        return c

    def inp(self, ref, value=None, fmt=None):
        c = self.ws[ref]
        if value is not None:
            c.value = value
        c.fill = INPUT_FILL
        c.border = BOX
        c.protection = Protection(locked=False)
        c.font = Font(name=FONT)
        c.alignment = Alignment(horizontal="right", vertical="center")
        if fmt:
            c.number_format = fmt
        return c

    def calc(self, ref, formula, fmt=None, total=False):
        c = self.ws[ref]
        c.value = formula
        c.fill = TOTAL_FILL if total else CALC_FILL
        c.border = BOX
        c.protection = Protection(locked=True, hidden=False)
        c.font = Font(name=FONT, bold=total)
        c.alignment = Alignment(horizontal="right", vertical="center")
        if fmt:
            c.number_format = fmt
        return c

    def header(self, row, names: list[str], start_col=1):
        for i, name in enumerate(names):
            c = self.ws.cell(row=row, column=start_col + i, value=name)
            c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
            c.fill = PatternFill("solid", fgColor=TEAL)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = BOX
        self.ws.row_dimensions[row].height = 34
        self.ws.print_title_rows = f"{row}:{row}"
        self.ws.freeze_panes = self.ws.cell(row=row + 1, column=1)

    def validate(self, sqref, kind, lo=None, hi=None, prompt=None, values=None):
        if kind == "list":
            dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
        elif kind == "date":
            dv = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=True)
        else:
            dv = DataValidation(type="decimal", operator="between", formula1=str(lo), formula2=str(hi), allow_blank=True)
        dv.error = prompt or "قيمة غير صالحة"
        dv.errorTitle = "تحقق من الإدخال"
        dv.showErrorMessage = True
        self.ws.add_data_validation(dv)
        dv.add(sqref)

    def note(self, ref, text):
        c = self.ws[ref]
        c.value = text
        c.font = Font(name=FONT, size=9, color="8A5A00")
        c.alignment = Alignment(horizontal="right", wrap_text=True, vertical="top")


def instructions(wb, title, lines):
    ws = wb.create_sheet("تعليمات", 0)
    s = Sheet(ws, {"A": 110}, landscape=False)
    s.title(title, "A1:A1")
    rows = [
        "الخلايا الصفراء للإدخال، والرمادية والخضراء محسوبة ومقفلة لمنع التعديل غير المقصود.",
        "لإلغاء القفل: مراجعة ← إلغاء حماية الورقة (بلا كلمة مرور). أعد الحماية بعد التعديل.",
        *lines,
        "هذه أداة إدارية مساعدة؛ راجع النتائج مع محاسبك أو مهندسك قبل الاعتماد عليها في تعاقد أو إقرار ضريبي.",
        "لا يحتوي الملف على أسعار مواد أو أجور سوقية؛ كل الأرقام من إدخالك.",
        f"الإصدار {VERSION} — غازي للأعمال. ترخيص استخدام لمنشأة واحدة، ويمنع إعادة البيع أو النشر.",
    ]
    for i, line in enumerate(rows, start=3):
        c = ws.cell(row=i, column=1, value=f"• {line}")
        c.font = Font(name=FONT, size=11)
        c.alignment = Alignment(horizontal="right", wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 30
    return ws


# ---------------------------------------------------------------- 01 pricing
def build_pricing(n=30, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "التسعير"
    s = Sheet(ws, {"A": 10, "B": 30, "C": 8, "D": 10, "E": 12, "F": 12, "G": 12, "H": 15, "I": 14, "J": 16, "K": 22})
    s.title("غازي | تسعير أعمال التشطيبات", "A1:K1", sub="الربح والمصاريف العامة نسبة من سعر البيع قبل الضريبة.")
    s.label("A3", "هامش الربح من سعر البيع")
    s.inp("C3", 0.15, PCT)
    s.label("A4", "المصاريف العامة من سعر البيع")
    s.inp("C4", 0.10, PCT)
    s.label("A5", "ضريبة القيمة المضافة")
    s.inp("C5", 0.15, PCT)
    for r in (3, 4, 5):
        ws.merge_cells(f"A{r}:B{r}")
    s.validate("C3:C4", "decimal", 0, 0.9, "النسبة بين 0% و90%")
    s.validate("C5", "decimal", 0, 1, "النسبة بين 0% و100%")
    s.calc("E3", '=IF(C3+C4>=1,"خطأ: مجموع الربح والمصاريف يجب أن يكون أقل من 100%","")')
    ws.merge_cells("E3:K3")
    ws["E3"].font = Font(name=FONT, bold=True, color="B42318")
    hr = 7
    s.header(hr, ["الكود", "وصف البند", "الوحدة", "الكمية", "مواد/وحدة", "عمالة/وحدة", "معدات/وحدة (اختياري)",
                  "التكلفة المباشرة", "سعر الوحدة", "إجمالي البيع", "اكتمال البند"])
    first, last = hr + 1, hr + n
    defaults = [("FIN-01", "دهانات داخلية", "م²"), ("FIN-02", "جبس بورد وأسقف مستعارة", "م²"),
                ("FIN-03", "بلاط وسيراميك أرضيات", "م²"), ("FIN-04", "أبواب داخلية", "عدد"),
                ("FIN-05", "معالجة وتجهيز أسطح", "م²"), ("FIN-06", "دهان خارجي", "م²")]
    for r in range(first, last + 1):
        for col in "ABC":
            s.inp(f"{col}{r}")
        for col in "DEFG":
            s.inp(f"{col}{r}", fmt=MONEY)
        s.calc(f"H{r}", f'=IF(OR(D{r}="",E{r}="",F{r}=""),"",D{r}*(E{r}+F{r}+N(G{r})))', MONEY)
        s.calc(f"J{r}", f'=IF(H{r}="","",IF($C$3+$C$4>=1,"",H{r}/(1-$C$3-$C$4)))', MONEY)
        s.calc(f"I{r}", f'=IF(OR(J{r}="",D{r}=0),"",J{r}/D{r})', MONEY)
        s.calc(f"K{r}", f'=IF(AND(B{r}="",D{r}="",E{r}="",F{r}=""),"",IF(OR(D{r}="",E{r}="",F{r}=""),"ناقص","مكتمل"))')
    for i, (code, desc, unit) in enumerate(defaults[: n]):
        r = first + i
        ws[f"A{r}"], ws[f"B{r}"], ws[f"C{r}"] = code, desc, unit
    s.validate(f"D{first}:G{last}", "decimal", 0, 1e9, "أدخل رقمًا غير سالب")
    t = last + 2
    rng = lambda c: f"{c}{first}:{c}{last}"  # noqa: E731
    totals = [
        ("إجمالي التكلفة المباشرة", f"=SUM({rng('H')})"),
        ("إجمالي البيع قبل الضريبة", f"=SUM({rng('J')})"),
        ("منها المصاريف العامة", f"=J{t + 1}*$C$4"),
        ("منها الربح المستهدف", f"=J{t + 1}*$C$3"),
        ("ضريبة القيمة المضافة", f"=J{t + 1}*$C$5"),
        ("الإجمالي شامل الضريبة", f"=J{t + 1}+J{t + 4}"),
        ("بنود ناقصة لم تُحسب", f'=COUNTIF({rng("K")},"ناقص")'),
    ]
    for i, (lab, f) in enumerate(totals):
        r = t + i
        s.label(f"H{r}", lab)
        ws.merge_cells(f"H{r}:I{r}")
        s.calc(f"J{r}", f, None if i == 6 else MONEY, total=True)
    ws.print_area = f"A1:K{t + len(totals)}"
    if sample:
        for i, row in enumerate(sample):
            r = first + i
            for col, v in zip("ABCDEFG", row, strict=False):
                ws[f"{col}{r}"] = v
    instructions(wb, "تسعير أعمال التشطيبات — طريقة الاستخدام", [
        "أدخل نسب الربح والمصاريف العامة والضريبة في أعلى ورقة التسعير.",
        "لكل بند: الكمية وتكلفة المواد والعمالة للوحدة (المعدات اختيارية). البند الناقص يظهر «ناقص» ولا يدخل الإجمالي.",
        "سعر البيع = التكلفة المباشرة ÷ (1 − الربح − المصاريف). مثال: تكلفة 1,500 مع 15% و10% = 2,000 قبل الضريبة.",
    ])
    return wb


# ---------------------------------------------------------------- 02 project costs
def build_projects(n=30, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "المشاريع"
    s = Sheet(ws, {c: w for c, w in zip("ABCDEFGHIJKLM", [11, 26, 12, 15, 15, 15, 15, 15, 15, 15, 12, 15, 24], strict=False)})
    s.title("غازي | إدارة تكاليف وربحية المشاريع", "A1:M1", sub="القيم قبل الضريبة. المستخلصات = ما صدر للعميل، التحصيل = ما استُلم فعلًا.")
    hr = 8
    first, last = hr + 1, hr + n
    rng = lambda c: f"{c}{first}:{c}{last}"  # noqa: E731
    summary = [("عدد المشاريع", f"=COUNTA({rng('B')})", None), ("إجمالي قيم العقود", f"=SUM({rng('D')})", MONEY),
               ("إجمالي المستحق غير المحصل", f"=SUM({rng('L')})", MONEY),
               ("مشاريع تتجاوز الموازنة", f'=COUNTIF({rng("M")},"تجاوز الموازنة")', None)]
    for i, (lab, f, fmt) in enumerate(summary):
        s.label(f"A{3 + i}", lab)
        ws.merge_cells(f"A{3 + i}:B{3 + i}")
        s.calc(f"C{3 + i}", f, fmt, total=True)
        ws.merge_cells(f"C{3 + i}:D{3 + i}")
    s.header(hr, ["رقم المشروع", "اسم المشروع", "المدينة", "قيمة العقد", "الموازنة", "المصروف الفعلي", "المستخلصات الصادرة",
                  "المحصل", "الربح المتوقع", "المستخلصات − المصروف", "نسبة صرف الموازنة", "مستحق غير محصل", "الحالة"])
    for r in range(first, last + 1):
        for col in "ABC":
            s.inp(f"{col}{r}")
        for col in "DEFGH":
            s.inp(f"{col}{r}", fmt=MONEY)
        s.calc(f"I{r}", f'=IF(OR(D{r}="",E{r}=""),"",D{r}-E{r})', MONEY)
        s.calc(f"J{r}", f'=IF(OR(G{r}="",F{r}=""),"",G{r}-F{r})', MONEY)
        s.calc(f"K{r}", f'=IF(OR(E{r}="",F{r}=""),"",IF(E{r}=0,"",F{r}/E{r}))', PCT)
        # FIX (audit step 0): uncollected must count invoices with nothing collected yet.
        s.calc(f"L{r}", f'=IF(G{r}="","",MAX(0,G{r}-N(H{r})))', MONEY)
        s.calc(f"M{r}", (f'=IF(B{r}="","",IF(N(H{r})>N(G{r}),"تحصيل يتجاوز المستخلصات",'
                         f'IF(AND(D{r}<>"",N(G{r})>N(D{r})),"مستخلصات تتجاوز العقد",'
                         f'IF(AND(E{r}<>"",F{r}<>"",N(F{r})>N(E{r})),"تجاوز الموازنة","ضمن الموازنة"))))'))
    s.validate(f"D{first}:H{last}", "decimal", 0, 1e11, "أدخل رقمًا غير سالب")
    ws.print_area = f"A1:M{last}"
    if sample:
        for i, row in enumerate(sample):
            for col, v in zip("ABCDEFGH", row, strict=False):
                ws[f"{col}{first + i}"] = v
    instructions(wb, "إدارة تكاليف المشاريع — طريقة الاستخدام", [
        "سجّل لكل مشروع: قيمة العقد، الموازنة المعتمدة، المصروف الفعلي حتى تاريخه، المستخلصات الصادرة، والمحصل.",
        "المستحق غير المحصل = المستخلصات − المحصل، ويُحسب حتى لو لم يُحصَّل شيء بعد.",
        "الحالة تنبه إلى: تحصيل يتجاوز المستخلصات، مستخلصات تتجاوز العقد، أو مصروف يتجاوز الموازنة.",
    ])
    return wb


# ---------------------------------------------------------------- 03 progress invoices
VAT_BASE_OPTS = ["قبل خصم المحتجزات", "بعد خصم المحتجزات"]


def build_invoices(n=40, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "المستخلصات"
    s = Sheet(ws, {c: w for c, w in zip("ABCDEFGHIJKLMNOP", [11, 18, 10, 12, 15, 13, 13, 15, 13, 15, 14, 14, 13, 12, 10, 15], strict=False)})
    s.title("غازي | المستخلصات والتحصيل", "A1:P1",
            sub="قيمة الأعمال قبل الضريبة. راجع أساس احتساب الضريبة على المحتجزات مع محاسبك وفق أدلة هيئة الزكاة والضريبة والجمارك.")
    settings = [("ضريبة القيمة المضافة", 0.15, PCT), ("نسبة المحتجزات (ضمان)", 0.05, PCT),
                ("نسبة استرداد الدفعة المقدمة", 0.0, PCT), ("مدة السداد (يوم)", 30, "0"),
                ("أساس الضريبة", VAT_BASE_OPTS[0], None)]
    for i, (lab, v, fmt) in enumerate(settings):
        r = 3 + i
        s.label(f"A{r}", lab)
        ws.merge_cells(f"A{r}:B{r}")
        s.inp(f"C{r}", v, fmt)
    s.validate("C3:C5", "decimal", 0, 1, "النسبة بين 0% و100%")
    s.validate("C6", "decimal", 0, 365, "بين 0 و365 يومًا")
    s.validate("C7", "list", values=VAT_BASE_OPTS)
    hr = 9
    first, last = hr + 1, hr + n
    rng = lambda c: f"{c}{first}:{c}{last}"  # noqa: E731
    summary = [("صافي المستحق (شامل الضريبة)", f"=SUM({rng('J')})"), ("إجمالي المحصل", f"=SUM({rng('K')})"),
               ("إجمالي المتبقي", f"=SUM({rng('L')})"), ("المتأخر عن الاستحقاق", f'=SUMIF({rng("P")},"متأخر",{rng("L")})'),
               ("محتجزات لدى العملاء", f"=SUM({rng('G')})")]
    for i, (lab, f) in enumerate(summary):
        r = 3 + i
        s.label(f"E{r}", lab)
        ws.merge_cells(f"E{r}:F{r}")
        s.calc(f"G{r}", f, MONEY, total=True)
        ws.merge_cells(f"G{r}:H{r}")
    s.label("J3", "دفعات زائدة (رصيد للعميل)")
    ws.merge_cells("J3:K3")
    s.calc("L3", f"=SUM({rng('M')})", MONEY, total=True)
    s.header(hr, ["المشروع", "العميل", "رقم المستخلص", "تاريخ الإصدار", "قيمة الأعمال", "استرداد الدفعة المقدمة",
                  "المحتجزات", "الوعاء الضريبي", "الضريبة", "صافي المستحق", "المحصل", "المتبقي", "دفعة زائدة",
                  "تاريخ الاستحقاق", "أيام التأخر", "الحالة"])
    for r in range(first, last + 1):
        for col in "ABC":
            s.inp(f"{col}{r}")
        s.inp(f"D{r}", fmt=DATE)
        s.inp(f"E{r}", fmt=MONEY)
        s.inp(f"K{r}", fmt=MONEY)
        s.calc(f"F{r}", f'=IF(E{r}="","",E{r}*$C$5)', MONEY)
        s.calc(f"G{r}", f'=IF(E{r}="","",E{r}*$C$4)', MONEY)
        s.calc(f"H{r}", f'=IF(E{r}="","",IF($C$7="{VAT_BASE_OPTS[1]}",E{r}-F{r}-G{r},E{r}-F{r}))', MONEY)
        s.calc(f"I{r}", f'=IF(H{r}="","",H{r}*$C$3)', MONEY)
        s.calc(f"J{r}", f'=IF(E{r}="","",E{r}-F{r}-G{r}+I{r})', MONEY)
        s.calc(f"L{r}", f'=IF(J{r}="","",MAX(0,J{r}-N(K{r})))', MONEY)
        # FIX (audit step 0): overpayment is surfaced instead of hidden by MAX(0,..).
        s.calc(f"M{r}", f'=IF(J{r}="","",MAX(0,N(K{r})-J{r}))', MONEY)
        s.calc(f"N{r}", f'=IF(OR(D{r}="",E{r}=""),"",D{r}+$C$6)', DATE)
        s.calc(f"O{r}", f'=IF(OR(N{r}="",L{r}=""),"",IF(L{r}>0,MAX(0,TODAY()-N{r}),0))', "0")
        s.calc(f"P{r}", (f'=IF(J{r}="","",IF(M{r}>0,"دفعة زائدة",IF(L{r}=0,"محصل",'
                         f'IF(AND(O{r}<>"",N(O{r})>0),"متأخر",IF(N(K{r})>0,"محصل جزئيًا","غير محصل")))))'))
    s.validate(f"D{first}:D{last}", "date", prompt="أدخل تاريخًا صحيحًا")
    s.validate(f"E{first}:E{last}", "decimal", 0, 1e11, "أدخل رقمًا غير سالب")
    s.validate(f"K{first}:K{last}", "decimal", 0, 1e11, "أدخل رقمًا غير سالب")
    ws.print_area = f"A1:P{last}"
    if sample:
        for i, row in enumerate(sample):
            for col, v in zip("ABCDEK", row, strict=False):
                if v is not None:
                    ws[f"{col}{first + i}"] = v
    instructions(wb, "المستخلصات والتحصيل — طريقة الاستخدام", [
        "اضبط نسب الضريبة والمحتجزات واسترداد الدفعة المقدمة ومدة السداد في أعلى الورقة.",
        "صافي المستحق = قيمة الأعمال − استرداد الدفعة المقدمة − المحتجزات + الضريبة.",
        "الوعاء الضريبي افتراضيًا = قيمة الأعمال − استرداد الدفعة المقدمة (لأن الضريبة استُحقت عند استلام الدفعة المقدمة). يمكن تغييره إلى «بعد خصم المحتجزات» إن أوصى محاسبك بذلك.",
        "الحالة: محصل، محصل جزئيًا، غير محصل، متأخر (تجاوز تاريخ الاستحقاق)، دفعة زائدة (رصيد دائن للعميل).",
    ])
    return wb


# ---------------------------------------------------------------- 04 labor
def build_labor(n=30, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "العمالة"
    s = Sheet(ws, {c: w for c, w in zip("ABCDEFGHIJKL", [22, 10, 10, 10, 12, 12, 12, 13, 15, 15, 13, 15], strict=False)})
    s.title("غازي | تكلفة فرق العمالة", "A1:L1", sub="النقل والسكن والمصاريف الأخرى اختيارية؛ تركها فارغة يعني صفرًا.")
    hr = 7
    first, last = hr + 1, hr + n
    rng = lambda c: f"{c}{first}:{c}{last}"  # noqa: E731
    for i, (lab, f, fmt) in enumerate([("إجمالي تكلفة العمالة", f"=SUM({rng('I')})", MONEY),
                                       ("إجمالي ساعات العمل", f"=SUM({rng('J')})", "#,##0"),
                                       ("متوسط تكلفة الساعة", f'=IF(SUM({rng("J")})=0,"",SUM({rng("I")})/SUM({rng("J")}))', MONEY)]):
        s.label(f"A{3 + i}", lab)
        s.calc(f"B{3 + i}", f, fmt, total=True)
        ws.merge_cells(f"B{3 + i}:C{3 + i}")
    s.header(hr, ["التخصص", "عدد العمال", "أيام العمل", "ساعات/يوم", "أجر/يوم", "نقل/عامل/يوم", "سكن/عامل/يوم",
                  "مصاريف أخرى/عامل/يوم", "إجمالي التكلفة", "إجمالي الساعات", "تكلفة الساعة", "تكلفة العامل اليومية"])
    names = ["عامل دهانات", "عامل جبس وأسقف", "عامل بلاط وسيراميك", "نجار أبواب", "عامل صيانة عامة"]
    for r in range(first, last + 1):
        s.inp(f"A{r}")
        for col in "BCD":
            s.inp(f"{col}{r}", fmt="0.##")
        for col in "EFGH":
            s.inp(f"{col}{r}", fmt=MONEY)
        # FIX (audit step 0): optional cost columns no longer blank the whole row.
        s.calc(f"I{r}", f'=IF(COUNT(B{r},C{r},E{r})<3,"",B{r}*C{r}*(E{r}+N(F{r})+N(G{r})+N(H{r})))', MONEY)
        s.calc(f"J{r}", f'=IF(COUNT(B{r}:D{r})<3,"",B{r}*C{r}*D{r})', "#,##0")
        s.calc(f"K{r}", f'=IF(OR(I{r}="",J{r}=""),"",IF(J{r}=0,"",I{r}/J{r}))', MONEY)
        s.calc(f"L{r}", f'=IF(E{r}="","",E{r}+N(F{r})+N(G{r})+N(H{r}))', MONEY)
    for i, name in enumerate(names[:n]):
        ws[f"A{first + i}"] = name
    s.validate(f"B{first}:H{last}", "decimal", 0, 1e9, "أدخل رقمًا غير سالب")
    s.validate(f"D{first}:D{last}", "decimal", 0, 24, "الساعات بين 0 و24")
    ws.print_area = f"A1:L{last}"
    if sample:
        for i, row in enumerate(sample):
            for col, v in zip("ABCDEFGH", row, strict=False):
                if v is not None:
                    ws[f"{col}{first + i}"] = v
    instructions(wb, "تكلفة فرق العمالة — طريقة الاستخدام", [
        "أدخل لكل فريق: عدد العمال، أيام العمل، ساعات اليوم، والأجر اليومي للعامل.",
        "النقل والسكن والمصاريف الأخرى لكل عامل في اليوم، واتركها فارغة إن لم تنطبق.",
        "تكلفة الساعة = إجمالي التكلفة ÷ إجمالي الساعات.",
    ])
    return wb


# ---------------------------------------------------------------- 05 quotation
def build_quotation(n=25, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "عرض السعر"
    s = Sheet(ws, {"A": 15, "B": 40, "C": 9, "D": 15, "E": 14, "F": 16}, landscape=False)
    s.title("عرض سعر", "A1:F1", sub="هذا المستند عرض سعر وليس فاتورة ضريبية.")
    fields = [("اسم المنشأة", "B3"), ("السجل التجاري", "B4"), ("الرقم الضريبي", "B5"), ("العميل", "B6"),
              ("المشروع / الموقع", "B7"), ("رقم العرض", "E3"), ("التاريخ", "E4"), ("صلاحية العرض (يوم)", "E5"),
              ("نسبة الضريبة", "E6")]
    for lab, ref in fields:
        col = ref[0]
        lab_col = "A" if col == "B" else "D"
        s.label(f"{lab_col}{ref[1:]}", lab)
        s.inp(ref)
    ws["E4"].number_format = DATE
    ws["E5"] = 15
    ws["E6"] = 0.15
    ws["E6"].number_format = PCT
    s.validate("E6", "decimal", 0, 1, "النسبة بين 0% و100%")
    s.label("D7", "ينتهي العرض في")
    s.calc("E7", '=IF(OR(E4="",E5=""),"",E4+E5)', DATE)
    hr = 9
    first, last = hr + 1, hr + n
    s.header(hr, ["#", "وصف البند", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي"])
    for i, r in enumerate(range(first, last + 1), start=1):
        c = ws.cell(row=r, column=1, value=i)
        c.border = BOX
        c.alignment = Alignment(horizontal="center")
        s.inp(f"B{r}")
        s.inp(f"C{r}")
        s.inp(f"D{r}", fmt="#,##0.##")
        s.inp(f"E{r}", fmt=MONEY)
        s.calc(f"F{r}", f'=IF(OR(D{r}="",E{r}=""),"",D{r}*E{r})', MONEY)
    s.validate(f"D{first}:E{last}", "decimal", 0, 1e11, "أدخل رقمًا غير سالب")
    t = last + 1
    s.label(f"E{t}", "المجموع")
    s.calc(f"F{t}", f"=SUM(F{first}:F{last})", MONEY, total=True)
    s.label(f"E{t + 1}", "خصم")
    s.inp(f"F{t + 1}", 0, MONEY)
    s.validate(f"F{t + 1}", "decimal", 0, 1e11, "الخصم رقم غير سالب")
    s.label(f"E{t + 2}", "الصافي قبل الضريبة")
    s.calc(f"F{t + 2}", f"=MAX(0,F{t}-N(F{t + 1}))", MONEY, total=True)
    s.label(f"E{t + 3}", "الضريبة")
    s.calc(f"F{t + 3}", f"=F{t + 2}*N(E6)", MONEY, total=True)
    s.label(f"E{t + 4}", "الإجمالي شامل الضريبة")
    s.calc(f"F{t + 4}", f"=F{t + 2}+F{t + 3}", MONEY, total=True)
    s.calc(f"B{t + 1}", f'=IF(N(F{t + 1})>F{t},"تنبيه: الخصم أكبر من المجموع","")')
    s.label(f"A{t + 6}", "الشروط")
    terms = ["الأسعار بالريال السعودي.", "مدة التنفيذ: ", "الدفعات: ", "يشمل / لا يشمل: "]
    for i, term in enumerate(terms):
        r = t + 7 + i
        ws.merge_cells(f"B{r}:F{r}")
        s.inp(f"B{r}", term)
    sig = t + 12
    s.label(f"B{sig}", "توقيع وختم المنشأة: ____________________")
    ws.print_area = f"A1:F{sig}"
    if sample:
        meta, items = sample
        for ref, v in meta.items():
            ws[ref] = v
        for i, row in enumerate(items):
            for col, v in zip("BCDE", row, strict=False):
                ws[f"{col}{first + i}"] = v
    instructions(wb, "نموذج عرض السعر — طريقة الاستخدام", [
        "أدخل بيانات منشأتك والعميل ورقم العرض وتاريخه ومدة صلاحيته.",
        "أدخل البنود بالكمية وسعر الوحدة؛ يُطرح الخصم ثم تُحسب الضريبة على الصافي.",
        "اطبع الورقة على A4 عمودي أو صدّرها PDF من قائمة ملف.",
    ])
    return wb


# ---------------------------------------------------------------- 06 profitability
def build_profitability(n=20, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "المصاريف العامة"
    s = Sheet(ws, {"A": 34, "B": 18, "C": 40}, landscape=False)
    s.title("غازي | المصاريف العامة الشهرية", "A1:C1", sub="المصاريف التي لا تخص مشروعًا بعينه.")
    s.header(3, ["البند", "المبلغ الشهري", "ملاحظات"])
    items = ["إيجار المكتب والمستودع", "رواتب الإدارة والمحاسبة", "رسوم حكومية وتجديد رخص", "سيارات ووقود",
             "اتصالات وأنظمة", "تأمينات", "صيانة معدات", "أخرى"]
    of, ol = 4, 4 + 14
    for r in range(of, ol + 1):
        s.inp(f"A{r}")
        s.inp(f"B{r}", fmt=MONEY)
        s.inp(f"C{r}")
    for i, it in enumerate(items):
        ws[f"A{of + i}"] = it
    s.validate(f"B{of}:B{ol}", "decimal", 0, 1e10, "أدخل رقمًا غير سالب")
    tr = ol + 2
    s.label(f"A{tr}", "إجمالي المصاريف العامة الشهرية")
    s.calc(f"B{tr}", f"=SUM(B{of}:B{ol})", MONEY, total=True)
    s.label(f"A{tr + 1}", "متوسط الإيراد الشهري المتوقع (قبل الضريبة)")
    s.inp(f"B{tr + 1}", fmt=MONEY)
    s.label(f"A{tr + 2}", "نسبة تحميل المصاريف العامة")
    s.calc(f"B{tr + 2}", f'=IF(N(B{tr + 1})=0,"",B{tr}/B{tr + 1})', PCT, total=True)
    s.label(f"A{tr + 3}", "الهامش الصافي المستهدف")
    s.inp(f"B{tr + 3}", 0.10, PCT)
    s.validate(f"B{tr + 3}", "decimal", 0, 0.9, "بين 0% و90%")
    oh_ref = f"'المصاريف العامة'!$B${tr + 2}"
    tgt_ref = f"'المصاريف العامة'!$B${tr + 3}"
    ws.print_area = f"A1:C{tr + 3}"

    p = wb.create_sheet("ربحية المشاريع")
    ps = Sheet(p, {c: w for c, w in zip("ABCDEFGHIJK", [26, 15, 14, 14, 14, 14, 14, 15, 15, 15, 18], strict=False)})
    ps.title("غازي | صافي ربحية المشاريع", "A1:K1", sub="المصاريف العامة تُحمَّل بنسبة من قيمة العقد محسوبة في الورقة السابقة.")
    hr = 4
    first, last = hr + 1, hr + n
    ps.label("A3", "نسبة التحميل المستخدمة")
    ps.calc("B3", f"={oh_ref}", PCT, total=True)
    ps.header(hr, ["المشروع", "قيمة العقد", "مواد", "عمالة", "معدات", "مقاولو باطن", "مباشرة أخرى",
                   "إجمالي التكلفة المباشرة", "المصاريف العامة المحمّلة", "صافي الربح", "الهامش الصافي / الحالة"])
    for r in range(first, last + 1):
        ps.inp(f"A{r}")
        for col in "BCDEFG":
            ps.inp(f"{col}{r}", fmt=MONEY)
        ps.calc(f"H{r}", f'=IF(B{r}="","",SUM(C{r}:G{r}))', MONEY)
        ps.calc(f"I{r}", f'=IF(OR(B{r}="",$B$3=""),"",B{r}*$B$3)', MONEY)
        ps.calc(f"J{r}", f'=IF(B{r}="","",B{r}-H{r}-N(I{r}))', MONEY)
        ps.calc(f"K{r}", (f'=IF(OR(B{r}="",N(B{r})=0),"",TEXT(J{r}/B{r},"0.0%")&" — "&'
                          f'IF(J{r}<0,"خسارة",IF(J{r}/B{r}<{tgt_ref},"هامش منخفض","مقبول")))'))
    ps.validate(f"B{first}:G{last}", "decimal", 0, 1e11, "أدخل رقمًا غير سالب")
    t = last + 2
    ps.label(f"A{t}", "الإجماليات")
    for col in "BHIJ":
        ps.calc(f"{col}{t}", f"=SUM({col}{first}:{col}{last})", MONEY, total=True)
    p.print_area = f"A1:K{t}"
    if sample:
        overhead, revenue, projects = sample
        for i, (name, amt) in enumerate(overhead):
            ws[f"A{of + i}"], ws[f"B{of + i}"] = name, amt
        ws[f"B{tr + 1}"] = revenue
        for i, row in enumerate(projects):
            for col, v in zip("ABCDEFG", row, strict=False):
                p[f"{col}{first + i}"] = v
    instructions(wb, "الربحية والمصاريف العامة — طريقة الاستخدام", [
        "في ورقة المصاريف العامة: أدخل المصاريف الشهرية التي لا تخص مشروعًا، ومتوسط الإيراد الشهري.",
        "نسبة التحميل = المصاريف العامة ÷ الإيراد، وتُطبَّق على قيمة كل عقد في ورقة ربحية المشاريع.",
        "الحالة: خسارة، هامش أقل من المستهدف، أو مقبول.",
    ])
    return wb


# ---------------------------------------------------------------- 07 execution tracking
def build_execution(n=30, sample=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "متابعة التنفيذ"
    s = Sheet(ws, {c: w for c, w in zip("ABCDEFGH", [32, 10, 13, 13, 13, 14, 12, 18], strict=False)})
    s.title("غازي | متابعة تنفيذ المشروع", "A1:H1", sub="المخطط حتى اليوم يُحسب خطيًا بين تاريخي البداية والنهاية.")
    s.label("A3", "اسم المشروع")
    s.inp("B3")
    ws.merge_cells("B3:D3")
    hr = 8
    first, last = hr + 1, hr + n
    rng = lambda c: f"{c}{first}:{c}{last}"  # noqa: E731
    s.label("A4", "مجموع الأوزان")
    s.calc("B4", f"=SUM({rng('B')})", PCT, total=True)
    s.calc("C4", '=IF(ABS(B4-1)>0.001,"تنبيه: مجموع الأوزان يجب أن يساوي 100%","")')
    ws.merge_cells("C4:H4")
    s.label("A5", "الإنجاز الفعلي المرجّح")
    s.calc("B5", f'=IF(SUM({rng("B")})=0,"",SUMPRODUCT({rng("B")},{rng("E")})/SUM({rng("B")}))', PCT, total=True)
    s.label("A6", "المخطط حتى اليوم المرجّح")
    s.calc("B6", f'=IF(SUM({rng("B")})=0,"",SUMPRODUCT({rng("B")},{rng("I")})/SUM({rng("B")}))', PCT, total=True)
    s.header(hr, ["النشاط", "الوزن", "بداية مخططة", "نهاية مخططة", "الإنجاز الفعلي", "المخطط حتى اليوم", "الانحراف", "الحالة"])
    for r in range(first, last + 1):
        s.inp(f"A{r}")
        s.inp(f"B{r}", fmt=PCT)
        s.inp(f"C{r}", fmt=DATE)
        s.inp(f"D{r}", fmt=DATE)
        s.inp(f"E{r}", fmt=PCT)
        s.calc(f"F{r}", f'=IF(OR(C{r}="",D{r}=""),"",IF(TODAY()<=C{r},0,IF(TODAY()>=D{r},1,(TODAY()-C{r})/(D{r}-C{r}))))', PCT)
        s.calc(f"G{r}", f'=IF(OR(E{r}="",F{r}=""),"",E{r}-F{r})', PCT)
        s.calc(f"H{r}", (f'=IF(A{r}="","",IF(E{r}="","لم يُحدَّث",IF(E{r}>=1,"منجز",'
                         f'IF(AND(G{r}<>"",N(G{r})<-0.05),"متأخر","حسب الخطة"))))'))
        # hidden numeric helper so SUMPRODUCT never sees text
        h = ws[f"I{r}"]
        h.value = f"=N(F{r})"
        h.protection = Protection(locked=True, hidden=True)
    ws.column_dimensions["I"].hidden = True
    s.validate(f"B{first}:B{last}", "decimal", 0, 1, "الوزن بين 0% و100%")
    s.validate(f"E{first}:E{last}", "decimal", 0, 1, "الإنجاز بين 0% و100%")
    s.validate(f"C{first}:D{last}", "date", prompt="أدخل تاريخًا صحيحًا")
    ws.print_area = f"A1:H{last}"
    if sample:
        for i, row in enumerate(sample):
            for col, v in zip("ABCDE", row, strict=False):
                if v is not None:
                    ws[f"{col}{first + i}"] = v
    instructions(wb, "متابعة تنفيذ المشروع — طريقة الاستخدام", [
        "أدخل الأنشطة بأوزانها (مجموعها 100%) وتاريخي البداية والنهاية المخططين.",
        "حدّث نسبة الإنجاز الفعلي دوريًا؛ النشاط المتأخر هو ما يقل إنجازه عن المخطط بأكثر من 5 نقاط.",
    ])
    return wb


# ---------------------------------------------------------------- sample data (illustrative only)
def sample_data():
    today = dt.date.today()
    d = lambda days: dt.datetime.combine(today + dt.timedelta(days=days), dt.time())  # noqa: E731
    return {
        "01_finishing_pricing": [("FIN-01", "دهانات داخلية (مثال)", "م²", 500, 12, 8, 1),
                                 ("FIN-02", "جبس بورد (مثال)", "م²", 200, 35, 20, None),
                                 ("FIN-03", "بلاط أرضيات (مثال)", "م²", 150, 45, 25, 2),
                                 ("FIN-04", "أبواب داخلية (مثال)", "عدد", 12, 650, 120, None),
                                 ("FIN-05", "بند ناقص (مثال)", "م²", 80, None, None, None)],
        "02_project_costs": [("P-01", "فيلا سكنية (مثال)", "الرياض", 480000, 400000, 250000, 300000, 220000),
                             ("P-02", "ترميم مدرسة (مثال)", "جدة", 220000, 190000, 205000, 150000, None)],
        "03_progress_invoices": [("P-01", "عميل أ (مثال)", "1", d(-60), 100000, 110000),
                                 ("P-01", "عميل أ (مثال)", "2", d(-35), 80000, 30000),
                                 ("P-02", "عميل ب (مثال)", "1", d(-5), 50000, None)],
        "04_labor_cost": [("عامل دهانات", 4, 22, 8, 120, 10, 15, None),
                          ("عامل جبس وأسقف", 3, 22, 8, 140, 10, 15, 5)],
        "05_quotation": ({"B3": "مؤسسة مثال للمقاولات", "B6": "عميل مثال", "B7": "فيلا — حي مثال", "E3": "Q-001",
                          "E4": d(0)},
                         [("دهانات داخلية", "م²", 500, 26), ("جبس بورد", "م²", 200, 75)]),
        "06_profitability": ([("إيجار المكتب والمستودع", 8000), ("رواتب الإدارة والمحاسبة", 18000)], 260000,
                             [("فيلا سكنية (مثال)", 480000, 190000, 120000, 15000, 40000, 5000)]),
        "07_execution_tracking": [("أعمال الجبس", 0.3, d(-20), d(10), 0.5), ("الدهانات", 0.4, d(-5), d(25), 0.05),
                                  ("البلاط", 0.3, d(5), d(30), None)],
    }


BUILDERS = {
    "01_finishing_pricing": build_pricing,
    "02_project_costs": build_projects,
    "03_progress_invoices": build_invoices,
    "04_labor_cost": build_labor,
    "05_quotation": build_quotation,
    "06_profitability": build_profitability,
    "07_execution_tracking": build_execution,
}


def lock_all(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.protection and not c.protection.locked:
                    c.protection = Protection(locked=True)
        ws.protection.sheet = True


def recalc(src_files: list[Path], out_dir: Path):
    """Recalculate with LibreOffice headless so cached values exist for previewers."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run(
            ["soffice", f"-env:UserInstallation=file://{prof}", "--headless", "--calc", "--convert-to", "xlsx:Calc MS Excel 2007 XML",
             "--outdir", str(out_dir), *map(str, src_files)],
            check=True, capture_output=True, timeout=300,
        )


def build_all(do_recalc=True):
    data = sample_data()
    for sub in ("full", "sample"):
        shutil.rmtree(RAW / sub, ignore_errors=True)
        (RAW / sub).mkdir(parents=True)
    for name, fn in BUILDERS.items():
        fn().save(RAW / "full" / f"{name}.xlsx")
        n_rows = 5
        wb = fn(n=n_rows, sample=data[name])
        ws0 = wb.worksheets[1]
        ws0["A2"] = SAMPLE_BANNER
        ws0["A2"].font = Font(name=FONT, bold=True, color="B42318")
        lock_all(wb)
        wb.save(RAW / "sample" / f"{name}.xlsx")
    if do_recalc:
        for sub in ("full", "sample"):
            shutil.rmtree(DIST / sub, ignore_errors=True)
            recalc(sorted((RAW / sub).glob("*.xlsx")), DIST / sub)
    return DIST


if __name__ == "__main__":
    out = build_all(do_recalc="--no-recalc" not in sys.argv)
    print("built", out)
