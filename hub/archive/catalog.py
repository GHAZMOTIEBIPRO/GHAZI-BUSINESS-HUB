"""The official Etimad open datasets (tenders, awards, contracts) and which of them the archive already holds.

The list comes from the public open data library of the National Center for Government Resource Systems, read as an
anonymous visitor (see data/etimad_open_datasets.json). The files themselves are on open.data.gov.sa, which is not
reachable from cloud hosts; an admin downloads each file and uploads it, choosing the dataset here so its publisher,
attribution and link are recorded with every record.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ImportBatch

CATALOG_PATH = Path(__file__).with_name("data") / "etimad_open_datasets.json"


@lru_cache(maxsize=1)
def catalog() -> dict:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    data["by_id"] = {d["id"]: d for d in data["datasets"]}
    return data


def dataset(dataset_id: str) -> dict | None:
    return catalog()["by_id"].get(str(dataset_id or ""))


def batch_label(d: dict) -> str:
    return f"{d['id']} — {d['title']}"


def import_status(s: Session) -> dict[str, dict]:
    """dataset id -> latest batch {id, status, tenders_new}, for datasets that were uploaded at least once."""
    out: dict[str, dict] = {}
    rows = s.scalars(select(ImportBatch).where(ImportBatch.status != "rolled_back").order_by(ImportBatch.id)).all()
    for b in rows:
        dataset_id = (b.label or "").split(" — ", 1)[0]
        if dataset_id in catalog()["by_id"]:
            out[dataset_id] = {"batch_id": b.id, "status": b.status, "tenders_new": (b.counts or {}).get("tenders_new", 0)}
    return out
