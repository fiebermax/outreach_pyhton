"""Schritt CRAWL für alle offenen Unternehmen.

- Nur Unternehmen mit eigener Website; Verzeichnisprofil/keine Website → SKIPPED mit Grund.
- Vorübergehende Ursachen (TIMEOUT, FAILED) → FAILED, damit `--retry-failed` sie erneut versucht.
- Dauerhafte Ursachen (BLOCKED, INVALID_URL, HTTP 4xx) und Erfolg → DONE; der Crawl ist gespeichert.
- Ein Fehler bei einem Unternehmen bricht den Lauf nie ab.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from config import Settings
from models.schemas import ContactKind, CrawlStatus
from modules.browser import BrowserLimits, BrowserRenderer, ConsentPolicy
from modules.database import Database, Step, StepStatus
from modules.html_store import HtmlStore
from modules.scraper import CrawlConfig, SiteCrawler

log = logging.getLogger(__name__)
RETRYABLE = {CrawlStatus.TIMEOUT, CrawlStatus.FAILED}


@dataclass
class StepReport:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


async def run_crawls(db: Database, settings: Settings, *, company_ids: list[int] | None = None,
                     retry_failed: bool = False, use_browser: bool = True, allow_local: bool = False) -> StepReport:
    report = StepReport()
    ids = db.companies_needing(Step.CRAWL, include_failed=retry_failed)
    if company_ids is not None:
        ids = [i for i in ids if i in set(company_ids)]
    config = CrawlConfig.from_settings(settings)
    if allow_local:
        from dataclasses import replace
        config = replace(config, fetch=replace(config.fetch, allow_local=True))
    store = HtmlStore(settings.data_dir / "html")
    sem = asyncio.Semaphore(settings.crawl_concurrency)

    async def one(cid: int, renderer: BrowserRenderer | None) -> None:
        company = db.get_company(cid)
        if company["website_kind"] != "own" or not company["website"]:
            db.start_step(cid, Step.CRAWL)
            reason = {"directory": "nur Verzeichnis-/Social-Profil", "none": "keine Website"}.get(
                company["website_kind"], "keine Website")
            db.finish_step(cid, Step.CRAWL, StepStatus.SKIPPED, reason)
            report.skipped += 1
            report.details.append(f"{company['name']}: übersprungen ({reason})")
            return
        async with sem:
            db.start_step(cid, Step.CRAWL)
            try:
                result, _ = await SiteCrawler(config, renderer=renderer, html_store=store).crawl_site(company["website"])
                db.save_crawl(cid, result)
                contacts = result.contacts(ContactKind.EMAIL) + result.contacts(ContactKind.PHONE)
                db.add_contacts(cid, contacts)
                status = StepStatus.FAILED if result.status in RETRYABLE else StepStatus.DONE
                db.finish_step(cid, Step.CRAWL, status, result.failure_reason)
                report.failed += status is StepStatus.FAILED
                report.done += status is StepStatus.DONE
                report.details.append(f"{company['name']}: {result.status.value}"
                                      + (f" – {result.failure_reason[:80]}" if result.failure_reason else ""))
            except Exception as exc:  # ein Unternehmen darf den Lauf nie abbrechen
                log.exception("Crawl fehlgeschlagen", extra={"lead_id": cid, "step": "crawl"})
                db.log_error(cid, Step.CRAWL, exc, temporary=True)
                db.finish_step(cid, Step.CRAWL, StepStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500])
                report.failed += 1
                report.details.append(f"{company['name']}: Fehler {type(exc).__name__}")

    if use_browser:
        limits = BrowserLimits(allow_local=allow_local, consent_policy=ConsentPolicy(settings.consent_policy))
        async with BrowserRenderer(limits) as renderer:
            await asyncio.gather(*(one(cid, renderer) for cid in ids))
    else:
        await asyncio.gather(*(one(cid, None) for cid in ids))
    return report

