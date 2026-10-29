"""Qualitätskontrolle: Ist ein Extraktionsergebnis wirklich verwertbar?

Unabhängig vom Scraper: bewertet nur, was extrahiert wurde. Ein HTTP 200 oder
eine geöffnete Browserseite ist noch kein Erfolg.

Reihenfolge der Prüfungen (die erste zutreffende entscheidet den Status):
1. Blockade (Captcha, Bot-Schutz)           → BLOCKED
2. Fehlerseite trotz Inhalt (Soft-404)      → HTTP_ERROR
3. (fast) kein Text + JS-Hinweise           → JS_RENDERING_REQUIRED (bzw. EMPTY_CONTENT nach Rendering)
4. (fast) kein Text                         → EMPTY_CONTENT
5. Platzhalterseite (im Aufbau, geparkt)    → EMPTY_CONTENT
6. Score ≥ Mindestqualität, genug Wörter    → SUCCESS
7. etwas Text                               → PARTIAL_SUCCESS
Schwellen und Gewichte: siehe docs/qualitaet.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.schemas import CrawlStatus, PageResult

# --- Schwellen (Startwerte, überprüft durch tests/test_quality_control.py) ---
MIN_WORDS_ANY = 15          # darunter gilt eine Seite als leer
MIN_WORDS_SUCCESS = 50      # darunter höchstens PARTIAL_SUCCESS
WORDS_FULL_SCORE = 150      # ab hier volle Punkte für Textmenge
JS_SUSPECT_WORDS = 100      # unter dieser Menge lohnt Rendering, wenn JS-Hinweise da sind
ERROR_PAGE_MAX_WORDS = 150  # Fehlerseiten sind kurz; lange Seiten mit "404" im Text sind keine

WEIGHTS = {"length": 0.45, "uniqueness": 0.25, "headings": 0.15, "title": 0.15}

_BLOCK_MARKERS = [
    r"just a moment\.\.\.", r"checking your browser", r"attention required", r"cf-browser-verification",
    r"verify you are (a )?human", r"are you a robot", r"captcha", r"access denied", r"zugriff verweigert",
    r"ddos protection", r"bot protection", r"request blocked", r"enable javascript and cookies to continue",
]
_ERROR_MARKERS = [
    r"\b404\b", r"seite nicht gefunden", r"page not found", r"nicht gefunden", r"not found",
    r"fehler 50\d", r"internal server error", r"service unavailable", r"bad gateway", r"error establishing a database",
]
_PLACEHOLDER_MARKERS = [
    r"im aufbau", r"under construction", r"coming soon", r"demnächst online", r"wartungsarbeiten",
    r"maintenance mode", r"domain (ist |is )?(zu verkaufen|for sale)", r"this domain is parked",
    r"diese domain wurde registriert", r"hier entsteht", r"website is under maintenance",
]
_GENERIC_TITLES = {"home", "startseite", "start", "index", "untitled", "willkommen", "welcome", "neue seite", "page"}


@dataclass
class QualityAssessment:
    status: CrawlStatus
    score: float
    reasons: list[str]
    metrics: dict[str, float | int | bool | str] = field(default_factory=dict)
    needs_rendering: bool = False        # nächste Stufe (Browser) empfohlen

    @property
    def failure_reason(self) -> str | None:
        return None if self.status.is_usable else "; ".join(self.reasons) or self.status.value


def _matches(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    return None


def text_metrics(text: str) -> dict[str, float | int]:
    words = re.findall(r"\w{2,}", text)
    lines = [line.strip().casefold() for line in text.splitlines() if line.strip()]
    unique_ratio = len(set(lines)) / len(lines) if lines else 0.0
    return {
        "chars": len(text),
        "words": len(words),
        "lines": len(lines),
        "unique_line_ratio": round(unique_ratio, 3),
    }


def score_page(words: int, unique_ratio: float, heading_count: int, title: str | None) -> tuple[float, dict]:
    title_ok = bool(title) and title.strip().casefold() not in _GENERIC_TITLES and len(title.strip()) >= 4
    parts = {
        "length": min(1.0, words / WORDS_FULL_SCORE),
        "uniqueness": unique_ratio,
        "headings": 1.0 if heading_count >= 1 else 0.0,
        "title": 1.0 if title_ok else (0.5 if title else 0.0),
    }
    score = sum(WEIGHTS[k] * v for k, v in parts.items())
    return round(score, 3), parts


def assess_page(
    *,
    cleaned_text: str,
    title: str | None,
    heading_texts: list[str],
    js_signals: list[str] | None = None,
    rendered: bool = False,
    min_quality: float = 0.5,
) -> QualityAssessment:
    """Bewertet eine einzelne extrahierte Seite.

    `rendered=True` heißt: Das Ergebnis stammt bereits aus dem Browser; dann gibt es
    keine weitere Stufe mehr, und leerer Text ist EMPTY_CONTENT statt JS_RENDERING_REQUIRED.
    """
    js_signals = js_signals or []
    metrics = text_metrics(cleaned_text)
    words, unique_ratio = int(metrics["words"]), float(metrics["unique_line_ratio"])
    score, parts = score_page(words, unique_ratio, len(heading_texts), title)
    metrics.update({"score": score, "headings": len(heading_texts), "js_signals": len(js_signals), "rendered": rendered})
    head = " ".join(filter(None, [title or "", *heading_texts[:3], cleaned_text[:400]]))

    def result(status: CrawlStatus, *reasons: str, needs_rendering: bool = False) -> QualityAssessment:
        return QualityAssessment(status, score, list(reasons), metrics, needs_rendering)

    marker = _matches(_BLOCK_MARKERS, head)
    if marker and words < ERROR_PAGE_MAX_WORDS:
        return result(CrawlStatus.BLOCKED, f"Zugriffsschutz erkannt: „{marker}“")

    marker = _matches(_ERROR_MARKERS, " ".join(filter(None, [title or "", *heading_texts[:2]])))
    if marker and words < ERROR_PAGE_MAX_WORDS:
        return result(CrawlStatus.HTTP_ERROR, f"Fehlerseite trotz Inhalt: „{marker}“ in Titel/Überschrift")

    if words < MIN_WORDS_ANY:
        if js_signals and not rendered:
            return result(CrawlStatus.JS_RENDERING_REQUIRED, f"nur {words} Wörter, JS-Hinweise: {js_signals[0]}",
                          needs_rendering=True)
        if not rendered:
            # Kein klarer JS-Hinweis, aber leer: Browser einmal versuchen, bevor wir aufgeben.
            return result(CrawlStatus.EMPTY_CONTENT, f"nur {words} Wörter ohne JS-Hinweis", needs_rendering=True)
        return result(CrawlStatus.EMPTY_CONTENT, f"auch nach Rendering nur {words} Wörter")

    marker = _matches(_PLACEHOLDER_MARKERS, head)
    if marker and words < ERROR_PAGE_MAX_WORDS:
        return result(CrawlStatus.EMPTY_CONTENT, f"Platzhalterseite: „{marker}“")

    needs_rendering = bool(js_signals) and words < JS_SUSPECT_WORDS and not rendered
    reasons = [f"{words} Wörter, Score {score:.2f}"]
    if unique_ratio < 0.5:
        reasons.append(f"viele Wiederholungen ({unique_ratio:.0%} eindeutige Zeilen)")
    if needs_rendering:
        reasons.append(f"wenig Text bei JS-Hinweisen: {js_signals[0]}")

    if words >= MIN_WORDS_SUCCESS and score >= min_quality:
        return result(CrawlStatus.SUCCESS, *reasons, needs_rendering=needs_rendering)
    return result(CrawlStatus.PARTIAL_SUCCESS, *reasons, needs_rendering=needs_rendering)


def is_better(new: QualityAssessment, old: QualityAssessment) -> bool:
    """Ersetzt ein Folgestufen-Ergebnis das bisherige? Verwertbar schlägt unverwertbar, sonst Score."""
    if new.status.is_usable != old.status.is_usable:
        return new.status.is_usable
    rank = {CrawlStatus.SUCCESS: 2, CrawlStatus.PARTIAL_SUCCESS: 1}
    if rank.get(new.status, 0) != rank.get(old.status, 0):
        return rank.get(new.status, 0) > rank.get(old.status, 0)
    return new.score > old.score


# --------------------------------------------------------------------------- Website-Ebene

@dataclass
class SiteAssessment:
    status: CrawlStatus
    reasons: list[str]
    usable_pages: int
    failed_pages: list[str]

    @property
    def failure_reason(self) -> str | None:
        return None if self.status.is_usable else "; ".join(self.reasons)


def assess_site(pages: list[PageResult]) -> SiteAssessment:
    """Fasst Seitenergebnisse zusammen. Die erste Seite ist die Startseite.

    - Startseite + alle angefragten Unterseiten verwertbar → SUCCESS
    - Startseite verwertbar, aber Unterseiten fehlgeschlagen, oder Startseite nur teilweise → PARTIAL_SUCCESS
    - Startseite unbrauchbar, aber eine Unterseite verwertbar → PARTIAL_SUCCESS
    - nichts verwertbar → Status der Startseite (Ursache bleibt erhalten)

    Eine Unterseite, die gar nicht verlinkt ist (z. B. fehlendes Impressum), macht den Crawl
    nicht unvollständig – das ist ein möglicher Befund der Website, kein Crawl-Fehler.
    """
    if not pages:
        return SiteAssessment(CrawlStatus.FAILED, ["keine Seite abgerufen"], 0, [])
    home, subpages = pages[0], pages[1:]
    usable = [p for p in pages if p.crawl_status.is_usable]
    failed = [f"{p.url} ({p.crawl_status.value})" for p in subpages if not p.crawl_status.is_usable]

    if not usable:
        reason = home.failure_reason or home.crawl_status.value
        return SiteAssessment(home.crawl_status, [f"Startseite: {reason}"], 0, failed)
    if not home.crawl_status.is_usable:
        return SiteAssessment(CrawlStatus.PARTIAL_SUCCESS,
                              [f"Startseite unbrauchbar ({home.crawl_status.value}), {len(usable)} Unterseite(n) verwertbar"],
                              len(usable), failed)
    reasons = []
    if failed:
        reasons.append(f"{len(failed)} Unterseite(n) nicht verwertbar")
    if home.crawl_status is CrawlStatus.PARTIAL_SUCCESS:
        reasons.append("Startseite nur teilweise verwertbar")
    status = CrawlStatus.PARTIAL_SUCCESS if reasons else CrawlStatus.SUCCESS
    return SiteAssessment(status, reasons or [f"{len(usable)} Seite(n) verwertbar"], len(usable), failed)
