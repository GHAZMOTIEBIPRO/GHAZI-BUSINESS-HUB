"""The source directory: every channel a contractor can use, with its documented reuse rights.

Data lives in hub/data/sources_directory.json so a reviewer can add a source without touching code.
Rules enforced by validate():
- rights must be one of SOURCE_RIGHTS; `verified_public_reuse` is only allowed when evidence_status is
  `verified_text` (we read the official text ourselves) and the evidence quotes it;
- every entry carries a dated rights check, an https homepage and terms URL, known cities and a category;
- `channel: true` entries are mirrored into the Source table (manual, link-only) so reviewers can file
  opportunities under them; nothing in the directory is ever fetched automatically unless its rights allow it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from functools import lru_cache
from pathlib import Path

from .models import CITIES, SOURCE_RIGHTS

PATH = Path(__file__).resolve().parent / "data" / "sources_directory.json"
EVIDENCE_STATUS = {"verified_text": "نص رسمي مقروء", "secondary": "مصدر ثانوي — يحتاج تأكيدًا", "not_checked": "لم يُفحص بعد"}
AUTOMATION = {"none": "لا جلب آلي", "candidate": "مرشح للجلب الآلي", "active": "جلب آلي يعمل"}
REQUIRED = ("key", "name", "owner", "category", "cities", "homepage", "terms_url", "what", "how_to_use", "rights",
            "rights_evidence", "evidence_status", "rights_checked_at", "automation", "channel", "notes")
RIGHTS_BADGE = {"verified_public_reuse": "b-ok", "licensed_private": "b-ok", "link_only": "b-lead", "unknown": "b-warn",
                "restricted": "b-bad"}
FOCUS_CITIES = ["ينبع", "الرياض", "جدة"]
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


@lru_cache(maxsize=1)
def load() -> dict:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    errors = validate(data)
    if errors:
        raise ValueError("sources_directory.json: " + "; ".join(errors))
    return data


def validate(data: dict) -> list[str]:
    errors: list[str] = []
    cats = data.get("categories") or {}
    seen: set[str] = set()
    for i, s in enumerate(data.get("sources") or []):
        tag = f"#{i} {s.get('key', '?')}"
        for f in REQUIRED:
            if f not in s:
                errors.append(f"{tag}: missing {f}")
        if not KEY_RE.match(str(s.get("key", ""))):
            errors.append(f"{tag}: bad key")
        if s.get("key") in seen:
            errors.append(f"{tag}: duplicate key")
        seen.add(s.get("key"))
        if s.get("category") not in cats:
            errors.append(f"{tag}: unknown category")
        if s.get("rights") not in SOURCE_RIGHTS:
            errors.append(f"{tag}: unknown rights")
        if s.get("evidence_status") not in EVIDENCE_STATUS:
            errors.append(f"{tag}: unknown evidence_status")
        if s.get("automation") not in AUTOMATION:
            errors.append(f"{tag}: unknown automation")
        if s.get("rights") in ("verified_public_reuse", "licensed_private"):
            if s.get("evidence_status") != "verified_text":
                errors.append(f"{tag}: reusable rights need evidence_status=verified_text")
            if len(str(s.get("rights_evidence", ""))) < 80:
                errors.append(f"{tag}: reusable rights need quoted evidence")
        if s.get("automation") == "active" and s.get("rights") not in ("verified_public_reuse", "licensed_private"):
            errors.append(f"{tag}: automation needs reusable rights")
        for f in ("homepage", "terms_url"):
            if not str(s.get(f, "")).startswith("https://"):
                errors.append(f"{tag}: {f} must be https")
        for c in s.get("cities") or []:
            if c != "all" and c not in CITIES:
                errors.append(f"{tag}: unknown city {c}")
        if not s.get("cities"):
            errors.append(f"{tag}: cities empty")
        try:
            dt.date.fromisoformat(str(s.get("rights_checked_at")))
        except ValueError:
            errors.append(f"{tag}: rights_checked_at must be YYYY-MM-DD")
        if not isinstance(s.get("channel"), bool):
            errors.append(f"{tag}: channel must be true/false")
    return errors


def sources(city: str | None = None, category: str | None = None) -> list[dict]:
    rows = load()["sources"]
    if city and city != "all":
        rows = [s for s in rows if city in s["cities"] or "all" in s["cities"]]
    if category:
        rows = [s for s in rows if s["category"] == category]
    return rows


def categories() -> dict:
    return load()["categories"]


def by_key(key: str) -> dict | None:
    return next((s for s in load()["sources"] if s["key"] == key), None)


def channel_registry() -> list[dict]:
    """Rows for the Source table: link-only, manual channels reviewers can file opportunities under."""
    out = []
    for s in load()["sources"]:
        if not s["channel"]:
            continue
        out.append({"key": s["key"], "name": s["name"][:160], "homepage": s["homepage"], "kind": "manual",
                    "rights": s["rights"], "rights_evidence": s["rights_evidence"],
                    "rights_checked_at": dt.datetime.fromisoformat(s["rights_checked_at"]), "trust": "review_all",
                    "notes": s["notes"] or s["what"]})
    return out
