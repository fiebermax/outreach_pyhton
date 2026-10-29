"""Schritte SCORE und DRAFT: bewerten, und nur für qualifizierte Leads einen Entwurf speichern.

Versand gehört nicht hierher – er läuft nur per Einzelbefehl (`main.py send`).
"""

from __future__ import annotations

import logging

from config import Settings
from core.errors import SendNotAllowed
from core.niche import NicheProfile
from modules.database import Database, Step, StepStatus
from modules.lead_scoring import ScoringInput, score_lead
from modules.mailer import save_draft
from modules.pitch import Sender, build_draft, lint_draft
from services.crawl_service import StepReport

log = logging.getLogger(__name__)


def choose_recipient(db: Database, company_id: int) -> str | None:
    """Geschäftliche Adresse bevorzugen: gleiche Domain wie die Website, dann verlinkt, dann sonstige."""
    company = db.get_company(company_id)
    candidates = [r["value"] for r in db.get_contacts(company_id, "EMAIL")]
    if company["email"]:
        candidates.insert(0, company["email"])
    if not candidates:
        return None
    domain = company["domain"] or ""
    origins = {r["value"]: r["origin"] for r in db.get_contacts(company_id, "EMAIL")}
    return sorted(dict.fromkeys(candidates),
                  key=lambda e: (not (domain and e.endswith("@" + domain)), origins.get(e) != "LINK"))[0]


def run_scoring_and_drafts(db: Database, settings: Settings, niche: NicheProfile, sender: Sender, *,
                           company_ids: list[int] | None = None) -> tuple[StepReport, StepReport]:
    scored, drafted = StepReport(), StepReport()
    ids = [i for i in db.companies_needing(Step.SCORE) if db.is_step_done(i, Step.CRAWL)]
    if company_ids is not None:
        ids = [i for i in ids if i in set(company_ids)]
    for cid in ids:
        company = db.get_company(cid)
        name = company["name"]
        db.start_step(cid, Step.SCORE)
        try:
            report_id = db.latest_report_id(cid) if db.step_status(cid, Step.ANALYZE) is StepStatus.DONE else None
            report = db.get_report(report_id) if report_id else None
            recipient = choose_recipient(db, cid)
            decision = db.may_contact(cid, email=recipient, cooldown_days=settings.contact_cooldown_days)
            result = score_lead(ScoringInput(report, company["website_kind"], company["niche_fit"], recipient,
                                             decision.allowed, decision.reason), niche)
            db.save_score(cid, report_id, result.score)
            db.finish_step(cid, Step.SCORE, StepStatus.DONE)
            scored.done += 1
            if result.score.disqualified:
                scored.details.append(f"{name}: ausgeschlossen – {'; '.join(result.score.disqualification_reasons)}")
                db.start_step(cid, Step.DRAFT)
                db.finish_step(cid, Step.DRAFT, StepStatus.SKIPPED, "; ".join(result.score.disqualification_reasons))
                drafted.skipped += 1
                continue
            scored.details.append(f"{name}: Score {result.score.score}")

            db.start_step(cid, Step.DRAFT)
            draft = build_draft(result.pitch_findings, recipient, sender)
            if draft is None:
                db.finish_step(cid, Step.DRAFT, StepStatus.SKIPPED, "kein Befund mit Textbaustein")
                drafted.skipped += 1
                continue
            problems = lint_draft(draft, result.pitch_findings, sender)
            if problems:
                db.finish_step(cid, Step.DRAFT, StepStatus.FAILED, "Textprüfung: " + "; ".join(problems))
                drafted.failed += 1
                drafted.details.append(f"{name}: Entwurf verworfen ({problems[0]})")
                continue
            draft_id = save_draft(db, cid, draft, settings)
            db.finish_step(cid, Step.DRAFT, StepStatus.DONE)
            drafted.done += 1
            drafted.details.append(f"{name}: Entwurf #{draft_id} „{draft.subject}“")
        except SendNotAllowed as exc:
            db.finish_step(cid, Step.DRAFT, StepStatus.SKIPPED, str(exc))
            drafted.skipped += 1
            drafted.details.append(f"{name}: {exc}")
        except Exception as exc:
            log.exception("Scoring/Entwurf fehlgeschlagen", extra={"lead_id": cid, "step": "score"})
            db.log_error(cid, Step.SCORE, exc, temporary=False)
            step = Step.DRAFT if db.step_status(cid, Step.DRAFT) is StepStatus.RUNNING else Step.SCORE
            db.finish_step(cid, step, StepStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500])
            scored.failed += 1
            scored.details.append(f"{name}: Fehler {type(exc).__name__}")
    return scored, drafted
