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
