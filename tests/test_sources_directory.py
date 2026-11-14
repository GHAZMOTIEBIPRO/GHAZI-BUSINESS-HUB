"""The source directory: validated data, mirrored channels, and the public reference page."""
import copy

import pytest
from sqlalchemy import select

from hub import directory, jobs, radar
from hub.models import Source

from .conftest import new_client


def test_directory_file_is_valid_and_covers_the_focus_cities():
    data = directory.load()
    assert directory.validate(data) == []
    keys = {s["key"] for s in data["sources"]}
    assert {"etimad", "rcjy-yanbu", "rcrc-riyadh", "gastat-cci", "saudi-exchange", "sca-muqawil"} <= keys
    for city in directory.FOCUS_CITIES:
        assert any(city in s["cities"] for s in data["sources"]), city


def test_reusable_rights_require_quoted_official_text():
    data = copy.deepcopy(directory.load())
    s = next(x for x in data["sources"] if x["key"] == "gastat-cci")
    assert s["rights"] == "verified_public_reuse" and s["evidence_status"] == "verified_text"
    assert "الأغراض التجارية" in s["rights_evidence"], "the commercial-use clause is quoted, not assumed"
    s["evidence_status"] = "secondary"
    assert any("verified_text" in e for e in directory.validate(data))
    s["evidence_status"], s["rights_evidence"] = "verified_text", "short"
    assert any("quoted evidence" in e for e in directory.validate(data))


@pytest.mark.parametrize("field,value,expect", [
    ("homepage", "http://insecure.example", "https"),
    ("cities", ["مدينة غير موجودة"], "unknown city"),
    ("rights", "free_for_all", "unknown rights"),
    ("rights_checked_at", "yesterday", "YYYY-MM-DD"),
    ("category", "misc", "unknown category"),
    ("automation", "active", "automation needs reusable rights"),
])
def test_validation_catches_bad_entries(field, value, expect):
    data = copy.deepcopy(directory.load())
    bad = next(x for x in data["sources"] if x["key"] == "etimad")
    bad[field] = value
    assert any(expect in e for e in directory.validate(data)), directory.validate(data)


def test_city_filter_keeps_nationwide_sources():
    yanbu = {s["key"] for s in directory.sources("ينبع")}
    assert "rcjy-yanbu" in yanbu and "etimad" in yanbu and "rcrc-riyadh" not in yanbu
    riyadh = {s["key"] for s in directory.sources("الرياض")}
    assert "rcrc-riyadh" in riyadh and "rcjy-yanbu" not in riyadh
    assert len(directory.sources("all", "market")) >= 2


def test_channels_are_mirrored_with_evidence_and_never_fetched(session):
    for entry in directory.channel_registry():
        row = session.scalar(select(Source).where(Source.key == entry["key"]))
        assert row is not None and row.kind == "manual" and row.enabled
        assert row.rights_evidence == entry["rights_evidence"] and row.rights_checked_at
        assert radar.fetch_source(row).outcome == "skipped"
        assert radar.source_health(row, radar.utcnow()) == "manual"


def test_sync_updates_descriptions_and_disables_removed_manual_rows(session):
    legacy = Source(key="muqawil-market", name="قديم", homepage="https://muqawil.sa/ar/market", kind="manual", rights="unknown")
    etimad = session.scalar(select(Source).where(Source.key == "etimad"))
    etimad.notes, etimad.rights_evidence = "stale", ""
    session.add(legacy)
    session.commit()
    jobs.sync_sources(session)
    session.commit()
    session.refresh(etimad)
    session.refresh(legacy)
    assert etimad.rights_evidence and etimad.notes != "stale"
    assert legacy.enabled is False, "rows no longer in the registry are disabled, not deleted"
    assert session.scalar(select(Source.id).where(Source.key == "muqawil-market")) == legacy.id


def test_sources_page_lists_directory_with_filters_and_evidence(app):
    c = new_client(app)
    page = c.get("/sources").text
    assert 'id="directory"' in page and 'id="src-etimad"' in page and 'id="src-gastat-cci"' in page
    assert "الأغراض التجارية" in page, "evidence text is shown, not just a label"
    assert 'id="jobRuns"' in page and 'id="channelsTable"' in page
    yanbu = c.get("/sources?city=ينبع").text
    assert 'id="src-rcjy-yanbu"' in yanbu and 'id="src-rcrc-riyadh"' not in yanbu
    market = c.get("/sources?city=all&cat=market").text
    assert 'id="src-gastat-cci"' in market and 'id="src-etimad"' not in market
    assert c.get("/sources?city=<script>&cat=../x").status_code == 200  # unknown filters are ignored, never echoed
    assert "<script>" not in c.get("/sources?city=<script>").text


def test_disabled_channels_are_hidden_from_the_public_table(app, session):
    row = session.scalar(select(Source).where(Source.key == "saudi-exchange"))
    row.enabled = False
    session.commit()
    page = new_client(app).get("/sources").text
    table = page.split('id="channelsTable"')[1].split("</table>")[0]
    assert "تداول السعودية" not in table
    assert 'id="src-saudi-exchange"' in page, "the directory entry itself stays"
