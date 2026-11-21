"""Counts only, after a proof import on a throw-away database: what the archive now holds and how it is organised.

No names, references or amounts per record are printed (the workflow log is public); only totals and distributions.

    HUB_DATABASE_URL=... python scripts/archive_proof_counts.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from hub import db as dbm  # noqa: E402
from hub.archive.models import Company, ProcurementIssuer, ProcurementTender, TenderAward, VerificationEvent  # noqa: E402


def main() -> int:
    with dbm.SessionLocal() as s:
        tenders = s.scalars(select(ProcurementTender)).all()
        awards = s.scalars(select(TenderAward)).all()
        out = {
            "tenders": len(tenders),
            "awards": len(awards),
            "awards_official_open_data": sum(a.verification_status == "official_open_data" for a in awards),
            "awards_by_year": dict(sorted(Counter(a.award_date.year for a in awards if a.award_date).items())),
            "award_value_total_sar": round(sum(a.award_amount_sar or 0 for a in awards)),
            "issuers": s.scalar(select(func.count(ProcurementIssuer.id))),
            "companies": s.scalar(select(func.count(Company.id))),
            "companies_cr_verified": s.scalar(select(func.count(Company.id)).where(
                Company.identity_status == "registry_verified")),
            "same_cr_different_name_for_review": s.scalar(select(func.count(VerificationEvent.id)).where(
                VerificationEvent.event == "same_cr_as_company")),
            "tenders_by_target_trade": dict(Counter(w for t in tenders for w in (t.work_types or []))),
            "publicly_visible": sum(t.publish_allowed for t in tenders),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
