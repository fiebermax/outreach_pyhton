# Stichprobe echter Websites (Paket 4.7)

Durchgeführt am 29.10.2026 mit `scripts/sample_crawl.py --queries 4 --max-seconds 60`.
Quelle: Google Places, Suchanfragen aus `niche.toml` (Schreinerei in Jettingen-Scheppach, Zimmerei in
Dinkelscherben, Elektriker in Ichenhausen, Dachdecker in Burgau). Nur Firmen mit eigener Website.

**Wichtig:** 10 Websites sind eine kleine Stichprobe. Die Zahlen unten sind gemessen, aber keine
belastbare Erfolgsquote. Für eine Quote braucht es einen größeren Lauf (siehe Ende).

## Rohergebnis (zweiter Lauf, vor den Korrekturen unten)

Abfragen: 4 · Websites: 10 · Zeitlimit je Website: 60s
Übersprungen: kein Website-Eintrag: 2, Verzeichnis/Social-Profil: 5, außerhalb Nische: 0

| Firma | Branche | Status | Seiten (ok/ges.) | Wörter Start | Methode Start | Overflow | Tel. Link/Text | E-Mail | © | Zeit |
|---|---|---|---|---|---|---|---|---|---|---|
| Rolf Nagel GmbH | Zimmerei/Holzbau | SUCCESS | 4/4 | 434 | TRAFILATURA | False | 4/1 | 2 | 2026 | 9s |
| Elektro Stempfle | Elektro | SUCCESS | 5/5 | 459 | TRAFILATURA | False | 0/1 | 1 | – | 12s |
| Elektro Suntec | Elektro | SUCCESS | 4/4 | 244 | TRAFILATURA | None | 1/1 | 1 | 2026 | 7s |
| Elektro Gelfert GmbH | Elektro | BLOCKED | 0/1 | – | NONE | None | 0/0 | 0 | – | 0s |
| Hans-Jörg Thoma | Zimmerei/Holzbau | SUCCESS | 5/5 | 119 | TRAFILATURA | True | 0/2 | 1 | – | 5s |
| Schmid + Nagel GmbH / ÜBERDACH | Zimmerei/Holzbau | PARTIAL_SUCCESS | 5/5 | 219 | TRAFILATURA | False | 1/0 | 1 | 2026 | 24s |
| DAFA Dachsanierungen / Dachdec | Zimmerei/Holzbau | SUCCESS | 6/6 | 404 | TRAFILATURA | False | 1/0 | 1 | – | 5s |
| Elektro-Hausgeräte Bibracher | Elektro | SUCCESS | 3/3 | 248 | TRAFILATURA | False | 0/2 | 1 | 2026 | 13s |
| kds GmbH komplette Dachsanieru | Bau/Sanierung | SUCCESS | 6/6 | 513 | TRAFILATURA | False | 2/1 | 9 | 2024 | 13s |
| Elektro Rick | Elektro | TIMEOUT | 0/1 | – | NONE | None | 0/0 | 0 | – | 60s |

Laufzeit gesamt: 61s

Von den Places-Treffern übersprungen: 2 ohne Website-Eintrag, 5 nur mit Verzeichnis-/Social-Profil.

## Ergebnis in Zahlen (gemessen)

- 10 Websites, Gesamtlaufzeit 61 s bei 4 parallel.
- 8 × verwertbar (`SUCCESS`/`PARTIAL_SUCCESS`), 1 × `BLOCKED` (HTTP 403), 1 × `TIMEOUT`.
- Nach Korrektur der Ursache (Fall 1) ist auch der Timeout-Fall verwertbar: 9 von 10.
- 1 echter Handy-Overflow gefunden und unabhängig bestätigt (Fall 2).

## Geklärte Auffälligkeiten

### Fall 1 – elektro-rick.base44.app: `TIMEOUT` nach 60 s → **Fehler im Crawler, behoben**
HTTP-Abruf 0,6 s, reines Playwright 3 s. Ursache: Die Cookie-Banner-Suche ruft `evaluate` in jedem
iframe auf; die Seite hat ein leeres iframe ohne URL, in dem nie ein Dokument entsteht – Playwright
wartet dann unbegrenzt. Jetzt hat jeder Frame-Zugriff ein eigenes Zeitlimit (3 s), hängende Frames
werden übersprungen. Test `test_hanging_iframe_does_not_block_render` scheitert mit dem alten Code
(21 s) und besteht mit dem neuen. Danach: `SUCCESS` in 22 s, 6 Seiten.

### Fall 2 – dachdecker-thoma.com: Overflow `True` → **echter Befund**
Unabhängig gemessen: bei 375 px Viewport ist das Dokument 1040 px breit, die Seite lässt sich
665 px seitlich scrollen. Screenshot nach dem Scrollen zeigt die abgeschnittene Desktop-Seite.
(Screenshot nicht im Repository, da Inhalte einer fremden Website.)

### Fall 3 – elektro-suntec.de / bibracher.de: Overflow nicht gemessen → **vorübergehender Serverfehler, Wiederholung ergänzt**
Notizen: HTTP-Abruf 200, Browser-Aufruf kurz danach `HTTP 502`. Bibracher war im ersten Lauf
betroffen, im zweiten nicht. Das Ergebnis blieb korrekt „nicht gemessen“ statt erfunden. Neu: ein
Wiederholungsversuch der Browser-Stufe bei vorübergehenden Fehlern
(`test_browser_retried_once_on_temporary_error`).

### Nicht geändert – elektro-gelfert.com: HTTP 403
Der Server lehnt den ehrlichen User-Agent ab. Das System umgeht Zugriffssperren bewusst nicht und
startet bei einer Sperre auch keinen Browser. Status `BLOCKED`, Lead wird übersprungen.

## Nächster Schritt für eine belastbare Quote
Lauf mit `--queries 15 --max-sites 60` (15 Places-Abfragen) und Stichprobe von 10 Seiten händisch
gegenprüfen (Text vollständig? Kontakte richtig? Befunde echt?).
