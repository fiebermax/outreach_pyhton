# Extraktions- und Qualitätsregeln

Dieses Dokument beschreibt die Regeln, nach denen Text, Kontakte und Copyright
extrahiert werden. Schwellenwerte sind Startwerte und werden in Paket 4.5/4.7
an gelabelten Beispielen und einer echten Stichprobe überprüft.

## Textwahl (Stufe 2 vs. Stufe 3) – `content_extractor.choose_text`

| # | Bedingung | Ergebnis |
|---|---|---|
| 1 | weder sichtbarer Text noch Haupttext | `NONE` |
| 2 | Haupttext (Trafilatura) fehlt oder < 80 Zeichen | sichtbarer Text (`HTML_PARSER`) |
| 3 | < 90 % der Haupttext-Wörter kommen im sichtbaren Text vor | `HTML_PARSER` (Haupttext stammt aus unsichtbaren Quellen) |
| 4 | Haupttext < 15 % des sichtbaren Texts **und** sichtbarer Text > 1500 Zeichen | `HTML_PARSER` (Leistungslisten nicht verlieren) |
| 5 | sonst | Haupttext (`TRAFILATURA`) |

`<noscript>` wird vor beiden Stufen entfernt. Begründung: Trafilatura liefert auf
reinen JavaScript-Seiten sonst „Bitte aktivieren Sie JavaScript“ als Inhalt
(reproduziert mit `tests/fixtures/sites/js-only.html`). Der noscript-Text wird
stattdessen als JS-Signal gespeichert.

## JS-Signale

- noscript-Hinweis vorhanden
- leeres App-Element (`#root`, `#app`, `#__next`, …)
- Skripte vorhanden bei < 200 Zeichen sichtbarem Text

## Kontakte

- Kontakte, Footer und Copyright kommen aus dem **ganzen** sichtbaren Text, weil die
  Haupttext-Extraktion den Footer absichtlich entfernt.
- Jeder Fund speichert Wert (normalisiert), Original, Seite, Textumgebung und Herkunft
  (`LINK` = `mailto:`/`tel:`, `TEXT` = nur Text).
- Telefonnummern im Text nur mit Kennzeichnung (Tel., Telefon, Mobil …) oder im Format
  `+49`/`0049`. Fax wird ignoriert. Suche zeilenweise, Beschriftung in eigener Zeile wird
  mit der Folgezeile verbunden. Lieber eine Nummer übersehen als eine PLZ als Telefon melden.
- `tel:`-Links werden URL-dekodiert; bei kaputten Links zählt der gültige Nummernanfang,
  das Original bleibt in `raw` erhalten.

## Copyright

- Muster `©`, `(c)`, `Copyright`, optional mit Text dazwischen, Jahr oder Bereich.
- Bereich zählt mit Endjahr. Plausibel: 1990 bis aktuelles Jahr + 1 (zur Laufzeit ermittelt).
- Erst Footer, dann ganzer Text; Fundstelle wird gespeichert.
- Bekannte Grenze: Jahre, die per JavaScript eingesetzt werden, sieht erst Stufe 4.

## Erste Beobachtungen an echten Seiten (8 Handwerker, Jettingen-Scheppach, 29.10.2026)

Nur Startseiten, Stufen 1–3. Keine Statistik, sondern Anlass für Regeln:

- 7 von 8 lieferten verwertbaren Text, Trafilatura-Überlappung jeweils 100 %.
- 1 Seite (27 Zeichen, 15 Skripte) braucht JavaScript-Rendering → Stufe 4.
- 3 Telefonnummern wurden zunächst übersehen (`+ 49`, URL-kodierter `tel:`-Link,
  Beschriftung in eigenem Element) – behoben, jetzt Testfälle.
- Echter Tippfehler gefunden („Innen-AuBenputz“ in einer Überschrift), echter kaputter `tel:`-Link.

## Qualitätsbewertung einer Seite – `quality_control.assess_page`

Die erste zutreffende Regel entscheidet:

