"""Stufe 1: HTTP-Abruf einer einzelnen Seite.

Liefert entweder ein `FetchResponse` mit dekodiertem HTML oder wirft einen
typisierten Fehler aus `core.errors`. Was daraus für den Crawl-Status folgt,
entscheidet `status_for_error()`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx

from config import Settings
from core.errors import (
    BlockedError, ContentTooLarge, FetchTimeout, HttpClientError, InvalidUrlError, NetworkError, OutreachError,
    RateLimited, ServerError, SslCertificateError, TemporaryError, TooManyRedirects, UnsupportedContentType,
    error_for_http_status,
)
from core.normalize import normalize_url
from core.retry import retry_async
from models.schemas import CrawlStatus

# Ehrlicher User-Agent: Wir geben uns nicht als Browser aus.
USER_AGENT = "Mozilla/5.0 (compatible; WebsiteCheck/0.1; lokale Website-Analyse)"

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


@dataclass(frozen=True)
class FetchLimits:
    timeout_seconds: float = 15.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    retries: int = 2
    allow_local: bool = False          # nur für Tests mit lokalem Server
    user_agent: str = USER_AGENT

    @classmethod
    def from_settings(cls, settings: Settings) -> FetchLimits:
        return cls(timeout_seconds=settings.crawl_timeout_seconds, max_bytes=settings.crawl_max_bytes,
                   retries=settings.crawl_retries)


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status: int
    content_type: str
    html: str
    size_bytes: int
    elapsed_seconds: float
    redirects: list[tuple[int, str]] = field(default_factory=list)   # (Status, URL) je Sprung
    encoding: str = "utf-8"


def decode_html(body: bytes, header_charset: str | None) -> tuple[str, str]:
    """HTTP-Header-Charset → <meta charset> → UTF-8 → cp1252 (verlustfrei für Bytes)."""
    candidates = []
    if header_charset:
        candidates.append(header_charset)
    meta = _META_CHARSET.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for enc in candidates:
        try:
            return body.decode(enc), enc.lower()
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("cp1252", errors="replace"), "cp1252"


def _looks_like_html(prefix: bytes) -> bool:
    head = prefix.lstrip()[:200].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in head


def _charset_from_header(content_type: str) -> str | None:
    match = re.search(r"charset=([\w\-]+)", content_type, re.I)
    return match.group(1) if match else None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


async def _fetch_once(client: httpx.AsyncClient, url: str, limits: FetchLimits) -> FetchResponse:
    started = time.monotonic()
    try:
        async with client.stream("GET", url) as response:
            redirects = [(r.status_code, str(r.url)) for r in response.history]
            final_url = str(response.url)

            error = error_for_http_status(response.status_code, final_url, _retry_after(response))
            if error is not None:
                raise error

            content_type = response.headers.get("content-type", "").lower()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limits.max_bytes:
                raise ContentTooLarge(f"{declared} Bytes angekündigt, Limit {limits.max_bytes}")
            if content_type and not content_type.startswith(_HTML_TYPES):
                raise UnsupportedContentType(f"Content-Type {content_type.split(';')[0]} ist kein HTML")

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limits.max_bytes:
                    raise ContentTooLarge(f"mehr als {limits.max_bytes} Bytes")
                chunks.append(chunk)
            body = b"".join(chunks)

            if not content_type and not _looks_like_html(body[:1024]):
                raise UnsupportedContentType("kein Content-Type und Inhalt sieht nicht nach HTML aus")

            html, encoding = decode_html(body, _charset_from_header(content_type))
            return FetchResponse(
                url=url, final_url=final_url, status=response.status_code,
                content_type=content_type or "text/html (erkannt)", html=html, size_bytes=size,
                elapsed_seconds=time.monotonic() - started, redirects=redirects, encoding=encoding,
            )
    except httpx.TooManyRedirects as exc:
        raise TooManyRedirects(f"mehr als {limits.max_redirects} Weiterleitungen: {url}") from exc
    except httpx.TimeoutException as exc:
        raise FetchTimeout(f"Zeitlimit {limits.timeout_seconds}s überschritten: {url}") from exc
    except httpx.ConnectError as exc:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
            raise SslCertificateError(f"SSL-Zertifikat ungültig: {text[:200]}") from exc
        raise NetworkError(f"Verbindung fehlgeschlagen: {text[:200]}") from exc
    except httpx.RequestError as exc:
        raise NetworkError(f"Netzwerkfehler: {type(exc).__name__}: {str(exc)[:200]}") from exc


def make_client(limits: FetchLimits) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=limits.max_redirects,
        timeout=httpx.Timeout(limits.timeout_seconds),
        headers={"User-Agent": limits.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                 "Accept-Language": "de-DE,de;q=0.9,en;q=0.5"},
    )


async def fetch_http(
    url: str,
    limits: FetchLimits = FetchLimits(),
    client: httpx.AsyncClient | None = None,
    sleep=None,
) -> FetchResponse:
    """Ruft eine Seite ab. Temporäre Fehler werden begrenzt wiederholt, alle anderen sofort gemeldet."""
    url = normalize_url(url, allow_local=limits.allow_local)       # wirft InvalidUrlError
    own_client = client is None
    client = client or make_client(limits)
    kwargs = {"sleep": sleep} if sleep else {}
    try:
        return await retry_async(lambda: _fetch_once(client, url, limits), attempts=limits.retries + 1,
                                 base_delay=1.0, max_delay=10.0, label=url, **kwargs)
    finally:
        if own_client:
            await client.aclose()


def status_for_error(exc: BaseException) -> tuple[CrawlStatus, str]:
    """Übersetzt einen Abruffehler in Crawl-Status + Begründung."""
    reason = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, InvalidUrlError):
        return CrawlStatus.INVALID_URL, reason
    if isinstance(exc, FetchTimeout):
        return CrawlStatus.TIMEOUT, reason
    if isinstance(exc, BlockedError):
        return CrawlStatus.BLOCKED, reason
    if isinstance(exc, HttpClientError) and exc.status in (401, 403):
        return CrawlStatus.BLOCKED, reason
    if isinstance(exc, (HttpClientError, ServerError, RateLimited, TooManyRedirects, SslCertificateError,
                        NetworkError)):
        # Website von außen nicht sauber erreichbar – kein Fehler des Scrapers.
        return CrawlStatus.HTTP_ERROR, reason
    if isinstance(exc, (OutreachError, TemporaryError)):
        return CrawlStatus.FAILED, reason
    return CrawlStatus.FAILED, f"unerwartet: {reason}"
