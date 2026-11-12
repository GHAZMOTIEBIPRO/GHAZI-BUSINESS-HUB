# غازي للأعمال — GHAZI Business Hub

منصة سعودية عربية (RTL) لمقاولي التشطيبات والصيانة والترميم: **رادار فرص** من مصادر موثقة الحقوق + **متجر أدوات Excel** مختبرة + **مساحة عمل** تربط الفرصة بعرض السعر والمتابعة.

> مشروع مستقل تمامًا عن GHAZIBOT (الأسهم). لا يشاركه كودًا ولا بيانات.
> الحالة: **نسخة عملية P0+P1 على staging محلي — غير منشورة، والمدفوعات Sandbox فقط.** التفاصيل في [`docs/STATUS.md`](docs/STATUS.md).

## البنية
```
hub/            التطبيق (FastAPI + Jinja2 RTL، بلا إطار JS، CSP صارم 'self')
  app.py        المسارات وحراسة الملكية (IDOR) والصلاحيات
  radar.py      بوابة الحقوق، الجلب، التطبيع، كشف التكرار، المراجعة، الصلاحية، المطابقة
  jobs.py       المهمة المجدولة: تحديث + تقادم + تنبيهات + صحة المصادر
  store.py      الطلبات، الدفع (sandbox / Moyasar)، الاستحقاقات، روابط تنزيل لمرة واحدة
  quotes.py     من الفرصة إلى مسودة عرض سعر و Excel
  security.py   scrypt، جلسات، CSRF، تحديد معدل الطلبات
products/       build_workbooks.py يولّد 7 أدوات + عينات، ويعيد حسابها بـ LibreOffice
tests/          pytest (81 اختبارًا) + tests/ui/ux_tasks.cjs (Playwright 390/1440)
scripts/        backup.py (نسخ/استعادة مختبرة)، seed_demo.py (بيانات أمثلة لـ staging فقط)
docs/           STATUS, OPERATIONS, COMPETITORS, THIRD_PARTY
original/       النموذج الأولي المسلَّم كما هو (مرجع ومصدر)
```

## التشغيل السريع (staging محلي)
```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python products/build_workbooks.py          # يتطلب libreoffice
HUB_ENV=staging .venv/bin/python scripts/seed_demo.py
HUB_ENV=staging .venv/bin/uvicorn hub.app:app --port 8000
# http://127.0.0.1:8000  — admin@demo.example.sa / contractor@demo.example.sa  كلمة المرور: demo-password-123
```

## التحقق (ما يشغّله CI)
```bash
ruff check .
pytest -q
node tests/ui/ux_tasks.cjs http://127.0.0.1:8000 ui-shots   # يتطلب playwright وخادمًا يعمل ببيانات seed_demo
```

## قواعد ثابتة
- لا جلب آلي لأي مصدر قبل توثيق حق إعادة الاستخدام واحترام robots.txt؛ الرفض 401/403/451 لا يُعاد.
- «طلب باطن مؤكد» يتطلب دليلًا واعتمادًا بشريًا دائمًا.
- لا أسماء أو أرقام مخترعة؛ الحقل غير المؤكد يظهر «غير معلوم».
- الملفات المدفوعة في مجلد خاص، تُسلَّم فقط عبر رمز لمرة واحدة صالح 5 دقائق ومرتبط بالمستخدم.
- لا دفع حقيقي دون `HUB_PAYMENTS_LIVE_ENABLED=true` وموافقة المالك.
