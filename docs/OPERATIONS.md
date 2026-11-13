# التشغيل والاستضافة والأتمتة

## البيئات
| البيئة | قاعدة البيانات | الدفع | البيانات |
|---|---|---|---|
| development | SQLite محلي `data/hub.db` | sandbox | `scripts/seed_demo.py` (أمثلة موسومة) |
| staging | SQLite أو PostgreSQL منفصل | sandbox فقط | أمثلة + مراجعة يدوية تجريبية |
| production | **PostgreSQL** + نسخ احتياطي | Moyasar بعد موافقة المالك | مصادر موثقة الحقوق فقط |

`HUB_ENV=production` يمنع: تشغيل seed_demo، صفحة الدفع التجريبية، وتشغيل التطبيق دون `HUB_SECRET_KEY`.

## متغيرات البيئة (لا تُحفظ في المستودع أبدًا)
| المتغير | الغرض |
|---|---|
| `HUB_ENV` | development / staging / production |
| `HUB_DATABASE_URL` | مثل `postgresql+psycopg://...` (يتطلب إضافة psycopg) |
| `HUB_SECRET_KEY` | سر عشوائي طويل (إلزامي في الإنتاج) |
| `HUB_BASE_URL` | `https://...` — يفعّل الكوكيز الآمنة وHSTS |
| `HUB_PRODUCT_DIR` | مجلد خاص للملفات المدفوعة **خارج أي مسار عام** |
| `HUB_JOBS_TOKEN` | رمز استدعاء `/internal/jobs/refresh` |
| `HUB_PAYMENT_PROVIDER` | `sandbox` أو `moyasar` |
| `HUB_PAYMENTS_LIVE_ENABLED` | `false` افتراضيًا. **لا يُغيَّر إلا بموافقة المالك المكتوبة.** |
| `MOYASAR_SECRET_KEY`, `MOYASAR_PUBLISHABLE_KEY`, `MOYASAR_WEBHOOK_SECRET_TOKEN` | مفاتيح Moyasar (المفتاح الحي `sk_live_` مرفوض ما لم يُفعّل الدفع الحي) |
| `HUB_VAT_REGISTERED`, `HUB_SELLER_VAT_NUMBER`, `HUB_SELLER_NAME` | بيانات البائع والضريبة |
| `HUB_ADMIN_ALERT_EMAIL` | بريد تنبيه توقف المصادر |

## التشغيل
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python products/build_workbooks.py      # يتطلب LibreOffice لإعادة الحساب
HUB_ENV=staging .venv/bin/uvicorn hub.app:app --host 0.0.0.0 --port 8000 --proxy-headers
```
خلف وكيل HTTPS (Caddy/Nginx). يعمل التطبيق بالكامل دون أي خدمة ذكاء اصطناعي أو اشتراك Claude.

## الأتمتة المجدولة
المهمة الوحيدة: `python -m hub.jobs refresh` (أو `POST /internal/jobs/refresh` مع `Authorization: Bearer $HUB_JOBS_TOKEN`). تقوم بـ:
1. تشغيل المصادر المستحقة فقط (`refresh_hours` لكل مصدر) وتجاوز غير الموثق حقوقه («gated»).
2. احترام robots.txt؛ رفض 401/403/451 يُسجَّل ولا يُعاد آليًا.
3. إدخال/تحديث/كشف التكرار، وإرسال المشكوك فيه والمتغير جوهريًا لقائمة المراجعة.
4. تعليم المنتهية `CLOSED` والتي تجاوزت مهلة التحقق `stale` (تختفي من النتائج الحديثة).
5. تنبيهات المستخدمين المطابقين (داخل المنصة لكل الحسابات، وبريد للمشتركين) مرة واحدة لكل فرصة.
6. تنبيه المسؤول عند «متوقف/متأخر/لم ينجح بعد».

**الجدولة المقترحة:** cron على الخادم كل ساعة (المهمة تقرر ما المستحق):
```
7 * * * *  cd /srv/hub && HUB_ENV=production .venv/bin/python -m hub.jobs refresh >> /var/log/hub-refresh.log 2>&1
```
بديل: `.github/workflows/refresh.yml` يستدعي نقطة النهاية المحمية (يتطلب سر `HUB_JOBS_TOKEN` و`HUB_BASE_URL` في GitHub).

**إرسال البريد:** الرسائل تُحفظ في `email_outbox` وتُرسل عبر HTTPS API (Brevo أو Resend) بالمتغيرات `HUB_MAIL_PROVIDER`, `HUB_MAIL_API_KEY`, `HUB_MAIL_FROM`. لا SMTP إطلاقًا (Render Free يحجبه). كل رسالة تُحاول حتى 5 مرات ويُسجَّل آخر خطأ؛ مفتاح خاطئ (401/403) يوقف الدفعة فورًا.

## إضافة مصدر آلي جديد
1. وثّق إذن إعادة الاستخدام (صفحة الشروط أو ترخيص مكتوب) في `rights_evidence` و`rights_checked_at`.
2. اضبط `rights` على `verified_public_reuse` أو `licensed_private`، و`kind` على `json_feed` أو `rss`، و`feed_url` HTTPS.
3. ابدأ بـ `trust=review_all`. لا تنتقل إلى `auto_publish` إلا بعد أسبوعين من دقة المراجعة.

## النسخ الاحتياطي والاستعادة
- SQLite: `python scripts/backup.py backup data/hub.db /secure/backups` يوميًا، واستعادة: `python scripts/backup.py restore <file> data/hub.db` (تحقق سلامة قبل وبعد، والقديم يُحفظ `.before-restore`). مختبر في `tests/test_backup.py`.
- PostgreSQL: `pg_dump -Fc` يومي + اختبار `pg_restore` شهري على staging.
- شفّر النسخ وانقلها خارج الخادم. سجّل نتيجة كل اختبار استعادة.

## قبل أي نشر للإنتاج (بموافقة المالك)
- [ ] مراجعة قانونية: الخصوصية، الشروط، الاسترداد، نماذج العقود.
- [ ] السجل التجاري/وثيقة العمل الحر، توثيق المتجر في المركز السعودي للأعمال، الحساب البنكي.
- [ ] حسم التسجيل في ضريبة القيمة المضافة والفوترة الإلكترونية (مزود معتمد للمرحلة الثانية إن انطبقت).
- [ ] PostgreSQL + Alembic migrations (حاليًا `create_all`).
- [ ] مزود بريد، ومراقبة أخطاء، وسجلات مركزية.
- [ ] اختبار اختراق خارجي.
