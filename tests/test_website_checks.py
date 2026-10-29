import asyncio

import httpx
import pytest

from models.schemas import (
    ContactInfo, ContactKind, ContactOrigin, CrawlResult, CrawlStatus, EvidenceType, ExtractionMethod,
    OverflowCulprit, PageResult,
)
from modules.fetcher import FetchLimits
from modules.scraper import CrawlConfig, SiteCrawler
from modules.website_checks import (
    CheckContext, probe_broken_links, probe_http_redirect, run_checks,
)

URL = "https://www.beispiel.de/"


LINKED_PHONE = ContactInfo(kind=ContactKind.PHONE, value="+49822599999", raw="+49822599999",
                           origin=ContactOrigin.LINK, source_url=URL, context="Anrufen")


def page(**kw) -> PageResult:
    base = dict(url=URL, final_url=URL, crawl_status=CrawlStatus.SUCCESS, cleaned_text="Text " * 60,
                extraction_method=ExtractionMethod.TRAFILATURA, page_title="Schreinerei Beispiel Jettingen",
                meta_description="Möbel nach Maß", has_viewport_meta=True, has_h1=True, word_count=120,
                internal_links=[URL + "impressum"], response_seconds=0.4, html_bytes=40_000,
                phone_numbers=[LINKED_PHONE])
    return PageResult(**(base | kw))


def site(*pages, status=CrawlStatus.SUCCESS, reason=None) -> CrawlResult:
    return CrawlResult(site_url=URL, status=status, pages=list(pages), failure_reason=reason)


def ids(findings):
    return sorted(f.issue_id for f in findings)


def ctx(**probes):
    return CheckContext(current_year=2026, probes=probes)


def test_clean_site_has_no_findings():
    assert run_checks(site(page()), ctx()) == []


def test_every_finding_has_evidence_and_source():
    p = page(page_title="Home", meta_description=None, has_h1=False, has_viewport_meta=False,
             copyright_year=2019, copyright_text="© 2019 Beispiel", internal_links=[])
    findings = run_checks(site(p), ctx())
    assert ids(findings) == ["copyright_outdated", "h1_missing", "impressum_link_not_found",
                             "meta_description_missing", "title_generic", "viewport_meta_missing"]
    for f in findings:
        assert f.evidence and f.source_url and 0 < f.confidence <= 1


def test_copyright_uses_runtime_year_and_tolerates_last_year():
    p = page(copyright_year=2025, copyright_text="© 2025")
    assert run_checks(site(p), CheckContext(current_year=2026)) == []
    assert ids(run_checks(site(p), CheckContext(current_year=2027))) == ["copyright_outdated"]


def test_overflow_is_measured_with_culprit():
    p = page(viewport_width=375, document_width=640, horizontal_overflow=True,
             overflow_culprits=[OverflowCulprit(selector="div.hero", right_edge=640, width=640)])
    (f,) = run_checks(site(p), ctx())
    assert f.issue_id == "horizontal_overflow_mobile" and f.evidence_type is EvidenceType.MEASURED
    assert f.measurement == {"viewport_width": 375, "document_width": 640, "culprit": "div.hero"}
    assert "div.hero" in f.evidence


def phone(origin, url=URL, raw="08225 12345"):
    return ContactInfo(kind=ContactKind.PHONE, value="+49822512345", raw=raw, origin=origin, source_url=url,
                       context="Telefon: 08225 12345")


def test_phone_text_only_on_page_is_flagged_even_if_linked_elsewhere():
    home = page(phone_numbers=[phone(ContactOrigin.TEXT), LINKED_PHONE])
    kontakt = page(url=URL + "kontakt", phone_numbers=[phone(ContactOrigin.LINK, URL + "kontakt")])
    (f,) = run_checks(site(home, kontakt), ctx())
    assert f.issue_id == "phone_not_clickable" and f.source_url == URL and "Telefon: 08225 12345" in f.evidence


def test_phone_linked_on_same_page_not_flagged():
    home = page(phone_numbers=[phone(ContactOrigin.TEXT), phone(ContactOrigin.LINK), LINKED_PHONE])
    assert run_checks(site(home), ctx()) == []


def test_malformed_tel_link():
    """Echter Fall maler-farbtec.de: tel:01743810360">0174 3810360"""
    home = page(phone_numbers=[phone(ContactOrigin.LINK, raw='01743810360">0174 3810360')])
    (f,) = run_checks(site(home), ctx())
    assert f.issue_id == "tel_link_malformed" and '">' in f.evidence


def test_no_contact_is_reported_with_moderate_confidence():
    findings = run_checks(site(page(phone_numbers=[])), ctx())
    (f,) = findings
    assert f.issue_id == "no_contact_found" and f.confidence < 0.6     # Kontakt kann im Bild stehen