| # | Bedingung | Status | Browser-Stufe? |
|---|---|---|---|
| 1 | Schutz-Hinweis (Captcha, „Just a moment…“, Access denied) in Titel/Überschriften/Textanfang **und** < 150 Wörter | `BLOCKED` | nein – wird nie umgangen |
| 2 | Fehlerhinweis (404, „Seite nicht gefunden“, 5xx-Text) in Titel/H1–H2 **und** < 150 Wörter | `HTTP_ERROR` (Soft-404) | nein |
| 3 | < 15 Wörter, JS-Hinweise, noch nicht gerendert | `JS_RENDERING_REQUIRED` | ja |
| 4 | < 15 Wörter, ohne JS-Hinweis, noch nicht gerendert | `EMPTY_CONTENT` | ja (einmal versuchen) |
| 5 | < 15 Wörter nach Rendering | `EMPTY_CONTENT` | nein – endgültig |
| 6 | Platzhalter („im Aufbau“, „coming soon“, Domain zu verkaufen) **und** < 150 Wörter | `EMPTY_CONTENT` | nein |
| 7 | ≥ 50 Wörter **und** Score ≥ `MIN_EXTRACTION_QUALITY` (Standard 0,5) | `SUCCESS` | wenn JS-Hinweise und < 100 Wörter |
| 8 | sonst | `PARTIAL_SUCCESS` | wie 7 |

Die Wortgrenze von 150 bei Regel 1, 2 und 6 verhindert Fehlalarme: Eine lange Seite, die
„Captcha“ oder „404“ im Text erwähnt, ist keine Sperr- oder Fehlerseite.

**Score (0–1)** = 0,45 × Textmenge (Wörter / 150, max. 1) + 0,25 × Anteil eindeutiger Zeilen
+ 0,15 × Überschrift vorhanden + 0,15 × Titel (1 = aussagekräftig, 0,5 = generisch wie „Home“).

## Website-Ebene – `quality_control.assess_site`

- Startseite und alle angefragten Unterseiten verwertbar → `SUCCESS`
- Unterseite fehlgeschlagen oder Startseite nur teilweise → `PARTIAL_SUCCESS`
- Startseite unbrauchbar, Unterseite verwertbar → `PARTIAL_SUCCESS`
- nichts verwertbar → Status der Startseite, damit die Ursache erhalten bleibt
  (`BLOCKED`/`TIMEOUT`/`HTTP_ERROR`/`INVALID_URL` = Website nicht zugänglich;
  `EMPTY_CONTENT`/`JS_RENDERING_REQUIRED`/`FAILED` = Extraktion gescheitert)
- Eine nicht verlinkte Unterseite (z. B. fehlendes Impressum) ist ein möglicher Befund der
  Website, kein unvollständiger Crawl.

## Stand der Validierung

- Lokale Fixtures: alle 6 Seitentypen erhalten den erwarteten Status (`tests/test_quality_control.py`).
- Echte Startseiten (29.10.2026): 7 von 8 Handwerkerseiten `SUCCESS` (Score 0,63–1,00),
  1 korrekt `JS_RENDERING_REQUIRED`. Händisch gegengeprüft im Quelltext, kein Fehlurteil gefunden.
- Noch offen (Paket 4.5/4.7): größere gelabelte Stichprobe mit Fehlerquote.

## Browser-Stufe und Cookie-Banner (Pakete 3.9–3.11)

- Rendering im Handy-Viewport 375 px. Warten: `domcontentloaded` Pflicht, `networkidle` optional
  (3 s, bei 2 von 8 echten Seiten nie erreicht), danach kurz auf sichtbaren Text.
- **Overflow** zählt nur, wenn sich die Seite tatsächlich seitlich scrollen lässt. `scrollWidth`
  allein meldet bei `overflow-x: hidden` 800 px statt 375 px – nachgewiesen mit
  `overflow-hidden.html`. Verursacher-Elemente werden als Indiz gespeichert; eine breite Tabelle
  in einem scrollbaren Container zählt nicht. Bei 8 echten Seiten: kein Overflow.
- **Consent-Dialoge** werden vor jeder Textauswertung entfernt (bekannte Tools + generische
  Namen, nur wenn der Text nach Cookie-Hinweis klingt; `<html>/<body>/<main>` nie).
- **Klickregel** (`CONSENT_POLICY`): Standard `reject` – erst prüfen, ob der Inhalt ohne Klick
  lesbar ist; wenn nicht, höchstens „Ablehnen/Nur notwendige“. „Akzeptieren“ nur bei
  `reject_then_accept`. „Einstellungen“ wird nie geklickt. Jeder Klick wird protokolliert.
  Buttons werden nur nach Beschriftung eingeordnet und nur in Frames mit Cookie-Kontext gesucht.
- **Echter Fall hins-zimmerei.de:** Ohne Banner-Entfernung galt der Banner-Text als Inhalt
  (41 Wörter, `PARTIAL_SUCCESS`) – Fehlurteil, behoben. Diagnose: Startseite hat auch nach
  Zustimmung nur 3 Wörter und 6 Bilder. Die Statusmeldung behauptet deshalb keine Ursache,
  sondern nennt nur das Gemessene.
