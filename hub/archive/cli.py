"""Archive import command.

    python -m hub.archive.cli import PACKAGE --dry-run      read + validate, no database at all (default)
    python -m hub.archive.cli import PACKAGE --validate     also compare with what the archive already holds (read-only)
    python -m hub.archive.cli import PACKAGE --staging      write to the private staging area (chunked, resumable)
    python -m hub.archive.cli import PACKAGE --resume ID    continue an interrupted staging batch
    python -m hub.archive.cli commit ID                     move a staged batch into the private archive
    python -m hub.archive.cli rollback ID                   undo everything a batch wrote
    python -m hub.archive.cli migrate                       apply archive migrations (alembic upgrade head)

The database is HUB_DATABASE_URL / DATABASE_URL. The URL is never printed. Nothing becomes public:
records stay private until a licence for their source is verified (see docs/ARCHIVE.md).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .importer import Importer, PackageError


def _engine():
    from .. import db
    return db.engine, db.SessionLocal


def _write_report(report: dict, out: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        Path(out).with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    summary = {k: report.get(k) for k in ("mode", "batch_id", "read", "accepted", "rejected_count", "warnings_count",
                                          "duplicates_in_file", "status_counts", "awards_published", "batch_counts")
               if k in report}
    db = report.get("database") or {}
    for key in ("archive_totals", "tenders_already_in_archive", "tenders_would_be_new", "previous_batches_of_this_package"):
        if key in db:
            summary[key] = db[key]
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def render_markdown(r: dict) -> str:
    lines = [f"# تقرير استيراد الأرشيف — {r.get('mode')}", "", f"- البصمة: `{r.get('package_sha256')}`",
             f"- وقت التوليد (UTC): {r.get('generated_at')}", ""]
    if r.get("batch_id"):
        lines.append(f"- رقم الدفعة: {r['batch_id']}")
    lines += ["", "## الأعداد", "| البند | المقروء | المقبول |", "|---|---|---|"]
    for k in ("tenders", "bids"):
        lines.append(f"| {k} | {r['read'][k]} | {r['accepted'][k]} |")
    lines.append(f"| companies (أسماء) | {r['read']['companies']} | — |")
    lines += ["", f"- المرفوض: **{r['rejected_count']}** · التحذيرات: {r['warnings_count']}",
              f"- المكرر داخل الملف: {r['duplicates_in_file'] or 0}", f"- الحقول الناقصة: {r['missing_fields']}",
              f"- الحالات: {r['status_counts']} · التصنيف: {r['lifecycle_counts']}",
              f"- ترسيات منشورة بأسماء فائزين: {r['awards_published']}", "",
              "## المتقدمون: المعروف مقابل المعلن",
              f"- صفوف العروض المعروفة: {r['bidders']['known_bid_rows']}",
              f"- منافسات معلن فيها عدد العروض: {r['bidders']['tenders_with_claimed_count']} (إجمالي العروض المعلنة "
              f"{r['bidders']['claimed_offers_total']}، المعروف منها بالاسم {r['bidders']['known_bids_in_those_tenders']})",
              f"- منافسات قائمتها العلنية مكتملة: {r['bidders']['tenders_with_complete_public_list']}", "",
              "## الشركات", f"- أسماء مميزة مرتبطة بعروض/ترسيات: {r['companies']['distinct_names_linked_in_package']}",
              f"- صفوف ملف الشركات: {r['companies']['companies_csv_rows']}", f"- الهوية: {r['companies']['identity']}"]
    if r.get("rejected"):
        lines += ["", "## المرفوض", "| الملف | الصف | السبب |", "|---|---|---|"]
        lines += [f"| {x['file']} | {x['row']} | {x['reason']} |" for x in r["rejected"]]
    if r.get("warnings"):
        lines += ["", "## تحذيرات (السجل مقبول مع ملاحظة)", "| الملف | الصف | الملاحظة |", "|---|---|---|"]
        lines += [f"| {x['file']} | {x['row']} | {x['reason']} |" for x in r["warnings"]]
    db = r.get("database")
    if db and db.get("per_tender"):
        lines += ["", "## كل منافسة والشركات المرتبطة", "| الرقم | الحالة | الجهة | الفائز | المعروف/المعلن | الشركات |", "|---|---|---|---|---|---|"]
        for t in db["per_tender"]:
            lines.append(f"| {t['ref']} | {t['status']} | {t['issuer'] or '—'} | {'، '.join(t['winner']) or '—'} | "
                         f"{t['bidders_known']}/{t['offer_count_claimed'] if t['offer_count_claimed'] is not None else '؟'} | "
                         f"{'، '.join(t['companies']) or '—'} |")
        lines += ["", f"- مجموعات أسماء متشابهة (للمراجعة فقط، لم تُدمج): {len(db['possible_same_entity_groups'])}",
                  f"- إجماليات الأرشيف: {db['archive_totals']}"]
    lines += ["", f"**الحقوق:** {r['rights']['note']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hub.archive.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("package")
    mode = imp.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--staging", action="store_true")
    mode.add_argument("--resume", type=int, metavar="BATCH_ID")
    imp.add_argument("--chunk-size", type=int, default=200)
    imp.add_argument("--report")
    for name in ("commit", "rollback"):
        sp = sub.add_parser(name)
        sp.add_argument("batch_id", type=int)
    sub.add_parser("migrate")
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        from .migrate import upgrade
        engine, _ = _engine()
        upgrade(engine)
        print("archive migrations: at head")
        return 0
    engine, sf = _engine()
    importer = Importer(sf, chunk_size=getattr(args, "chunk_size", 200))
    try:
        if args.cmd == "commit":
            print(json.dumps(importer.commit(args.batch_id), ensure_ascii=False))
            return 0
        if args.cmd == "rollback":
            print(json.dumps(importer.rollback(args.batch_id), ensure_ascii=False))
            return 0
        if args.staging or args.resume:
            from .migrate import upgrade
            upgrade(engine)
            report = importer.stage(args.package, resume_batch_id=args.resume)
        elif args.validate:
            from .migrate import upgrade
            upgrade(engine)
            report = importer.validate_against_db(args.package)
        else:
            report = importer.dry_run(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_report(report, args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
