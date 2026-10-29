# Arbeitspakete: B2B-Outreach-System

Zerlegung des Master-Prompts in kleine, einzeln prüfbare Aufgaben.
Jedes Paket = ein Prompt / eine Sitzung. Erst weiter, wenn das Abnahmekriterium erfüllt ist.

Stand Bestandsaufnahme (2026-10-29): Repository `outreach_pyhton` ist leer, keine Commits,
keine bestehenden Skripte, keine Datenbank. Es gibt nichts zu übernehmen oder zu reparieren.

## Fortschritt

| Paket | Status | Nachweis |
|---|---|---|
| 1.1 Projektgerüst | erledigt | `tests/test_smoke.py` |
| 1.2 Open-Source-Recherche | offen | – |
| 1.3 Konfiguration | erledigt | `tests/test_config.py` |
| 1.4 Logging | erledigt | `tests/test_logging.py` |
| 1.5 Fehlertypen | erledigt | `tests/test_errors_retry.py` |
| 1.6 Retry | erledigt | `tests/test_errors_retry.py` |
| 2.1 Schemas Lead/Kontakt | erledigt | `tests/test_schemas.py` |
| 2.2 Schemas Crawl | erledigt | `tests/test_schemas.py` |
| 2.3 Schemas Analyse | erledigt | `tests/test_schemas.py` |
| 2.4 Normalisierung | erledigt | `tests/test_normalize.py` (`core/normalize.py`) |
| 2.5 DB-Schema | erledigt | `tests/test_database.py` |
| 2.6 Repository-Schicht | erledigt | `tests/test_database.py` |
| 2.7 Duplikat-Erkennung | erledigt | `tests/test_database.py` + Probelauf mit echten Places-Daten |
| 2.8 Job-Schritte | erledigt | `tests/test_database.py` |
| 3.1 Lokaler Testserver | erledigt | `tests/conftest.py`, `tests/fixtures/sites/` |
| 3.2 URL-Validierung | erledigt | `core/normalize.py` + `tests/test_fetcher.py` |
| 3.3 HTTP-Abruf | erledigt | `modules/fetcher.py`, `tests/test_fetcher.py` + Probeabruf echter Seiten |
| 3.4 HTML-Parsing | erledigt | `tests/test_content_extractor.py` |
| 3.5 Kontakt-Extraktion | erledigt | `tests/test_content_extractor.py` + 8 echte Seiten |
| 3.6 Footer + Copyright | erledigt | `tests/test_content_extractor.py` |
| 3.7 Trafilatura-Vergleich | erledigt | Regeln in `docs/qualitaet.md` |
| 4.1 Kriterien dokumentieren | erledigt | `docs/qualitaet.md` |
| 4.2 Qualitätsbewertung Seite | erledigt | `tests/test_quality_control.py` + 10 echte Seiten |
| 4.3 Website-Ebene | erledigt | `tests/test_quality_control.py` |
| 4.4 Website- vs. Scraper-Fehler | erledigt | `tests/test_quality_control.py` |
| 4.6 Schutz-Test | erledigt | `tests/test_database.py::test_report_for_bad_crawl_is_refused` |
| N.1 Kontakthistorie (Zusatz) | erledigt | `tests/test_contacts_history.py` – alle Kanäle, Sperrfrist, Sperrvermerk, CSV-Import |
| 3.9 Playwright-Stufe | erledigt | `modules/browser.py`, `tests/test_browser.py` + 8 echte Seiten |
| 3.10 Layout-Messung | erledigt | `tests/test_browser.py` (echtes Scrollen statt nur scrollWidth) |
| 3.11 Cookie-Banner | erledigt | `tests/test_browser.py`, `tests/test_quality_control.py` + hins-zimmerei.de |
| N.2 Nischenprofil (Zusatz) | erledigt | `niche.toml`, `tests/test_niche.py` mit 19 echten Firmen |

