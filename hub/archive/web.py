"""Staff-only archive screens. Nothing here is public: records are private until their source licence is verified."""
from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import TRADES, AuditLog
from . import curation
from .curation import CurationError
from .importer import MAX_PACKAGE_BYTES, Importer, PackageError
from .models import (
    EVIDENCE_KINDS,
    IDENTITY,
    LIFECYCLE,
    RIGHTS,
    SUBCONTRACT_REVIEW,
    TENDER_STATUS,
    VISIBILITY,
    Company,
    ImportBatch,
    ImportReject,
    ProcurementSource,
    ProcurementTender,
    SubcontractOpportunity,
    TenderAward,
    TenderBid,
)
from .search import company_profile, export_rows, facets, insights, parse_filters, search_companies, search_tenders
from .sources import ensure_sources

PRIORITY_CITIES = ["ينبع الصناعية", "ينبع", "جدة", "الرياض"]  # owner's priority order
LABELS = dict(TENDER_STATUS=TENDER_STATUS, LIFECYCLE=LIFECYCLE, RIGHTS=RIGHTS, VISIBILITY=VISIBILITY, IDENTITY=IDENTITY,
              TRADES=TRADES, SUBCONTRACT_REVIEW=SUBCONTRACT_REVIEW, EVIDENCE_KINDS=EVIDENCE_KINDS)


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
        # D lives in its own table and counts only after a second person confirmed the evidence
        stats["by_lifecycle"]["D_subcontract_confirmed"] = db.scalar(select(func.count(SubcontractOpportunity.id)).where(
            SubcontractOpportunity.review_status == "confirmed", SubcontractOpportunity.status == "open"))
        batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)).all()
        return render(request, "archive_home.html", db, user, rows=rows, total=total, f=f, facets=facets(db),
                      stats=stats, batches=batches, pages=max(1, -(-total // 50)), **LABELS)

    @app.get("/admin/archive/insights", response_class=HTMLResponse)
    def archive_insights(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        f = parse_filters(request.query_params)
        return render(request, "archive_insights.html", db, user, f=f, facets=facets(db), ins=insights(db, f),
                      priority_cities=PRIORITY_CITIES, **LABELS)

    @app.get("/admin/archive/export.csv")
    def archive_export(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        """Private staff export of the current search. Logged; the file says it must not be republished."""
        f = parse_filters(request.query_params)
        rows = export_rows(db, f)
        buf = io.StringIO()
        buf.write("﻿")  # BOM so Excel reads Arabic as UTF-8
        w = csv.writer(buf)
        w.writerow(["خاص بفريق المنصة — غير مرخص لإعادة النشر. حقوق المصادر غير متحقق منها."])
        w.writerows(rows)
        db.add(AuditLog(actor_id=user.id, action="archive.export_csv", target=f"rows={len(rows) - 1}",
                        detail=str(request.url.query)[:500]))
        db.commit()
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="archive_private_export.csv"', "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex"})

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
        conflict_id = request.query_params.get("conflict", "")
        conflict = db.get(Company, int(conflict_id)) if conflict_id.isdigit() else None
        merged_into = db.get(Company, prof["company"].merged_into_id) if prof["company"].merged_into_id else None
        merged_from = db.scalars(select(Company).where(Company.merged_into_id == company_id)).all()
        return render(request, "archive_company.html", db, user, p=prof, conflict=conflict, merged_into=merged_into,
                      merged_from=merged_from, **LABELS)

    # ---- identity (admin): a CR / unified number with an evidence link; merge only on the same verified number
    def _company_action(company_id: int, db: Session, work, ok_msg: str):
        try:
            work()
        except CurationError as exc:
            db.rollback()
            url = f"/admin/archive/companies/{company_id}"
            if exc.conflict_company_id:
                url += f"?conflict={exc.conflict_company_id}"
            return redirect(url, str(exc))
        db.commit()
        return redirect(f"/admin/archive/companies/{company_id}", ok_msg)

    @app.post("/admin/archive/companies/{company_id}/identity", dependencies=[Depends(verify_csrf)])
    async def archive_company_identity(company_id: int, request: Request, db: Session = Depends(get_db),
                                       user=Depends(admin_only)):
        form = await request.form()
        return _company_action(company_id, db, lambda: curation.verify_identity(
            db, company_id, cr_number=str(form.get("cr_number") or ""), unified_number=str(form.get("unified_number") or ""),
            evidence_url=str(form.get("evidence_url") or ""), by=user.email), "سُجّل رقم الهوية مع الدليل")

    @app.post("/admin/archive/companies/{company_id}/merge", dependencies=[Depends(verify_csrf)])
    async def archive_company_merge(company_id: int, request: Request, db: Session = Depends(get_db),
                                    user=Depends(admin_only)):
        form = await request.form()
        target = str(form.get("target_id") or "")
        if not target.isdigit():
            return redirect(f"/admin/archive/companies/{company_id}", "اختر الكيان الهدف")
        return _company_action(company_id, db, lambda: curation.merge_companies(
            db, company_id, int(target), registry_number=str(form.get("registry_number") or ""),
            evidence_url=str(form.get("evidence_url") or ""), by=user.email), "تم الدمج بنفس رقم السجل (قابل للتراجع)")

    @app.post("/admin/archive/companies/{company_id}/unmerge", dependencies=[Depends(verify_csrf)])
    def archive_company_unmerge(company_id: int, db: Session = Depends(get_db), user=Depends(admin_only)):
        return _company_action(company_id, db, lambda: curation.unmerge_company(db, company_id, by=user.email),
                               "تم التراجع عن الدمج")

    # ---- lifecycle D: subcontract opportunities with evidence, confirmed by a second staff member
    @app.get("/admin/archive/subcontracts", response_class=HTMLResponse)
    def archive_subcontracts(request: Request, review: str = "", db: Session = Depends(get_db), user=Depends(staff)):
        stmt = select(SubcontractOpportunity).options(selectinload(SubcontractOpportunity.main_contractor))
        if review in SUBCONTRACT_REVIEW:
            stmt = stmt.where(SubcontractOpportunity.review_status == review)
        rows = db.scalars(stmt.order_by(SubcontractOpportunity.id.desc()).limit(200)).all()
        counts = dict(db.execute(select(SubcontractOpportunity.review_status, func.count())
                                 .group_by(SubcontractOpportunity.review_status)).all())
        return render(request, "archive_subcontracts.html", db, user, rows=rows, review=review, counts=counts,
                      error=None, form={}, **LABELS)

    @app.post("/admin/archive/subcontracts", dependencies=[Depends(verify_csrf)])
    async def archive_subcontract_add(request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        form = {k: str(v) for k, v in (await request.form()).items() if isinstance(v, str)}

        def num(key):
            v = form.get(key, "").strip()
            return int(v) if v.isdigit() else None
        try:
            curation.add_subcontract(
                db, user_id=user.id, title=form.get("title", ""), main_contractor_name=form.get("main_contractor_name", ""),
                evidence_kind=form.get("evidence_kind", ""), evidence_url=form.get("evidence_url", ""),
                trade=form.get("trade", ""), city=form.get("city", ""), evidence_note=form.get("evidence_note", ""),
                published_date=form.get("published_date", ""), response_deadline=form.get("response_deadline", ""),
                main_contractor_company_id=num("main_contractor_company_id"), related_tender_id=num("related_tender_id"))
        except CurationError as exc:
            db.rollback()
            rows = db.scalars(select(SubcontractOpportunity).order_by(SubcontractOpportunity.id.desc()).limit(200)).all()
            return render(request, "archive_subcontracts.html", db, user, status=400, rows=rows, review="", counts={},
                          error=str(exc), form=form, **LABELS)
        db.commit()
        return redirect("/admin/archive/subcontracts", "أُدخلت الفرصة وهي بانتظار مراجعة شخص ثانٍ")

    @app.post("/admin/archive/subcontracts/{op_id}/review", dependencies=[Depends(verify_csrf)])
    async def archive_subcontract_review(op_id: int, request: Request, db: Session = Depends(get_db), user=Depends(staff)):
        form = await request.form()
        try:
            curation.review_subcontract(db, op_id, user_id=user.id, decision=str(form.get("decision") or ""),
                                        note=str(form.get("note") or ""))
        except CurationError as exc:
            db.rollback()
            return redirect("/admin/archive/subcontracts", str(exc))
        db.commit()
        return redirect("/admin/archive/subcontracts", "سُجّلت المراجعة")

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
