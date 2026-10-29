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


def format_row(name, branche, url, result, secs, error, outcomes=None) -> str:
    name = name.replace("|", "/")
    if result is None:
        return f"| {name[:30]} | {branche} | ABSTURZ | – | – | – | – | – | – | – | {secs:.0f}s |  ← {error[:80]}"
    home = result.pages[0]
    phones = result.contacts(ContactKind.PHONE)
    links = sum(1 for p in phones if p.origin is ContactOrigin.LINK)
    return (f"| {name[:30]} | {branche} | {result.status.value} | {len(result.usable_pages)}/{len(result.pages)} | "
            f"{home.word_count if home.word_count is not None else '–'} | {home.extraction_method.value} | "
            f"{home.horizontal_overflow} | {links}/{len(phones) - links} | {len(result.contacts(ContactKind.EMAIL))} | "
            f"{home.copyright_year or '–'} | {secs:.0f}s |")


async def crawl_all(sites: list[tuple[str, str, str]], concurrency: int, max_seconds: float) -> list[tuple]:
    """Crawlt parallel und gibt jede Zeile sofort aus (ein Abbruch verliert nichts)."""
    from dataclasses import replace
    config = replace(CrawlConfig.from_settings(get_settings()), max_seconds=max_seconds)
    sem = asyncio.Semaphore(concurrency)
    rows = []
    async with BrowserRenderer(BrowserLimits()) as renderer:
        async def one(name: str, branche: str, url: str):
            async with sem:
                t = time.monotonic()
                try:
                    result, outcomes = await SiteCrawler(config, renderer=renderer).crawl_site(url)
                    return name, branche, url, result, time.monotonic() - t, None, outcomes
                except Exception as exc:  # Stichprobe soll nie abbrechen; Fehler wird berichtet
                    return name, branche, url, None, time.monotonic() - t, f"{type(exc).__name__}: {exc}", []
        for task in asyncio.as_completed([one(*s) for s in sites]):
            row = await task
            rows.append(row)
            print(format_row(*row), flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=int, default=4, help="Anzahl Places-Abfragen (Kosten!)")
    parser.add_argument("--max-sites", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-seconds", type=float, default=60.0, help="Zeitlimit je Website")
    parser.add_argument("--details", help="Pfad für JSON mit URLs und Ablaufnotizen je Seite")
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

    print(f"# Stichprobe Crawl ({time.strftime('%d.%m.%Y %H:%M')})\n")
    print(f"Abfragen: {args.queries} · Websites: {len(sites)} · Zeitlimit je Website: {args.max_seconds:.0f}s")
    print("Übersprungen: " + ", ".join(f"{k}: {v}" for k, v in skipped.items()) + "\n")
    print("| Firma | Branche | Status | Seiten (ok/ges.) | Wörter Start | Methode Start | Overflow | Tel. Link/Text | E-Mail | © | Zeit |")
    print("|---|---|---|---|---|---|---|---|---|---|---|", flush=True)
    t0 = time.monotonic()
    rows = asyncio.run(crawl_all(sites, args.concurrency, args.max_seconds))
    print(f"\nLaufzeit gesamt: {time.monotonic() - t0:.0f}s\n\n## Gründe bei nicht vollem Erfolg\n")
    for name, _, url, result, _, error, _ in rows:
        if result is not None and result.failure_reason:
            print(f"- **{name[:40].replace('|', '/')}** ({url}): {result.failure_reason[:200]}")
    if args.details:
        details = [{"name": n, "url": u, "status": r.status.value if r else "ABSTURZ", "error": e,
                    "pages": [{"url": o.page.url, "status": o.page.crawl_status.value, "notes": o.notes} for o in outs]}
                   for n, _, u, r, _, e, outs in rows]
        Path(args.details).write_text(__import__("json").dumps(details, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