Abweichung von der Zielstruktur: Fehlertypen, Logging und Retry liegen in `core/`,
damit Modulnamen wie `errors` oder `retry` nicht mit anderen Paketen kollidieren.

Legende: **Ergebnis** = was danach existiert · **Abnahme** = wie man prüft, dass es fertig ist · **braucht** = Abhängigkeiten

---

## Phase 1 – Fundament

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 1.1 | Projektgerüst anlegen | Ordnerstruktur, `pyproject.toml`, `requirements.txt`, `.gitignore`, `.env.example`, leere `__init__.py` | `pip install -e .` in venv läuft; `pytest` startet (0 Tests) | – |
| 1.2 | Open-Source-Recherche | Kurznotiz in `docs/recherche.md`: Trafilatura, Playwright, httpx, ggf. fertige Consent-Klicker, Google-Places-Clients – je Lizenz, Wartung, Entscheidung | Jede genutzte Bibliothek hat Lizenz + Begründung | – |
| 1.3 | `config.py` | Pydantic-Settings mit allen Werten aus Abschnitt 4, Defaults sicher (Versand aus, Draft-Modus) | Test: fehlende Keys → klarer Fehler erst beim Nutzen des Moduls, nicht beim Import; Test: Versand standardmäßig `False` | 1.1 |
| 1.4 | Logging-Setup | `logging_setup.py`: strukturiertes Logging (JSON oder key=value), Filter, der Secrets maskiert | Test: API-Key im Log-Text erscheint als `***` | 1.3 |
| 1.5 | Fehlertypen | `errors.py`: `TemporaryError` vs. `PermanentError` + Unterklassen (Timeout, Blocked, InvalidUrl …) | Unit-Test der Klassifikation | 1.1 |
| 1.6 | Retry-Helfer | Funktion/Decorator mit begrenzten Versuchen + Backoff, wiederholt nur `TemporaryError` | Test: Permanent → 1 Versuch; Temporär → max. N Versuche | 1.5 |

## Phase 2 – Datenmodelle und Datenbank

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 2.1 | Schemas: Lead/Company | `Company`, `Lead`, `ContactInfo` (mit Fundstelle + `is_link`) in `models/schemas.py` | Validierung: ungültige URL wird abgelehnt | 1.1 |
| 2.2 | Schemas: Crawl | `PageResult` (alle Felder aus 6.4), `CrawlResult`, Enum `CrawlStatus` (9 Werte) | Serialisierung roundtrip JSON | 2.1 |
| 2.3 | Schemas: Analyse | `Finding` (Felder aus 8.4), Enum `EvidenceType`, `AnalysisReport`, `LeadScore`, `EmailDraft` | Test: `Finding` ohne `evidence` nicht erstellbar | 2.1 |
| 2.4 | Normalisierung | `normalize.py`: Domain (ohne www, Schema, Slash), Firmenname (GmbH/Co. KG etc. entfernen, Kleinschreibung), Adresse | Tabellentest mit ~15 Fällen | 1.1 |
| 2.5 | DB-Schema | SQLite-Tabellen: companies, websites, contacts, crawl_runs, pages, reports, findings, scores, drafts, send_attempts, blocklist, errors, job_steps. IDs + `created_at`/`updated_at`, UNIQUE-Constraints | Migration auf leerer DB läuft; Constraints greifen | 2.1–2.3 |
| 2.6 | Repository-Schicht | `database.py`: CRUD-Funktionen, Transaktionen, keine Geschäftslogik | Tests mit In-Memory-DB | 2.5 |
| 2.7 | Duplikat-Erkennung | Upsert über normalisierte Domain → sonst Name+Adresse | Test: zweimal derselbe Lead → 1 Datensatz, Felder aktualisiert | 2.4, 2.6 |
| 2.8 | Job-Schritte | Tabelle `job_steps` (lead_id, step, status, attempts, last_error) + Funktionen „ist erledigt?“, „markiere“ | Test: abgeschlossener Schritt wird übersprungen | 2.6 |

