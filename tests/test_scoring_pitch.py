import pytest

from core.niche import load_niche
from models.schemas import AnalysisReport, EvidenceType, Finding, FindingCategory, Severity
from modules.lead_scoring import ScoringInput, is_eligible, score_lead
from modules.pitch import FORBIDDEN, Sender, build_draft, lint_draft

URL = "https://nh-bauunternehmen.de/"
SENDER = Sender(email="kontakt@beispiel-webdesign.de")


def f(issue_id="phone_not_clickable", category=FindingCategory.CONTACT, evidence="„Telefon: 08225 12345“ (kein tel:-Link)",
      evidence_type=EvidenceType.DIRECTLY_OBSERVED, confidence=0.85, severity=Severity.MEDIUM, relevance=Severity.MEDIUM,
      measurement=None, url=URL):
    return Finding(issue_id=issue_id, category=category, severity=severity, description="Beschreibung des Befunds.",
                   evidence=evidence, source_url=url, evidence_type=evidence_type, confidence=confidence,
                   business_relevance=relevance, measurement=measurement)


TYPO = f("ai_spelling_1", FindingCategory.SPELLING, "„Innen-AuBenputz“ → „Innen-Außenputz“",
         EvidenceType.AI_INTERPRETATION, 0.85, relevance=Severity.LOW, measurement={"quote_match": 1.0})
WEAK_AI = f("ai_clarity_2", FindingCategory.CLARITY, "„Sonstiges“", EvidenceType.AI_INTERPRETATION, 0.5)


def inp(*findings, quality=1.0, **kw):
    base = dict(report=AnalysisReport(site_url=URL, findings=list(findings), analysis_quality=quality),
                website_kind="own", niche_fit=1.0, recipient_email="info@nh-bauunternehmen.de")
    return ScoringInput(**(base | kw))


# --- Scoring ---

def test_eligibility_rules():
    assert is_eligible(f()) and is_eligible(TYPO)
    assert not is_eligible(WEAK_AI)                                       # schwache KI-Einschätzung
    assert not is_eligible(f(confidence=0.55))                            # zu unsicher
    assert not is_eligible(f("impressum_link_not_found", FindingCategory.LEGAL))   # kein Gesprächsanlass


@pytest.mark.parametrize("kw,reason", [
    ({"website_kind": "none"}, "Neubau"), ({"website_kind": "directory"}, "Neubau"),
    ({"recipient_email": None}, "E-Mail"), ({"niche_fit": 0.0}, "Zielnische"),
    ({"may_contact": False, "may_contact_reason": "Sperrfrist"}, "Sperrfrist"),
    ({"quality": 0.5}, "Analysequalität"), ({"report": None}, "keine Analyse"),
])
def test_disqualification(kw, reason):
    quality = kw.pop("quality", 1.0)
    result = score_lead(inp(f(), quality=quality, **kw))
    assert result.score.disqualified and result.score.score == 0
    assert any(reason in r for r in result.score.disqualification_reasons)


def test_no_invented_reasons_when_only_weak_findings():
    result = score_lead(inp(WEAK_AI))
    assert result.score.disqualified and "kein belastbarer Befund" in result.score.disqualification_reasons


def test_score_prefers_few_strong_findings_and_niche():
    niche = load_niche()
    one = score_lead(inp(f()), niche).score.score
    two = score_lead(inp(f(), TYPO), niche).score.score
    many_weak = score_lead(inp(f(), *[WEAK_AI] * 10), niche).score.score
    assert 0 < one < two <= 100 and many_weak == one                     # Masse schwacher Befunde zählt nicht
    assert score_lead(inp(f(), niche_fit=0.6), niche).score.score < one


def test_pitch_findings_limited_to_two_best():
    mobile = f("horizontal_overflow_mobile", FindingCategory.MOBILE, "Bei 375px ...", EvidenceType.MEASURED, 0.8,
               measurement={"viewport_width": 375, "document_width": 640, "culprit": "div.hero"})
    result = score_lead(inp(TYPO, f(), mobile), load_niche())
    assert [x.issue_id for x in result.pitch_findings] == ["horizontal_overflow_mobile", "phone_not_clickable"]


# --- Anti-Pitch ---

