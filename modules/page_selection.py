"""Paket 3.13: Welche Unterseiten lohnen sich?

Nicht wahllos crawlen: Pro Kategorie (Kontakt, Impressum, Leistungen, Über uns,
Referenzen) höchstens eine Seite, in fester Priorität. Datenschutzerklärungen
werden erkannt, aber nicht gecrawlt (lang, Standardtext, ohne Analysewert).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from modules.content_extractor import Link

# (Kategorie, Muster für URL-Pfad oder Linktext) – Reihenfolge = Priorität
CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("kontakt", re.compile(r"kontakt|contact|anfahrt|so-finden-sie-uns", re.I)),
    ("impressum", re.compile(r"impressum|imprint|legal[-_ ]?notice|anbieterkennzeichnung", re.I)),
    ("leistungen", re.compile(r"leistung|service|angebot|produkt|unsere[-_ ]arbeit|was[-_ ]wir|taetigkeit|tätigkeit|"
                              r"sortiment|dienstleistung|kompetenz", re.I)),
    ("ueber_uns", re.compile(r"[uü]e?ber[-_ ]?uns|about|team|unternehmen|firma|geschichte|wir[-_ ]stellen", re.I)),
    ("referenzen", re.compile(r"referenz|projekt|galerie|gallery|portfolio|arbeiten", re.I)),
]
NOT_CRAWLED = re.compile(r"datenschutz|privacy|cookie|agb|widerruf", re.I)
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|pptx?|mp4|mp3|ics|vcf)$", re.I)
_SKIP_PATH = re.compile(r"wp-admin|wp-login|/login|/cart|warenkorb|/feed|/tag/|/category/|/author/|\?s=|/wp-json", re.I)


@dataclass(frozen=True)
class SelectedPage:
    url: str
    category: str
    reason: str


def _clean(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), (parts.hostname or "").lower() + (f":{parts.port}" if parts.port else ""),
                       path, parts.query, ""))


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).hostname or "", urlsplit(b).hostname or ""
    return ha.removeprefix("www.") == hb.removeprefix("www.") and urlsplit(a).port == urlsplit(b).port


def categorize(link: Link) -> str | None:
    path = urlsplit(link.url).path
    if NOT_CRAWLED.search(path) or NOT_CRAWLED.search(link.text or ""):
        return None
    for name, pattern in CATEGORIES:
        if pattern.search(path) or pattern.search(link.text or ""):
            return name
    return None


def select_subpages(links: list[Link], home_url: str, *, max_pages: int, already: set[str] | None = None,
                    found_categories: set[str] | None = None) -> list[SelectedPage]:
    """Wählt bis zu `max_pages` Unterseiten, je Kategorie eine, kürzester Pfad gewinnt.

    `already`: bereits gecrawlte/geplante URLs. `found_categories`: schon abgedeckte Kategorien.
    """
    seen = {_clean(u) for u in (already or set())} | {_clean(home_url)}
    covered = set(found_categories or set())
    best: dict[str, tuple[int, SelectedPage]] = {}
    for link in links:
        if link.url.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = _clean(link.url)
        if url in seen or not _same_site(url, home_url):
            continue
        path = urlsplit(url).path
        if _SKIP_EXT.search(path) or _SKIP_PATH.search(url):
            continue
        category = categorize(link)
        if category is None or category in covered:
            continue
        rank = len(path)
        if category not in best or rank < best[category][0]:
            best[category] = (rank, SelectedPage(url, category, f"Link „{(link.text or path)[:40]}“"))
    order = [name for name, _ in CATEGORIES]
    chosen = sorted((p for _, p in best.values()), key=lambda p: order.index(p.category))
    return chosen[:max_pages]


def has_link_to(links: list[Link], pattern: str) -> bool:
    """Gibt es überhaupt einen Link dieser Art (z. B. Datenschutz)? Für spätere Prüfungen."""
    rx = re.compile(pattern, re.I)
    return any(rx.search(urlsplit(l.url).path) or rx.search(l.text or "") for l in links)