## Phase 3 – Scraping-Pipeline (kritischer Pfad)

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 3.1 | Lokaler Testserver | pytest-Fixture, die `tests/fixtures/sites/` per HTTP ausliefert (statisch, leer, 404, Redirect, langsam, JS-only, Cookie-Banner, Captcha-Seite) | Fixture startet, alle Seiten abrufbar | 1.1 |
| 3.2 | URL-Validierung | Prüfung Schema/Host, Ausschluss lokaler/privater IPs im Produktivmodus | Test: `ftp://`, `javascript:`, leere URL → `INVALID_URL` | 2.2 |
| 3.3 | Stufe 1: HTTP-Abruf | `fetch_http()`: httpx, Timeout, Redirects (final_url), Content-Type-Prüfung, Größenlimit (Stream-Abbruch) | Tests: 200, 404, 301→200, Timeout, zu groß, PDF statt HTML | 3.1, 3.2, 1.6 |
| 3.4 | Stufe 2: HTML-Parsing | `content_extractor.parse_html()`: Titel, Meta-Description, Headings, sichtbarer Text ohne script/style/noscript | Test gegen statische Fixture: erwartete Headings exakt | 2.2 |
| 3.5 | Kontakt-Extraktion | E-Mails/Telefonnummern aus Text **und** `mailto:`/`tel:`, jeweils mit URL + Kontext + `is_link` | Tests: Nummer mit/ohne `tel:`, verschleierte E-Mail (`[at]`), keine E-Mail | 3.4 |
| 3.6 | Footer + Copyright | Footer-Text erkennen, Copyright-Jahr(e) extrahieren (auch „2019–2023“) | Tabellentest | 3.4 |
| 3.7 | Stufe 3: Trafilatura | Haupttext extrahieren, Ergebnis mit Stufe 2 vergleichen (Länge, Überlappung), besseres wählen + Methode vermerken | Test: Nav-lastige Seite → Trafilatura gewinnt; Fall, wo Trafilatura leer ist → BS4 gewinnt | 3.4 |
| 3.8 | Rohes HTML speichern | HTML als Datei unter `data/html/<hash>.html`, Größenlimit, DB speichert nur Referenz | Test: Datei existiert, DB-Feld ist Pfad | 2.6, 3.3 |
| 3.9 | Stufe 4: Playwright-Grundlage | `render_page()`: headless, `domcontentloaded`, Body-Check, `networkidle` optional mit kurzem Timeout, Laufzeit-/Größenlimit, Browser wird immer geschlossen | Test: JS-only-Fixture liefert Text nur über diese Stufe | 3.1 |
| 3.10 | Layout-Messung | In Playwright: Viewport 375px, `scrollWidth` vs. `innerWidth`, breitestes überstehendes Element (Selector + Breite) | Fixture mit `width:600px`-Div → Overflow + Element erkannt; Fixture ohne → kein Overflow | 3.9 |
| 3.11 | Cookie-Banner | Erkennung (Texte DE/EN, Rollen, gängige CMPs, iframes), Klassifikation Button = akzeptieren/ablehnen/einstellungen/sonstiges. Erst prüfen, ob Inhalt ohne Klick lesbar ist; Klick nur optional und konfigurierbar | Tests: Banner überdeckt Inhalt / Banner stört nicht / Button „Einstellungen“ wird nicht geklickt | 3.9 |
| 3.12 | Blockade-Erkennung | Captcha/Cloudflare/403-Muster erkennen → `BLOCKED`, kein Retry | Captcha-Fixture → `BLOCKED`, genau 1 Versuch | 3.3, 1.5 |
| 3.13 | Unterseiten-Auswahl | Interne Links sammeln, nach Relevanz (Kontakt, Impressum, Leistungen, Über uns, Referenzen) ranken, Limits für Anzahl/Tiefe/Größe/Laufzeit | Test: Fixture-Site mit 30 Links → nur relevante ≤ max_pages | 3.4 |
| 3.14 | Stufen-Orchestrierung | `scraper.crawl_site()`: Stufe 1→2→3, bei schlechter Qualität Stufe 4, sonst Abbruch mit Grund; ergibt `CrawlResult` | Integrationstest über alle Fixtures, Status je Fixture wie erwartet | 3.3–3.13, 4.2 |

