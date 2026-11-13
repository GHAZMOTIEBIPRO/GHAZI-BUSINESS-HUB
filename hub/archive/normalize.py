"""Pure normalisation helpers (no database). Conservative by design: they clean, they never guess."""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata

from ..models import CITIES, TRADES

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_SPACES = re.compile(r"\s+")
REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-/]{2,63}$")

# Published status text -> archive status. Anything unknown stays "unknown" (and is reported).
STATUS_MAP = {
    "تمت الترسية": "awarded",
    "مرساة": "awarded",
    "معلنة - التقديم بحسب المصدر": "announced",
    "معلنة": "announced",
    "مطروحة": "open",
    "مفتوحة": "open",
    "مغلقة": "closed",
    "ملغاة": "cancelled",
    "ملغية": "cancelled",
}
UNKNOWN_LOCATION = {"", "غير محددة في الملخص", "غير محدد", "غير معلوم"}
CITY_ALIASES = {"ينبع الصناعية": "ينبع"}  # same city for the region lookup; the published text is kept as-is
EXTRA_REGIONS = {"صبيا": "جازان", "رفحاء": "الحدود الشمالية", "تيماء": "تبوك", "القنفذة": "مكة المكرمة",
                 "رابغ": "مكة المكرمة", "العلا": "المدينة المنورة", "الجبيل الصناعية": "الشرقية"}

# Title keywords -> owner's target trades (models.TRADES). A tender can carry several; none is fine.
TRADE_KEYWORDS = {
    "الجبس والأسقف المستعارة": ("جبس", "أسقف", "اسقف", "سقف مستعار", "جبسي"),
    "البلاط والسيراميك": ("بلاط", "سيراميك", "رخام", "بورسلان"),
    "الدهانات": ("دهان", "دهانات", "طلاء"),
    "الأبواب": ("أبواب", "ابواب", "باب "),
    "إعادة تأهيل المباني والمساكن": ("ترميم", "تأهيل", "تاهيل", "إعادة تأهيل", "تهيئة"),
    "الصيانة المدنية والمعمارية": ("صيانة", "نظافة وصيانة", "تشغيل وصيانة"),
}
assert set(TRADE_KEYWORDS) <= set(TRADES)


def clean_text(value: str | None) -> str:
    return _SPACES.sub(" ", (value or "").replace("‏", "").replace("‎", "").replace("﻿", "")).strip()


def name_key(name: str) -> str:
    """Identity key: NFKC + whitespace + tatweel + diacritics removed. Different spellings stay different."""
    text = unicodedata.normalize("NFKC", clean_text(name)).replace(_TATWEEL, "")
    return _DIACRITICS.sub("", text).casefold()


def fold_key(name: str) -> str:
    """Looser key used only to FLAG possible duplicates (ة/ه, أ/إ/آ/ا, ى/ي). Never used to merge."""
    k = name_key(name)
    for a, b in (("ة", "ه"), ("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ؤ", "و"), ("ئ", "ي")):
        k = k.replace(a, b)
    return k.replace(" ", "")


def parse_date(value: str | None) -> tuple[dt.date | None, str | None]:
    """ISO date or None. Returns (date, problem). An empty value is not a problem; a malformed one is."""
    text = clean_text(value)
    if not text:
        return None, None
    try:
        return dt.date.fromisoformat(text[:10]), None
    except ValueError:
        return None, f"تاريخ غير صالح: {text[:20]}"


def parse_amount(value: str | None) -> tuple[float | None, str | None]:
    text = clean_text(value).replace(",", "").replace("٬", "").replace("ر.س", "").replace("SAR", "").strip()
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789."))
    if not text:
        return None, None
    try:
        amount = float(text)
    except ValueError:
        return None, f"مبلغ غير صالح: {text[:20]}"
    if not math.isfinite(amount) or amount < 0:
        return None, f"مبلغ غير صالح: {text[:20]}"
    return amount, None


def parse_int(value: str | None) -> int | None:
    text = clean_text(value)
    return int(text) if text.isdigit() else None


def map_status(text: str | None) -> str:
    return STATUS_MAP.get(clean_text(text), "unknown")


def city_region(text: str | None) -> tuple[str | None, str | None]:
    city = clean_text(text)
    if city in UNKNOWN_LOCATION:
        return None, None
    base = CITY_ALIASES.get(city, city)
    return city, CITIES.get(base) or EXTRA_REGIONS.get(base)


def classify_trades(title: str) -> list[str]:
    t = clean_text(title)
    return [trade for trade, words in TRADE_KEYWORDS.items() if any(w in t for w in words)]


def lifecycle(status: str, deadline: dt.date | None, today: dt.date) -> str:
    """A/B/C only. D (confirmed subcontract) needs human evidence and is never derived from a tender."""
    if status == "awarded":
        return "C_awarded"
    if status in ("announced", "open") and deadline and deadline >= today:
        return "A_open"
    return "B_closed"


def completeness(fields: dict) -> float:
    """Share of the fields a full record would have, given its status (an open tender has no winner yet)."""
    wanted = ["title", "issuer", "city", "published_date", "submission_deadline", "status"]
    if fields.get("status") == "awarded":
        wanted += ["award_date", "winner", "award_amount", "offer_count_claimed", "bidders_known"]
    present = sum(1 for k in wanted if fields.get(k) not in (None, "", 0, []))
    return round(present / len(wanted), 3)
