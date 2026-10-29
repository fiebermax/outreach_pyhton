"""Paket 3.14: Website-Crawl über alle Stufen.

Pro Seite:
  1. HTTP-Abruf (fetcher)          – Fehler → Status mit Grund, Website-Problem vs. Scraper-Problem
  2./3. Extraktion (content_extractor) + Qualitätskontrolle (quality_control)
  4. Browser (browser), nur wenn die Qualitätskontrolle es empfiehlt – oder auf der Startseite,
     weil nur der Browser das Handy-Layout messen kann. Das bessere Ergebnis gewinnt.
  5. Kein brauchbarer Text → kontrollierter Fehlerstatus, keine Endlosschleifen.

Pro Website: Startseite, dann relevante Unterseiten (page_selection) bis zu den
Grenzen für Seitenzahl, Tiefe und Laufzeit. Blockierte oder nicht erreichbare
Websites werden nicht weiter abgefragt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from config import Settings
from core.errors import InvalidUrlError, OutreachError
from core.normalize import normalize_url
from models.schemas import CrawlResult, CrawlStatus, ExtractionMethod, PageResult, utcnow
from modules.browser import BrowserRenderer, RenderResult
from modules.content_extractor import ExtractedContent, Link, extract_content
from modules.fetcher import FetchLimits, fetch_http, make_client, status_for_error
from modules.html_store import HtmlStore
from modules.page_selection import select_subpages
from modules.quality_control import MIN_WORDS_ANY, QualityAssessment, assess_page, assess_site, is_better

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrawlConfig:
    fetch: FetchLimits = FetchLimits()
    max_pages: int = 6                 # inkl. Startseite
    max_depth: int = 2
    max_seconds: float = 90.0
    min_quality: float = 0.5
    render_homepage: bool = True       # Layout-Messung braucht den Browser

    @classmethod
    def from_settings(cls, settings: Settings) -> CrawlConfig:
        return cls(fetch=FetchLimits.from_settings(settings), max_pages=settings.crawl_max_pages,
                   max_depth=settings.crawl_max_depth, max_seconds=settings.crawl_max_seconds_per_site,
                   min_quality=settings.min_extraction_quality)


@dataclass
class PageOutcome:
    page: PageResult
    links: list[Link] = field(default_factory=list)
    content: ExtractedContent | None = None
    assessment: QualityAssessment | None = None
    render: RenderResult | None = None
    notes: list[str] = field(default_factory=list)      # nachvollziehbarer Ablauf


def _assess(content: ExtractedContent, rendered: bool, min_quality: float) -> QualityAssessment:
    return assess_page(cleaned_text=content.cleaned_text, title=content.title,
                       heading_texts=[h.text for h in content.headings], js_signals=content.js_signals,
                       consent_banners=content.consent_banners, rendered=rendered, min_quality=min_quality)


def _enough_text(html: str) -> bool:
    return len(extract_content(html, "https://x.invalid/").cleaned_text.split()) >= MIN_WORDS_ANY


def _page_result(url: str, content: ExtractedContent, assessment: QualityAssessment, *, final_url: str,
                 http_status: int | None, method: ExtractionMethod, html_ref: str | None,
                 render: RenderResult | None, response=None) -> PageResult:
    usable = assessment.status.is_usable
    internal = list(dict.fromkeys(l.url.split("#")[0] for l in content.links if l.internal))[:150]
    return PageResult(
        has_viewport_meta=content.has_viewport_meta, has_h1=any(h.level == 1 for h in content.headings),
        internal_links=internal,
        redirect_chain=[f"{status} {u}" for status, u in response.redirects] if response else [],
        response_seconds=round(response.elapsed_seconds, 2) if response else None,
        html_bytes=response.size_bytes if response else None,
        word_count=len(content.visible_text.split()),
        url=url, final_url=final_url, http_status=http_status, page_title=content.title,
        meta_description=content.meta_description, raw_html_reference=html_ref,
        cleaned_text=content.cleaned_text if usable else "", headings=content.headings,
        emails=content.emails, phone_numbers=content.phone_numbers, contact_links=content.contact_links,
        footer_text=content.footer_text, copyright_year=content.copyright_year,
        copyright_text=content.copyright_text,
        viewport_width=render.viewport_width if render else None,
        document_width=render.document_width if render else None,
        horizontal_overflow=render.horizontal_overflow if render else None,
        overflow_culprits=render.overflow_culprits if render else [],
        extraction_method=method if usable else ExtractionMethod.NONE,
        extraction_quality=assessment.score, crawl_status=assessment.status,
        failure_reason=assessment.failure_reason,
    )


class SiteCrawler:
    def __init__(self, config: CrawlConfig = CrawlConfig(), *, renderer: BrowserRenderer | None = None,
                 html_store: HtmlStore | None = None, client: httpx.AsyncClient | None = None):
        self.config = config
        self.renderer = renderer
        self.html_store = html_store
        self.client = client

    async def crawl_page(self, url: str, *, is_home: bool) -> PageOutcome:
        cfg = self.config
        notes: list[str] = []
        # --- Stufe 1 ---
        try:
            response = await fetch_http(url, cfg.fetch, client=self.client)
        except OutreachError as exc:
            status, reason = status_for_error(exc)
            notes.append(f"HTTP: {reason}")
            # Blockiert/ungültig/nicht erreichbar: der Browser würde nur dieselbe Sperre treffen.
            return PageOutcome(PageResult(url=url, crawl_status=status, failure_reason=reason[:500]), notes=notes)
        notes.append(f"HTTP {response.status}, {response.size_bytes} B, {response.elapsed_seconds:.1f}s")

        # --- Stufen 2/3 ---
        content = extract_content(response.html, response.final_url)
        assessment = _assess(content, rendered=False, min_quality=cfg.min_quality)
        method, html, final_url, http_status = content.method, response.html, response.final_url, response.status
        notes.append(f"Extraktion {content.method.value}: {assessment.status.value} ({assessment.reasons[0]})")

        # --- Stufe 4 ---
        render: RenderResult | None = None
        want_browser = assessment.needs_rendering or (is_home and cfg.render_homepage)
        if assessment.status is CrawlStatus.BLOCKED:
            want_browser = False            # Schutzmechanismen werden nicht umgangen
        if want_browser and self.renderer is not None:
            try:
                render = await self.renderer.render(response.final_url, content_ok=_enough_text)
                r_content = extract_content(render.html, render.final_url)
                r_assessment = _assess(r_content, rendered=True, min_quality=cfg.min_quality)
                notes.append(f"Browser: {r_assessment.status.value} ({r_assessment.reasons[0]}), "
                             f"gewartet: {', '.join(render.waited_for)}")
                if render.consent and render.consent.detected:
                    notes.append(f"Cookie-Banner: Klicks {render.consent.clicked or 'keine'}")
                if is_better(r_assessment, assessment):
                    content, assessment, method = r_content, r_assessment, ExtractionMethod.PLAYWRIGHT
                    html, final_url, http_status = render.html, render.final_url, render.status or http_status
                elif assessment.status.is_usable and not r_assessment.status.is_usable:
                    notes.append("Browser-Ergebnis schlechter, HTTP-Ergebnis behalten")
                elif not assessment.status.is_usable:
                    assessment = r_assessment       # beide unbrauchbar: endgültiger Status nach Rendering
            except OutreachError as exc:
                notes.append(f"Browser: {status_for_error(exc)[1]}")
        elif want_browser:
            notes.append("Browser nicht verfügbar – Stufe 4 übersprungen")

        html_ref = self.html_store.save(html) if self.html_store else None
        page = _page_result(url, content, assessment, final_url=final_url, http_status=http_status,
                            method=method, html_ref=html_ref, render=render, response=response)
        return PageOutcome(page, links=content.links, content=content, assessment=assessment, render=render,
                           notes=notes)

    async def _crawl_page_within(self, url: str, *, is_home: bool, deadline: float) -> PageOutcome | None:
        """crawl_page mit harter Obergrenze; None, wenn kein Budget mehr übrig ist."""
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return None
        try:
            return await asyncio.wait_for(self.crawl_page(url, is_home=is_home), timeout=remaining)
        except asyncio.TimeoutError:
            reason = f"Zeitlimit der Website ({self.config.max_seconds:.0f}s) während dieser Seite erreicht"
            return PageOutcome(PageResult(url=url, crawl_status=CrawlStatus.TIMEOUT, failure_reason=reason),
                               notes=[reason])

    async def crawl_site(self, url: str) -> tuple[CrawlResult, list[PageOutcome]]:
        cfg = self.config
        started_at, started = utcnow(), time.monotonic()
        try:
            home_url = normalize_url(url, allow_local=cfg.fetch.allow_local)
        except InvalidUrlError as exc:
            result = CrawlResult(site_url=url or "-", status=CrawlStatus.INVALID_URL, failure_reason=str(exc),
                                 started_at=started_at, finished_at=utcnow())
            return result, []

        own_client = self.client is None
        if own_client:
            self.client = make_client(cfg.fetch)
        try:
            deadline = started + cfg.max_seconds
            outcomes = [await self._crawl_page_within(home_url, is_home=True, deadline=deadline)]
            home = outcomes[0].page
            if not home.crawl_status.is_site_inaccessible:
                outcomes += await self._crawl_subpages(home_url, outcomes, deadline)
        finally:
            if own_client:
                await self.client.aclose()
                self.client = None

        site = assess_site([o.page for o in outcomes])
        result = CrawlResult(site_url=home_url, status=site.status, pages=[o.page for o in outcomes],
                             failure_reason=site.failure_reason, started_at=started_at, finished_at=utcnow())
        log.info("Crawl %s: %s, %d Seite(n), %.1fs", home_url, site.status.value, len(outcomes),
                 time.monotonic() - started, extra={"url": home_url, "status": site.status.value})
        return result, outcomes

    async def _crawl_subpages(self, home_url: str, outcomes: list[PageOutcome], deadline: float) -> list[PageOutcome]:
        cfg = self.config
        crawled = {home_url}
        found: set[str] = set()
        new: list[PageOutcome] = []
        frontier = [(o, 0) for o in outcomes]
        while frontier and len(outcomes) + len(new) < cfg.max_pages:
            source, depth = frontier.pop(0)
            if depth >= cfg.max_depth:
                continue
            slots = cfg.max_pages - len(outcomes) - len(new)
            for selected in select_subpages(source.links, home_url, max_pages=slots, already=crawled,
                                            found_categories=found):
                crawled.add(selected.url)
                found.add(selected.category)
                outcome = await self._crawl_page_within(selected.url, is_home=False, deadline=deadline)
                if outcome is None:
                    log.warning("Zeitlimit %ss für %s erreicht", cfg.max_seconds, home_url)
                    return new
                outcome.notes.insert(0, f"Kategorie {selected.category}: {selected.reason}")
                new.append(outcome)
                if outcome.page.crawl_status in (CrawlStatus.BLOCKED, CrawlStatus.TIMEOUT):
                    return new                  # Sperre oder Zeitbudget gilt für die ganze Website
                frontier.append((outcome, depth + 1))
        return new