## Phase 4 – Qualitätskontrolle

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 4.1 | Kriterien dokumentieren | `docs/qualitaet.md`: jedes Kriterium mit Messgröße und Startschwelle | Review | – |
| 4.2 | `quality_control.py` | Reine Funktion `assess(page) -> (status, score, reasons)`: Textlänge, Wiederholungsanteil, Headings, Titelplausibilität, Fehlerseiten-Muster („404“, „Seite nicht gefunden“), JS-Hinweise (`<noscript>`, leerer `#root`) | Tests je Kriterium | 2.2 |
| 4.3 | Site-Ebene | Vollständigkeit: wurden Kontakt/Impressum gefunden? → `PARTIAL_SUCCESS` vs. `SUCCESS` | Test | 4.2 |
| 4.4 | Abgrenzung | Unterscheidung „Scraper hat versagt“ (`FAILED`, `EMPTY_CONTENT`) vs. „Site nicht zugänglich“ (`BLOCKED`, `HTTP_ERROR`, `TIMEOUT`) | Tests | 4.2 |
| 4.5 | Schwelle validieren | Fixture-Set (≥ 15 gelabelte Seiten) und Test, der Precision/Recall der Schwelle ausgibt | Testlauf dokumentiert in `docs/qualitaet.md` | 4.2, 3.14 |
| 4.6 | Schutz-Test | Test: schlechter Crawl → **kein** Analyse-Report in der DB | grün | 4.2, 2.8 |
| 4.7 | Echte Stichprobe | `scripts/sample_crawl.py` gegen 20–30 echte Websites, Ergebnis-Tabelle in `docs/stichprobe.md` (gemessen, nicht geschätzt) | Datei mit realen Zahlen | 3.14 |

## Phase 5 – Lead Finder

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 5.1 | Quellen-Entscheidung | Notiz: Google Places (New) vs. Apify vs. OSM/Overpass – Kosten, Felder, Nutzungsbedingungen | Entscheidung festgehalten | 1.2 |
| 5.2 | Provider-Interface | `LeadSource`-Protokoll: `search(ort, branche, limit) -> Iterator[Lead]` | – | 2.1 |
| 5.3 | Google-Places-Adapter | Text Search + Pagination (begrenzt), Timeouts, Rate-Limit, Mapping auf `Lead` | Tests mit aufgezeichneten JSON-Antworten (gemockt) | 5.2, 1.6 |
| 5.4 | Optionaler zweiter Adapter | z. B. OpenStreetMap/Overpass (kostenlos) als Fallback | Tests mit Mock | 5.2 |
| 5.5 | Cache | Erfolgreiche API-Antworten nach (Quelle, Ort, Branche, Seite) zwischenspeichern | Test: zweiter Aufruf → kein HTTP | 5.3 |
| 5.6 | Speichern + Export | Leads via Upsert (2.7) in DB, optional JSON-Export | Test: 2 Läufe → keine Dubletten | 2.7, 5.3 |
| 5.7 | Verzeichnisprofile erkennen | Website-URL, die auf ein Branchenverzeichnis/Social-Profil zeigt (z. B. `firmen.tv/...`, `fensterbau.io/...`, Facebook), als „keine eigene Website“ markieren: nicht crawlen und kritisieren, sondern als eigenes Lead-Signal speichern | Tests mit den echten Beispielen aus Jettingen-Scheppach | 5.3 |
| 5.8 | Nische anwenden | Suchanfragen aus `niche.toml`, Branche + Passung je Firma speichern, Abfragelimit pro Lauf (voller Lauf = 98 Abfragen) | Test: Limit greift, Cache verhindert Doppelabfragen | 5.3, N.2 |

