"""Paket 4.7: Stichprobe echter Websites crawlen und Ergebnisse als Markdown-Tabelle ausgeben.

Aufruf:
    python scripts/sample_crawl.py --queries 4 --max-sites 25 > docs/stichprobe_roh.md

Holt Firmen über Google Places mit den Suchanfragen aus niche.toml (je Anfrage 1 API-Abfrage),
überspringt Verzeichnis-/Social-Profile und crawlt die eigenen Websites vollständig.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import get_settings  # noqa: E402
from core.niche import classify, load_niche  # noqa: E402
from models.schemas import ContactKind, ContactOrigin  # noqa: E402
from modules.browser import BrowserLimits, BrowserRenderer  # noqa: E402
from modules.database import identity_domain  # noqa: E402
from modules.scraper import CrawlConfig, SiteCrawler  # noqa: E402


def places(query: str, key: str) -> list[dict]:
    r = httpx.post("https://places.googleapis.com/v1/places:searchText", timeout=20,
                   headers={"X-Goog-Api-Key": key,
                            "X-Goog-FieldMask": "places.id,places.displayName,places.websiteUri,places.types"},
                   json={"textQuery": query, "pageSize": 20, "languageCode": "de"})
    r.raise_for_status()
    return r.json().get("places", [])


async def crawl_all(sites: list[tuple[str, str, str]], concurrency: int) -> list[tuple]:
    config = CrawlConfig.from_settings(get_settings())
    sem = asyncio.Semaphore(concurrency)
    async with BrowserRenderer(BrowserLimits()) as renderer:
        async def one(name: str, branche: str, url: str):
            async with sem:
                t = time.monotonic()
                try:
                    result, outcomes = await SiteCrawler(config, renderer=renderer).crawl_site(url)
                    return name, branche, url, result, outcomes, time.monotonic() - t, None
                except Exception as exc:  # Stichprobe soll nie abbrechen; Fehler wird berichtet
                    return name, branche, url, None, [], time.monotonic() - t, f"{type(exc).__name__}: {exc}"
        return await asyncio.gather(*(one(*s) for s in sites))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=int, default=4, help="Anzahl Places-Abfragen (Kosten!)")
    parser.add_argument("--max-sites", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

    key = get_settings().require_secret("google_places_api_key")
    niche = load_niche()
    seen, sites, skipped = set(), [], {"kein Website-Eintrag": 0, "Verzeichnis/Social-Profil": 0, "außerhalb Nische": 0}
    # Kernbranchen über verschiedene Orte verteilen statt nur den ersten Ort abzufragen
    queries = niche.search_queries()
    step = max(1, len(queries) // args.queries)
    for _, query in queries[::step][: args.queries]:
        for p in places(query, key):
            name, url = p["displayName"]["text"], p.get("websiteUri")
            match = classify(niche, name, p.get("types", []))
            if not url:
                skipped["kein Website-Eintrag"] += 1
                continue
            domain = identity_domain(url)
            if domain is None:
                skipped["Verzeichnis/Social-Profil"] += 1
                continue
            if match.fit == 0:
                skipped["außerhalb Nische"] += 1
                continue
            if domain not in seen:
                seen.add(domain)
                sites.append((name, match.branche, url))
    sites = sites[: args.max_sites]

    t0 = time.monotonic()
    rows = asyncio.run(crawl_all(sites, args.concurrency))
    print(f"# Stichprobe Crawl ({time.strftime('%d.%m.%Y %H:%M')})\n")
    print(f"Abfragen: {args.queries} · Websites gecrawlt: {len(rows)} · Laufzeit gesamt: {time.monotonic() - t0:.0f}s")
    print(f"Übersprungen: " + ", ".join(f"{k}: {v}" for k, v in skipped.items()) + "\n")
    print("| Firma | Branche | Status | Seiten (ok/ges.) | Wörter Start | Methode Start | Overflow | Tel. Link/Text | E-Mail | © | Zeit |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, branche, url, result, outcomes, secs, error in rows:
        if result is None:
            print(f"| {name[:30]} | {branche} | ABSTURZ | – | – | – | – | – | – | – | {secs:.0f}s |  ← {error[:80]}")
            continue
        home = result.pages[0]
        phones = result.contacts(ContactKind.PHONE)
        links = sum(1 for p in phones if p.origin is ContactOrigin.LINK)
        emails = len(result.contacts(ContactKind.EMAIL))
        ok = len(result.usable_pages)
        print(f"| {name[:30]} | {branche} | {result.status.value} | {ok}/{len(result.pages)} | "
              f"{len(home.cleaned_text.split())} | {home.extraction_method.value} | {home.horizontal_overflow} | "
              f"{links}/{len(phones) - links} | {emails} | {home.copyright_year or '–'} | {secs:.0f}s |")
    print("\n## Gründe bei nicht vollem Erfolg\n")
    for name, _, url, result, outcomes, _, error in rows:
        if result is not None and result.failure_reason:
            print(f"- **{name[:40]}** ({url}): {result.failure_reason[:200]}")


if __name__ == "__main__":
    main()
