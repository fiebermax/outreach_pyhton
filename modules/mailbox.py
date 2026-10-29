"""Postfach-Abgleich per IMAP (IONOS, Gmail, …): Entwürfe ins Postfach legen, „gesendet“ und „Antwort“ erkennen.

IONOS: IMAP_HOST=imap.ionos.de · Gmail: IMAP_HOST=imap.gmail.com mit App-Passwort.
Es wird nur gelesen und in den Entwürfe-Ordner geschrieben – versendet wird hier nichts.
"""

from __future__ import annotations

import imaplib
import re
import time
from dataclasses import dataclass, field
from email.message import EmailMessage

from config import Settings
from core.errors import MissingCredentialError
from modules.database import Channel, Database, Outcome

_FALLBACK = {"\\Drafts": ["Entwürfe", "Drafts", "INBOX.Drafts", "[Gmail]/Entw&APw-rfe"],
             "\\Sent": ["Gesendete Objekte", "Gesendet", "Sent", "INBOX.Sent", "[Gmail]/Gesendet"]}


@dataclass
class SyncResult:
    sent: list[str] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)


def connect(settings: Settings) -> imaplib.IMAP4_SSL:
    if not settings.imap_host:
        raise MissingCredentialError("IMAP_HOST ist nicht gesetzt (IONOS: imap.ionos.de, Gmail: imap.gmail.com).")
    user = settings.imap_user or settings.sender_address
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    imap.login(user, settings.require_secret("imap_password"))
    return imap


def find_folder(imap, flag: str) -> str:
    """Ordner mit Sonderkennzeichen (\\Drafts, \\Sent) finden; sonst übliche Namen."""
    _, lines = imap.list()
    names = []
    for raw in lines or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) "?[^ "]*"? (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if flag.lower() in m.group("flags").lower():
            return name
        names.append(name)
    for candidate in _FALLBACK[flag]:
        if candidate in names:
            return candidate
    raise MissingCredentialError(f"Ordner {flag} im Postfach nicht gefunden (vorhanden: {', '.join(names[:10])})")


def put_draft(settings: Settings, message: EmailMessage) -> str:
    imap = connect(settings)
    try:
        folder = find_folder(imap, "\\Drafts")
        imap.append(f'"{folder}"', "(\\Draft)", imaplib.Time2Internaldate(time.time()), bytes(message))
        return folder
    finally:
        imap.logout()


def _has_mail(imap, folder: str, criterion: str, address: str) -> bool:
    if imap.select(f'"{folder}"', readonly=True)[0] != "OK":
        return False
    _, data = imap.search(None, criterion, f'"{address}"')
    return bool(data and data[0].split())


def sync(db: Database, settings: Settings) -> SyncResult:
    """Für jeden Entwurf: im Gesendet-Ordner an den Empfänger? → gesendet. Im Posteingang von ihm? → Antwort."""
    result = SyncResult()
    imap = connect(settings)
    try:
        sent_folder = find_folder(imap, "\\Sent")
        for d in db.list_drafts():
            outcomes = {r["outcome"] for r in db.contact_history(d["company_id"])}
            if "sent" not in outcomes and _has_mail(imap, sent_folder, "TO", d["recipient"]):
                db.record_contact(d["company_id"], Channel.EMAIL, Outcome.SENT, recipient=d["recipient"],
                                  draft_id=d["id"], note="im Gesendet-Ordner gefunden", source="system")
                if d["status"] == "draft":
                    db.set_draft_status(d["id"], "sent")
                result.sent.append(d["company_name"])
                outcomes.add("sent")
            if "sent" in outcomes and "replied" not in outcomes and _has_mail(imap, "INBOX", "FROM", d["recipient"]):
                db.record_contact(d["company_id"], Channel.EMAIL, Outcome.REPLIED, recipient=d["recipient"],
                                  draft_id=d["id"], note="Antwort im Posteingang", source="system")
                result.replied.append(d["company_name"])
    finally:
        imap.logout()
    return result
