# Outreach-System

Lokale Pipeline für die B2B-Akquise eines Webdesigners: lokale Handwerksbetriebe finden, deren
Websites gründlich crawlen, konkrete und **belegte** Probleme feststellen und daraus kurze,
unaufdringliche E-Mail-Entwürfe („Anti-Pitch“) erstellen.

**Es wird nichts automatisch versendet.** Entwürfe landen als `.eml`-Dateien in `data/drafts/`.

## Inhalt

1. [Ablauf](#ablauf) · 2. [Architektur](#architektur) · 3. [Installation](#installation) ·
4. [Konfiguration](#konfiguration) · 5. [Benutzung](#benutzung) · 6. [Tests](#tests) ·
7. [Fehlerbehebung](#fehlerbehebung) · 8. [Grenzen](#bekannte-grenzen) · 9. [Recht & Datenschutz](#recht-und-datenschutz)

## Ablauf

```
niche.toml ──► Lead-Suche (Google Places) ──► Datenbank (Duplikate, Kontakthistorie)
                                                    │
        ┌───────────────────────────────────────────┘
        ▼
Crawl je Website: HTTP → HTML-Text → Trafilatura → (bei Bedarf) Browser + Handy-Layout
        │          Qualitätskontrolle entscheidet: verwertbar? nächste Stufe? Fehlergrund?
        ▼
Analyse: deterministische Prüfungen (HTTPS, Mobil, Kontakt, Links …) + KI (Unklares, kein Tippfehler-Suchen)
        │          jeder KI-Befund braucht ein wörtliches Zitat aus der Website, sonst verworfen
        ▼
Scoring: nur belastbare, geschäftlich relevante Befunde; Ausschluss bei Sperre/fehlender E-Mail …
        ▼
Entwurf: feste Textbausteine je Befundtyp + automatische Textprüfung → .eml in data/drafts/
        ▼
Excel-Übersicht data/firmen.xlsx: alle Firmen, gelb = manuell prüfen (mit Grund)

Oberfläche (python main.py ui): Website eingeben → Screenshots + Design-Urteil (veraltet? ansprechend?)
```

Jeder Schritt ist pro Unternehmen in der Datenbank vermerkt. Ein zweiter Lauf überspringt
Erledigtes; ein Fehler bei einem Unternehmen stoppt den Lauf nicht.

## Architektur

```
config.py              zentrale Einstellungen (.env)
niche.toml             Zielnische: Branchen, Orte, Ausschlüsse, Angebotspassung
main.py                Kommandozeile
core/                  Fehlertypen, Logging (maskiert Zugangsdaten), Retry, Normalisierung, Nische
models/schemas.py      Pydantic-Modelle mit eingebauten Regeln (z. B. kein Erfolg ohne Text)
modules/
  lead_finder.py       Google Places, Umkreis, Cache, Kostenbremse
  fetcher.py           Stufe 1: HTTP
  content_extractor.py Stufen 2/3: Text, Kontakte mit Fundstelle, Copyright, Consent-Banner entfernen
  browser.py           Stufe 4: Playwright, Cookie-Banner, Handy-Layout-Messung
  scraper.py           Stufensteuerung pro Seite und Website
  page_selection.py    relevante Unterseiten (Kontakt, Impressum, Leistungen, …)
  quality_control.py   Qualitätsbewertung (docs/qualitaet.md)
  website_checks.py    deterministische Prüfungen
  llm.py / analyzer.py KI-Analyse mit Belegprüfung und Injection-Schutz
  design_review.py     Screenshots, Design-Signale, Design-Urteil (Regeln + DeepSeek)
  excel_export.py      Excel-Übersicht aller Firmen
  web_ui.py            lokale Oberfläche (Standardbibliothek, kein Framework)
  lead_scoring.py      Bewertung und Auswahl der Befunde
  pitch.py             Anti-Pitch-Bausteine und Textprüfung
  mailer.py            .eml-Entwürfe; Versand nur mit Freigabe (standardmäßig aus)
  database.py          SQLite, Duplikate, Job-Schritte, Kontakthistorie, Sperrliste
  contact_import.py    bisherige Kontakte aus CSV
services/              Schritte crawl / analyze / score+draft für alle Leads
tests/                 pytest, lokaler Testserver mit Beispielseiten
docs/                  Arbeitspakete, Qualitätsregeln, Stichprobe
data/, logs/           lokal, nicht im Repository
```

## Installation

Voraussetzung: Python 3.11 oder neuer.

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

```bash
playwright install chromium
```

```bash
cp .env.example .env
```

Danach `.env` öffnen und mindestens `GOOGLE_PLACES_API_KEY` eintragen. `DEEPSEEK_API_KEY` ist
optional – ohne ihn läuft die Analyse nur mit den deterministischen Prüfungen.

## Konfiguration

### Google Places

1. Google-Cloud-Projekt mit aktivem Rechnungskonto.
2. „Places API (New)“ aktivieren.
3. API-Schlüssel anlegen und unter „API-Einschränkungen“ auf „Places API (New)“ beschränken.
4. Unter „Kontingente“ ein Tageslimit und unter „Abrechnung“ einen Budget-Alarm setzen.

Zusätzlich bremst das System selbst: `PLACES_MAX_REQUESTS_PER_RUN` (Standard 30) und
`PLACES_MONTHLY_LIMIT` (800, in der Datenbank gezählt). Antworten werden 30 Tage zwischengespeichert.
Ein voller Lauf über alle Branchen und Orte aus `niche.toml` braucht rund 100 Abfragen –
mit `--max-queries` begrenzen.

### DeepSeek

Schlüssel unter platform.deepseek.com. `AI_MAX_COST_EUR` begrenzt die Kosten pro Lauf.
Die Preise in `.env` (`DEEPSEEK_PRICE_*`) sind eine **Annahme** – bitte mit der aktuellen
Preisliste abgleichen.

### Nische

`niche.toml` legt fest, wer gesucht wird (Pilot: Handwerk im Raum Jettingen-Scheppach/Günzburg),
welche Ketten ausgeschlossen sind und wie gut welche Befundart zu deinem Angebot passt.

### Wichtige Einstellungen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `MAIL_MODE` | `draft` | `draft` = nur Entwürfe, `smtp` = Versand möglich |
| `SEND_ENABLED` | `false` | zweiter, unabhängiger Schalter für den Versand |
| `CONSENT_POLICY` | `reject` | Cookie-Banner: höchstens „Ablehnen“ klicken |
| `CONTACT_COOLDOWN_DAYS` | `365` | Sperrfrist nach jedem Kontakt |
| `CRAWL_MAX_PAGES` / `CRAWL_MAX_SECONDS_PER_SITE` | `8` / `90` | Grenzen je Website |
| `MIN_EXTRACTION_QUALITY` | `0.5` | Mindest-Score für „verwertbar“ |

## Benutzung

Alles auf einmal (fragt Ort und Branche ab, Enter = Werte aus `niche.toml`):

```bash
python main.py run
```

Beispielworkflow für einen kleinen, günstigen Lauf:

```bash
python main.py run --ort Jettingen-Scheppach --branche Maler --max-queries 1 --yes
```

```bash
python main.py leads
```

```bash
python main.py drafts
```

Die `.eml`-Dateien lassen sich mit jedem Mailprogramm öffnen, prüfen und von Hand versenden.

### Excel-Übersicht

Nach jedem `run` wird `data/firmen.xlsx` geschrieben (oder von Hand: `python main.py export`).
Blatt „Firmen“: eine Zeile pro Firma mit Kontaktdaten, Status je Schritt, Score, Design-Urteil.
**Gelb = manuell prüfen** (kein Entwurf entstanden), die Spalte „Grund“ sagt warum – z. B.
„Crawl übersprungen: nur Verzeichnis-/Social-Profil“ oder „kein belastbarer Befund“. Diese Zeilen
stehen oben. Blatt „Befunde“: alle Befunde der letzten Analyse.

```bash
python main.py export
```

### Oberfläche: Screenshot + Design-Urteil

```bash
python main.py ui
```

Unter Windows: Doppelklick auf `start.bat`. Öffnet http://127.0.0.1:8765 (nur lokal erreichbar).

- URL eingeben oder bei einer Firma auf „Prüfen“ klicken.
- Es entstehen Screenshots am Computer (1366 px) und am Handy (390 px), beide herunterladbar.
- Das Urteil (modern / in Ordnung / veraltet / sehr veraltet, Note 1–10, „Schlecht“, „Gut“,
  Aufhänger fürs Gespräch) kommt von DeepSeek, wenn `DEEPSEEK_API_KEY` gesetzt ist, sonst aus festen
  Regeln. Es geht nur ums Design, nicht um Tippfehler.
- Gespeichert wird alles in `data/design/<domain>/`; das Urteil erscheint auch in der Excel-Datei.
- **Alles in der Oberfläche:** „Neue Suche“ (Ort, Branche, Anzahl Suchanfragen) startet den ganzen Lauf inkl.
  Design-Prüfung; Klick auf eine Firma zeigt Entwurf (bearbeitbar), Befunde, Design-Urteil und Kontaktverlauf.
  Ohne Entwurf, aber mit schlechtem Design: „E-Mail-Entwurf aus dem Design-Urteil erstellen“.
- **Postfach (IONOS oder Gmail):** `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` in `.env` (IONOS: `imap.ionos.de`,
  Gmail: `imap.gmail.com` mit App-Passwort). Dann legt „Als Entwurf ins Postfach legen“ die Mail in deinen
  Entwürfe-Ordner; du sendest sie selbst. „Postfach abgleichen“ erkennt, was gesendet wurde und wer geantwortet hat.

Kontakte festhalten (auch Telefon, Besuch, frühere Kontakte):

```bash
python main.py contact --lead 12 --kanal telefon --ergebnis kein_interesse --notiz "hat schon eine Agentur"
```

```bash
python main.py contacts import docs/kontakte_vorlage.csv
```

```bash
python main.py contacts list
```

Sperren, Status, einzelne Schritte wiederholen:

```bash
python main.py block --email info@firma.de --grund "Widerspruch"
```

```bash
python main.py status
```

```bash
python main.py retry --lead 12 --step crawl
```

```bash
python main.py run --skip-find --lead 12
```

Fehlgeschlagene Schritte (z. B. Zeitüberschreitung) erneut versuchen:

```bash
python main.py run --skip-find --retry-failed
```

## Tests

```bash
pytest
```

Die Tests brauchen weder Internet noch API-Schlüssel noch einen Mailserver: Websites kommen von
einem lokalen Testserver (`tests/fixtures/sites/`), Google, DeepSeek und SMTP werden simuliert.
Die Browser-Tests starten ein echtes Headless-Chromium.

Stand 29.10.2026: 475 Tests bestanden. Ergebnisse mit echten Websites: `docs/stichprobe.md`
und `docs/qualitaet.md`.

## Fehlerbehebung

| Meldung | Ursache / Lösung |
|---|---|
| `GOOGLE_PLACES_API_KEY ist nicht gesetzt` | Schlüssel in `.env` eintragen |
| `Places API verweigert (403)` | API nicht aktiviert, Schlüssel eingeschränkt oder Abrechnung inaktiv |
| `Limit pro Lauf erreicht` / `Monatslimit erreicht` | gewollte Kostenbremse; Werte in `.env` anpassen |
| Crawl `BLOCKED` | Website lehnt den Zugriff ab (403/Captcha) – wird bewusst nicht umgangen |
| Crawl `JS_RENDERING_REQUIRED` / `EMPTY_CONTENT` | Seite liefert keinen Text; mit Browser versuchen (`--no-browser` weglassen) |
| Overflow „nicht gemessen“ | Browser-Aufruf scheiterte (z. B. Server-502); Grund steht im Fehlerprotokoll |
| `Executable doesn't exist` (Playwright) | `playwright install chromium` ausführen |
| `Versand ist deaktiviert` | gewollt; siehe Abschnitt Recht |

Logs: `logs/outreach.jsonl` (eine JSON-Zeile pro Eintrag, Zugangsdaten werden maskiert).

## Bekannte Grenzen

- Telefonnummern werden im Text nur mit Kennzeichnung („Tel.“, „Mobil“ …) oder im Format +49
  erkannt – bewusst vorsichtig, damit keine Postleitzahl als Nummer gilt.
- Inhalte in Bildern (z. B. Kontaktdaten als Grafik) werden nicht erkannt.
- Rechtschreib-/Tippfehler werden bewusst nicht mehr gesucht (kein Gesprächsanlass).
- KI-Einschätzungen zu Verständlichkeit/Widersprüchen zählen im Scoring kaum; die echte Stichprobe ist noch klein (`docs/stichprobe.md`).
- Performance wird nur grob aus einer einzelnen Messung beurteilt, nicht mit Lighthouse.
- Firmen ohne eigene Website werden markiert, aber nicht angeschrieben (separate Ansprache nötig).
- Die DeepSeek-API kann keine Bilder ansehen. Das Design-Urteil beruht auf gemessenen Signalen
  (Tabellen-Layout, veraltete Tags, feste Breite, Schriften, Bildqualität, Handy-Tauglichkeit …);
  den Screenshot beurteilst du selbst. Moderne, aber langweilige Seiten erkennt das nicht.

## Recht und Datenschutz

- **§ 7 UWG:** Werbe-E-Mails an Unternehmen ohne vorherige Einwilligung sind in Deutschland
  grundsätzlich unzulässig. Eine öffentlich auffindbare Adresse ist **keine** Einwilligung, und
  auch ein freundlicher, individueller Hinweis kann als Werbung gelten. Deshalb ist der Versand
  dreifach gesperrt: `MAIL_MODE=smtp`, `SEND_ENABLED=true` **und** eine dokumentierte Freigabe pro
  Unternehmen (`python main.py approve …` mit Rechtsgrundlage: Einwilligung, Bestandskunde oder
  Anfrage des Empfängers). Vor dem Versand werden Sperrliste, Widerspruch und Doppelversand geprüft.
  Ob und wie du Entwürfe nutzt, ist deine Entscheidung – im Zweifel rechtlich beraten lassen.
- **DSGVO:** Gespeichert werden geschäftliche Kontaktdaten aus öffentlichen Quellen mit Fundstelle.
  Widerspruch → `contact … --ergebnis sperren` sperrt dauerhaft. Datenbank, HTML-Kopien, Entwürfe
  und Logs liegen nur lokal in `data/` und `logs/` und sind von Git ausgeschlossen.
- **Google Places:** Inhalte nur begrenzt zwischenspeichern (Cache-Ablauf 30 Tage); `place_id`
  darf dauerhaft gespeichert werden.
- **Crawling:** ehrlicher User-Agent, Zugriffssperren und Captchas werden nie umgangen,
  Cookie-Banner werden standardmäßig höchstens abgelehnt.
