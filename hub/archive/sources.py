"""Source registry: what each procurement source is, how it may be accessed, and what we could verify.

Facts below are from research on 2026-11-13 (see docs/SOURCES_MATRIX.md for evidence links). Nothing here
turns automation on: `automation_enabled` stays False until a licence is recorded as verified *and* a
connector has proven a real run. "unknown" means we could not verify it — it is never read as "allowed".
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ProcurementSource, SourceLicense

REGISTRY: list[dict] = [
    dict(key="etimad", name="منصة اعتماد — المنافسات الحكومية ونتائج الترسية", owner="وزارة المالية",
         url="https://tenders.etimad.sa/", access_method="link_only", official_api="coming_soon",
         export_available="no", commercial_reuse="unknown", requires_subscription="yes",
         record_types="منافسات، مواعيد، ترسيات، أسماء متقدمين في بعض المنافسات", years_covered="2018–الآن (بحسب المنصة)",
         evidence_url="https://apiportal.etimad.sa/ar/api_products/TendersInquiryService",
         notes="خدمة «الاستعلام عن بيانات المنافسات» في بوابة مطوري اعتماد معروضة بعبارة «قريبًا» وتتطلب حسابًا "
               "واشتراكًا. لا تصدير جماعي رسمي. الإجراء: طلب اشتراك/اتفاقية من المالك عند إتاحة الخدمة."),
    dict(key="saudi_open_data", name="المنصة الوطنية للبيانات المفتوحة", owner="هيئة البيانات والذكاء الاصطناعي (سدايا)",
         url="https://od.data.gov.sa/", access_method="licensed_upload", official_api="unknown", export_available="yes",
         commercial_reuse="unknown", requires_subscription="no",
         record_types="مجموعات بيانات منشورة من الجهات (منها: منافسات وعقود وزارة الصحة 2020–2023، XLSX)",
         years_covered="بحسب كل مجموعة", rights_status="unverified",
         evidence_url="https://od.data.gov.sa/Data/en/dataset/moh-tenders-and-contracts-2020-2023",
         notes="رخصة البيانات المفتوحة تسمح بإعادة الاستخدام مع ذكر المصدر وإرفاق الرخصة (بحسب صفحات سياسات الجهات)؛ "
               "نص الرخصة v2.0 لم يُقرأ مباشرة. الموقع يقطع الاتصال من الخوادم السحابية خارج المملكة (هذه البيئة و"
               "GitHub run 43408255334). المسار المتاح: تنزيل المالك للملف يدويًا ثم رفعه من صفحة الأرشيف الخاصة."),
    dict(key="rcjy", name="الهيئة الملكية للجبيل وينبع — المنافسات", owner="الهيئة الملكية للجبيل وينبع",
         url="https://www.rcjy.gov.sa/", access_method="link_only", official_api="unknown", export_available="unknown",
         commercial_reuse="unknown", requires_subscription="unknown", record_types="منافسات الهيئة (أولوية ينبع الصناعية)",
         notes="لم يُعثر على مجموعة بيانات مفتوحة للهيئة. إدخال يدوي مراجَع بالرابط حتى يثبت ترخيص."),
    dict(key="muqawil", name="الهيئة السعودية للمقاولين — منصة مقاول", owner="الهيئة السعودية للمقاولين",
         url="https://muqawil.org/", access_method="link_only", official_api="unknown", export_available="unknown",
         commercial_reuse="unknown", requires_subscription="unknown", record_types="بيانات المقاولين وتصنيفاتهم، فرص",
         evidence_url="https://vote.muqawil.org/public/consulting/terms_conditions/terms_conditions.pdf",
         notes="الاستخدام يخضع لشروط المنصة. مفيد للتحقق من هوية المقاول يدويًا، لا جلب آلي."),
    dict(key="monafasati", name="منافساتي", owner="ناشر خاص", url="https://monafasati.co/", access_method="link_only",
         commercial_reuse="unknown", requires_subscription="unknown",
         record_types="ملخصات منافسات وترسيات وأسماء متقدمين ظاهرة",
         notes="مصدر 30 منافسة في حزمة 2026-11-13. شروط إعادة الاستخدام غير متحقق منها: لا كشط، لا نشر. الإجراء: طلب ترخيص."),
    dict(key="mnaqasat", name="منصة المناقصات (daall)", owner="ناشر خاص", url="https://mnaqasat.daall.sa/",
         access_method="link_only", commercial_reuse="unknown", requires_subscription="unknown",
         record_types="ملخصات منافسات وترسيات وأسماء متقدمين ظاهرة",
         notes="مصدر 8 منافسات في حزمة 2026-11-13. الموقع غير قابل للوصول من هذه البيئة؛ الشروط غير متحقق منها. الإجراء: طلب ترخيص."),
    dict(key="scavo", name="SCAVO", owner="ناشر خاص", url="", access_method="link_only",
         commercial_reuse="requires_agreement", requires_subscription="unknown", record_types="بيانات مشاريع ومناقصات (تجاري)",
         notes="لم يُتحقق من الرابط الرسمي ولا الشروط في هذه الجلسة. ناشر خاص: أي استيراد يتطلب اتفاقية مكتوبة تسمح بالتخزين والعرض."),
    dict(key="etimad_titles_hf", name="Hugging Face: chaliasif/etimad-tender-titles", owner="مستخدم Hugging Face",
         url="https://huggingface.co/datasets/chaliasif/etimad-tender-titles", access_method="link_only",
         commercial_reuse="unknown", record_types="عناوين ووصف وتصنيفات منافسات دون متقدمين أو فائزين",
         notes="غير قابل للوصول من هذه البيئة. الأساس القانوني لنقل بيانات اعتماد غير واضح: لا يُستورد قبل مراجعة الرخصة والأصل."),
    dict(key="company_announcements", name="إعلانات الشركات والمطورين (طلبات مقاولي باطن)", owner="متعدد",
         url="", access_method="manual", commercial_reuse="unknown",
         record_types="طلبات مقاولي باطن مؤكدة بدليل",
         notes="تدخل عبر المراجعة البشرية فقط (فئة D)، ولا تُشتق من ترسية."),
    dict(key="research_package_2026_11_13", name="حزمة بحث مناقصات 2026-11-13 (عينة موثقة)", owner="المالك (تجميع بحثي)",
         url="", access_method="research_package", commercial_reuse="unknown", rights_status="unverified",
         record_types="38 منافسة، 138 عرضًا ظاهرًا، 121 اسمًا", years_covered="2021–2026",
         notes="مجمّعة من ملخصات ناشرين مستقلين (منافساتي، منصة المناقصات). خاصة بالمراجعة: publish_allowed=false."),
    dict(key="synthetic_perf", name="بيانات اختبار أداء اصطناعية", owner="اختبار", url="", access_method="synthetic",
         rights_status="synthetic_test", record_types="سجلات مولدة للأداء فقط", notes="لا تمثل السوق ولا تُعرض أبدًا."),
]


def ensure_sources(session: Session) -> dict[str, ProcurementSource]:
    """Insert missing registry rows; never overwrite a row an admin has edited (licences, probe results)."""
    existing = {s.key: s for s in session.scalars(select(ProcurementSource))}
    for entry in REGISTRY:
        if entry["key"] not in existing:
            src = ProcurementSource(**{"rights_status": "unverified", **entry})
            session.add(src)
            existing[entry["key"]] = src
    session.flush()
    for key in ("saudi_open_data",):
        src = existing[key]
        if not src.licenses:
            session.add(SourceLicense(source_id=src.id, license_name="رخصة البيانات المفتوحة السعودية (Open Data License v2.0)",
                                      license_url="https://od.data.gov.sa/", allows_storage="unknown",
                                      allows_public_display="unknown", allows_commercial="unknown", status="unverified",
                                      evidence="وصف الرخصة من صفحات سياسات الجهات: إعادة الاستخدام مع ذكر المصدر وإرفاق الرخصة. "
                                               "الاستخدام التجاري غير مذكور صراحة؛ النص الأصلي لم يُقرأ."))
    session.flush()
    return existing
