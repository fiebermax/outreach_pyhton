"""Stufe 4 gegen den lokalen Testserver mit echtem Headless-Chromium."""

import asyncio

import pytest

from core.errors import FetchTimeout, HttpClientError, InvalidUrlError
from modules.browser import BrowserLimits, BrowserRenderer
from modules.content_extractor import extract_content
from modules.quality_control import assess_page

LIMITS = BrowserLimits(navigation_timeout_seconds=5, networkidle_timeout_seconds=1, text_wait_seconds=2,
                       allow_local=True)


def render(url, limits=LIMITS):
    async def run():
        async with BrowserRenderer(limits) as browser:
            return await browser.render(url)
    return asyncio.run(run())


def test_js_page_gets_content_only_in_browser(site_server):
    """Kernfall: HTTP-Stufen sehen nichts, der Browser sieht den Inhalt."""
    result = render(site_server + "/js-only.html")
    content = extract_content(result.html, result.final_url)
    assessment = assess_page(cleaned_text=content.cleaned_text, title=content.title,
                             heading_texts=[h.text for h in content.headings], js_signals=content.js_signals,
                             rendered=True)
    assert "Fassaden" in content.cleaned_text
    assert result.title == "Malerbetrieb Beispiel"
    assert assessment.status.is_usable
    assert "domcontentloaded" in result.waited_for


def test_static_page_no_overflow(site_server):
    result = render(site_server + "/static.html")
    assert result.viewport_width == 375
    assert not result.horizontal_overflow and result.overflow_culprits == []


def test_real_overflow_detected_with_culprit(site_server):
    result = render(site_server + "/overflow.html")
    assert result.horizontal_overflow
    assert result.document_width >= 640 and result.max_scroll_x > 0
    assert result.overflow_culprits[0].selector == "div.banner.hero-banner"
    assert result.overflow_culprits[0].width == 640


def test_scrollable_table_is_not_overflow(site_server):
    """Breite Tabelle in overflow-x:auto ist legitim – weder Overflow noch Verursacher."""
    result = render(site_server + "/overflow-legit.html")
    assert not result.horizontal_overflow and result.overflow_culprits == []


def test_clipped_content_is_not_scroll_overflow(site_server):
    """overflow-x:hidden: Nutzer können nicht seitlich scrollen – kein Overflow, aber Element bleibt als Indiz."""
    result = render(site_server + "/overflow-hidden.html")
    assert not result.horizontal_overflow and result.max_scroll_x == 0


def test_networkidle_is_optional(site_server):
    # js-only lädt nichts nach; networkidle wird erreicht oder sauber übersprungen – nie ein Fehler
    result = render(site_server + "/static.html")
    assert any(w.startswith("networkidle") for w in result.waited_for)


def test_http_error_in_browser(site_server):
    with pytest.raises(HttpClientError):
        render(site_server + "/missing")


def test_navigation_timeout(site_server):
    with pytest.raises(FetchTimeout):
        render(site_server + "/slow", BrowserLimits(navigation_timeout_seconds=0.5, allow_local=True))


def test_local_url_refused_without_flag(site_server):
    with pytest.raises(InvalidUrlError):
        render(site_server + "/static.html", BrowserLimits())


def test_one_browser_many_pages(site_server):
    async def run():
        async with BrowserRenderer(LIMITS) as browser:
            return [await browser.render(site_server + p) for p in ("/static.html", "/overflow.html", "/static.html")]
    results = asyncio.run(run())
    assert [r.horizontal_overflow for r in results] == [False, True, False]


# --- Cookie-Banner (Paket 3.11) ---

from modules.browser import ConsentPolicy, classify_button  # noqa: E402


@pytest.mark.parametrize("text,kind", [
    ("Alle akzeptieren", "accept"), ("ANNEHMEN", "accept"), ("Zustimmen", "accept"), ("Accept all", "accept"),
    ("Allow all", "accept"), ("Ablehnen", "reject"), ("Alle ablehnen", "reject"), ("Nur notwendige Cookies", "reject"),
    ("Nur notwendige akzeptieren", "reject"),          # Ablehnung trotz "akzeptieren"
    ("EINSTELLUNGEN & INFOS", "settings"), ("Einstellungen", "settings"), ("Mehr Optionen", "settings"),
    ("Kontakt", "other"), ("Weiterlesen", "other"),
])
def test_button_classification(text, kind):
    assert classify_button(text) == kind


def _words_ok(html: str) -> bool:
    return len(extract_content(html, "https://x.de/").cleaned_text.split()) >= 15


def render_with(site_server, path, policy):
    limits = BrowserLimits(navigation_timeout_seconds=5, networkidle_timeout_seconds=1, text_wait_seconds=1,
                           allow_local=True, consent_policy=policy, consent_wait_seconds=0.3)

    async def run():
        async with BrowserRenderer(limits) as browser:
            return await browser.render(site_server + path, content_ok=_words_ok)
    return asyncio.run(run())


def test_banner_but_content_readable_no_click(site_server):
    """Inhalt ist ohne Zustimmung lesbar → nichts anklicken."""
    r = render_with(site_server, "/cookie-banner.html", ConsentPolicy.REJECT)
    assert r.consent.detected and r.consent.content_without_consent is True
    assert r.consent.clicked == []
    assert {k for k, _ in r.consent.buttons} == {"accept", "reject", "settings"}


def test_reject_reveals_content(site_server):
    r = render_with(site_server, "/consent-reject-ok.html", ConsentPolicy.REJECT)
    assert r.consent.clicked == ["reject: Nur notwendige Cookies"]
    assert 'data-clicked="reject"' in r.html
    assert "Förderprogrammen" in extract_content(r.html, r.final_url).cleaned_text


def test_default_never_accepts(site_server):
    """Inhalt nur nach Zustimmung: Standardrichtlinie lehnt ab und gibt dann auf – kein Zustimmen."""
    r = render_with(site_server, "/consent-accept-only.html", ConsentPolicy.REJECT)
    assert r.consent.clicked == ["reject: Ablehnen"]
    assert 'data-clicked="accept"' not in r.html
    assert not _words_ok(r.html)


def test_without_reject_button_default_does_not_accept(site_server):
    r = render_with(site_server, "/consent-no-reject.html", ConsentPolicy.REJECT)
    assert r.consent.detected and r.consent.clicked == []
    assert not _words_ok(r.html)


def test_accept_only_when_configured(site_server):
    r = render_with(site_server, "/consent-no-reject.html", ConsentPolicy.REJECT_THEN_ACCEPT)
    assert r.consent.clicked == ["accept: Alle akzeptieren"]       # nie "Einstellungen"
    assert "Dachfenster" in extract_content(r.html, r.final_url).cleaned_text


def test_policy_none_never_clicks(site_server):
    r = render_with(site_server, "/consent-reject-ok.html", ConsentPolicy.NONE)
    assert r.consent.detected and r.consent.clicked == []
    assert "data-clicked" not in r.html


def test_iframe_banner(site_server):
    r = render_with(site_server, "/consent-iframe.html", ConsentPolicy.REJECT)
    assert r.consent.clicked == ["reject: Alle ablehnen"]
    assert "Wallboxen" in extract_content(r.html, r.final_url).cleaned_text


def test_page_without_banner(site_server):
    r = render_with(site_server, "/static.html", ConsentPolicy.REJECT)
    assert not r.consent.detected and r.consent.clicked == []