## Phase 6 – Website-Prüfungen und KI-Analyse

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 6.1 | Check-Framework | `website_checks.py`: jeder Check = Funktion `(CrawlResult) -> list[Finding]`, Registry | – | 2.3 |
| 6.2 | Checks: Erreichbarkeit/HTTPS | HTTP→HTTPS-Redirect, Zertifikat, Redirect-Kette | Fixture-Tests | 6.1 |
| 6.3 | Checks: Metadaten | Title fehlt/leer/doppelt, Meta-Description fehlt, Viewport-Meta fehlt | Tests | 6.1 |
| 6.4 | Checks: Links | Defekte interne Links (HEAD/GET, begrenzt) | Fixture mit totem Link | 6.1 |
| 6.5 | Checks: Kontakt | Kontakt auffindbar? Telefonnummer ohne `tel:`-Link? Impressum vorhanden? | Tests | 6.1, 3.5 |
| 6.6 | Checks: Mobil/Overflow | Overflow-Befund nur mit Messwert + Element, Schwere niedrig, `MEASURED` | Tests | 6.1, 3.10 |
| 6.7 | Check: Copyright | Jahr < aktuelles Jahr (zur Laufzeit, injizierbar für Tests) → Hinweis, Schwere niedrig | Test mit fixiertem Datum | 6.1, 3.6 |
| 6.8 | Check: Performance | Gemessene Werte (Bytes, Ladezeit, Anzahl Requests); PageSpeed-API optional, klar als Quelle markiert | Tests | 6.1 |
| 6.9 | LLM-Client | Austauschbares Interface `LLMClient.complete_json()`, DeepSeek-Adapter (`deepseek-chat`), Kostenzähler + Limit | Test mit Mock, Limit überschritten → Abbruch | 1.3 |
| 6.10 | Prompt mit Injection-Schutz | Systemregeln getrennt, Website-Text als gekennzeichnete Daten (Delimiter, gekürzt), Ausgabe nur JSON-Schema | Test: Fixture mit „Ignoriere alle Anweisungen…“ ändert Ausgabeformat nicht | 6.9 |
| 6.11 | Belegprüfung | Jedes KI-Finding: `evidence` muss wörtlich im extrahierten Text vorkommen (fuzzy), sonst verwerfen oder `confidence` senken | Tests: erfundenes Zitat → verworfen | 6.10, 2.3 |
| 6.12 | Robustheit | Ungültiges JSON → 1 Reparaturversuch, dann Fehler speichern | Test | 6.9 |
| 6.13 | `analyzer.py` + Service | Deterministische Checks + KI zusammenführen → `AnalysisReport` in DB | Integrationstest mit Mock-LLM | 6.1–6.12 |

## Phase 7 – Scoring und Anti-Pitch

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 7.1 | Scoring-Regeln | `lead_scoring.py`: gewichtete, dokumentierte Formel (Schwere, Relevanz, Belegqualität, Passung, Aufwand) | Tabellentest | 2.3 |
| 7.2 | Disqualifikation | Keine Website / kein belegter Befund / Qualität zu niedrig / kein Kontakt / gesperrt / schon verarbeitet | Test je Grund | 7.1 |
| 7.3 | Befund-Auswahl | 1 (max. 2) beste Befunde mit `MEASURED`/`DIRECTLY_OBSERVED` bevorzugt | Test | 7.1 |
| 7.4 | Anti-Pitch-Text | Aufbau aus Abschnitt 10, LLM oder Template; Absender Maximilian Fieber | Golden-Tests gegen Mock | 7.3, 6.9 |
| 7.5 | Text-Linter | Prüfung: verbotene Floskeln, Länge, keine Zahlen/Behauptungen, die nicht im Befund stehen | Test: erfundene „30 % weniger Anfragen“ → abgelehnt | 7.4 |
| 7.6 | Entwurf speichern | Entwurf + verwendete Finding-IDs + Zeitstempel in DB | Test | 7.4, 2.6 |

