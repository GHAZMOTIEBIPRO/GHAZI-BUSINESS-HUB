"""Tender-package importer: dry-run, validate, staging, commit, rollback, resume.

Package layout (directory or .zip, as delivered on 2026-11-13):
  manifest.json
  import/tenders_staging.csv    one row per tender (key: tender_reference)
  import/bids_staging.csv       bids published for a tender (key: tender_reference + bidder name)
  import/companies_staging.csv  name strings seen in the package (a cross-check, NOT legal identities)

Guarantees:
- dry-run touches no database; validate only reads it;
- staging writes in chunks, each chunk + its checkpoint in one transaction, so an interrupted run resumes
  exactly where it stopped (`resume`), and a re-run of the same package changes nothing (idempotent);
- every overwritten value goes to change_history; rollback deletes the rows a batch created and restores
  the values it overwrote;
- everything lands private (visibility=private_staging, publish_allowed=False) whatever the input says;
- a winner comes only from the published winner field of an awarded tender — never from the lowest bid;
  an announced/open tender never gets an award.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import JSON, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..db import utcnow
from . import normalize as N
from .models import (
    ChangeHistory,
    Company,
    CompanyAlias,
    ImportBatch,
    ImportReject,
    ProcurementIssuer,
    ProcurementTender,
    TenderAward,
    TenderBid,
    VerificationEvent,
)
from .sources import ensure_sources

FILES = {"tenders": "import/tenders_staging.csv", "bids": "import/bids_staging.csv",
         "companies": "import/companies_staging.csv"}
TENDER_COLUMNS = {"tender_reference", "title", "issuer", "city_or_location", "announced_status", "published_date",
                  "submission_deadline", "award_date", "winner_name_as_published", "award_amount_sar_as_published",
                  "offer_count_claimed", "bidders_name_count_public", "source_url", "source_publisher",
                  "verification_notes"}
BID_COLUMNS = {"tender_reference", "bidder_name_as_published", "bid_price_sar_as_published",
               "technical_or_award_result", "bid_status", "source_url"}
MAX_PACKAGE_BYTES = 25_000_000


class PackageError(ValueError):
    """The package itself is unusable (missing file, wrong columns, too large)."""


class Interrupted(RuntimeError):
    """Raised by the test hook to simulate a crash between chunks."""


@dataclass
class Package:
    sha256: str
    manifest: dict
    tenders: list[dict]
    bids: list[dict]
    companies: list[dict]


@dataclass
class Validated:
    tenders: list[dict] = field(default_factory=list)  # normalised, accepted
    bids: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)  # {file,row,severity,reason,raw}
    missing: Counter = field(default_factory=Counter)
    dup_in_file: Counter = field(default_factory=Counter)


# ------------------------------------------------------------------ reading
def _read_csv(blob: bytes, name: str, required: set[str]) -> list[dict]:
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageError(f"{name}: الترميز ليس UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    cols = set(reader.fieldnames or [])
    if not required <= cols:
        raise PackageError(f"{name}: أعمدة ناقصة {sorted(required - cols)}")
    return [{k: (v or "") for k, v in row.items() if k} for row in reader]


def load_package(path: str | Path) -> Package:
    path = Path(path)
    blobs: dict[str, bytes] = {}
    digest = hashlib.sha256()
    if path.is_file() and zipfile.is_zipfile(path):
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise PackageError("الحزمة أكبر من 25 ميجابايت")
        with zipfile.ZipFile(path) as zf:
            names = {Path(n).as_posix().lstrip("./"): n for n in zf.namelist() if not n.endswith("/")}
            for want in [*FILES.values(), "manifest.json"]:
                hit = next((orig for clean, orig in names.items() if clean == want or clean.endswith("/" + want)), None)
                if hit is not None:
                    info = zf.getinfo(hit)
                    if info.file_size > MAX_PACKAGE_BYTES:
                        raise PackageError(f"{want}: الملف أكبر من المسموح")
                    blobs[want] = zf.read(hit)
    elif path.is_dir():
        for want in [*FILES.values(), "manifest.json"]:
            p = path / want
            if p.exists():
                blobs[want] = p.read_bytes()
    else:
        raise PackageError("المسار ليس ملف ZIP ولا مجلدًا")
    for key in FILES.values():
        if key not in blobs:
            raise PackageError(f"الملف المطلوب غير موجود في الحزمة: {key}")
    for key in sorted(blobs):  # stable package fingerprint from the data files themselves
        digest.update(key.encode())
        digest.update(blobs[key])
    manifest = {}
    if "manifest.json" in blobs:
        try:
            manifest = json.loads(blobs["manifest.json"].decode("utf-8-sig"))
        except ValueError:
            manifest = {"_error": "manifest.json غير صالح"}
    return Package(sha256=digest.hexdigest(), manifest=manifest,
                   tenders=_read_csv(blobs[FILES["tenders"]], FILES["tenders"], TENDER_COLUMNS),
                   bids=_read_csv(blobs[FILES["bids"]], FILES["bids"], BID_COLUMNS),
                   companies=_read_csv(blobs[FILES["companies"]], FILES["companies"], {"company_name_as_published"}))


# ------------------------------------------------------------------ validation (pure)
def validate(pkg: Package, today: dt.date) -> Validated:
    v = Validated()
    seen_refs: set[str] = set()
    tender_by_ref: dict[str, dict] = {}

    def reject(file, row, reason, raw, severity="reject"):
        v.rejects.append({"file": file, "row": row, "severity": severity, "reason": reason, "raw": raw})

    for i, raw in enumerate(pkg.tenders, start=2):  # row numbers as in a spreadsheet (header = 1)
        ref = N.clean_text(raw.get("tender_reference"))
        title = N.clean_text(raw.get("title"))
        if not ref or not N.REF_RE.match(ref):
            reject("tenders", i, "رقم المنافسة مفقود أو غير صالح", raw)
            continue
        if not title:
            reject("tenders", i, "اسم المنافسة مفقود", raw)
            continue
        if ref in seen_refs:
            v.dup_in_file["tenders"] += 1
            reject("tenders", i, "رقم منافسة مكرر داخل الملف (أُخذ أول ظهور)", raw, "warning")
            continue
        seen_refs.add(ref)
        status = N.map_status(raw.get("announced_status"))
        if status == "unknown":
            reject("tenders", i, f"حالة غير معروفة: {N.clean_text(raw.get('announced_status'))[:40]}", raw, "warning")
        pub, e1 = N.parse_date(raw.get("published_date"))
        deadline, e2 = N.parse_date(raw.get("submission_deadline"))
        award_date, e3 = N.parse_date(raw.get("award_date"))
        amount, e4 = N.parse_amount(raw.get("award_amount_sar_as_published"))
        for problem in (e1, e2, e3, e4):
            if problem:
                reject("tenders", i, problem, raw, "warning")
        city, region = N.city_region(raw.get("city_or_location"))
        winner = N.clean_text(raw.get("winner_name_as_published"))
        if winner and status != "awarded":
            reject("tenders", i, "اسم فائز لمنافسة غير مرساة: لم يُسجل فائز", raw, "warning")
            winner = ""
        if status == "awarded" and not winner:
            reject("tenders", i, "منافسة مرساة دون اسم فائز منشور: بلا ترسية مسجلة", raw, "warning")
        rec = {
            "row": i, "official_reference": ref, "title": title, "issuer": N.clean_text(raw.get("issuer")),
            "city": city, "region": region, "work_types": N.classify_trades(title), "status": status,
            "status_as_published": N.clean_text(raw.get("announced_status")), "published_date": pub,
            "submission_deadline": deadline, "award_date": award_date,
            "lifecycle": N.lifecycle(status, deadline, today), "winner": winner,
            "award_amount": amount if winner else None, "award_amount_raw": N.clean_text(raw.get("award_amount_sar_as_published")),
            "offer_count_claimed": N.parse_int(raw.get("offer_count_claimed")),
            "bidders_public_claimed": N.parse_int(raw.get("bidders_name_count_public")),
            "source_url": N.clean_text(raw.get("source_url")), "source_publisher": N.clean_text(raw.get("source_publisher")),
            "verification_notes": N.clean_text(raw.get("verification_notes")), "raw": raw,
        }
        if rec["source_url"] and not rec["source_url"].startswith("https://"):
            reject("tenders", i, "رابط المصدر ليس HTTPS: حُذف الرابط", raw, "warning")
            rec["source_url"] = ""
        for fld in ("issuer", "city", "published_date", "submission_deadline", "offer_count_claimed", "source_url"):
            if rec[fld] in (None, ""):
                v.missing[fld] += 1
        if status == "awarded":
            for fld in ("award_date", "winner", "award_amount"):
                if rec[fld] in (None, ""):
                    v.missing[fld] += 1
        v.tenders.append(rec)
        tender_by_ref[ref] = rec

    seen_bids: set[tuple[str, str]] = set()
    for i, raw in enumerate(pkg.bids, start=2):
        ref = N.clean_text(raw.get("tender_reference"))
        name = N.clean_text(raw.get("bidder_name_as_published"))
        if ref not in tender_by_ref:
            reject("bids", i, "عرض لمنافسة غير موجودة في ملف المنافسات (يتيم)", raw)
            continue
        if not name:
            reject("bids", i, "اسم المتقدم مفقود", raw)
            continue
        key = (ref, N.name_key(name))
        if key in seen_bids:
            v.dup_in_file["bids"] += 1
            reject("bids", i, "عرض مكرر لنفس المتقدم في نفس المنافسة", raw, "warning")
            continue
        seen_bids.add(key)
        amount, problem = N.parse_amount(raw.get("bid_price_sar_as_published"))
        if problem:
            reject("bids", i, problem, raw, "warning")
        tender = tender_by_ref[ref]
        status_text = N.clean_text(raw.get("bid_status"))
        if status_text == "فائز" and N.name_key(name) != N.name_key(tender["winner"] or ""):
            reject("bids", i, "عرض موسوم «فائز» لا يطابق اسم الفائز المنشور للمنافسة: لم يُعتمد فائزًا", raw, "warning")
        if tender["status"] != "awarded" and status_text == "فائز":
            status_text = "غير محدد (المنافسة غير مرساة)"
        v.bids.append({"row": i, "ref": ref, "name": name, "amount": amount,
                       "amount_raw": N.clean_text(raw.get("bid_price_sar_as_published")),
                       "technical": N.clean_text(raw.get("technical_or_award_result")), "status": status_text,
                       "source_url": N.clean_text(raw.get("source_url")) if N.clean_text(raw.get("source_url")).startswith("https://") else ""})

    published = Counter(b["ref"] for b in v.bids)
    for rec in v.tenders:
        claimed = rec["bidders_public_claimed"]
        if claimed is not None and claimed != published.get(rec["official_reference"], 0):
            reject("tenders", rec["row"], f"عدد الأسماء المعلن ({claimed}) ≠ الأسماء المرفقة ({published.get(rec['official_reference'], 0)})",
                   rec["raw"], "warning")
    return v


# ------------------------------------------------------------------ importer
class Importer:
    def __init__(self, session_factory: sessionmaker, *, source_key: str = "research_package_2026_11_13",
                 chunk_size: int = 200, today: dt.date | None = None, created_by: str = "cli",
                 fail_after_chunks: int | None = None, synthetic: bool = False):
        self.sf = session_factory
        self.source_key = source_key
        self.chunk = max(1, chunk_size)
        self.today = today or dt.date.today()
        self.created_by = created_by
        self.fail_after_chunks = fail_after_chunks  # test hook: simulate a crash after N committed chunks
        self.synthetic = synthetic
        self._chunks_done = 0
        # per-run caches of committed ids (rows are only cached after their chunk committed or within it)
        self._issuer_ids: dict[str, int] = {}
        self._company_ids: dict[str, int] = {}
        self._aliases_seen: set[tuple[int, str]] = set()

    # ---- read-only modes
    def dry_run(self, path) -> dict:
        pkg = load_package(path)
        v = validate(pkg, self.today)
        return self._report(pkg, v, mode="dry-run", db_view=None)

    def validate_against_db(self, path) -> dict:
        pkg = load_package(path)
        v = validate(pkg, self.today)
        with self.sf() as s:
            refs = [t["official_reference"] for t in v.tenders]
            existing = set(s.scalars(select(ProcurementTender.official_reference).where(
                ProcurementTender.official_reference.in_(refs)))) if refs else set()
            same_pkg = s.scalars(select(ImportBatch).where(ImportBatch.package_sha256 == pkg.sha256)).all()
            view = {"tenders_already_in_archive": len(existing), "tenders_would_be_new": len(set(refs) - existing),
                    "previous_batches_of_this_package": [{"id": b.id, "status": b.status} for b in same_pkg]}
        return self._report(pkg, v, mode="validate", db_view=view)

    # ---- writing modes
    def stage(self, path, *, resume_batch_id: int | None = None) -> dict:
        pkg = load_package(path)
        v = validate(pkg, self.today)
        with self.sf() as s:
            sources = ensure_sources(s)
            source = sources[self.source_key]
            if resume_batch_id is None:
                running = s.scalars(select(ImportBatch).where(ImportBatch.package_sha256 == pkg.sha256,
                                                              ImportBatch.status == "in_progress")).first()
                if running:
                    raise PackageError(f"دفعة سابقة لنفس الحزمة لم تكتمل (#{running.id}): استخدم --resume {running.id}")
                batch = ImportBatch(source_id=source.id, package_sha256=pkg.sha256, status="in_progress",
                                    label=str(Path(path).name), created_by=self.created_by, is_synthetic=self.synthetic,
                                    counts={}, checkpoint={"stage": "tenders", "next": 0})
                s.add(batch)
                s.flush()
                for r in v.rejects:
                    s.add(ImportReject(batch_id=batch.id, file=r["file"], row_number=r["row"], severity=r["severity"],
                                       reason=r["reason"][:300], raw=r["raw"]))
                s.commit()
                batch_id = batch.id
            else:
                batch = s.get(ImportBatch, resume_batch_id)
                if batch is None or batch.status != "in_progress":
                    raise PackageError("لا توجد دفعة غير مكتملة بهذا الرقم")
                if batch.package_sha256 != pkg.sha256:
                    raise PackageError("الحزمة لا تطابق بصمة الدفعة الأصلية: لا يمكن الاستكمال بملف مختلف")
                batch_id = batch.id
            source_id = source.id

        self._run_chunks(batch_id, source_id, v)
        with self.sf() as s:
            batch = s.get(ImportBatch, batch_id)
            report = self._report(pkg, v, mode="staging", db_view=self._db_view(s, batch_id, v))
            report["batch_counts"] = dict(batch.counts or {})  # new / updated / unchanged written by this batch
            batch.status = "staged"
            batch.report = report
            batch.finished_at = utcnow()
            s.commit()
            report["batch_id"] = batch_id
            return report

    def _run_chunks(self, batch_id: int, source_id: int, v: Validated) -> None:
        stages = [("tenders", v.tenders), ("bids", v.bids), ("finalize", None)]
        while True:
            with self.sf() as s:
                batch = s.get(ImportBatch, batch_id)
                cp = dict(batch.checkpoint or {})
                stage, start = cp.get("stage", "tenders"), int(cp.get("next", 0))
                if stage == "done":
                    return
                rows = dict(stages)[stage]
                counts = Counter(batch.counts or {})
                if stage == "finalize":
                    self._finalize(s, batch_id, v, counts)
                    nxt = {"stage": "done", "next": 0}
                else:
                    part = rows[start:start + self.chunk]
                    for rec in part:
                        (self._upsert_tender if stage == "tenders" else self._upsert_bid)(s, batch_id, source_id, rec, counts)
                    end = start + len(part)
                    nxt = {"stage": stage, "next": end} if end < len(rows) else \
                        {"stage": "bids" if stage == "tenders" else "finalize", "next": 0}
                batch.counts = dict(counts)
                batch.checkpoint = nxt
                s.commit()  # chunk + checkpoint are one transaction
            self._chunks_done += 1
            if self.fail_after_chunks is not None and self._chunks_done >= self.fail_after_chunks:
                raise Interrupted(f"simulated interruption after {self._chunks_done} chunk(s)")

    # ---- per-record upserts
    def _issuer(self, s: Session, batch_id: int, name: str, counts: Counter) -> int | None:
        if not name:
            return None
        key = N.name_key(name)
        if key in self._issuer_ids:
            return self._issuer_ids[key]
        issuer = s.scalars(select(ProcurementIssuer).where(ProcurementIssuer.name_key == key)).first()
        if issuer is None:
            issuer = ProcurementIssuer(name=name, name_key=key, first_batch_id=batch_id)
            s.add(issuer)
            s.flush()
            counts["issuers_new"] += 1
        self._issuer_ids[key] = issuer.id
        return issuer.id

    def _company(self, s: Session, batch_id: int, source_id: int, name: str, counts: Counter, city: str | None) -> Company:
        key = N.name_key(name)
        cid = self._company_ids.get(key)
        company = s.get(Company, cid) if cid else s.scalars(select(Company).where(Company.name_key == key)).first()
        if company is None:
            company = Company(display_name=name, name_key=key, fold_key=N.fold_key(name), first_batch_id=batch_id,
                              identity_status="name_only_unverified", specialties=[], cities=[])
            s.add(company)
            s.flush()
            counts["companies_new"] += 1
        hops = 0
        while company.merged_into_id and hops < 5:  # a name merged by verified CR belongs to the surviving row
            company, hops = s.get(Company, company.merged_into_id), hops + 1
        self._company_ids[key] = company.id
        if (company.id, name) not in self._aliases_seen:
            if not s.scalars(select(CompanyAlias.id).where(CompanyAlias.company_id == company.id,
                                                           CompanyAlias.alias_raw == name)).first():
                s.add(CompanyAlias(company_id=company.id, alias_raw=name, source_id=source_id, first_batch_id=batch_id))
                counts["aliases_new"] += 1
            self._aliases_seen.add((company.id, name))
        if city and city not in (company.cities or []):
            company.cities = [*(company.cities or []), city]
        return company

    def _history(self, s, batch_id, etype, eid, fld, old, new):
        s.add(ChangeHistory(batch_id=batch_id, entity_type=etype, entity_id=eid, field=fld,
                            old_value=_serialise(old), new_value=_serialise(new)))

    def _upsert_tender(self, s: Session, batch_id: int, source_id: int, rec: dict, counts: Counter) -> None:
        issuer_id = self._issuer(s, batch_id, rec["issuer"], counts)
        values = {
            "title": rec["title"], "issuer_id": issuer_id, "city": rec["city"], "region": rec["region"],
            "work_types": rec["work_types"], "work_types_text": "|".join(rec["work_types"]),
            "status": rec["status"], "status_as_published": rec["status_as_published"],
            "lifecycle": rec["lifecycle"], "published_date": rec["published_date"],
            "submission_deadline": rec["submission_deadline"], "award_date": rec["award_date"],
            "offer_count_claimed": rec["offer_count_claimed"], "source_url": rec["source_url"],
            "source_publisher": rec["source_publisher"], "verification_notes": rec["verification_notes"],
        }
        tender = s.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == rec["official_reference"])).first()
        if tender is None:
            tender = ProcurementTender(official_reference=rec["official_reference"], source_id=source_id,
                                       raw=rec["raw"], first_batch_id=batch_id, last_batch_id=batch_id,
                                       source_rights_status="synthetic_test" if self.synthetic else "unverified",
                                       publish_allowed=False, visibility="private_staging",
                                       review_status="synthetic" if self.synthetic else "pending_rights_review",
                                       is_synthetic=self.synthetic, **values)
            s.add(tender)
            s.flush()
            counts["tenders_new"] += 1
            s.add(VerificationEvent(entity_type="tender", entity_id=tender.id, event="imported_from_secondary_source",
                                    result=rec["source_publisher"] or "unknown", source_url=rec["source_url"], batch_id=batch_id))
        else:
            changed = False
            for fld, new in values.items():
                old = getattr(tender, fld)
                if new in (None, "", []) and old not in (None, "", []):
                    continue  # never erase a known value with a blank one
                if old != new:
                    self._history(s, batch_id, "tender", tender.id, fld, old, new)  # every overwrite is kept
                    setattr(tender, fld, new)
                    changed = True
            if changed:
                tender.last_batch_id = batch_id
                counts["tenders_updated"] += 1
            else:
                counts["tenders_existing_unchanged"] += 1
                s.add(VerificationEvent(entity_type="tender", entity_id=tender.id, event="reconfirmed_unchanged",
                                        source_url=rec["source_url"], batch_id=batch_id))
        if rec["winner"]:
            company = self._company(s, batch_id, source_id, rec["winner"], counts, rec["city"])
            award = s.scalars(select(TenderAward).where(TenderAward.tender_id == tender.id,
                                                        TenderAward.winner_name_raw == rec["winner"])).first()
            if award is None:
                s.add(TenderAward(tender_id=tender.id, company_id=company.id, winner_name_raw=rec["winner"],
                                  award_amount_sar=rec["award_amount"], award_amount_raw=rec["award_amount_raw"],
                                  award_date=rec["award_date"], source_url=rec["source_url"], first_batch_id=batch_id,
                                  verification_status="secondary_source_unverified"))
                counts["awards_new"] += 1
            else:
                for fld, new in (("award_amount_sar", rec["award_amount"]), ("award_date", rec["award_date"])):
                    old = getattr(award, fld)
                    if new is not None and old != new:
                        self._history(s, batch_id, "award", award.id, fld, old, new)
                        setattr(award, fld, new)
                        counts["awards_updated"] += 1

    def _upsert_bid(self, s: Session, batch_id: int, source_id: int, rec: dict, counts: Counter) -> None:
        tender = s.scalars(select(ProcurementTender).where(ProcurementTender.official_reference == rec["ref"])).first()
        company = self._company(s, batch_id, source_id, rec["name"], counts, tender.city)
        bid = s.scalars(select(TenderBid).where(TenderBid.tender_id == tender.id,
                                                TenderBid.bidder_name_raw == rec["name"])).first()
        if bid is None:
            s.add(TenderBid(tender_id=tender.id, company_id=company.id, bidder_name_raw=rec["name"],
                            bid_amount_sar=rec["amount"], bid_amount_raw=rec["amount_raw"], technical_result=rec["technical"],
                            bid_status=rec["status"], source_url=rec["source_url"], first_batch_id=batch_id))
            counts["bids_new"] += 1
            return
        changed = False
        for fld, new in (("bid_amount_sar", rec["amount"]), ("technical_result", rec["technical"]), ("bid_status", rec["status"])):
            old = getattr(bid, fld)
            if new not in (None, "") and old != new:
                self._history(s, batch_id, "bid", bid.id, fld, old, new)
                setattr(bid, fld, new)
                changed = True
        counts["bids_updated" if changed else "bids_existing_unchanged"] += 1

    def _finalize(self, s: Session, batch_id: int, v: Validated, counts: Counter) -> None:
        """Derived per-tender facts: bidders known, completeness, price rank among *published* bids."""
        refs = [t["official_reference"] for t in v.tenders]
        by_ref = {t["official_reference"]: t for t in v.tenders}
        for tender in s.scalars(select(ProcurementTender).where(ProcurementTender.official_reference.in_(refs))):
            bids = sorted(tender.bids, key=lambda b: (b.bid_amount_sar is None, b.bid_amount_sar or 0))
            for rank, bid in enumerate([b for b in bids if b.bid_amount_sar is not None], start=1):
                bid.price_rank_among_published = rank
            tender.bidders_known = len(tender.bids)
            tender.bid_list_complete = bool(tender.offer_count_claimed) and tender.bidders_known >= tender.offer_count_claimed
            rec = by_ref[tender.official_reference]
            tender.completeness = N.completeness({
                "title": tender.title, "issuer": tender.issuer_id, "city": tender.city,
                "published_date": tender.published_date, "submission_deadline": tender.submission_deadline,
                "status": tender.status, "award_date": tender.award_date, "winner": rec["winner"],
                "award_amount": rec["award_amount"], "offer_count_claimed": tender.offer_count_claimed,
                "bidders_known": tender.bidders_known})
            for bid in tender.bids:
                trades = set(bid.company.specialties or [])
                bid.company.specialties = sorted(trades | set(tender.work_types or []))

    # ---- commit / rollback
    def commit(self, batch_id: int) -> dict:
        with self.sf() as s:
            batch = s.get(ImportBatch, batch_id)
            if batch is None or batch.status != "staged":
                raise PackageError("يمكن اعتماد دفعة في حالة staged فقط")
            n = s.execute(update(ProcurementTender).where(ProcurementTender.last_batch_id == batch_id,
                                                          ProcurementTender.visibility == "private_staging")
                          .values(visibility="archive_private")).rowcount
            batch.status = "committed"
            batch.finished_at = utcnow()
            s.add(VerificationEvent(entity_type="batch", entity_id=batch_id, event="committed_to_private_archive",
                                    result=f"{n} tenders", batch_id=batch_id))
            s.commit()
            # still private: publish_allowed stays False until a licence is verified for the source
            return {"batch_id": batch_id, "status": "committed", "tenders_moved_to_private_archive": n,
                    "publicly_visible": 0}

    def rollback(self, batch_id: int) -> dict:
        with self.sf() as s:
            batch = s.get(ImportBatch, batch_id)
            if batch is None or batch.status in ("rolled_back",):
                raise PackageError("الدفعة غير موجودة أو تم التراجع عنها")
            out = Counter()
            # 1) restore overwritten values, newest first
            for h in s.scalars(select(ChangeHistory).where(ChangeHistory.batch_id == batch_id).order_by(ChangeHistory.id.desc())):
                model = {"tender": ProcurementTender, "bid": TenderBid, "award": TenderAward}.get(h.entity_type)
                row = s.get(model, h.entity_id) if model else None
                if row is not None:
                    setattr(row, h.field, _coerce(model, h.field, h.old_value))
                    out["values_restored"] += 1
            # 2) delete rows this batch created (children first)
            out["bids_deleted"] = s.execute(delete(TenderBid).where(TenderBid.first_batch_id == batch_id)).rowcount
            out["awards_deleted"] = s.execute(delete(TenderAward).where(TenderAward.first_batch_id == batch_id)).rowcount
            out["tenders_deleted"] = s.execute(delete(ProcurementTender).where(ProcurementTender.first_batch_id == batch_id)).rowcount
            out["aliases_deleted"] = s.execute(delete(CompanyAlias).where(CompanyAlias.first_batch_id == batch_id)).rowcount
            # a row a person verified or merged by CR is curation, not import output: it stays
            orphan_companies = select(Company.id).where(Company.first_batch_id == batch_id,
                                                        Company.identity_status == "name_only_unverified",
                                                        ~Company.bids.any(), ~Company.awards.any())
            out["companies_deleted"] = s.execute(delete(Company).where(Company.id.in_(orphan_companies))).rowcount
            used_issuers = select(ProcurementTender.issuer_id).where(ProcurementTender.issuer_id.is_not(None))
            out["issuers_deleted"] = s.execute(delete(ProcurementIssuer).where(
                ProcurementIssuer.first_batch_id == batch_id, ProcurementIssuer.id.not_in(used_issuers))).rowcount
            batch.status = "rolled_back"
            batch.finished_at = utcnow()
            s.add(VerificationEvent(entity_type="batch", entity_id=batch_id, event="rolled_back",
                                    result=json.dumps(dict(out)), batch_id=None))
            s.commit()
            return {"batch_id": batch_id, "status": "rolled_back", **out}

    # ---- reporting
    def _db_view(self, s: Session, batch_id: int, v: Validated) -> dict:
        refs = [t["official_reference"] for t in v.tenders]
        tenders = s.scalars(select(ProcurementTender).where(ProcurementTender.official_reference.in_(refs))
                            .order_by(ProcurementTender.official_reference)).all() if refs else []
        per = []
        for t in tenders:
            per.append({"ref": t.official_reference, "status": t.status, "lifecycle": t.lifecycle,
                        "issuer": t.issuer.name if t.issuer else None, "city": t.city,
                        "winner": [a.winner_name_raw for a in t.awards],
                        "award_amount_sar": [a.award_amount_sar for a in t.awards],
                        "bidders_known": t.bidders_known, "offer_count_claimed": t.offer_count_claimed,
                        "bid_list_complete": t.bid_list_complete, "completeness": t.completeness,
                        "companies": [b.bidder_name_raw for b in sorted(t.bids, key=lambda b: b.id)],
                        "visibility": t.visibility, "publish_allowed": t.publish_allowed})
        groups = defaultdict(set)
        for c in s.scalars(select(Company)):
            groups[c.fold_key].add(c.display_name)
        possible = [sorted(names) for names in groups.values() if len(names) > 1]
        return {"per_tender": per, "possible_same_entity_groups": possible,
                "archive_totals": {
                    "tenders": s.scalar(select(func.count(ProcurementTender.id)).where(ProcurementTender.is_synthetic.is_(False))),
                    "bids": s.scalar(select(func.count(TenderBid.id))),
                    "awards": s.scalar(select(func.count(TenderAward.id))),
                    "companies": s.scalar(select(func.count(Company.id))),
                    "publicly_visible_tenders": s.scalar(select(func.count(ProcurementTender.id)).where(
                        ProcurementTender.publish_allowed.is_(True)))}}

    def _report(self, pkg: Package, v: Validated, *, mode: str, db_view: dict | None) -> dict:
        names_pkg = {N.name_key(b["name"]) for b in v.bids} | {N.name_key(t["winner"]) for t in v.tenders if t["winner"]}
        names_csv = {N.name_key(r.get("company_name_as_published", "")) for r in pkg.companies if r.get("company_name_as_published")}
        manifest = pkg.manifest or {}
        mcheck = {k: {"manifest": manifest.get(m), "actual": a, "match": manifest.get(m) == a}
                  for k, m, a in (("tenders", "tender_records", len(pkg.tenders)), ("bids", "bid_records", len(pkg.bids)),
                                  ("companies", "company_names", len(pkg.companies)))}
        rejected = [r for r in v.rejects if r["severity"] == "reject"]
        warnings = [r for r in v.rejects if r["severity"] == "warning"]
        claimed = [t["offer_count_claimed"] for t in v.tenders if t["offer_count_claimed"] is not None]
        refs_with_claim = {t["official_reference"] for t in v.tenders if t["offer_count_claimed"] is not None}
        bids_per_ref = Counter(b["ref"] for b in v.bids)
        known_where_claimed = sum(bids_per_ref[r] for r in refs_with_claim)
        report = {
            "mode": mode, "generated_at": utcnow().isoformat(timespec="seconds"), "package_sha256": pkg.sha256,
            "manifest_check": mcheck,
            "read": {"tenders": len(pkg.tenders), "bids": len(pkg.bids), "companies": len(pkg.companies)},
            "accepted": {"tenders": len(v.tenders), "bids": len(v.bids)},
            "rejected_count": len(rejected), "warnings_count": len(warnings),
            "rejected": [{k: r[k] for k in ("file", "row", "reason")} for r in rejected],
            "warnings": [{k: r[k] for k in ("file", "row", "reason")} for r in warnings],
            "duplicates_in_file": dict(v.dup_in_file), "missing_fields": dict(v.missing),
            "status_counts": dict(Counter(t["status"] for t in v.tenders)),
            "lifecycle_counts": dict(Counter(t["lifecycle"] for t in v.tenders)),
            "bidders": {"known_bid_rows": len(v.bids), "tenders_with_claimed_count": len(claimed),
                        "claimed_offers_total": sum(claimed), "known_bids_in_those_tenders": known_where_claimed,
                        "tenders_with_complete_public_list": sum(
                            1 for t in v.tenders if t["offer_count_claimed"] and
                            bids_per_ref[t["official_reference"]] >= t["offer_count_claimed"])},
            "companies": {"distinct_names_linked_in_package": len(names_pkg), "companies_csv_rows": len(pkg.companies),
                          "csv_names_not_linked": sorted(names_csv - names_pkg), "linked_names_not_in_csv": sorted(names_pkg - names_csv),
                          "identity": "name strings only — no CR / unified number in the package; no names merged"},
            "awards_published": sum(1 for t in v.tenders if t["winner"]),
            "rights": {"source_rights_status": "unverified", "publish_allowed": False,
                       "note": "خاص بالمراجعة. لا نشر قبل توثيق حق إعادة الاستخدام."},
        }
        if db_view is not None:
            report["database"] = db_view
        return report


def _serialise(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _coerce(model, fld, value):
    """Turn a change_history string back into the column's Python type (used by rollback)."""
    if value is None:
        return None
    col_type = model.__table__.c[fld].type
    if isinstance(col_type, JSON):
        return json.loads(value)
    py = col_type.python_type
    if py is dt.date:
        return dt.date.fromisoformat(value)
    if py is bool:
        return value == "True"
    if py in (int, float):
        return py(float(value))
    return value
