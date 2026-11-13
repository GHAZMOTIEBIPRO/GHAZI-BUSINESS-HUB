# أرشيف المناقصات والترسيات (Procurement Archive)

كل ما في الأرشيف **خاص** (`private_staging` ثم `archive_private`) حتى يُتحقق من حقوق إعادة الاستخدام لمصدره.
لا يوجد في هذا الإصدار أي مسار يجعل سجلًا عامًا. المصادر وحقوقها: `docs/SOURCES_MATRIX.md`.

## القواعد المطبقة في الكود
- **لا فائز من أقل عرض.** الترسية تُنشأ فقط إذا كانت حالة المنافسة «تمت الترسية» **و** اسم الفائز منشور.
  ترتيب الأسعار (`price_rank_among_published`) معلومة وصفية عن العروض الظاهرة فقط.
- **لا أسماء متقدمين مخترعة.** يُخزَّن الاسم كما نُشر (`bidder_name_raw`)، و`bid_list_complete` صحيح فقط إذا
  طابق عدد الأسماء المنشورة عدد العروض المعلن.
- **لا دمج بالاسم.** الهوية بـ`name_key` (تطبيع آمن: NFKC، المسافات، التطويل، التشكيل). `fold_key`
  (ة/ه، أ/ا، ى/ي) يُستخدم **للتنبيه** فقط («أسماء متشابهة قد لا تكون كيانًا واحدًا»). كل شركة
  `name_only_unverified` حتى يُسجَّل رقم سجل تجاري أو رقم موحد.
- **دورة الحياة منفصلة:** `A_open` / `B_closed` / `C_awarded` / `D_subcontract_confirmed`.
  الفئة D لا تُشتق من أي ترسية؛ تدخل فقط بدليل ومراجعة بشرية.
- **الاصطناعي لا يختلط بالحقيقي:** `is_synthetic=true`، مرجع `SYN-`، أسماء تبدأ بـ«اصطناعي»، ومستبعد من البحث افتراضيًا.

## الجداول (Alembic `migrations/versions/0001_procurement_archive.py`)
`procurement_sources`, `source_licenses`, `import_batches`, `import_rejects`, `procurement_issuers`, `companies`,
`company_aliases`, `procurement_tenders`, `tender_bids`, `tender_awards`, `verification_events`, `change_history`.
جدول الإصدارات منفصل: `alembic_version_archive`. التطبيق يشغّل `upgrade head` عند الإقلاع.

## أمر الاستيراد
```
python -m hub.archive.cli import PACKAGE.zip --dry-run          # قراءة وتحقق فقط، بلا قاعدة
python -m hub.archive.cli import PACKAGE.zip --validate         # مقارنة بما في الأرشيف (قراءة فقط)
python -m hub.archive.cli import PACKAGE.zip --staging --report out.json   # كتابة مجزأة قابلة للاستئناف
python -m hub.archive.cli import PACKAGE.zip --resume BATCH_ID  # استئناف دفعة انقطعت (يرفض ملفًا مختلفًا)
python -m hub.archive.cli commit BATCH_ID                       # نقل إلى الأرشيف الخاص (يبقى غير عام)
python -m hub.archive.cli rollback BATCH_ID                     # حذف ما أنشأته الدفعة واسترجاع القيم المعدلة
python -m hub.archive.cli migrate                               # alembic upgrade head
```
- كل جزء (chunk) يُكتب مع نقطة الاستئناف في معاملة واحدة.
- إعادة تشغيل الحزمة نفسها لا تكرر شيئًا: المفتاح `official_reference`، وأي تعديل يُسجَّل في `change_history`.
- لا تُمسح قيمة معروفة بقيمة فارغة.
- يمكن الرفع أيضًا من `/admin/archive/import` (مدير فقط + CSRF).

## نتائج الحزمة الحقيقية 2026-11-13 (PostgreSQL 16 محلي، ليست قاعدة الإنتاج)
| | العدد |
|---|---|
| منافسات | 38 (8 مفتوحة A، 30 مرساة C) |
| عروض ظاهرة | 138 |
| ترسيات بفائز منشور | 30 |
| شركات (أسماء) | 121، كلها `name_only_unverified` |
| مرفوض / تحذيرات | 0 / 0 |
| سجلات عامة | 0 |
| إعادة التشغيل | 0 جديد، 0 محدث، 38 منافسة و138 عرضًا دون تغيير |
| rollback | حذف 38/138/30/121 وعاد العدد إلى 0 |
| commit | 38 إلى `archive_private`، المرئي للعامة 0 |

## الأداء (بيانات اصطناعية فقط، قاعدة منفصلة)
| | 10 آلاف عبر المستورد الحقيقي | 110 آلاف (تحميل جماعي) |
|---|---|---|
| زمن التحميل | 208.7 ث (47.9 منافسة/ث) | 57.2 ث |
| بحث بالمدينة / السنة | 19 / ~25 مللي ث | 28 / 70 مللي ث |
| بحث نص العنوان | 24 مللي ث | 176 مللي ث |
| بحث باسم الشركة | 22 مللي ث | 30 مللي ث |
| مدى قيمة الترسية / مركب | 13 / 17 مللي ث | 62 / 38 مللي ث |
| الصفحة 200 | 37 مللي ث | 66 مللي ث |
| ملف الشركة | 18 مللي ث | 26 مللي ث |

إعادة التشغيل: `HUB_DATABASE_URL=... python scripts/archive_perf.py --tenders 10000 --via-importer`.
البحث النصي فوق 100 ألف قد يحتاج لاحقًا فهرس `pg_trgm`، وهذا قرار تشغيلي لم يُتخذ بعد.

## قبل أي استيراد إلى قاعدة الإنتاج (Neon)
1. دمج PR #1 ثم هذا الفرع بعد موافقة المالك.
2. نسخة احتياطية: `python scripts/backup.py` (أو Neon branch)، ثم **اختبار استعادة** على قاعدة منفصلة.
3. `python -m hub.archive.cli migrate`. التطبيق يشغّله عند الإقلاع أيضًا.
4. `--validate` ثم `--staging`، ثم مراجعة التقرير (`/admin/archive/batches/ID`). `commit` بعد المراجعة فقط.
5. التراجع: `rollback BATCH_ID`؛ وللمخطط: `alembic downgrade base`، وهو يحذف جداول الأرشيف فقط.

الإعداد على Render: **Dashboard → خدمة ghazi-business-hub → Environment → `HUB_DATABASE_URL`** (رابط Neon المُجمَّع `-pooler` مع `sslmode=require`)
ويُدخله المالك هناك ولا يُلصق في المحادثة. ثم يُفحص `/healthz` حتى يعرض `"db":"postgresql"`.
