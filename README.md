# Outreach-System

Modulare Python-Pipeline: lokale Unternehmen finden, deren Websites crawlen und prüfen,
belegte Befunde bewerten und daraus zurückhaltende E-Mail-Entwürfe erstellen.
Versand ist standardmäßig deaktiviert.

Stand: Projektgerüst (Arbeitspaket 1.1). Fahrplan: [`docs/ARBEITSPAKETE.md`](docs/ARBEITSPAKETE.md).
Vollständige Dokumentation folgt in Paket 10.2.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # danach Keys eintragen
```

## Tests

```bash
pytest
```