def test_draft_structure_and_lint():
    draft = build_draft([f(), TYPO], "info@nh-bauunternehmen.de", SENDER)
    body = draft.body
    assert body.startswith("Guten Tag,")
    assert "nh-bauunternehmen.de" in body and "Telefon: 08225 12345" in body
    assert "„Innen-AuBenputz“, gemeint ist vermutlich „Innen-Außenputz“" in body
    assert "Webdesigner aus Jettingen-Scheppach" in body and "keine weiteren Nachrichten" in body
    assert draft.finding_ids == ["phone_not_clickable", "ai_spelling_1"]
    assert lint_draft(draft, [f(), TYPO], SENDER) == []
    assert body.index("Telefon: 08225") < body.index("Webdesigner")      # Vorstellung erst am Ende


def test_no_draft_without_usable_block():
    assert build_draft([f("h1_missing", FindingCategory.METADATA, "kein <h1>")], "a@b.de", SENDER) is None
    assert build_draft([], "a@b.de", SENDER) is None


def test_lint_catches_violations():
    draft = build_draft([f()], "a@b.de", SENDER)
    bad = draft.model_copy(update={"body": draft.body + "\nGarantiert 30 % mehr Umsatz! Jetzt sofort!"})
    problems = lint_draft(bad, [f()], SENDER)
    assert any("garantiert" in p for p in problems) and any("Umsatz".lower() in p for p in problems)
    assert any("30" in p for p in problems) and any("Ausrufezeichen" in p for p in problems)


@pytest.mark.parametrize("finding", [
    f(),
    f("tel_link_malformed", evidence='href="tel:01743810360">0174 3810360"'),
    f("copyright_outdated", FindingCategory.MAINTENANCE, "© 2019", measurement={"copyright_year": 2019, "current_year": 2026}),
    f("no_https", FindingCategory.ACCESSIBILITY, "Endgültige Adresse: http://x.de/", EvidenceType.MEASURED,
      measurement={"final_url": "http://x.de/"}),
    f("homepage_almost_no_text", FindingCategory.CLARITY, "3 Wörter", EvidenceType.MEASURED, measurement={"words": 3}),
    TYPO,
])
def test_every_block_passes_lint(finding):
    draft = build_draft([finding], "info@x.de", SENDER)
    assert draft is not None and lint_draft(draft, [finding], SENDER) == []
    assert not any(w in draft.body.lower() for w in FORBIDDEN)


def test_low_relevance_finding_not_pitched():
    impressum_phone = f(severity=Severity.LOW, relevance=Severity.LOW, url=URL + "impressum")
    assert not is_eligible(impressum_phone)
    result = score_lead(inp(impressum_phone, TYPO), load_niche())
    assert [x.issue_id for x in result.pitch_findings] == ["ai_spelling_1"]


def test_sentence_start_keeps_case():
    draft = build_draft([f(url=URL + "kontakt")], "a@b.de", SENDER)
    assert "Auf der Seite /kontakt steht" in draft.body             # nicht "Auf der seite"


def test_drafts_contain_no_generic_ai_phrases():
    phrases = ["ich hoffe, diese nachricht", "in der heutigen digitalen", "zögern sie nicht", "spannend",
               "potenzial", "optimieren sie", "ihr online-auftritt", "digitale präsenz", "auf ein neues level"]
    for finding in [f(), TYPO, f("no_https", FindingCategory.ACCESSIBILITY, "http", EvidenceType.MEASURED,
                                  measurement={"final_url": "http://x.de/"})]:
        body = build_draft([finding], "a@b.de", SENDER).body.lower()
        assert not any(p in body for p in phrases), body


def test_lint_rejects_sweeping_claims():
    draft = build_draft([f()], "a@b.de", SENDER)
    bad = draft.model_copy(update={"body": draft.body + "\nDie meisten Kunden rufen oft direkt an."})
    problems = lint_draft(bad, [f()], SENDER)
    assert any("die meisten" in p.lower() for p in problems) and any("oft" in p for p in problems)


def test_phone_block_uses_clean_number():
    finding = f(evidence="„Telefon: 08225 12345 ·“ (kein tel:-Link)", measurement={"number": "08225 12345"})
    body = build_draft([finding], "a@b.de", SENDER).body
    assert "(„08225 12345“)" in body and "·" not in body
