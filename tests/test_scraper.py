"""Website-Crawl über alle Stufen gegen den lokalen Testserver."""

import asyncio

import pytest

from models.schemas import ContactKind, ContactOrigin, CrawlStatus, ExtractionMethod
from modules.browser import BrowserLimits, BrowserRenderer
from modules.fetcher import FetchLimits
from modules.html_store import HtmlStore
from modules.scraper import CrawlConfig, SiteCrawler

FETCH = FetchLimits(timeout_seconds=2, retries=0, allow_local=True)
BROWSER = BrowserLimits(navigation_timeout_seconds=5, networkidle_timeout_seconds=1, text_wait_seconds=1,
                        allow_local=True, consent_wait_seconds=0.3)


def crawl(url, *, browser=False, store=None, fetch=FETCH, **cfg):
    config = CrawlConfig(fetch=fetch, **cfg)

    async def run():
        if browser:
            async with BrowserRenderer(BROWSER) as renderer:
                return await SiteCrawler(config, renderer=renderer, html_store=store).crawl_site(url)
        return await SiteCrawler(config, html_store=store).crawl_site(url)
    return asyncio.run(run())


def test_full_site_without_browser(site_server, tmp_path):
    result, outcomes = crawl(site_server + "/", store=HtmlStore(tmp_path))
    urls = [p.url.removeprefix(site_server) for p in result.pages]
    # Startseite + Kontakt, Impressum, Leistungen; von Leistungen aus: Referenzen (Tiefe 2, existiert nicht → 404)
    assert urls == ["/", "/kontakt.html", "/impressum.html", "/leistungen.html", "/referenzen.html"]
    statuses = {p.url.removeprefix(site_server): p.crawl_status for p in result.pages}
    assert statuses["/referenzen.html"] is CrawlStatus.HTTP_ERROR
    assert result.status is CrawlStatus.PARTIAL_SUCCESS          # eine Unterseite fehlgeschlagen
    assert "1 Unterseite(n) nicht verwertbar" in result.failure_reason
    # Kontakte aus mehreren Seiten, mit Herkunft
    phones = result.contacts(ContactKind.PHONE)
    assert [(p.value, p.origin) for p in phones] == [("+49822512345", ContactOrigin.LINK)]   # Link schlägt Text
    emails = {e.value for e in result.contacts(ContactKind.EMAIL)}
    assert emails == {"info@schreinerei-beispiel.de"}               # mailto + verschleiert = gleiche Adresse
    home = result.pages[0]
    assert home.copyright_year == 2019 and home.raw_html_reference
    assert home.horizontal_overflow is None                         # ohne Browser nicht gemessen
    assert any("Browser nicht verfügbar" in n for n in outcomes[0].notes)


def test_max_pages_limit(site_server):
    result, _ = crawl(site_server + "/", max_pages=2)
    assert len(result.pages) == 2


def test_depth_zero_only_homepage_links(site_server):
    result, _ = crawl(site_server + "/", max_depth=1)
    assert not any(p.url.endswith("/referenzen.html") for p in result.pages)


def test_blocked_site_not_crawled_further(site_server):
    result, outcomes = crawl(site_server + "/forbidden")
    assert result.status is CrawlStatus.BLOCKED and len(result.pages) == 1
    assert result.status.is_site_inaccessible and result.failure_reason


def test_invalid_url():
    result, outcomes = crawl("javascript:alert(1)")
    assert result.status is CrawlStatus.INVALID_URL and outcomes == []


def test_timeout_site(site_server):
    result, _ = crawl(site_server + "/slow", fetch=FetchLimits(timeout_seconds=0.5, retries=0, allow_local=True))
    assert result.status is CrawlStatus.TIMEOUT


def test_js_page_without_browser_is_not_success(site_server):
    """Kernregel: ohne Browser darf eine JS-Seite nicht als Erfolg gelten."""
    result, _ = crawl(site_server + "/js-only.html")
    assert not result.status.is_usable
    assert result.pages[0].cleaned_text == ""


def test_js_page_with_browser(site_server):
    result, outcomes = crawl(site_server + "/js-only.html", browser=True)
    home = result.pages[0]
    assert home.crawl_status.is_usable and home.extraction_method is ExtractionMethod.PLAYWRIGHT
    assert "Fassaden" in home.cleaned_text
    assert home.viewport_width == 375 and home.horizontal_overflow is False


def test_overflow_measured_on_homepage(site_server):
    result, _ = crawl(site_server + "/overflow.html", browser=True, max_pages=1)
    home = result.pages[0]
    assert home.horizontal_overflow and home.overflow_culprits[0].selector == "div.banner.hero-banner"
    assert home.extraction_method in (ExtractionMethod.TRAFILATURA, ExtractionMethod.HTML_PARSER)  # HTTP-Text genügte


def test_consent_page_with_browser(site_server):
    result, outcomes = crawl(site_server + "/consent-reject-ok.html", browser=True, max_pages=1)
    assert result.pages[0].crawl_status.is_usable
    assert any("Cookie-Banner: Klicks ['reject: Nur notwendige Cookies']" in n for n in outcomes[0].notes)


def test_captcha_page_never_rendered(site_server):
    result, outcomes = crawl(site_server + "/captcha.html", browser=True)
    assert result.status is CrawlStatus.BLOCKED
    assert not any(n.startswith("Browser") for n in outcomes[0].notes)


def test_crawl_result_storable(site_server, tmp_path):
    from models.schemas import Lead
    from modules.database import Database
    result, _ = crawl(site_server + "/", max_pages=2)
    db = Database(tmp_path / "x.db")
    cid = db.upsert_company(Lead(name="Schreinerei Beispiel", source="test"))
    run_id = db.save_crawl(cid.company_id, result)
    assert db.latest_crawl(cid.company_id)[1].pages[0].copyright_year == 2019 and run_id


def test_hard_site_deadline(site_server):
    """Eine hängende Seite darf das Website-Budget nicht überschreiten."""
    import time as _t
    slow_fetch = FetchLimits(timeout_seconds=10, retries=0, allow_local=True)
    t0 = _t.monotonic()
    result, outcomes = crawl(site_server + "/slow", fetch=slow_fetch, max_seconds=1.5)
    assert _t.monotonic() - t0 < 3
    assert result.status is CrawlStatus.TIMEOUT and "Zeitlimit der Website" in result.failure_reason


def test_browser_retried_once_on_temporary_error(site_server, server_hits):
    """Echter Fall elektro-suntec.de: HTTP 200, Browser-Aufruf 502 → einmal wiederholen."""
    result, outcomes = crawl(site_server + "/browser-502", browser=True, max_pages=1)
    home = result.pages[0]
    assert server_hits["/browser-502"] == 3
    assert home.horizontal_overflow is False            # Layout trotzdem gemessen