## Phase 8 – Mailer

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 8.1 | Draft-Modus | `.eml`-Dateien in `data/drafts/` (Default) | Datei lässt sich mit `email` parsen | 7.6 |
| 8.2 | Sperrliste | Tabelle + CLI zum Sperren (E-Mail, Domain), Prüfung vor jedem Versand/Entwurf | Test | 2.6 |
| 8.3 | Versandfreigabe | Pro Lead dokumentierte Rechtsgrundlage (Feld + Freitext + Datum + Person); ohne → kein Versand | Test: Versand ohne Freigabe → verweigert | 2.6 |
| 8.4 | SMTP-Modus | TLS, Credentials aus env, nur wenn global aktiviert **und** Freigabe vorhanden | Test mit Fake-SMTP (`aiosmtpd`/Mock) | 8.3 |
| 8.5 | Idempotenz | Kein zweiter Versand an denselben Empfänger für denselben Entwurf; Versuche protokolliert | Test | 8.4 |
| 8.6 | SMTP-Fehler | Auth-Fehler, Verbindungsabbruch → protokolliert, Status `failed`, kein Absturz | Test | 8.4 |

## Phase 9 – Orchestrierung

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 9.1 | Services | `crawl_service`, `analysis_service`, `outreach_service`: je ein Schritt pro Lead, nutzt `job_steps` | Tests | 2.8, 3.14, 6.13, 7.6 |
| 9.2 | Fehler-Isolation | Exception bei einem Lead → geloggt, gespeichert, nächster Lead | Test mit absichtlich kaputtem Lead | 9.1 |
| 9.3 | CLI | `main.py` mit Unterbefehlen: `run`, `find`, `crawl`, `analyze`, `draft`, `send`, `retry --lead ID --step X`, `block`, `status` | `--help` + Smoke-Test | 9.1 |
| 9.4 | Wiederaufnahme | Abbruch mitten im Lauf → Neustart überspringt erledigte Schritte | Test | 9.1 |
| 9.5 | Zusammenfassung | Am Ende: Anzahl je Status, Fehlerliste, erzeugte Entwürfe | Test der Ausgabe | 9.3 |

## Phase 10 – Abschluss

| # | Aufgabe | Ergebnis | Abnahme | braucht |
|---|---|---|---|---|
| 10.1 | Testsuite komplett | Alle Punkte aus Abschnitt 15 abgedeckt, Mapping-Tabelle in `docs/tests.md` | `pytest` grün, Ausgabe dokumentiert | alle |
| 10.2 | README | Alle Kapitel aus Abschnitt 16 inkl. Rechtshinweis § 7 UWG | Installation auf frischer venv nach README nachvollzogen | alle |
| 10.3 | Secret-Check | Scan (`git grep`, ggf. `gitleaks`) auf Keys/Passwörter | keine Treffer | alle |
| 10.4 | Definition of Done | Checkliste Abschnitt 18 abhaken, offene Punkte benennen | Datei `docs/status.md` | alle |

---

## Kritischer Pfad (empfohlene Reihenfolge)

1.1 → 1.3 → 1.5 → 2.2 → 3.1 → 3.3 → 3.4 → 4.2 → 3.7 → 3.9 → 3.14 → 4.6 → 4.7

Erst wenn 4.7 (echte Stichprobe) brauchbare Werte zeigt, lohnen Lead Finder, Analyse und Mailer.

## Offene Entscheidungen / externe Voraussetzungen

- Datenquelle: Google Places API Key oder Apify-Token (Kosten prüfen)
- DeepSeek API Key
- SMTP-Zugang (erst für Phase 8, Versand bleibt ohnehin aus)
- Rechtliche Grundlage für Versand (§ 7 UWG): nicht technisch lösbar, muss vor 8.4-Nutzung geklärt sein
