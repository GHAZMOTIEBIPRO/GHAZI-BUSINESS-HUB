"""Staff-only archive screens. Nothing here is public: records are private until their source licence is verified."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import TRADES
from .importer import MAX_PACKAGE_BYTES, Importer, PackageError
from .models import (
    IDENTITY,
    LIFECYCLE,
    RIGHTS,
    TENDER_STATUS,
    VISIBILITY,
    Company,
    ImportBatch,
    ImportReject,
    ProcurementSource,
    ProcurementTender,
    TenderAward,
    TenderBid,
)
from .search import company_profile, facets, parse_filters, search_companies, search_tenders
from .sources import ensure_sources

LABELS = dict(TENDER_STATUS=TENDER_STATUS, LIFECYCLE=LIFECYCLE, RIGHTS=RIGHTS, VISIBILITY=VISIBILITY, IDENTITY=IDENTITY,
              TRADES=TRADES)


def register(app, *, render, staff, admin_only, verify_csrf, get_db, redirect):
    @app.get("/admin/archive", response_class=HTMLResponse)
    def archive_home(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        f = parse_filters(request.query_params)
        rows, total = search_tenders(db, f)
        stats = {
            "tenders": db.scalar(select(func.count(ProcurementTender.id)).where(ProcurementTender.is_synthetic.is_(False))),
            "bids": db.scalar(select(func.count(TenderBid.id))),
            "awards": db.scalar(select(func.count(TenderAward.id))),
            "companies": db.scalar(select(func.count(Company.id))),
            "public": db.scalar(select(func.count(ProcurementTender.id)).where(ProcurementTender.publish_allowed.is_(True))),
            "by_lifecycle": dict(db.execute(select(ProcurementTender.lifecycle, func.count()).where(
                ProcurementTender.is_synthetic.is_(False)).group_by(ProcurementTender.lifecycle)).all()),
        }
        batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)).all()
        return render(request, "archive_home.html", db, user, rows=rows, total=total, f=f, facets=facets(db),
                      stats=stats, batches=batches, pages=max(1, -(-total // 50)), **LABELS)

    @app.get("/admin/archive/tenders/{tender_id}", response_class=HTMLResponse)
    def archive_tender(tender_id: int, request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        t = db.scalars(select(ProcurementTender).where(ProcurementTender.id == tender_id).options(
            selectinload(ProcurementTender.bids).selectinload(TenderBid.company),
            selectinload(ProcurementTender.awards).selectinload(TenderAward.company))).first()
        if t is None:
            raise HTTPException(404)
        bids = sorted(t.bids, key=lambda b: (b.price_rank_among_published is None, b.price_rank_among_published or 0))
        return render(request, "archive_tender.html", db, user, t=t, bids=bids, **LABELS)

    @app.get("/admin/archive/companies", response_class=HTMLResponse)
    def archive_companies(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(staff)):
        return render(request, "archive_companies.html", db, user, q=q[:120], rows=search_companies(db, q), **LABELS)

    @app.get("/admin/archive/companies/{company_id}", response_class=HTMLResponse)
    def archive_company(company_id: int, request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        prof = company_profile(db, company_id)
        if prof is None:
            raise HTTPException(404)
        return render(request, "archive_company.html", db, user, p=prof, **LABELS)

    @app.get("/admin/archive/sources", response_class=HTMLResponse)
    def archive_sources(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        ensure_sources(db)
        db.commit()
        sources = db.scalars(select(ProcurementSource).options(selectinload(ProcurementSource.licenses))
                             .order_by(ProcurementSource.automation_enabled.desc(), ProcurementSource.id)).all()
        return render(request, "archive_sources.html", db, user, sources=sources, **LABELS)

    @app.get("/admin/archive/import", response_class=HTMLResponse)
    def archive_import_form(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
        return render(request, "archive_import.html", db, user, report=None, error=None, **LABELS)

    @app.post("/admin/archive/import", dependencies=[Depends(verify_csrf)])
    async def archive_import(request: Request, db: Session = Depends(get_db), user=Depends(admin_only)):
        form = await request.form()
        upload = form.get("file")
        mode = form.get("mode") or "dry-run"

        def fail(msg, status=400):
            return render(request, "archive_import.html", db, user, status=status, report=None, error=msg, **LABELS)
        if upload is None or not hasattr(upload, "read"):
            return fail("أرفق ملف الحزمة (ZIP)")
        raw = await upload.read(MAX_PACKAGE_BYTES + 1)
        if len(raw) > MAX_PACKAGE_BYTES:
            return fail("الحزمة أكبر من 25 ميجابايت")
        from .. import db as dbm
        importer = Importer(dbm.SessionLocal, created_by=f"web:{user.email}")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.zip"
            path.write_bytes(raw)
            try:
                if mode == "staging":
                    report = importer.stage(path)
                elif mode == "validate":
                    report = importer.validate_against_db(path)
                else:
                    report = importer.dry_run(path)
            except PackageError as exc:
                return fail(str(exc))
        return render(request, "archive_import.html", db, user, report=report, error=None, **LABELS)

    @app.get("/admin/archive/batches/{batch_id}", response_class=HTMLResponse)
    def archive_batch(batch_id: int, request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        b = db.get(ImportBatch, batch_id)
        if b is None:
            raise HTTPException(404)
        rejects = db.scalars(select(ImportReject).where(ImportReject.batch_id == batch_id).order_by(ImportReject.id)).all()
        return render(request, "archive_batch.html", db, user, b=b, rejects=rejects, **LABELS)

    @app.post("/admin/archive/batches/{batch_id}/{action}", dependencies=[Depends(verify_csrf)])
    def archive_batch_action(batch_id: int, action: str, request: Request, db: Session = Depends(get_db),
                             user=Depends(admin_only)):
        if action not in ("commit", "rollback"):
            raise HTTPException(404)
        from .. import db as dbm
        importer = Importer(dbm.SessionLocal, created_by=f"web:{user.email}")
        try:
            getattr(importer, action)(batch_id)
        except PackageError as exc:
            return redirect(f"/admin/archive/batches/{batch_id}", str(exc))
        return redirect(f"/admin/archive/batches/{batch_id}", "تم الاعتماد في الأرشيف الخاص" if action == "commit" else "تم التراجع")
