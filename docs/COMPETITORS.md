# بحث المنافسين والسوق — 2026-11-12

**تنبيه منهجي:** هذا بحث مكتبي أولي من نتائج بحث الويب ومواقع الموردين. ما يرد هنا «كما يعلن المصدر» وليس تحققًا مستقلًا. الأسعار غير المنشورة رسميًا مذكورة بوصفها كذلك. لم تُجرَ مقابلات مع عملاء بعد (مطلوبة في P0 — Validate: 20 مقابلة).

## 1. القنوات الرسمية (ليست منافسًا مباشرًا بل مصدرًا)
| الجهة | ما يعلنه | ما يعنيه لنا |
|---|---|---|
| **اعتماد** (monafasat.etimad.sa) | البوابة الحكومية للمنافسات. بعض كراسات الشروط برسوم (مثال منشور: 200 ر.س لوثيقة منافسة). | مصدر أساسي للمنافسات المفتوحة، لكن **شروط إعادة الاستخدام الآلي غير موثقة** → نربط بالرابط فقط ويُدخل المراجع ملخصًا. |
| **منصة مقاول — المشاريع** (الهيئة السعودية للمقاولين) | أصحاب المشاريع يطرحون مشاريعهم للحصول على عروض من مقاولين مؤهلين، والدخول عبر «نفاذ». | المنافس الأقرب في «ربط صاحب المشروع بالمقاول»، لكنه لا يركز على **مقاولي الباطن للتشطيبات** ولا يقدم أدوات تسعير/مستخلصات. |
| **منصة مقاول — التحالف** | تحالفات بين المقاولين (مشاريع مشتركة، اتحادات). | ليست سوق باطن؛ نذكرها مصدرًا للبحث فقط. |

## 2. قواعد بيانات المناقصات المدفوعة
| المنتج | ما وجدناه | نقطة الضعف بالنسبة لمقاول التشطيبات |
|---|---|---|
| **Blackridge Research** | قواعد بيانات مناقصات وترسيات للسعودية حسب القطاع؛ صفحات الأسعار تعرض باقات فردية/فريق/أعمال بأرقام غير متسقة العملة. | إنجليزية، موجهة للمشاريع الكبرى وEPC، لا تربط بأدوات تسعير، ولا تميز «باطن مؤكد». |
| **Bidsfactory** | يعيد عرض منافسات اعتماد. | يعيد نشر المحتوى؛ لا يتضح أساس حقوق إعادة النشر. لا نتبع هذا النموذج. |

## 3. برامج إدارة مشاريع المقاولات
| المنتج | ما يعلنه | الفجوة |
|---|---|---|
| **Procore** | توسع في الشرق الأوسط مع تركيز على الإمارات والسعودية. | مكلف ومعقد لمؤسسة تشطيبات صغيرة؛ لا يجلب فرصًا. |
| **Archdesk** | صفحات سعودية: تقدير، جدولة، ضبط تكاليف، فوترة، مستندات. | ERP للشركات المتوسطة/الكبيرة؛ لا رادار فرص. |
| **DANAOS ProjectVIEW / Odoo / Mutflex** | ERP بالعربية، بعضها يعلن توافق فوترة زاتكا المرحلة الثانية؛ تطبيق Odoo يستغرق 3–6 أشهر حسب مدونة مورد. | زمن تطبيق وتكلفة عاليان؛ لا تحل مشكلة «أين الفرصة القادمة». |
| **PlanRadar** | منصة جودة/موقع تستخدمها شركات كبرى في السعودية. | ميدانية، لا تسعير ولا فرص. |

## 4. قوالب Excel
- معظم القوالب المتاحة إنجليزية وعامة (ProjectManager, ClickUp, LetsBuild)، وبعضها **لا يجمع الإجماليات تلقائيًا** كما يذكر أحد المصادر. قالب مدفوع واحد ظهر بنحو 24.5 دولار.
- لم نجد في هذا البحث متجرًا عربيًا يقدم **مستخلصات بمحتجزات واسترداد دفعة مقدمة** مع اختبارات معادلات منشورة. (عدم العثور ≠ عدم الوجود؛ يلزم بحث أعمق.)

