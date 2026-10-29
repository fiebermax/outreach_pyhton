import asyncio

import pytest

from core.errors import (
    ContentTooLarge, FetchTimeout, HttpClientError, InvalidUrlError, NetworkError, RateLimited, ServerError,
    TooManyRedirects, UnsupportedContentType,
)
from models.schemas import CrawlStatus
from modules.fetcher import FetchLimits, decode_html, fetch_http, status_for_error

LOCAL = FetchLimits(timeout_seconds=1.0, max_bytes=200_000, retries=0, allow_local=True)


async def _no_sleep(_):
    pass


def fetch(url, **limits):
    lim = FetchLimits(**{**LOCAL.__dict__, **limits})
    return asyncio.run(fetch_http(url, lim, sleep=_no_sleep))


def test_static_page(site_server):
    r = fetch(site_server + "/static.html")
    assert r.status == 200
    assert "Möbel nach Maß" in r.html
    assert r.redirects == []
    assert r.content_type.startswith("text/html")


def test_redirect_followed_and_recorded(site_server):
    r = fetch(site_server + "/redirect-chain")
    assert r.final_url.endswith("/static.html")
    assert [status for status, _ in r.redirects] == [302, 301]


def test_redirect_loop(site_server):
    with pytest.raises(TooManyRedirects):
        fetch(site_server + "/redirect-loop")


def test_timeout(site_server):
    with pytest.raises(FetchTimeout):
        fetch(site_server + "/slow", timeout_seconds=0.3)


def test_404_is_http_error(site_server):
    with pytest.raises(HttpClientError) as exc:
        fetch(site_server + "/missing")
    assert exc.value.status == 404


def test_server_error_retried_then_reported(site_server, server_hits):
    with pytest.raises(ServerError):
        fetch(site_server + "/server-error", retries=2)
    assert server_hits["/server-error"] == 3


def test_transient_error_recovers(site_server, server_hits):
    r = fetch(site_server + "/flaky-a", retries=1)
    assert r.status == 200
    assert server_hits["/flaky-a"] == 2


def test_permanent_error_not_retried(site_server, server_hits):
    with pytest.raises(HttpClientError):
        fetch(site_server + "/missing", retries=3)
    assert server_hits["/missing"] == 1


def test_rate_limit(site_server):
    with pytest.raises(RateLimited):
        fetch(site_server + "/rate-limited")


def test_pdf_rejected(site_server):
    with pytest.raises(UnsupportedContentType, match="application/pdf"):
        fetch(site_server + "/document.pdf")


def test_missing_content_type_sniffed(site_server):
    assert "Möbel" in fetch(site_server + "/no-content-type").html


@pytest.mark.parametrize("path", ["/huge", "/huge-no-length"])
def test_size_limit(site_server, path):
    with pytest.raises(ContentTooLarge):
        fetch(site_server + path)


def test_latin1_decoded_via_meta(site_server):
    r = fetch(site_server + "/latin1.html")
    assert "Bäckerei Müller" in r.html and "Grüße" in r.html
    assert r.encoding == "iso-8859-1"


def test_local_address_refused_in_production_mode(site_server):
    with pytest.raises(InvalidUrlError):
        fetch(site_server + "/static.html", allow_local=False)


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "ftp://example.de"])
def test_invalid_url(url):
    with pytest.raises(InvalidUrlError):
        fetch(url)


def test_unreachable_host():
    # Port 9 auf localhost: Verbindung wird abgelehnt
    with pytest.raises(NetworkError):
        fetch("http://127.0.0.1:9/")


def test_decode_fallbacks():
    assert decode_html("Grüße".encode("utf-8"), None) == ("Grüße", "utf-8")
    assert decode_html("Grüße".encode("cp1252"), None)[0] == "Grüße"   # kaputtes UTF-8 → cp1252
    assert decode_html(b"x", "unknown-charset")[1] == "utf-8"


@pytest.mark.parametrize("exc,status", [
    (InvalidUrlError("x"), CrawlStatus.INVALID_URL),
    (FetchTimeout("x"), CrawlStatus.TIMEOUT),
    (HttpClientError("x", status=403), CrawlStatus.BLOCKED),
    (HttpClientError("x", status=404), CrawlStatus.HTTP_ERROR),
    (ServerError("x", status=500), CrawlStatus.HTTP_ERROR),
    (NetworkError("x"), CrawlStatus.HTTP_ERROR),
    (ContentTooLarge("x"), CrawlStatus.FAILED),
    (KeyError("bug"), CrawlStatus.FAILED),
])
def test_status_for_error(exc, status):
    result, reason = status_for_error(exc)
    assert result is status and reason
