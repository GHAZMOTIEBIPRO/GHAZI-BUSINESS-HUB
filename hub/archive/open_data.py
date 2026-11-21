"""Read an official open-data table (XLSX or CSV, the publisher's own column names) into an import package.

Open data portals publish procurement lists with their own headers ("رقم المنافسة", "اسم الجهة", "اسم المورد"...).
This module maps those headers onto the importer's columns by synonyms, converts Excel dates and amounts, and returns
the same `Package` the importer stages, so every archive rule still applies: winners only where a supplier / awardee
is published, nothing public, rejects reported, rollback by batch.

A contract list (each row = a signed contract with a named supplier) is a published award. A tender list without a
supplier column never creates a winner. Nothing here guesses a value that the file does not carry.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import unicodedata
from pathlib import Path

from . import normalize as N
from .importer import MAX_PACKAGE_BYTES, Package, PackageError

# importer column -> header variants seen on Saudi open-data procurement files (compared after _norm)
SYNONYMS: dict[str, list[str]] = {
    "tender_reference": ["رقم المنافسة", "الرقم المرجعي", "رقم المنافسه", "الرقم المرجعي للمنافسة", "رقم العقد",
                         "رقم المناقصة", "tender number", "reference number", "tender no", "contract number", "tender id"],
    "title": ["اسم المنافسة", "اسم المنافسه", "عنوان المنافسة", "اسم المشروع", "اسم العقد", "موضوع العقد",
              "اسم المناقصة", "tender name", "tender title", "competition name", "project name", "contract name",
              "contract title", "procurement title", "procurement name", "name of competitions", "عنوان المشتريات",
              "اسم المشتريات", "وصف المنافسة", "وصف العقد", "description", "tender description"],
    "issuer": ["الجهة", "اسم الجهة", "الجهة الحكومية", "الجهة الطارحة", "الجهة المالكة", "الإدارة", "الجهة المستفيدة",
               "اسم الجهة الحكومية", "agency", "entity", "entity name", "government entity", "agency name",
               "organization", "organization name", "department"],
    "city_or_location": ["المدينة", "مكان التنفيذ", "موقع التنفيذ", "المنطقة", "المحافظة", "city", "location", "region"],
    "announced_status": ["حالة المنافسة", "الحالة", "حالة العقد", "status", "tender status"],
    "published_date": ["تاريخ النشر", "تاريخ الطرح", "تاريخ الإعلان", "تاريخ نشر المنافسة", "تاريخ البدء",
                       "تاريخ الإنشاء", "publish date", "published date", "announcement date", "start date",
                       "creation date", "created date"],
    "submission_deadline": ["آخر موعد لتقديم العروض", "اخر موعد لتقديم العروض", "آخر موعد للتقديم",
                            "تاريخ إغلاق المنافسة", "تاريخ فتح العروض", "submission deadline", "closing date"],
    "award_date": ["تاريخ الترسية", "تاريخ توقيع العقد", "تاريخ العقد", "تاريخ بداية العقد", "award date",
                   "contract date", "contract start date"],
    "winner_name_as_published": ["اسم المورد", "المورد", "اسم المتعاقد", "المتعاقد", "اسم المقاول", "المقاول",
                                 "الشركة الفائزة", "اسم الشركة", "المورد الفائز", "المورد المرسى عليه",
                                 "اسم المورد المرسى عليه", "supplier", "supplier name", "suppliers", "contractor",
                                 "contractor name", "awardee", "awarded vendor", "awarded supplier", "vendor",
                                 "vendor name"],
    "award_amount_sar_as_published": ["قيمة العقد", "قيمة الترسية", "مبلغ العقد", "قيمة المنافسة", "قيمة المشتريات",
                                      "القيمة", "قيمة العقد بالريال", "contract value", "award value", "awarded value",
                                      "tender value", "procurement value", "value of competitions", "amount", "value"],
    # offers submitted only; "bids that passed evaluation" is a different number and stays in the source columns
    "offer_count_claimed": ["عدد العروض", "عدد العروض المقدمة", "number of bids", "number of offers",
                            "number of submitted bids"],
}
REQUIRED = ("title",)
DERIVED_PREFIX = "OD-"  # internal key for rows the publisher released without a tender / contract number
AWARDED_WITHOUT_STATUS = "عقد منشور بمورد مسمّى"  # status text used when a contract list has no status column
EXCEL_EPOCH = dt.datetime(1899, 12, 30)


def _norm(header: str) -> str:
    text = unicodedata.normalize("NFKC", str(header or "")).lower()
    text = re.sub(r"[ً-ْـ]", "", text)  # diacritics, tatweel
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"}))
    return re.sub(r"[\s_\-:()/]+", " ", text).strip()


NORMALISED = {field: [_norm(v) for v in variants] for field, variants in SYNONYMS.items()}
# a header containing one of these words is never taken for the field (it is a different measure)
EXCLUDE = {"offer_count_claimed": [_norm(w) for w in ("passed", "evaluation", "المجتازة", "اجتازت", "المقبولة", "الفائزة")]}


def detect_mapping(headers: list[str]) -> dict[str, str]:
    """importer column -> file header. Exact synonym first, then 'header contains synonym'; one header per field."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    normed = [(h, _norm(h)) for h in headers if str(h or "").strip()]
    for exact in (True, False):
        for field, variants in NORMALISED.items():
            if field in mapping:
                continue
            for header, nh in normed:
                if header in used or any(w in nh for w in EXCLUDE.get(field, [])):
                    continue
                if any(nh == v if exact else (len(v) >= 4 and v in nh) for v in variants):
                    mapping[field] = header
                    used.add(header)
                    break
    return mapping


