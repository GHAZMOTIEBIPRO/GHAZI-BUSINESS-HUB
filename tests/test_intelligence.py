"""Public intelligence must never leak private procurement archive content."""
from __future__ import annotations

from hub.archive.models import Company, ProcurementIssuer, ProcurementSource, ProcurementTender, TenderAward
from hub.daall_mcp import safe_source_url
from hub.intelligence import local_companies, local_company_projects, local_tenders

from .conftest import new_client


def _seed(session):
    src = ProcurementSource(key="intel_test", name="مصدر اختبار خاص", owner="اختبار",
                            access_method="manual")
    issuer = ProcurementIssuer(name="جهة اختبارية", name_key="جهة اختبارية")
    company = Company(display_name="شركة ألف للمقاولات", name_key="شركة ألف للمقاولات",
                      fold_key="شركة ألف للمقاولات", cr_number="1010111122",
                      identity_status="registry_verified")
    hidden_company = Company(display_name="شركة خفية للمقاولات", name_key="شركة خفية للمقاولات",
                             fold_key="شركة خفية للمقاولات", cr_number="1010199999")
    session.add_all([src, issuer, company, hidden_company])
    session.flush()
    public = ProcurementTender(
        source_id=src.id, issuer_id=issuer.id, official_reference="260439001976",
        title="اختبار صيانة الطرق", status="awarded", lifecycle="C_awarded",
        visibility="public", publish_allowed=True, is_synthetic=False,
        source_url="https://tenders.etimad.sa/", source_rights_status="verified_open_license")
    private = ProcurementTender(
        source_id=src.id, issuer_id=issuer.id, official_reference="888888888888",
        title="مشروع سري غير منشور", status="awarded", lifecycle="C_awarded",
        visibility="archive_private", publish_allowed=False, is_synthetic=False)
    synthetic = ProcurementTender(
        source_id=src.id, issuer_id=issuer.id, official_reference="777777777777",
        title="مناقصة تجريبية صناعية", status="awarded", lifecycle="C_awarded",
        visibility="public", publish_allowed=True, is_synthetic=True)
    session.add_all([public, private, synthetic])
    session.flush()
    session.add_all([
        TenderAward(tender_id=public.id, company_id=company.id, winner_name_raw=company.display_name,
                    award_amount_sar=900000.0, verification_status="official"),
        TenderAward(tender_id=private.id, company_id=hidden_company.id,
                    winner_name_raw=hidden_company.display_name, award_amount_sar=5000000.0),
    ])
    session.commit()
    return company.id, hidden_company.id


def test_only_public_rights_gated_data_is_searchable(app, session):
    visible_id, hidden_id = _seed(session)
    client = new_client(app)
    a = client.get("/intelligence?kind=company&q=1010111122")
    assert a.status_code == 200
    assert "شركة ألف للمقاولات" in a.text
    assert "شركة خفية للمقاولات" not in a.text
    assert client.get("/intelligence?kind=company&q=1010199999").status_code == 200
    assert "شركة خفية للمقاولات" not in client.get("/intelligence?kind=company&q=1010199999").text
    assert client.get(f"/intelligence/company/{visible_id}").status_code == 200
    assert client.get(f"/intelligence/company/{hidden_id}").status_code == 404
    assert "مشروع سري غير منشور" not in client.get("/intelligence?kind=tender&q=صيانة").text
    assert client.get("/intelligence/tender/888888888888").status_code == 200
    assert "مشروع سري غير منشور" not in client.get("/intelligence/tender/888888888888").text
    assert local_companies(session, "1010111122")
    assert not local_companies(session, "1010199999")
    assert len(local_tenders(session, "صيانة")) == 1
    assert len(local_company_projects(session, visible_id)["awards"]) == 1


def test_user_triggered_mcp_tender_and_company_results(app, monkeypatch):
    monkeypatch.setenv("HUB_MCP_ENABLED", "true")
    from hub import intelligence

    seen = []
    def mocked(action, **kwargs):
        seen.append((action, kwargs))
        if action == "get_tender":
            return {"reference_no": "260439001976", "name": "أعمال صيانة الطرق",
                    "agency": "الهيئة العامة للطرق", "status": "تمت الترسية",
                    "is_open": False, "url": "https://mnaqasat.daall.sa/tender/260439001976",
                    "etimad_url": "https://tenders.etimad.sa/"}
        if action == "company_summary":
            return {"name": "شركة ألف للمقاولات", "participations": 23,
                    "wins": 8, "won_value_sar": None, "recent_wins": [],
                    "url": "https://mnaqasat.daall.sa/companies/1"}
        return {"query": kwargs["query"], "total": 1, "items": [
            {"reference_no": "260439001976", "name": "أعمال صيانة الطرق",
             "agency": "الهيئة العامة للطرق", "status": "مغلقة"}]}
    monkeypatch.setattr(intelligence, "lookup", mocked)
    client = new_client(app)
    r = client.get("/intelligence?kind=tender&q=260439001976")
    assert r.status_code == 200 and "الهيئة العامة للطرق" in r.text
    r = client.get("/intelligence/tender/260439001976")
    assert r.status_code == 200 and "اعتماد" in r.text
    r = client.get("/intelligence?kind=tender&q=صيانة")
    assert r.status_code == 200 and "أعمال صيانة الطرق" in r.text
    r = client.get("/intelligence?kind=company&q=شركة ألف للمقاولات")
    assert r.status_code == 200 and "23" in r.text and "8" in r.text
    assert [a for a, _ in seen] == ["get_tender", "get_tender", "search_tenders", "company_summary"]
    count = len(seen)
    r = client.get("/intelligence?kind=company&q=1010111122")
    assert r.status_code == 200
    assert len(seen) == count, "CR must not be mistaken for provider's internal id"


def test_external_source_url_allowlist():
    assert safe_source_url("https://mnaqasat.daall.sa/tender/260439001976")
    assert not safe_source_url("javascript:alert(1)")
    assert not safe_source_url("https://mnaqasat.daall.sa.evil.test/")
    assert not safe_source_url("https://user:pass@mnaqasat.daall.sa/")
    assert not safe_source_url("http://tenders.etimad.sa/")
