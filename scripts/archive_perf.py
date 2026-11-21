"""Archive performance check on SYNTHETIC data (never real market records).

    HUB_DATABASE_URL=postgresql://.../hub_perf python scripts/archive_perf.py --tenders 10000 --via-importer
    HUB_DATABASE_URL=postgresql://.../hub_perf python scripts/archive_perf.py --tenders 100000

Every generated row has is_synthetic=True, source "synthetic_perf", rights "synthetic_test", reference prefix
"SYN-", and names starting with «اصطناعي». Searches exclude synthetic rows by default; this script opts in
explicitly to time them. Run it on a separate database, never on production.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import random
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, insert, select  # noqa: E402

from hub import db as dbm  # noqa: E402
from hub.archive.importer import Importer  # noqa: E402
from hub.archive.migrate import upgrade  # noqa: E402
from hub.archive.models import Company, CompanyAlias, ImportBatch, ProcurementTender, TenderAward, TenderBid  # noqa: E402
from hub.archive.search import TenderFilters, company_profile, search_tenders  # noqa: E402
from hub.archive.sources import ensure_sources  # noqa: E402

CITIES = ["ينبع", "جدة", "الرياض", "الدمام", "المدينة المنورة", "تبوك", "أبها", "حائل"]
WORK = ["دهانات", "بلاط", "ترميم", "صيانة", "أبواب", "أسقف جبسية", "تشغيل"]


def synthetic_rows(n: int, companies: int, seed: int = 7):
    rnd = random.Random(seed)
    names = [f"اصطناعي شركة {i}" for i in range(companies)]
    for i in range(n):
        ref = f"SYN-{i:08d}"
        pub = dt.date(2018, 1, 1) + dt.timedelta(days=rnd.randrange(0, 3200))
        awarded = rnd.random() < 0.8
        k = rnd.randrange(1, 6)
        bidders = rnd.sample(names, k)
        amounts = sorted(round(rnd.uniform(5e4, 5e7), 2) for _ in range(k))
        winner = bidders[rnd.randrange(k)] if awarded else ""
        yield ref, f"اصطناعي {rnd.choice(WORK)} مشروع {i}", rnd.choice(CITIES), pub, awarded, winner, list(zip(bidders, amounts, strict=True))


def via_importer(n: int, companies: int) -> dict:
    """Generate a package with the delivered layout and run the real importer on it (end-to-end path)."""
    t_rows, b_rows = [], []
    for ref, title, city, pub, awarded, winner, bids in synthetic_rows(n, companies):
        t_rows.append({"tender_reference": ref.replace("SYN-", "9"), "title": title, "issuer": "جهة اصطناعية",
                       "city_or_location": city, "announced_status": "تمت الترسية" if awarded else "مغلقة",
                       "published_date": pub.isoformat(), "submission_deadline": (pub + dt.timedelta(days=30)).isoformat(),
                       "award_date": (pub + dt.timedelta(days=90)).isoformat() if awarded else "",
                       "winner_name_as_published": winner, "award_amount_sar_as_published":
                           str(next(a for nm, a in bids if nm == winner)) if winner else "",
                       "offer_count_claimed": str(len(bids)), "bidders_name_count_public": str(len(bids)),
                       "source_url": "", "source_publisher": "synthetic", "verification_notes": "SYNTHETIC"})
        for nm, amount in bids:
            b_rows.append({"tender_reference": ref.replace("SYN-", "9"), "bidder_name_as_published": nm,
                           "bid_price_sar_as_published": str(amount), "technical_or_award_result": "",
                           "bid_status": "فائز" if nm == winner else "متقدم غير فائز", "source_url": ""})

    def to_csv(rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("import/tenders_staging.csv", to_csv(t_rows))
            zf.writestr("import/bids_staging.csv", to_csv(b_rows))
            zf.writestr("import/companies_staging.csv", b"company_name_as_published\n")
        imp = Importer(dbm.SessionLocal, source_key="synthetic_perf", chunk_size=500, synthetic=True, created_by="perf")
        t0 = time.perf_counter()
        report = imp.stage(path)
        elapsed = time.perf_counter() - t0
    return {"import_seconds": round(elapsed, 1), "tenders_per_second": round(n / elapsed, 1),
            "batch_counts": report["batch_counts"], "rejected": report["rejected_count"]}


def bulk(n: int, companies: int) -> dict:
    """Fast bulk load straight into the tables (to reach 100k for search timing)."""
    t0 = time.perf_counter()
    with dbm.SessionLocal() as s:
        src = ensure_sources(s)["synthetic_perf"]
        batch = ImportBatch(source_id=src.id, package_sha256="synthetic-bulk", label=f"synthetic bulk {n}",
                            status="staged", is_synthetic=True, created_by="perf")
        s.add(batch)
        s.flush()
        have = set(s.scalars(select(Company.display_name).where(Company.display_name.like("اصطناعي%"))))
        missing = [f"اصطناعي شركة {i}" for i in range(companies) if f"اصطناعي شركة {i}" not in have]
        if missing:
            s.execute(insert(Company), [{"display_name": nm, "name_key": nm, "fold_key": nm.replace(" ", ""),
                                         "identity_status": "name_only_unverified", "specialties": [], "cities": [],
                                         "first_batch_id": batch.id} for nm in missing])
            new_ids = dict(s.execute(select(Company.display_name, Company.id).where(Company.display_name.in_(missing))).all())
            s.execute(insert(CompanyAlias), [{"company_id": cid, "alias_raw": nm, "first_batch_id": batch.id}
                                             for nm, cid in new_ids.items()])
        ids = dict(s.execute(select(Company.display_name, Company.id).where(Company.display_name.like("اصطناعي%"))).all())
        rows = list(synthetic_rows(n, companies, seed=11))
        for start in range(0, n, 5000):
            part = rows[start:start + 5000]
            s.execute(insert(ProcurementTender), [{
                "official_reference": f"{ref}-B", "source_id": src.id, "title": title, "city": city, "region": None,
                "work_types": [], "work_types_text": "", "status": "awarded" if aw else "closed",
                "lifecycle": "C_awarded" if aw else "B_closed", "published_date": pub,
                "award_date": pub + dt.timedelta(days=90) if aw else None, "offer_count_claimed": len(bids),
                "bidders_known": len(bids), "source_rights_status": "synthetic_test", "publish_allowed": False,
                "visibility": "private_staging", "review_status": "synthetic", "is_synthetic": True, "raw": {},
                "first_batch_id": batch.id, "last_batch_id": batch.id}
                for ref, title, city, pub, aw, _w, bids in part])
            tid = dict(s.execute(select(ProcurementTender.official_reference, ProcurementTender.id).where(
                ProcurementTender.official_reference.in_([f"{p[0]}-B" for p in part]))).all())
            s.execute(insert(TenderBid), [{"tender_id": tid[f"{ref}-B"], "company_id": ids[nm], "bidder_name_raw": nm,
                                           "bid_amount_sar": amt, "first_batch_id": batch.id}
                                          for ref, _t, _c, _p, _a, _w, bids in part for nm, amt in bids])
            s.execute(insert(TenderAward), [{"tender_id": tid[f"{ref}-B"], "company_id": ids[w], "winner_name_raw": w,
                                             "award_amount_sar": next(a for nm, a in bids if nm == w), "first_batch_id": batch.id}
                                            for ref, _t, _c, _p, aw, w, bids in part if aw])
        s.commit()
    return {"bulk_load_seconds": round(time.perf_counter() - t0, 1)}


def timed_searches() -> dict:
    out = {}
    cases = {
        "city": TenderFilters(city="ينبع", include_synthetic=True),
        "year": TenderFilters(year=2022, include_synthetic=True),
        "title_text": TenderFilters(q="دهانات", include_synthetic=True),
        "company_name": TenderFilters(company="اصطناعي شركة 42", include_synthetic=True),
        "award_value_range": TenderFilters(min_value=1e6, max_value=2e6, include_synthetic=True),
        "combined": TenderFilters(city="جدة", year=2021, min_value=1e5, include_synthetic=True),
        "deep_page": TenderFilters(include_synthetic=True, page=200),
    }
    with dbm.SessionLocal() as s:
        for name, f in cases.items():
            t0 = time.perf_counter()
            rows, total = search_tenders(s, f)
            out[name] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "total": total, "page_rows": len(rows)}
        cid = s.scalar(select(Company.id).where(Company.display_name == "اصطناعي شركة 42"))
        t0 = time.perf_counter()
        prof = company_profile(s, cid)
        out["company_profile"] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "bids": len(prof["bids"]),
                                  "awards": len(prof["awards"])}
        out["rows"] = {"tenders": s.scalar(select(func.count(ProcurementTender.id))),
                       "bids": s.scalar(select(func.count(TenderBid.id))),
                       "real_tenders_in_this_db": s.scalar(select(func.count(ProcurementTender.id)).where(
                           ProcurementTender.is_synthetic.is_(False)))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenders", type=int, default=10000)
    ap.add_argument("--companies", type=int, default=3000)
    ap.add_argument("--via-importer", action="store_true", help="run the real importer (slower, end-to-end)")
    args = ap.parse_args()
    if not dbm.engine.url.drivername.startswith("postgresql"):
        print("run against a dedicated PostgreSQL database", file=sys.stderr)
        return 2
    upgrade(dbm.engine)
    result = {"synthetic": True, "tenders_requested": args.tenders}
    result["load"] = via_importer(args.tenders, args.companies) if args.via_importer else bulk(args.tenders, args.companies)
    result["search"] = timed_searches()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