def _cell(value) -> str:
    """One cell as the text the importer expects: ISO dates, plain numbers, trimmed text."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return N.clean_text(str(value))


DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")


def _date_text(value) -> str:
    """ISO for Excel dates, serial numbers and d/m/yyyy text; a Hijri year (13xx/14xx) is left as published."""
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:  # Excel serial day number
        return (EXCEL_EPOCH + dt.timedelta(days=float(value))).date().isoformat()
    text = _cell(value)
    m = DMY.match(text)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        if y >= 1900:
            try:
                return dt.date(y, mo, d).isoformat()
            except ValueError:
                return text
    return text


def read_table(path: Path) -> tuple[list[str], list[list]]:
    """Headers and rows of the first non-empty sheet (XLSX) or of the CSV (UTF-8, or Windows-1256 as published)."""
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise PackageError("الملف أكبر من 25 ميجابايت")
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # corrupt or not really xlsx
            raise PackageError("ملف XLSX غير صالح") from exc
        try:
            for ws in wb.worksheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                rows = [r for r in rows if any(c not in (None, "") for c in r)]
                if len(rows) >= 2:
                    header_at = next(i for i, r in enumerate(rows[:10]) if sum(c not in (None, "") for c in r) >= 2)
                    return [_cell(h) for h in rows[header_at]], rows[header_at + 1:]
        finally:
            wb.close()
        raise PackageError("لا توجد ورقة فيها صف عناوين وبيانات")
    if suffix == ".csv":
        blob = path.read_bytes()
        for encoding in ("utf-8-sig", "cp1256"):
            try:
                text = blob.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise PackageError("ترميز CSV غير معروف (UTF-8 أو Windows-1256)")
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        if len(rows) < 2:
            raise PackageError("ملف CSV بلا بيانات")
        return [_cell(h) for h in rows[0]], rows[1:]
    raise PackageError("صيغة غير مدعومة: XLSX أو CSV فقط")


def to_package(path: str | Path, *, publisher: str, source_url: str = "", mapping: dict[str, str] | None = None) -> Package:
    """Convert one open-data file into a Package. `publisher` names the official owner of the file."""
    path = Path(path)
    headers, rows = read_table(path)
    mapping = mapping or detect_mapping(headers)
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise PackageError("لم يُتعرف على أعمدة إلزامية: " + "، ".join(missing) + " — العناوين الموجودة: "
                           + "، ".join(h for h in headers if h)[:300])
    index = {h: i for i, h in enumerate(headers)}
    has_winner = "winner_name_as_published" in mapping
    has_status = "announced_status" in mapping
    # "حالة العقد" (active / ended) describes a signed contract, not a tender: with a named supplier it is an award
    contract_status = has_status and any(k in _norm(mapping["announced_status"]) for k in ("عقد", "contract"))
    tenders = []
    for row in rows:
        def get(field, row=row):
            header = mapping.get(field)
            if header is None or index[header] >= len(row):
                return ""
            value = row[index[header]]
            return _date_text(value) if field in ("published_date", "submission_deadline", "award_date") else _cell(value)
        rec = {field: get(field) for field in SYNONYMS}
        named = has_winner and rec["winner_name_as_published"]
        if named and (not has_status or (contract_status and N.map_status(rec["announced_status"]) != "cancelled")):
            rec["announced_status"] = AWARDED_WITHOUT_STATUS  # a named supplier on a published contract list
        notes = [f"open data file: {path.name}"]
        if not rec["tender_reference"] and rec["title"]:
            # the publisher released this row without a number: a stable internal key, labelled as such everywhere
            basis = "|".join([publisher, rec["issuer"], rec["title"], rec["published_date"]])
            rec["tender_reference"] = DERIVED_PREFIX + hashlib.sha256(basis.encode()).hexdigest()[:12].upper()
            notes.append("لا رقم منافسة في الملف المصدر: المرجع داخلي مشتق (OD-)")
        rec["bidders_name_count_public"] = ""
        rec["source_url"] = source_url if source_url.startswith("https://") else ""
        rec["source_publisher"] = publisher
        rec["verification_notes"] = "؛ ".join(notes)
        for header, i in index.items():  # every original column stays with the record (sector, phase, ...)
            if header and header not in mapping.values() and i < len(row):
                rec["src:" + header[:60]] = _cell(row[i])
        tenders.append(rec)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return Package(sha256=digest, manifest={"source_file": path.name, "publisher": publisher, "mapping": mapping,
                                            "rows": len(rows), "tender_records": len(tenders)},
                   tenders=tenders, bids=[], companies=[])