## 5. بوابة الدفع
- **Moyasar:** لا تنشر جدول رسوم رسميًا (تطلب التواصل مع المبيعات). مواقع مقارنة غير رسمية تذكر نحو 2.5% (مدى) و2.9% (فيزا/ماستر) ورسوم نزاع 100 ر.س — **غير مؤكد**. وثائق Moyasar تذكر `secret_token` في Webhook يحدده التاجر — منفّذ لدينا مع إعادة قراءة الدفع من API.
- الاسترداد حسب الأسئلة الشائعة لديهم: مدى 24 ساعة–3 أيام عمل، البطاقات 7–14 يوم عمل.

## 6. أين نتميز (فرضيات قابلة للاختبار، لا نتائج)
1. **تخصص ضيق:** تشطيبات وصيانة وترميم، بالعربية، لمؤسسات صغيرة ومتوسطة — بينما المنافسون يخدمون المشاريع الكبرى أو ERP عام.
2. **صدق التصنيف:** فصل صريح بين منافسة مفتوحة / مشروع مُرسى / باطن مؤكد بدليل / مؤشر غير مؤكد، مع «غير معلوم» بدل التخمين.
3. **من الفرصة إلى العرض:** حفظ الفرصة ← مسودة عرض سعر بالبنود المناسبة ← Excel مطبوع ← سجل تواصل ← مستخلصات بعد الترسية. لم نجد منافسًا يربط هذه الحلقة لمقاول الباطن الصغير.
4. **أدوات مختبرة آليًا** ومحمية ومطبوعة، بعينات قبل الشراء، وتعمل دون اشتراك.
5. **احترام الحقوق:** لا كشط لمواقع مغلقة؛ بوابة للشركات الرئيسية لنشر طلبات الباطن مباشرة (P3) تجعلنا مصدرًا أصليًا لا معيد نشر.

## 7. أسئلة مفتوحة يجب حسمها قبل التسعير
- هل يدفع مقاول التشطيبات 199 ر.س شهريًا مقابل جهات تواصل موثقة؟ (اختبار مع 10 عملاء تجريبيين).
- كم فرصة باطن «مؤكدة بدليل» يمكن توثيقها أسبوعيًا يدويًا؟ (هذا يحدد قيمة الاشتراك فعليًا).
- هل ستمنح الهيئة السعودية للمقاولين أو شركات رئيسية خلاصة/إذن رسمي؟

## المصادر
- [Blackridge Research — Saudi tenders database](https://www.blackridgeresearch.com/new-projects-near-me/self-storage-database/tender-rfp-rfq-bid/saudi-arabia/subscribe)
- [Bidsfactory — Etimad tender mirror example](https://bidsfactory.com/br/tenders/2026-saudi_etimad-00b10c10e002fc6c)
- [الاقتصادية — منصة مقاول](https://www.aleqt.com/2023/05/24/article_2553316.html) · [عاجل — الهيئة السعودية للمقاولين](https://ajel.sa/local/l9byyv4n9u) · [عكاظ](https://www.okaz.com.sa/economy/saudi/2204766)
- [Procore MENA expansion — PBC Today](https://www.pbctoday.co.uk/news/digital-construction/construction-technology-news/procore-middle-east/95798/)
- [Archdesk Saudi](https://archdesk.com/en-sa/solutions/erp) · [ProjectVIEW ERP](https://technicalreviewmiddleeast.com/construction/projectview-erp-leading-construction-erp-system-in-saudi-arabia) · [Odoo construction KSA](https://softhealer.com/blog/articals-11/how-odoo-erp-helps-construction-companies-in-saudi-arabia-save-money-13748) · [Mutflex](https://www.goodfirms.co/software/mutflex) · [PlanRadar KSA](https://zawya.com/press-release/companies-news/shapoorji-pallonji-advances-construction-excellence-quality-in-saudi-arabia-with-planradars-digital-platform-wu8d7rnk)
- [ClickUp contractor templates (AR)](https://clickup.com/ar/blog/180937/contractor-estimate-templates/) · [ProjectManager templates](https://www.projectmanager.com/templates/construction-quote-template) · [LetsBuild](https://letsbuild.com/blog/construction-estimating-spreadsheet) · [Payhip example](https://payhip.com/b/SMgsV)
- [Moyasar FAQ](https://www.moyasar.com/en/resources/faqs) · [Moyasar webhook reference](https://docs.moyasar.com/api/other/webhooks/webhook-reference) · [paymentproviders.io (unofficial)](https://paymentproviders.io/compare/moyasar-vs-a-pay?focus=fees)
