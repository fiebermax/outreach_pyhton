"""Schritt ANALYZE: Prüfungen + optionale KI-Analyse für alle gecrawlten Unternehmen.

Unverwertbare Crawls werden nicht analysiert (SKIPPED mit Grund) – ein unzureichender Crawl
wird nie als Analyse gespeichert (die Datenbank verweigert das zusätzlich).
"""

from __future__ import annotations

import logging

import httpx

from config import Settings
from modules.analyzer import analyze
from modules.database import Database, Step, StepStatus
from modules.llm import LLMClient
from modules.website_checks import CheckContext, probe_broken_links, probe_http_redirect
from services.crawl_service import StepReport

log = logging.getLogger(__name__)


async def run_analysis(db: Database, settings: Settings, llm: LLMClient | None, *,
                       company_ids: list[int] | None = None, retry_failed: bool = False,
                       network_probes: bool = True) -> StepReport:
    report = StepReport()
    ids = [i for i in db.companies_needing(Step.ANALYZE, include_failed=retry_failed)
           if db.step_status(i, Step.CRAWL) is StepStatus.DONE]
    if company_ids is not None:
        ids = [i for i in ids if i in set(company_ids)]
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; WebsiteCheck/0.1)"}) as client:
        for cid in ids:
            name = db.get_company(cid)["name"]
            db.start_step(cid, Step.ANALYZE)
            try:
                latest = db.latest_crawl(cid)
                if latest is None or not latest[1].status.is_usable:
                    status = latest[1].status.value if latest else "kein Crawl"
                    db.finish_step(cid, Step.ANALYZE, StepStatus.SKIPPED, f"Crawl nicht verwertbar: {status}")
                    report.skipped += 1
                    report.details.append(f"{name}: nicht analysiert ({status})")
                    continue
                run_id, result = latest
                ctx = CheckContext()
                if network_probes:
                    ctx.probes["http_redirect"] = await probe_http_redirect(result, client)
                    ctx.probes["broken_links"] = await probe_broken_links(result, client)
                analysis = analyze(result, llm, ctx)
                db.save_report(cid, run_id, analysis)
                db.finish_step(cid, Step.ANALYZE, StepStatus.DONE)
                report.done += 1
                report.details.append(f"{name}: {len(analysis.findings)} Befunde, Qualität {analysis.analysis_quality}"
                                      + (f", {analysis.discarded_findings} KI-Befunde ohne Beleg verworfen"
                                         if analysis.discarded_findings else ""))
            except Exception as exc:
                log.exception("Analyse fehlgeschlagen", extra={"lead_id": cid, "step": "analyze"})
                db.log_error(cid, Step.ANALYZE, exc, temporary=True)
                db.finish_step(cid, Step.ANALYZE, StepStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500])
                report.failed += 1
                report.details.append(f"{name}: Fehler {type(exc).__name__}")
    return report
