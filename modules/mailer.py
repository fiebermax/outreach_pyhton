"""Modul H: Entwürfe speichern (Standard) und – nur nach ausdrücklicher Freigabe – versenden.

Modus A (Standard): Entwurf als `.eml`-Datei + Datenbankeintrag. Es wird nichts verschickt.
Modus B (SMTP): nur wenn ALLE Bedingungen erfüllt sind:
  1. `MAIL_MODE=smtp` und `SEND_ENABLED=true` (zwei unabhängige Schalter, Standard: aus),
  2. dokumentierte Versandfreigabe für das Unternehmen mit Rechtsgrundlage (§ 7 UWG),
  3. kein Sperrvermerk, keine Sperrliste, noch nicht an diesen Empfänger versendet.
Eine öffentlich auffindbare E-Mail-Adresse ist KEINE Einwilligung. Das System nimmt das nie an.
Der Versand wird nie automatisch von der Pipeline ausgelöst, sondern nur per Einzelbefehl.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from config import Settings
from core.errors import MissingCredentialError, SendNotAllowed
from models.schemas import EmailDraft
from modules.database import Channel, Database, Outcome

LEGAL_BASES = {
    "einwilligung": "ausdrückliche, dokumentierte Einwilligung des Empfängers",
    "bestandskunde": "bestehende Kundenbeziehung im Sinne von § 7 Abs. 3 UWG",
    "anfrage_des_empfaengers": "der Empfänger hat selbst um Kontakt gebeten",
}


def build_message(draft: EmailDraft, sender_name: str, sender_address: str | None, draft_id: int) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_address}>" if sender_address else sender_name
    msg["To"] = draft.recipient
    msg["Subject"] = draft.subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=(sender_address or "entwurf.local").rsplit("@", 1)[-1])
    msg["X-Outreach-Draft-Id"] = str(draft_id)
    msg["X-Outreach-Findings"] = ",".join(draft.finding_ids)
    msg.set_content(draft.body, charset="utf-8")
    return msg


def save_draft(db: Database, company_id: int, draft: EmailDraft, settings: Settings, *,
               cooldown_days: int | None = None) -> int:
    """Speichert einen Entwurf als .eml und in der DB. Prüft vorher die Kontaktregeln."""
    decision = db.may_contact(company_id, email=draft.recipient,
                              cooldown_days=settings.contact_cooldown_days if cooldown_days is None else cooldown_days)
    if not decision.allowed:
        raise SendNotAllowed(f"kein Entwurf: {decision.reason}")
    report_id = db.latest_report_id(company_id)
    draft_id = db.insert_draft(company_id, report_id, draft, None)
    out_dir = settings.data_dir / "drafts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"entwurf_{draft_id:05d}.eml"
    path.write_bytes(bytes(build_message(draft, settings.sender_name, settings.sender_address, draft_id)))
    with db.transaction() as c:
        c.execute("UPDATE email_drafts SET eml_path = ? WHERE id = ?", (str(path.relative_to(settings.data_dir)), draft_id))
    return draft_id


def approve_sending(db: Database, company_id: int, legal_basis: str, note: str, approved_by: str) -> int:
    """Dokumentierte Freigabe. Ohne passende Rechtsgrundlage keine Freigabe."""
    if legal_basis not in LEGAL_BASES:
        raise ValueError(f"Rechtsgrundlage muss eine von {sorted(LEGAL_BASES)} sein")
    if len(note.strip()) < 10:
        raise ValueError("Bitte konkret dokumentieren (mind. 10 Zeichen), z. B. wann und wie eingewilligt wurde")
    if not approved_by.strip():
        raise ValueError("Wer gibt frei?")
    return db.add_send_approval(company_id, legal_basis, note, approved_by)


def check_send_allowed(db: Database, draft_id: int, settings: Settings) -> None:
    """Wirft SendNotAllowed mit konkretem Grund, wenn auch nur eine Bedingung fehlt."""
    if not settings.smtp_allowed:
        raise SendNotAllowed("Versand ist deaktiviert (MAIL_MODE=smtp und SEND_ENABLED=true nötig)")
    row = db.get_draft(draft_id)
    if row is None:
        raise SendNotAllowed(f"Entwurf {draft_id} existiert nicht")
    if row["status"] != "draft":
        raise SendNotAllowed(f"Entwurf {draft_id} hat Status {row['status']}")
    company = db.get_company(row["company_id"])
    if company["do_not_contact"]:
        raise SendNotAllowed(f"Unternehmen gesperrt: {company['do_not_contact_reason']}")
    if db.is_blocked(email=row["recipient"], domain=company["domain"]):
        raise SendNotAllowed("Empfänger oder Domain auf Sperrliste")
    if db.already_sent_to(row["company_id"], row["recipient"]):
        raise SendNotAllowed("an diesen Empfänger wurde bereits versendet")
    if db.active_send_approval(row["company_id"]) is None:
        raise SendNotAllowed("keine dokumentierte Versandfreigabe (Rechtsgrundlage) für dieses Unternehmen")
    if not settings.smtp_host or not settings.sender_address:
        raise MissingCredentialError("SMTP_HOST und SENDER_ADDRESS müssen gesetzt sein")


SmtpFactory = Callable[[str, int, ssl.SSLContext], smtplib.SMTP]


def _default_smtp(host: str, port: int, context: ssl.SSLContext) -> smtplib.SMTP:
    if port == 465:
        return smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.starttls(context=context)
    return smtp


def send_draft(db: Database, draft_id: int, settings: Settings, smtp_factory: SmtpFactory = _default_smtp) -> None:
    """Versendet genau einen Entwurf. Jeder Versuch wird protokolliert, Fehler stoppen sauber."""
    check_send_allowed(db, draft_id, settings)
    row = db.get_draft(draft_id)
    draft = EmailDraft(recipient=row["recipient"], subject=row["subject"], body=row["body"],
                       finding_ids=json.loads(row["finding_ids_json"]))
    msg = build_message(draft, settings.sender_name, settings.sender_address, draft_id)
    try:
        with smtp_factory(settings.smtp_host, settings.smtp_port, ssl.create_default_context()) as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.require_secret("smtp_password"))
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # Keine Zugangsdaten ins Protokoll: nur Fehlerart und Code.
        error = f"{type(exc).__name__}: {getattr(exc, 'smtp_code', '')}".strip()
        db.log_send_attempt(draft_id, False, error)
        db.set_draft_status(draft_id, "failed")
        raise
    db.log_send_attempt(draft_id, True, None)
    db.set_draft_status(draft_id, "sent")
    db.record_contact(row["company_id"], Channel.EMAIL, Outcome.SENT, recipient=row["recipient"],
                      draft_id=draft_id, source="system", note=f"Entwurf {draft_id}")


def eml_path(settings: Settings, row) -> Path | None:
    return settings.data_dir / row["eml_path"] if row and row["eml_path"] else None