def test_thin_homepage():
    """Echter Fall hins-zimmerei.de: Startseite 3 Wörter, Unterseiten mit Text."""
    home = PageResult(url=URL, crawl_status=CrawlStatus.EMPTY_CONTENT, failure_reason="nur 3 Wörter", word_count=3)
    sub = page(url=URL + "leistungen")
    findings = {f.issue_id: f for f in run_checks(site(home, sub, status=CrawlStatus.PARTIAL_SUCCESS, reason="x"), ctx())}
    assert "homepage_almost_no_text" in findings
    assert findings["homepage_almost_no_text"].measurement == {"words": 3}


def test_unusable_site_gets_no_content_findings():
    """Kernregel: aus einem gescheiterten Crawl entstehen keine inhaltlichen Befunde."""
    home = PageResult(url=URL, crawl_status=CrawlStatus.BLOCKED, failure_reason="Captcha", word_count=5)
    assert run_checks(site(home, status=CrawlStatus.BLOCKED, reason="Captcha"), ctx()) == []


def test_ssl_finding_without_content():
    home = PageResult(url=URL, crawl_status=CrawlStatus.HTTP_ERROR, failure_reason="SslCertificateError: expired")
    reason = "Startseite: SslCertificateError: SSL-Zertifikat ungültig: certificate has expired"
    (f,) = run_checks(site(home, status=CrawlStatus.HTTP_ERROR, reason=reason), ctx())
    assert f.issue_id == "ssl_certificate_invalid" and "expired" in f.evidence


def test_https_findings():
    http_home = page(url="http://beispiel.de/", final_url="http://beispiel.de/")
    assert ids(run_checks(site(http_home), ctx())) == ["no_https"]
    probe = {"url": "http://www.beispiel.de/", "status": 200, "final_url": "http://www.beispiel.de/", "redirects": []}
    assert ids(run_checks(site(page()), ctx(http_redirect=probe))) == ["http_not_redirected"]
    ok = {"url": "http://www.beispiel.de/", "status": 200, "final_url": URL, "redirects": [301]}
    assert run_checks(site(page()), ctx(http_redirect=ok)) == []


def test_slow_response_is_low_confidence_measurement():
    (f,) = run_checks(site(page(response_seconds=4.2)), ctx())
    assert f.issue_id == "slow_server_response" and f.confidence <= 0.5 and "einzelne Messung" in f.description


# --- Integration mit echtem Crawl der Testseite ---

def test_checks_on_crawled_fixture_site(site_server):
    async def run():
        crawler = SiteCrawler(CrawlConfig(fetch=FetchLimits(timeout_seconds=2, retries=0, allow_local=True)))
        result, _ = await crawler.crawl_site(site_server + "/")
        async with httpx.AsyncClient(timeout=2) as client:
            broken = await probe_broken_links(result, client)
        return result, broken
    result, broken = asyncio.run(run())
    findings = {f.issue_id: f for f in run_checks(result, ctx(broken_links=broken))}
    # no_https: der lokale Testserver läuft ohne TLS – das ist hier ein korrekter Befund
    assert set(findings) == {"copyright_outdated", "phone_not_clickable", "broken_internal_links", "no_https"}
    assert findings["copyright_outdated"].evidence == "© 2019"
    assert findings["broken_internal_links"].evidence.endswith("/referenzen.html → HTTP 404")


def test_http_redirect_probe(site_server):
    result = site(page(url=site_server.replace("http", "https") + "/", final_url=site_server.replace("http", "https") + "/"))

    async def run():
        async with httpx.AsyncClient(timeout=2) as client:
            return await probe_http_redirect(result, client)
    probe = asyncio.run(run())
    assert probe["status"] == 200 and probe["final_url"].startswith("http://")


def test_phone_only_in_impressum_is_low_relevance():
    """Echter Fall zimmerei-gay.de: Nummer nur im Impressum unverlinkt – stimmt, ist aber kaum relevant."""
    imp = page(url=URL + "impressum", phone_numbers=[phone(ContactOrigin.TEXT, URL + "impressum")])
    (f,) = run_checks(site(page(), imp), ctx())
    assert f.issue_id == "phone_not_clickable" and f.business_relevance.value == "LOW"


def test_customer_facing_phone_preferred_over_impressum():
    imp = page(url=URL + "impressum", phone_numbers=[phone(ContactOrigin.TEXT, URL + "impressum")])
    kontakt = page(url=URL + "kontakt", phone_numbers=[phone(ContactOrigin.TEXT, URL + "kontakt")])
    (f,) = run_checks(site(page(), imp, kontakt), ctx())
    assert f.source_url == URL + "kontakt" and f.business_relevance.value == "MEDIUM"
