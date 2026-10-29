"""Modul D, Teil 1: Deterministische Website-Prüfungen.

Jede Prüfung ist eine reine Funktion `(CrawlResult, CheckContext) -> list[Finding]` und
arbeitet nur mit gespeicherten Messwerten. Netzwerkprüfungen (HTTPS-Weiterleitung,
defekte Links) liegen getrennt in `probe_*` und liefern ihre Messwerte als Dict.

Grundsätze:
- Belegt oder gar nicht: Jeder Befund nennt die Fundstelle und, wo gemessen, die Messwerte.
- Indizien sind Indizien: niedrige `confidence`, wenn eine Beobachtung auch legitim sein kann.
- Nur prüfen, was der Crawl wirklich gesehen hat (unverwertbare Seiten werden übersprungen).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import httpx

from models.schemas import (
    ContactKind, ContactOrigin, CrawlResult, EvidenceType, Finding, FindingCategory, PageResult, Severity,
)

GENERIC_TITLES = {"home", "startseite", "start", "index", "untitled", "willkommen", "welcome", "neue seite", "page"}


@dataclass
class CheckContext:
    current_year: int = field(default_factory=lambda: date.today().year)
    probes: dict = field(default_factory=dict)       # Ergebnisse der Netzwerkprüfungen


def _home(result: CrawlResult) -> PageResult | None:
    return result.pages[0] if result.pages else None


def _usable(result: CrawlResult) -> list[PageResult]:
    return result.usable_pages


def _f(issue_id: str, category: FindingCategory, severity: Severity, description: str, evidence: str,
       source_url: str, evidence_type: EvidenceType, confidence: float, relevance: Severity,
       improvement: str, measurement: dict | None = None) -> Finding:
    return Finding(issue_id=issue_id, category=category, severity=severity, description=description,
                   evidence=evidence[:500], source_url=source_url, evidence_type=evidence_type,
                   confidence=confidence, business_relevance=relevance, suggested_improvement=improvement,
                   measurement=measurement)


# --------------------------------------------------------------------------- Erreichbarkeit / HTTPS

def check_ssl(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    """Ungültiges Zertifikat: Besucher sehen eine Browser-Warnung. Funktioniert auch ohne verwertbaren Inhalt."""
    reason = result.failure_reason or ""
    if "SslCertificateError" not in reason:
        return []
    detail = reason.split("SslCertificateError:", 1)[-1].strip()[:200]
    return [_f("ssl_certificate_invalid", FindingCategory.ACCESSIBILITY, Severity.HIGH,
               "Das SSL-Zertifikat der Website wird vom Browser nicht akzeptiert; Besucher sehen eine Sicherheitswarnung.",
               detail, result.site_url, EvidenceType.MEASURED, 0.95, Severity.HIGH,
               "Zertifikat erneuern bzw. korrekt einrichten (z. B. kostenlos über Let's Encrypt beim Hoster).",
               {"error": detail})]


def check_https(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    home = _home(result)
    if home is None or not home.crawl_status.is_usable:
        return []
    final = home.final_url or home.url
    if urlsplit(final).scheme == "http":
        return [_f("no_https", FindingCategory.ACCESSIBILITY, Severity.HIGH,
                   "Die Website wird unverschlüsselt (http) ausgeliefert; Browser markieren sie als „nicht sicher“.",
                   f"Endgültige Adresse nach allen Weiterleitungen: {final}", final, EvidenceType.MEASURED, 0.9,
                   Severity.HIGH, "HTTPS-Zertifikat einrichten und alle Aufrufe auf https weiterleiten.",
                   {"final_url": final})]
    probe = ctx.probes.get("http_redirect")
    if probe and probe.get("status") == 200 and urlsplit(probe.get("final_url", "")).scheme == "http":
        return [_f("http_not_redirected", FindingCategory.ACCESSIBILITY, Severity.MEDIUM,
                   "Die Website ist zusätzlich unverschlüsselt erreichbar und leitet http nicht auf https um.",
                   f"{probe['url']} antwortet mit HTTP 200 ohne Weiterleitung", probe["url"], EvidenceType.MEASURED,
                   0.8, Severity.MEDIUM, "Serverseitige Weiterleitung von http auf https einrichten (301).",
                   {"url": probe["url"], "status": probe["status"], "final_url": probe["final_url"],
                    "redirects": len(probe.get("redirects", []))})]
    return []


# --------------------------------------------------------------------------- Metadaten / Struktur

def check_metadata(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    home = _home(result)
    if home is None or not home.crawl_status.is_usable:
        return []
    url, out = home.final_url or home.url, []
    title = (home.page_title or "").strip()
    if not title:
        out.append(_f("title_missing", FindingCategory.METADATA, Severity.MEDIUM,
                      "Die Startseite hat keinen Seitentitel; in Google und im Browser-Tab erscheint dann nur die Adresse.",
                      "<title> fehlt oder ist leer", url, EvidenceType.DIRECTLY_OBSERVED, 0.95, Severity.MEDIUM,
                      "Aussagekräftigen Titel mit Leistung und Ort setzen."))
    elif title.casefold() in GENERIC_TITLES:
        out.append(_f("title_generic", FindingCategory.METADATA, Severity.LOW,
                      f"Der Seitentitel der Startseite lautet nur „{title}“ und sagt nichts über das Angebot.",
                      f"<title>{title}</title>", url, EvidenceType.DIRECTLY_OBSERVED, 0.9, Severity.MEDIUM,
                      "Titel mit Firmenname, Leistung und Ort ergänzen."))
    if not (home.meta_description or "").strip():
        out.append(_f("meta_description_missing", FindingCategory.METADATA, Severity.LOW,
                      "Die Startseite hat keine Meta-Beschreibung; Google wählt dann selbst einen Textausschnitt.",
                      '<meta name="description"> fehlt', url, EvidenceType.DIRECTLY_OBSERVED, 0.9, Severity.LOW,
                      "Kurze Beschreibung (ca. 150 Zeichen) mit Leistung und Region hinterlegen."))
    if home.has_h1 is False:
        out.append(_f("h1_missing", FindingCategory.METADATA, Severity.LOW,
                      "Die Startseite hat keine Hauptüberschrift (H1).",
                      "kein <h1>-Element auf der Startseite", url, EvidenceType.DIRECTLY_OBSERVED, 0.8, Severity.LOW,
                      "Eine sichtbare Hauptüberschrift mit der Kernleistung ergänzen."))
    return out


def check_thin_homepage(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    """Startseite fast ohne Text (z. B. nur Bilder), obwohl Unterseiten Text haben."""
    home = _home(result)
    if home is None or home.word_count is None or not result.usable_pages:
        return []
    if home.word_count >= 15 or home.crawl_status.is_site_inaccessible:
        return []
    return [_f("homepage_almost_no_text", FindingCategory.CLARITY, Severity.MEDIUM,
               f"Die Startseite enthält nur {home.word_count} Wörter lesbaren Text; Besucher und Suchmaschinen "
               "erfahren dort kaum, was der Betrieb anbietet.",
               f"{home.word_count} Wörter sichtbarer Text auf {home.url}", home.url, EvidenceType.MEASURED, 0.75,
               Severity.MEDIUM, "Kurzen Einleitungstext mit Leistungen und Einzugsgebiet auf der Startseite ergänzen.",
               {"words": home.word_count})]


# --------------------------------------------------------------------------- Mobil

def check_mobile(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    home = _home(result)
    if home is None or not home.crawl_status.is_usable:
        return []
    url, out = home.final_url or home.url, []
    if home.has_viewport_meta is False:
        out.append(_f("viewport_meta_missing", FindingCategory.MOBILE, Severity.HIGH,
                      "Der Startseite fehlt die Viewport-Angabe; Smartphones zeigen sie dann verkleinert wie am Desktop.",
                      '<meta name="viewport"> fehlt im <head>', url, EvidenceType.DIRECTLY_OBSERVED, 0.9, Severity.HIGH,
                      "Responsive Layout mit Viewport-Angabe umsetzen."))
    if home.horizontal_overflow:
        culprit = home.overflow_culprits[0] if home.overflow_culprits else None
        evidence = (f"Bei {home.viewport_width}px Bildschirmbreite ist die Seite {home.document_width}px breit"
                    + (f"; überstehendes Element: {culprit.selector} ({culprit.width:.0f}px)" if culprit else ""))
        out.append(_f("horizontal_overflow_mobile", FindingCategory.MOBILE, Severity.MEDIUM,
                      "Auf dem Smartphone lässt sich die Startseite seitlich verschieben, weil ein Element breiter ist als der Bildschirm.",
                      evidence, url, EvidenceType.MEASURED, 0.8 if culprit else 0.6, Severity.MEDIUM,
                      "Das überstehende Element auf maximale Bildschirmbreite begrenzen.",
                      {"viewport_width": home.viewport_width, "document_width": home.document_width,
                       "culprit": culprit.selector if culprit else "unbekannt"}))
    return out


# --------------------------------------------------------------------------- Kontakt / Rechtliches

def check_contact(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    if not result.usable_pages:
        return []
    out = []
    phones = [c for p in result.pages for c in p.phone_numbers]
    # Pro Seite: Ist die Nummer erst auf der Kontaktseite verlinkt, bleibt sie auf der Startseite unklickbar.
    linked = {(c.source_url, c.value) for c in phones if c.origin is ContactOrigin.LINK}
    text_only = [c for c in phones if c.origin is ContactOrigin.TEXT and (c.source_url, c.value) not in linked]
    if text_only:
        # Wo Kunden anrufen (Startseite, Kontakt) zählt; im Impressum/Datenschutz ist es kaum relevant.
        legal = re.compile(r"impressum|imprint|datenschutz|privacy|agb", re.I)
        customer_facing = [c for c in text_only if not legal.search(c.source_url)]
        c = customer_facing[0] if customer_facing else text_only[0]
        relevant = bool(customer_facing)
        out.append(_f("phone_not_clickable", FindingCategory.CONTACT, Severity.MEDIUM if relevant else Severity.LOW,
                      "Die Telefonnummer ist nicht als Link hinterlegt; auf dem Smartphone kann man sie nicht direkt antippen.",
                      f"„{c.context or c.raw}“ (kein tel:-Link)", c.source_url, EvidenceType.DIRECTLY_OBSERVED, 0.85,
                      Severity.MEDIUM if relevant else Severity.LOW, f'Nummer als <a href="tel:{c.value}"> verlinken.',
                      {"number": c.raw}))
    broken = [c for c in phones if c.origin is ContactOrigin.LINK and re.search(r"[\"<>]", c.raw)]
    if broken:
        c = broken[0]
        out.append(_f("tel_link_malformed", FindingCategory.CONTACT, Severity.MEDIUM,
                      "Der Telefon-Link enthält fehlerhaften HTML-Code; je nach Gerät öffnet er eine falsche oder keine Nummer.",
                      f'href="tel:{c.raw}"', c.source_url, EvidenceType.DIRECTLY_OBSERVED, 0.85, Severity.MEDIUM,
                      f'Link auf href="tel:{c.value}" korrigieren.'))
    if not phones and not result.contacts(ContactKind.EMAIL):
        pages = len(result.usable_pages)
        out.append(_f("no_contact_found", FindingCategory.CONTACT, Severity.MEDIUM,
                      "Auf den untersuchten Seiten war weder eine Telefonnummer noch eine E-Mail-Adresse als Text zu finden.",
                      f"{pages} verwertbare Seite(n) geprüft, keine Telefonnummer/E-Mail im Text", result.site_url,
                      EvidenceType.DIRECTLY_OBSERVED, 0.55, Severity.HIGH,
                      "Kontaktdaten als Text (nicht nur im Bild) auf Startseite und Kontaktseite angeben."))
    return out


def check_legal_pages(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    home = _home(result)
    if home is None or not home.crawl_status.is_usable:
        return []
    links = [l.lower() for p in result.pages for l in p.internal_links + p.contact_links]
    if any("impressum" in l or "imprint" in l for l in links):
        return []
    return [_f("impressum_link_not_found", FindingCategory.LEGAL, Severity.MEDIUM,
               "Auf der Startseite wurde kein Link zu einem Impressum gefunden.",
               f"{len(home.internal_links)} interne Links auf {home.url}, keiner enthält „impressum“",
               home.url, EvidenceType.DIRECTLY_OBSERVED, 0.6, Severity.MEDIUM,
               "Impressum von jeder Seite aus erreichbar verlinken (z. B. im Footer).")]


# --------------------------------------------------------------------------- Pflege / Performance / Links

def check_copyright(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    home = _home(result)
    if home is None or home.copyright_year is None:
        return []
    age = ctx.current_year - home.copyright_year
    if age < 2:                      # Vorjahr ist im Januar normal
        return []
    return [_f("copyright_outdated", FindingCategory.MAINTENANCE, Severity.LOW,
               f"Im Footer steht noch {home.copyright_year}; das wirkt auf Besucher, als würde die Seite nicht gepflegt.",
               home.copyright_text or f"© {home.copyright_year}", home.url, EvidenceType.DIRECTLY_OBSERVED, 0.9,
               Severity.LOW, "Jahreszahl aktualisieren oder automatisch setzen lassen.",
               {"copyright_year": home.copyright_year, "current_year": ctx.current_year})]


def check_performance(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    """Nur grobe, gemessene Indizien aus einem einzelnen Abruf – keine Lighthouse-Werte."""
    home = _home(result)
    if home is None or home.response_seconds is None:
        return []
    out = []
    if home.response_seconds >= 3.0:
        out.append(_f("slow_server_response", FindingCategory.PERFORMANCE, Severity.LOW,
                      f"Das HTML der Startseite kam erst nach {home.response_seconds:.1f} Sekunden an (einzelne Messung).",
                      f"Antwortzeit {home.response_seconds:.1f}s für {home.url}", home.url, EvidenceType.MEASURED, 0.5,
                      Severity.MEDIUM, "Ladezeit mit mehreren Messungen prüfen; ggf. Caching oder schnelleres Hosting.",
                      {"response_seconds": home.response_seconds}))
    if home.html_bytes and home.html_bytes > 1_500_000:
        out.append(_f("html_very_large", FindingCategory.PERFORMANCE, Severity.LOW,
                      f"Das HTML der Startseite ist {home.html_bytes / 1e6:.1f} MB groß (ohne Bilder).",
                      f"{home.html_bytes} Bytes HTML", home.url, EvidenceType.MEASURED, 0.7, Severity.LOW,
                      "Eingebettete Daten/Skripte reduzieren.", {"html_bytes": home.html_bytes}))
    return out


def check_broken_links(result: CrawlResult, ctx: CheckContext) -> list[Finding]:
    broken = ctx.probes.get("broken_links") or []
    crawled = [p for p in result.pages[1:] if p.http_status == 404 or "HTTP 404" in (p.failure_reason or "")]
    items = [(b["url"], b["status"]) for b in broken] + [(p.url, 404) for p in crawled]
    items = list(dict.fromkeys(items))
    if not items:
        return []
    url, status = items[0]
    return [_f("broken_internal_links", FindingCategory.LINKS, Severity.MEDIUM,
               f"{len(items)} interne(r) Link(s) führen ins Leere.",
               "; ".join(f"{u} → HTTP {s}" for u, s in items[:3]), result.site_url, EvidenceType.MEASURED, 0.9,
               Severity.MEDIUM, "Links korrigieren oder entfernen.", {"broken": len(items), "first": url})]


CHECKS: list[Callable[[CrawlResult, CheckContext], list[Finding]]] = [
    check_ssl, check_https, check_mobile, check_contact, check_metadata, check_thin_homepage,
    check_legal_pages, check_broken_links, check_copyright, check_performance,
]


def run_checks(result: CrawlResult, ctx: CheckContext | None = None) -> list[Finding]:
    ctx = ctx or CheckContext()
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(result, ctx))
    return findings


# --------------------------------------------------------------------------- Netzwerkprüfungen

async def probe_http_redirect(result: CrawlResult, client: httpx.AsyncClient) -> dict | None:
    """Ruft die http-Variante der Startseite auf und misst, wohin sie führt."""
    home = _home(result)
    if home is None or not home.crawl_status.is_usable:
        return None
    parts = urlsplit(home.final_url or home.url)
    if parts.scheme != "https":
        return None
    http_url = urlunsplit(("http", parts.netloc, "/", "", ""))
    try:
        r = await client.get(http_url, follow_redirects=True)
    except httpx.HTTPError as exc:
        return {"url": http_url, "error": type(exc).__name__}
    return {"url": http_url, "status": r.status_code, "final_url": str(r.url),
            "redirects": [h.status_code for h in r.history]}


async def probe_broken_links(result: CrawlResult, client: httpx.AsyncClient, limit: int = 20,
                             concurrency: int = 4) -> list[dict]:
    """Prüft bis zu `limit` interne Links der Startseite (HEAD, bei 405 GET). Nur 404/410 gelten als defekt."""
    home = _home(result)
    if home is None:
        return []
    crawled = {p.url for p in result.pages}
    candidates = [u for u in home.internal_links if u not in crawled and not u.lower().startswith(("mailto:", "tel:"))]
    sem = asyncio.Semaphore(concurrency)

    async def one(url: str) -> dict | None:
        async with sem:
            try:
                r = await client.head(url, follow_redirects=True)
                if r.status_code == 405:
                    r = await client.get(url, follow_redirects=True)
            except httpx.HTTPError:
                return None                 # Netzwerkfehler sind kein Beleg für einen toten Link
            return {"url": url, "status": r.status_code} if r.status_code in (404, 410) else None

    results = await asyncio.gather(*(one(u) for u in candidates[:limit]))
    return [r for r in results if r]
