"""Quote drafts: opportunity -> starter lines (no invented prices) -> totals -> filled 05_quotation workbook."""
from __future__ import annotations

import datetime as dt
import io
import math

import openpyxl

from .config import settings

MAX_LINES = 25
FIRST_ROW = 10  # matches products/build_workbooks.build_quotation
DISCOUNT_CELL = f"F{FIRST_ROW + MAX_LINES + 1}"

STARTER = {  # item names only; quantities and prices are always entered by the contractor
    "الجبس والأسقف المستعارة": [("توريد وتركيب أسقف جبس بورد", "م²"), ("فتحات إنارة وتكييف في السقف", "عدد")],
    "البلاط والسيراميك": [("توريد وتركيب بلاط أرضيات", "م²"), ("نعلات (وزرة)", "م ط")],
    "الدهانات": [("معجون وصنفرة وتجهيز أسطح", "م²"), ("دهان داخلي طبقتين", "م²")],
    "الأبواب": [("توريد وتركيب أبواب داخلية", "عدد")],
    "إعادة تأهيل المباني والمساكن": [("إزالة ونقل مخلفات", "مقطوعية"), ("معالجة تشققات", "م ط")],
    "الصيانة المدنية والمعمارية": [("أعمال صيانة معمارية", "مقطوعية")],
}


def starter_lines(trades: list[str] | None) -> list[dict]:
    lines = []
    for t in trades or []:
        lines += [{"desc": d, "unit": u, "qty": None, "unit_price": None} for d, u in STARTER.get(t, [])]
    return lines[:MAX_LINES] or [{"desc": "", "unit": "م²", "qty": None, "unit_price": None}]


def num(v, default=None):
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x) or x < 0:
        return default
    return x


def parse_lines(form) -> list[dict]:
    out = []
    descs, units, qtys, prices = (form.getlist(k) for k in ("desc", "unit", "qty", "unit_price"))
    for d, u, q, p in zip(descs, units, qtys, prices, strict=False):
        d = str(d).strip()[:200]
        if not d and not q and not p:
            continue
        out.append({"desc": d, "unit": str(u).strip()[:20], "qty": num(q), "unit_price": num(p)})
    return out[:MAX_LINES]


def totals(q) -> dict:
    priced = [line for line in q.lines if line.get("qty") is not None and line.get("unit_price") is not None]
    subtotal = round(sum(line["qty"] * line["unit_price"] for line in priced), 2)
    net = max(0.0, subtotal - (q.discount or 0))
    vat = round(net * (q.vat_rate or 0), 2)
    return {"subtotal": subtotal, "net": round(net, 2), "vat": vat, "total": round(net + vat, 2),
            "missing": len(q.lines) - len(priced), "discount_too_big": (q.discount or 0) > subtotal}


def to_xlsx(q, user) -> bytes:
    wb = openpyxl.load_workbook(settings.product_dir / "05_quotation.xlsx")
    ws = wb.worksheets[1]
    ws["B3"] = user.company or user.name
    ws["B6"] = q.client
    ws["B7"] = q.project
    ws["E3"] = f"Q-{q.id:05d}"
    ws["E4"] = dt.datetime.combine(dt.date.today(), dt.time())
    ws["E5"] = q.validity_days
    ws["E6"] = q.vat_rate
    for i, line in enumerate(q.lines[:MAX_LINES]):
        r = FIRST_ROW + i
        ws[f"B{r}"], ws[f"C{r}"] = line.get("desc"), line.get("unit")
        ws[f"D{r}"], ws[f"E{r}"] = line.get("qty"), line.get("unit_price")
    ws[DISCOUNT_CELL] = q.discount or 0
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
