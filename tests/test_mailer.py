"""Entwürfe und kontrollierter Versand – mit Fake-SMTP, nie echter Versand."""

import email
import smtplib

import pytest

from config import Settings
from core.errors import SendNotAllowed
from models.schemas import EmailDraft, Lead
from modules.database import Channel, Database, Outcome
from modules.mailer import approve_sending, check_send_allowed, save_draft, send_draft

BODY = ("Guten Tag,\n\nauf Ihrer Startseite steht „Innen-AuBenputz“, gemeint ist vermutlich „Innen-Außenputz“.\n\n"
        "Falls Sie keine weiteren Nachrichten von mir möchten, genügt eine kurze Antwort.")


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    cid = db.upsert_company(Lead(name="N&H Bau", website="https://nh-bauunternehmen.de/", source="test")).company_id
    yield db, cid, tmp_path
    db.close()


def settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, data_dir=tmp_path, sender_address="kontakt@beispiel-webdesign.de")
    return Settings(**(base | kw))


def draft(recipient="info@nh-bauunternehmen.de"):
    return EmailDraft(recipient=recipient, subject="Kleiner Tippfehler auf nh-bauunternehmen.de", body=BODY,
                      finding_ids=["ai_spelling_1"])


SMTP_ON = dict(mail_mode="smtp", send_enabled=True, smtp_host="smtp.example.org", smtp_user="u", smtp_password="geheim123")


class FakeSMTP:
    sent: list = []

    def __init__(self, fail=None):
        self.fail = fail

    def __call__(self, host, port, context):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def send_message(self, msg):
        if self.fail == "send":
            raise smtplib.SMTPRecipientsRefused({"x": (550, b"no")})
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.sent = []


# --- Entwurf (Standardmodus) ---

def test_draft_saved_as_eml_and_in_db(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    row = db.get_draft(did)
    assert row["status"] == "draft" and row["eml_path"] == f"drafts/entwurf_{did:05d}.eml"
    msg = email.message_from_bytes((tmp / row["eml_path"]).read_bytes())
    assert msg["To"] == "info@nh-bauunternehmen.de" and "Tippfehler" in msg["Subject"]
    assert msg["X-Outreach-Findings"] == "ai_spelling_1"
    assert "Innen-Außenputz" in msg.get_payload(decode=True).decode("utf-8")


def test_no_second_draft_while_one_is_open(env):
    db, cid, tmp = env
    save_draft(db, cid, draft(), settings(tmp))
    with pytest.raises(SendNotAllowed, match="offener Entwurf"):
        save_draft(db, cid, draft(), settings(tmp))


def test_no_draft_for_blocked_or_recently_contacted(env):
    db, cid, tmp = env
    db.record_contact(cid, Channel.PHONE, Outcome.NO_REPLY)
    with pytest.raises(SendNotAllowed, match="Sperrfrist"):
        save_draft(db, cid, draft(), settings(tmp))
    db.record_contact(cid, Channel.PHONE, Outcome.DO_NOT_CONTACT)
    with pytest.raises(SendNotAllowed, match="gesperrt"):
        save_draft(db, cid, draft(), settings(tmp, contact_cooldown_days=0))


# --- Versand: standardmäßig aus ---

def test_sending_disabled_by_default(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "einwilligung", "Einwilligung per Mail vom 01.10.2026", "Maximilian Fieber")
    with pytest.raises(SendNotAllowed, match="deaktiviert"):
        send_draft(db, did, settings(tmp), smtp_factory=FakeSMTP())
    assert FakeSMTP.sent == []


def test_smtp_mode_without_second_switch_does_not_send(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "einwilligung", "Einwilligung per Mail vom 01.10.2026", "Maximilian Fieber")
    with pytest.raises(SendNotAllowed, match="deaktiviert"):
        send_draft(db, did, settings(tmp, **(SMTP_ON | {"send_enabled": False})), smtp_factory=FakeSMTP())


def test_no_send_without_documented_approval(env):
    """Öffentlich auffindbare Adresse ist keine Einwilligung."""
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    with pytest.raises(SendNotAllowed, match="Versandfreigabe"):
        send_draft(db, did, settings(tmp, **SMTP_ON), smtp_factory=FakeSMTP())
    assert FakeSMTP.sent == []


@pytest.mark.parametrize("basis,note,by", [
    ("oeffentliche_adresse", "Adresse steht auf der Website", "M"),    # keine Rechtsgrundlage
    ("einwilligung", "ja", "M"),                                         # nicht dokumentiert
    ("einwilligung", "Einwilligung am Telefon 01.10.", " "),
])
def test_approval_requires_basis_and_documentation(env, basis, note, by):
    db, cid, _ = env
    with pytest.raises(ValueError):
        approve_sending(db, cid, basis, note, by)


def test_send_with_all_conditions(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "anfrage_des_empfaengers", "Hat am 20.10. um Rückmeldung per Mail gebeten", "M. Fieber")
    send_draft(db, did, settings(tmp, **SMTP_ON), smtp_factory=FakeSMTP())
    assert len(FakeSMTP.sent) == 1 and db.get_draft(did)["status"] == "sent"
    assert db.contact_history(cid)[0]["outcome"] == "sent"
    attempts = db.conn.execute("SELECT success FROM send_attempts WHERE draft_id = ?", (did,)).fetchall()
    assert [a["success"] for a in attempts] == [1]


def test_no_duplicate_send(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "einwilligung", "Einwilligung per Mail vom 01.10.2026", "M. Fieber")
    s = settings(tmp, **SMTP_ON)
    send_draft(db, did, s, smtp_factory=FakeSMTP())
    with pytest.raises(SendNotAllowed, match="Status sent"):
        send_draft(db, did, s, smtp_factory=FakeSMTP())
    second = db.insert_draft(cid, None, draft(), None)                  # zweiter Entwurf, gleicher Empfänger
    with pytest.raises(SendNotAllowed, match="bereits versendet"):
        send_draft(db, second, s, smtp_factory=FakeSMTP())
    assert len(FakeSMTP.sent) == 1


def test_blocklist_checked_right_before_sending(env):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "einwilligung", "Einwilligung per Mail vom 01.10.2026", "M. Fieber")
    db.add_block("email", "info@nh-bauunternehmen.de", "Widerspruch")
    with pytest.raises(SendNotAllowed, match="Sperrliste"):
        send_draft(db, did, settings(tmp, **SMTP_ON), smtp_factory=FakeSMTP())


@pytest.mark.parametrize("fail", ["auth", "send"])
def test_smtp_errors_logged_without_secrets(env, fail):
    db, cid, tmp = env
    did = save_draft(db, cid, draft(), settings(tmp))
    approve_sending(db, cid, "einwilligung", "Einwilligung per Mail vom 01.10.2026", "M. Fieber")
    with pytest.raises(smtplib.SMTPException):
        send_draft(db, did, settings(tmp, **SMTP_ON), smtp_factory=FakeSMTP(fail=fail))
    row = db.conn.execute("SELECT success, error FROM send_attempts WHERE draft_id = ?", (did,)).fetchone()
    assert row["success"] == 0 and "geheim123" not in row["error"]
    assert db.get_draft(did)["status"] == "failed" and db.contact_history(cid) == []
