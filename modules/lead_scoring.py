"""Modul E: Lead-Scoring.

Wenige, konkrete, belegte Probleme zählen – nicht die Länge der Fehlerliste.

Belastbar ("eligible") ist ein Befund nur, wenn
- er gemessen oder direkt beobachtet ist und `confidence` ≥ 0,6, oder
- er ein KI-Rechtschreib-/Grammatikbefund mit exaktem Zitat und `confidence` ≥ 0,7 ist, oder
- er eine andere KI-Einschätzung mit `confidence` ≥ 0,75 ist (selten; nach den ersten echten
  Ergebnissen sind diese Einschätzungen deutlich schwächer).

Wert eines Befunds = Schwere × geschäftliche Relevanz × Belegqualität × Sicherheit
                     × Passung zum eigenen Angebot (niche.toml [angebot]).
Score = 100 × min(1, bester + ½ × zweitbester) × (0,5 + 0,5 × Nischenpassung) × Analysequalität.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.niche import NicheProfile
from models.schemas import (
    AnalysisReport, EvidenceType, Finding, FindingCategory, LeadScore, Severity,
)

SEVERITY_W = {Severity.LOW: 0.35, Severity.MEDIUM: 0.65, Severity.HIGH: 1.0}
RELEVANCE_W = {Severity.LOW: 0.4, Severity.MEDIUM: 0.7, Severity.HIGH: 1.0}
EVIDENCE_W = {EvidenceType.MEASURED: 1.0, EvidenceType.DIRECTLY_OBSERVED: 0.9, EvidenceType.AI_INTERPRETATION: 0.6}
MIN_ANALYSIS_QUALITY = 0.6
MAX_PITCH_FINDINGS = 2
# Befunde, die als Gesprächsanlass nicht taugen, auch wenn sie stimmen.
NOT_PITCHABLE = {"impressum_link_not_found", "html_very_large", "slow_server_response"}


@dataclass
class ScoringInput:
    report: AnalysisReport | None
    website_kind: str | None            # own | directory | none
    niche_fit: float | None
    recipient_email: str | None
    may_contact: bool = True
    may_contact_reason: str = ""


@dataclass
class ScoringResult:
    score: LeadScore
    pitch_findings: list[Finding] = field(default_factory=list)
    eligible: list[Finding] = field(default_factory=list)


def is_eligible(f: Finding) -> bool:
    if f.issue_id in NOT_PITCHABLE:
        return False
    if f.severity is Severity.LOW and f.business_relevance is Severity.LOW and f.category not in (
            FindingCategory.SPELLING, FindingCategory.GRAMMAR):
        return False        # z. B. Telefonnummer nur im Impressum: stimmt, ist aber kein Gesprächsanlass
    if f.evidence_type is not EvidenceType.AI_INTERPRETATION:
        return f.confidence >= 0.6
    if f.category in (FindingCategory.SPELLING, FindingCategory.GRAMMAR):
        return f.confidence >= 0.7 and (f.measurement or {}).get("quote_match") == 1.0
    return f.confidence >= 0.75


def finding_value(f: Finding, niche: NicheProfile | None) -> float:
    fit = niche.offer_fit(f.category) if niche else 0.5
    return SEVERITY_W[f.severity] * RELEVANCE_W[f.business_relevance] * EVIDENCE_W[f.evidence_type] * f.confidence * fit


def score_lead(data: ScoringInput, niche: NicheProfile | None = None) -> ScoringResult:
    reasons: list[str] = []
    if data.website_kind == "none":
        reasons.append("keine Website – Kandidat für Neubau-Ansprache (nicht per Website-Hinweis)")
    elif data.website_kind == "directory":
        reasons.append("nur Verzeichnis-/Social-Profil – Kandidat für Neubau-Ansprache")
    if not data.may_contact:
        reasons.append(f"Kontakt nicht erlaubt: {data.may_contact_reason}")
    if data.niche_fit is not None and data.niche_fit == 0:
        reasons.append("außerhalb der Zielnische")
    if not data.recipient_email:
        reasons.append("keine geschäftliche E-Mail-Adresse gefunden")
    report = data.report
    if report is None:
        reasons.append("keine Analyse vorhanden")
    elif report.analysis_quality < MIN_ANALYSIS_QUALITY:
        reasons.append(f"Analysequalität zu niedrig ({report.analysis_quality:.2f})")

    eligible = sorted((f for f in (report.findings if report else []) if is_eligible(f)),
                      key=lambda f: finding_value(f, niche), reverse=True)
    if report is not None and not eligible:
        reasons.append("kein belastbarer Befund")

    if reasons:
        return ScoringResult(LeadScore(score=0, disqualified=True, disqualification_reasons=reasons), [], eligible)

    values = [finding_value(f, niche) for f in eligible]
    base = min(1.0, values[0] + 0.5 * (values[1] if len(values) > 1 else 0.0))
    niche_factor = 0.5 + 0.5 * (data.niche_fit if data.niche_fit is not None else 0.5)
    score = round(100 * base * niche_factor * report.analysis_quality, 1)
    components = {"best_finding": round(values[0], 3), "second_finding": round(values[1], 3) if len(values) > 1 else 0.0,
                  "niche_factor": niche_factor, "analysis_quality": report.analysis_quality,
                  "eligible_findings": float(len(eligible))}
    return ScoringResult(LeadScore(score=score, components=components), eligible[:MAX_PITCH_FINDINGS], eligible)
