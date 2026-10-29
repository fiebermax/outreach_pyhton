"""Import bisheriger Kontakte aus einer CSV-Datei (z. B. aus Excel exportiert).

Damit das System weiß, wen du schon vor dem Projekt angesprochen hast.

Spalten (Kopfzeile, Trennzeichen ; oder ,):
    firma; website; email; datum; kanal; ergebnis; notiz
Pflicht: firma, datum. Datum als TT.MM.JJJJ oder JJJJ-MM-TT.
kanal:    email | telefon | besuch | brief | social | sonstiges   (Standard: email)
ergebnis: gesendet | keine_antwort | antwort | interessiert | kein_interesse | termin | kunde | unzustellbar | sperren
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from models.schemas import Lead
from modules.database import Channel, Database, Outcome

_CHANNELS = {
    "email": Channel.EMAIL, "e-mail": Channel.EMAIL, "mail": Channel.EMAIL, "telefon": Channel.PHONE,
    "phone": Channel.PHONE, "anruf": Channel.PHONE, "besuch": Channel.VISIT, "vor ort": Channel.VISIT,
    "brief": Channel.LETTER, "post": Channel.LETTER, "social": Channel.SOCIAL, "linkedin": Channel.SOCIAL,
    "instagram": Channel.SOCIAL, "facebook": Channel.SOCIAL, "sonstiges": Channel.OTHER, "": Channel.EMAIL,
}
_OUTCOMES = {
    "gesendet": Outcome.SENT, "": Outcome.SENT, "keine_antwort": Outcome.NO_REPLY, "keine antwort": Outcome.NO_REPLY,
    "antwort": Outcome.REPLIED, "interessiert": Outcome.INTERESTED, "kein_interesse": Outcome.NOT_INTERESTED,
    "kein interesse": Outcome.NOT_INTERESTED, "termin": Outcome.MEETING, "kunde": Outcome.CUSTOMER,
    "unzustellbar": Outcome.BOUNCED, "sperren": Outcome.DO_NOT_CONTACT, "widerspruch": Outcome.DO_NOT_CONTACT,
}


@dataclass
class ImportReport:
    imported: int = 0
    matched_existing: int = 0
    created: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_date(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Datum nicht lesbar: {value!r} (erwartet TT.MM.JJJJ)")


def import_contacts(db: Database, source: Path | str) -> ImportReport:
    """Importiert Zeile für Zeile; eine fehlerhafte Zeile stoppt den Import nicht."""
    text = Path(source).read_text(encoding="utf-8-sig") if isinstance(source, Path) else source
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
    rows = csv.DictReader(io.StringIO(text), dialect=dialect)
    report = ImportReport()
    for line_no, raw in enumerate(rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        try:
            if not row.get("firma"):
                raise ValueError("Spalte 'firma' leer")
            channel = _CHANNELS.get(row.get("kanal", "").lower())
            outcome = _OUTCOMES.get(row.get("ergebnis", "").lower())
            if channel is None:
                raise ValueError(f"unbekannter Kanal {row['kanal']!r}")
            if outcome is None:
                raise ValueError(f"unbekanntes Ergebnis {row['ergebnis']!r}")
            when = _parse_date(row.get("datum", ""))
            email = row.get("email") or None
            website = row.get("website") or None

            company_id = db.find_company(website=website, email=email, name=row["firma"])
            if company_id is None:
                lead = Lead(name=row["firma"], website=website, email=email, source="manual_import")
                company_id = db.upsert_company(lead).company_id
                report.created += 1
            else:
                report.matched_existing += 1
            db.record_contact(company_id, channel, outcome, occurred_at=when, recipient=email,
                              note=row.get("notiz") or None, source="import")
            report.imported += 1
        except ValueError as exc:
            report.errors.append(f"Zeile {line_no}: {exc}")
    return report
