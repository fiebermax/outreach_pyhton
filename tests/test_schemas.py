import pytest
from pydantic import ValidationError

from models.schemas import (
    ContactInfo, ContactKind, ContactOrigin, CrawlResult, CrawlStatus, EmailDraft, EvidenceType,
    ExtractionMethod, Finding, FindingCategory, Lead, LeadScore, PageResult, Severity,
)

URL = "https://example.de/"


def page(**kw) -> PageResult:
    base = dict(url=URL, crawl_status=CrawlStatus.SUCCESS, cleaned_text="Wir bauen Möbel.",
                extraction_method=ExtractionMethod.TRAFILATURA, extraction_quality=0.8)
    return PageResult(**(base | kw))


def finding(**kw) -> Finding:
    base = dict(issue_id="copyright_outdated", category=FindingCategory.MAINTENANCE, severity=Severity.LOW,
                description="Copyright im Footer nennt 2019.", evidence="© 2019 Schreinerei Reiser",
                source_url=URL, evidence_type=EvidenceType.DIRECTLY_OBSERVED, confidence=0.95,
                business_relevance=Severity.LOW)
    return Finding(**(base | kw))


# --- Lead ---

def test_lead_normalizes_fields():
    lead = Lead(name=" Reiser ", website="www.reiser.de", email="Info@Reiser.de",
                phone="08225 12345", source="google_places")
    assert lead.name == "Reiser"
    assert lead.website == "https://www.reiser.de/"
    assert lead.email == "info@reiser.de"
    assert lead.phone == "+49822512345"
    assert lead.collected_at.tzinfo is not None


def test_lead_without_website_and_email_is_valid():
    assert Lead(name="Wegenmayer", source="google_places").website is None


@pytest.mark.parametrize("field,value", [
    ("website", "javascript:alert(1)"), ("email", "kein-mail"), ("name", ""), ("postal_code", "8934"),
])
def test_lead_rejects_invalid(field, value):
    data = dict(name="X", source="test") | {field: value}
    with pytest.raises(ValidationError):
        Lead(**data)


def test_unparseable_phone_kept_raw():
    assert Lead(name="X", source="t", phone="Durchwahl 12").phone == "Durchwahl 12"


# --- Crawl ---

def test_failed_page_needs_reason():
    with pytest.raises(ValidationError, match="failure_reason"):
        PageResult(url=URL, crawl_status=CrawlStatus.BLOCKED)
    assert PageResult(url=URL, crawl_status=CrawlStatus.BLOCKED, failure_reason="Captcha").failure_reason


def test_success_without_text_is_rejected():
    """Kernregel: ein leerer Crawl darf nie als Erfolg gelten."""
    with pytest.raises(ValidationError, match="ohne Text"):
        page(cleaned_text="   ")


def test_success_needs_method():
    with pytest.raises(ValidationError, match="Extraktionsmethode"):
        page(extraction_method=ExtractionMethod.NONE)


def test_overflow_needs_measurements():
    with pytest.raises(ValidationError, match="Messwerte"):
        page(horizontal_overflow=True)
    assert page(horizontal_overflow=True, viewport_width=375, document_width=620).horizontal_overflow


def test_site_result_needs_usable_page():
    bad = PageResult(url=URL, crawl_status=CrawlStatus.EMPTY_CONTENT, failure_reason="kein Text")
    with pytest.raises(ValidationError):
        CrawlResult(site_url=URL, status=CrawlStatus.SUCCESS, pages=[bad])
    assert CrawlResult(site_url=URL, status=CrawlStatus.PARTIAL_SUCCESS, pages=[bad, page()]).usable_pages


def test_status_classification():
    assert CrawlStatus.BLOCKED.is_site_inaccessible and not CrawlStatus.BLOCKED.is_usable
    assert not CrawlStatus.EMPTY_CONTENT.is_site_inaccessible   # Scraper-Problem, nicht Website-Problem
    assert CrawlStatus.PARTIAL_SUCCESS.is_usable


def test_contacts_dedup_prefers_link():
    text_hit = ContactInfo(kind=ContactKind.PHONE, value="+49822512345", raw="08225 12345",
                           origin=ContactOrigin.TEXT, source_url=URL)
    link_hit = text_hit.model_copy(update={"origin": ContactOrigin.LINK, "source_url": URL + "kontakt"})
    result = CrawlResult(site_url=URL, status=CrawlStatus.SUCCESS,
                         pages=[page(phone_numbers=[text_hit]), page(phone_numbers=[link_hit])])
    phones = result.contacts(ContactKind.PHONE)
    assert len(phones) == 1 and phones[0].is_link


# --- Analyse ---

def test_finding_needs_evidence():
    with pytest.raises(ValidationError):
        finding(evidence="")


def test_measured_finding_needs_measurement():
    with pytest.raises(ValidationError, match="Messwerte"):
        finding(evidence_type=EvidenceType.MEASURED)
    f = finding(evidence_type=EvidenceType.MEASURED, measurement={"viewport": 375, "document": 620})
    assert f.is_verified


def test_ai_finding_not_verified():
    assert not finding(evidence_type=EvidenceType.AI_INTERPRETATION, confidence=0.6).is_verified


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        finding(confidence=1.2)


def test_disqualification_rules():
    with pytest.raises(ValidationError):
        LeadScore(score=0, disqualified=True)
    with pytest.raises(ValidationError):
        LeadScore(score=40, disqualified=True, disqualification_reasons=["keine Website"])
    assert LeadScore(score=0, disqualified=True, disqualification_reasons=["keine Website"]).disqualified


def test_draft_needs_finding():
    body = "Guten Tag, mir ist auf Ihrer Website aufgefallen, dass das Copyright-Jahr veraltet ist."
    with pytest.raises(ValidationError):
        EmailDraft(recipient="info@x.de", subject="Ihre Website", body=body, finding_ids=[])
    assert EmailDraft(recipient="Info@X.de", subject="Ihre Website", body=body,
                      finding_ids=["copyright_outdated"]).recipient == "info@x.de"


def test_json_roundtrip():
    result = CrawlResult(site_url=URL, status=CrawlStatus.SUCCESS, pages=[page()])
    assert CrawlResult.model_validate_json(result.model_dump_json()) == result
