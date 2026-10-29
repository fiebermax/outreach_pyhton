"""Kontakthistorie: Wer wurde wann wie kontaktiert, und darf er (noch) angesprochen werden?"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from models.schemas import Lead
from modules.contact_import import import_contacts
from modules.database import _MIGRATIONS, Channel, Database, Outcome

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


def company(db, name="Malerbetrieb Kallup", website="https://www.malermeister-kallup.de/", **kw) -> int:
    return db.upsert_company(Lead(name=name, website=website, source="google_places", **kw)).company_id


def test_never_contacted_is_allowed(db):
    assert db.may_contact(company(db), now=NOW).allowed


def test_cooldown_after_contact(db):
    cid = company(db)
    db.record_contact(cid, Channel.PHONE, Outcome.NO_REPLY, occurred_at=NOW - timedelta(days=30))
    decision = db.may_contact(cid, cooldown_days=365, now=NOW)
    assert not decision.allowed and "per phone" in decision.reason and "2026-09-29" in decision.reason
    assert db.may_contact(cid, cooldown_days=365, now=NOW + timedelta(days=340)).allowed


@pytest.mark.parametrize("outcome", [Outcome.NOT_INTERESTED, Outcome.DO_NOT_CONTACT])
def test_refusal_blocks_permanently(db, outcome):
    cid = company(db)
    db.record_contact(cid, Channel.EMAIL, outcome, occurred_at=NOW - timedelta(days=900),
                      recipient="Info@Malermeister-Kallup.de", note="am Telefon abgelehnt")
    later = NOW + timedelta(days=3000)
    assert not db.may_contact(cid, now=later).allowed
    assert "gesperrt" in db.may_contact(cid, now=later).reason
    assert db.is_blocked(email="info@malermeister-kallup.de")


@pytest.mark.parametrize("outcome", [Outcome.CUSTOMER, Outcome.INTERESTED, Outcome.MEETING, Outcome.REPLIED])
def test_active_relationship_not_cold_contacted(db, outcome):
    cid = company(db)
    db.record_contact(cid, Channel.EMAIL, outcome, occurred_at=NOW - timedelta(days=800))
    decision = db.may_contact(cid, now=NOW)
    assert not decision.allowed and "laufender Kontakt" in decision.reason


def test_open_draft_prevents_second_draft(db):
    cid = company(db)
    now = NOW.isoformat()
    db.conn.execute("INSERT INTO email_drafts (company_id, recipient, subject, body, finding_ids_json, created_at,"
                    " updated_at) VALUES (?, 'a@b.de', 's', 'b', '[]', ?, ?)", (cid, now, now))
    assert "offener Entwurf" in db.may_contact(cid, now=NOW).reason


def test_blocked_domain(db):
    cid = company(db)
    db.add_block("domain", "malermeister-kallup.de")
    assert not db.may_contact(cid, now=NOW).allowed


def test_history_and_overview(db):
    a, b = company(db), company(db, name="N&H Bau", website="https://nh-bauunternehmen.de/")
    company(db, name="Nie kontaktiert", website="https://neu.de/")
    db.record_contact(a, Channel.EMAIL, Outcome.SENT, occurred_at=NOW - timedelta(days=10))
    db.record_contact(a, Channel.PHONE, Outcome.REPLIED, occurred_at=NOW - timedelta(days=2))
    db.record_contact(b, Channel.VISIT, Outcome.NO_REPLY, occurred_at=NOW - timedelta(days=5))
    assert [r["outcome"] for r in db.contact_history(a)] == ["replied", "sent"]
    overview = db.contact_overview()
    assert [(r["name"], r["outcome"], r["contacts"]) for r in overview] == [
        ("Malerbetrieb Kallup", "replied", 2), ("N&H Bau", "no_reply", 1)]


def test_find_company(db):
    cid = company(db, email="info@malermeister-kallup.de")
    assert db.find_company(website="http://malermeister-kallup.de") == cid
    assert db.find_company(email="INFO@malermeister-kallup.de") == cid
    assert db.find_company(email="chef@malermeister-kallup.de") == cid       # gleiche Firmendomain
    assert db.find_company(name="Malerbetrieb Kallup GmbH") == cid
    assert db.find_company(email="kallup@gmail.com") is None                 # Freemail ≠ Firma
    assert db.find_company(name="Unbekannt") is None


def test_ambiguous_name_not_guessed(db):
    company(db, name="Elektro Maier", website="https://maier-a.de/")
    company(db, name="Elektro Maier", website="https://maier-b.de/")
    assert db.find_company(name="Elektro Maier") is None


# --- Migration bestehender Datenbanken ---

def test_v1_database_upgrades_without_data_loss(tmp_path):
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;{_MIGRATIONS[0]}PRAGMA user_version = 1;COMMIT;")
    conn.execute("INSERT INTO companies (name, name_key, source, collected_at, created_at, updated_at)"
                 " VALUES ('Alt', 'alt', 'x', 't', 't', 't')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version == len(_MIGRATIONS)
    row = db.list_companies()[0]
    assert row["name"] == "Alt" and row["do_not_contact"] == 0
    db.record_contact(row["id"], Channel.EMAIL, Outcome.SENT)
    db.close()


# --- CSV-Import ---

CSV = """firma;website;email;datum;kanal;ergebnis;notiz
Malerbetrieb Kallup;https://www.malermeister-kallup.de;;12.03.2025;telefon;kein interesse;will keine neue Website
N&H Bau;;info@nh-bauunternehmen.de;2026-01-15;email;keine_antwort;
Neue Firma GmbH;https://neue-firma.de;;01.06.2026;besuch;termin;Messe Günzburg
Kaputt;;;gestern;email;;
Falsch;;;01.01.2026;fax;;
"""


def test_csv_import(db):
    kallup = company(db)
    nh = company(db, name="N&H Bau", website="https://nh-bauunternehmen.de/")
    report = import_contacts(db, CSV)
    assert report.imported == 3 and report.matched_existing == 2 and report.created == 1
    assert len(report.errors) == 2
    assert "Zeile 5" in report.errors[0] and "Datum" in report.errors[0]
    assert "Zeile 6" in report.errors[1] and "Kanal" in report.errors[1]

    assert not db.may_contact(kallup, now=NOW).allowed                      # kein Interesse → gesperrt
    assert "Sperrfrist" in db.may_contact(nh, now=NOW).reason               # Januar 2026 → innerhalb 365 Tage
    new = db.find_company(website="https://neue-firma.de")
    assert db.get_company(new)["source"] == "manual_import"
    assert "laufender Kontakt" in db.may_contact(new, now=NOW).reason


def test_csv_import_comma_separated(db):
    report = import_contacts(db, "firma,datum,kanal,ergebnis\nTest Firma,01.02.2026,email,gesendet\n")
    assert report.imported == 1 and not report.errors
