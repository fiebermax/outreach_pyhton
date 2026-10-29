import sqlite3

import pytest

from modules.database import Database, NotAnalyzableError, Step, StepStatus, identity_domain
from models.schemas import (
    AnalysisReport, ContactInfo, ContactKind, ContactOrigin, CrawlResult, CrawlStatus, EvidenceType,
    ExtractionMethod, Finding, FindingCategory, Lead, PageResult, Severity,
)

URL = "https://www.reiser.de/"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def lead(**kw) -> Lead:
    base = dict(name="Schreinerei Reiser", address="Burgauer Weg 14, 89343 Jettingen-Scheppach",
                website=URL, source="google_places", source_ref="place-1")
    return Lead(**(base | kw))


def good_crawl() -> CrawlResult:
    page = PageResult(url=URL, crawl_status=CrawlStatus.SUCCESS, cleaned_text="Wir bauen Möbel.",
                      extraction_method=ExtractionMethod.TRAFILATURA, extraction_quality=0.8)
    return CrawlResult(site_url=URL, status=CrawlStatus.SUCCESS, pages=[page])


def bad_crawl() -> CrawlResult:
    page = PageResult(url=URL, crawl_status=CrawlStatus.EMPTY_CONTENT, failure_reason="0 Zeichen Text")
    return CrawlResult(site_url=URL, status=CrawlStatus.EMPTY_CONTENT, failure_reason="kein Text", pages=[page])


def report() -> AnalysisReport:
    f = Finding(issue_id="copyright_outdated", category=FindingCategory.MAINTENANCE, severity=Severity.LOW,
                description="Copyright im Footer nennt 2019.", evidence="© 2019 Reiser", source_url=URL,
                evidence_type=EvidenceType.DIRECTLY_OBSERVED, confidence=0.95, business_relevance=Severity.LOW)
    return AnalysisReport(site_url=URL, findings=[f], analysis_quality=0.9)


# --- Schema ---

def test_schema_created_and_migration_idempotent(tmp_path):
    path = tmp_path / "x.db"
    Database(path).close()
    again = Database(path)
    assert again.schema_version == 1
    tables = {r[0] for r in again.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "contacts", "crawl_runs", "analysis_reports", "findings", "lead_scores",
            "email_drafts", "send_attempts", "blocklist", "job_steps", "error_log"} <= tables
    again.close()


def test_every_table_has_timestamps(db):
    tables = [r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cols = {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        assert {"id", "created_at", "updated_at"} <= cols, table


# --- Duplikate ---

def test_same_lead_twice_creates_one_record(db):
    first = db.upsert_company(lead())
    second = db.upsert_company(lead())
    assert first.created and not second.created
    assert len(db.list_companies()) == 1


def test_match_by_domain_despite_different_name(db):
    db.upsert_company(lead())
    res = db.upsert_company(lead(name="Reiser Insektenschutz", website="http://reiser.de/",
                                 source="osm", source_ref="node-9", address=None))
    assert res.matched_by == "domain"
    assert len(db.list_companies()) == 1


def test_match_by_name_and_address_without_website(db):
    db.upsert_company(lead(website=None, source_ref=None))
    res = db.upsert_company(lead(name="Schreinerei Reiser e.K.", website=None, source_ref=None,
                                 address="Burgauer Weg 14, 89343 Jettingen-Scheppach-Jettingen, Deutschland"))
    assert res.matched_by == "name_address"
    assert len(db.list_companies()) == 1


def test_different_companies_same_address_stay_separate(db):
    db.upsert_company(lead(website=None, source_ref=None))
    db.upsert_company(lead(name="Elektro Maier", website=None, source_ref=None))
    assert len(db.list_companies()) == 2


def test_facebook_pages_are_not_merged(db):
    """Viele Betriebe haben als 'Website' nur eine Facebook-Seite – das ist kein gemeinsames Merkmal."""
    db.upsert_company(lead(name="Bäckerei A", website="https://facebook.com/baeckereia", source_ref="p1", address=None))
    db.upsert_company(lead(name="Metzgerei B", website="https://www.facebook.com/metzgereib", source_ref="p2", address=None))
    assert len(db.list_companies()) == 2
    assert identity_domain("https://m.facebook.com/") is None
    assert identity_domain("https://www.reiser.de") == "reiser.de"


@pytest.mark.parametrize("url,expected", [
    ("https://www.schreinerei-hoegel.de/", "schreinerei-hoegel.de"),
    ("https://firma.de/index.html", "firma.de"),
    ("https://firma.de/de/", "firma.de"),
    # echte Places-Ergebnisse aus Jettingen-Scheppach: Verzeichnisprofile, keine eigene Website
    ("https://fensterbau.io/guenzburg/7213-schreinerei--insektenschutz-reiser-jettingen", None),
    ("https://www.firmen.tv/schuster-zimmerei-und-holzbau_edyswg", None),
    (None, None),
])
def test_identity_domain_only_for_homepages(url, expected):
    assert identity_domain(url) == expected


def test_two_directory_profiles_on_same_portal_stay_separate(db):
    db.upsert_company(lead(name="Zimmerei Schuster", website="https://www.firmen.tv/schuster_a1", source_ref="p1", address=None))
    db.upsert_company(lead(name="Malerei Huber", website="https://www.firmen.tv/huber_b2", source_ref="p2", address=None))
    assert len(db.list_companies()) == 2


def test_update_fills_gaps_but_never_erases(db):
    cid = db.upsert_company(lead(phone="08225 12345", email=None)).company_id
    db.upsert_company(lead(phone=None, email="info@reiser.de"))
    row = db.get_company(cid)
    assert row["phone"] == "+49822512345"
    assert row["email"] == "info@reiser.de"


def test_update_does_not_violate_unique_domain(db):
    """Lead A bekommt nachträglich die Domain von B: kein Absturz, B bleibt Eigentümer der Domain."""
    db.upsert_company(lead(name="A", website=None, source_ref="pa", address=None))
    db.upsert_company(lead(name="B", website="https://b.de", source_ref="pb", address=None))
    res = db.upsert_company(lead(name="A", website=None, source_ref="pa", address=None))
    assert not res.created
    res = db.upsert_company(Lead(name="A", website="https://b.de", source="google_places", source_ref="pa"))
    assert res.matched_by == "source_ref"
    a, b = db.list_companies()
    assert (a["name"], a["domain"]) == ("A", None)
    assert (b["name"], b["domain"]) == ("B", "b.de")


def test_other_source_fills_gaps_but_keeps_identity(db):
    cid = db.upsert_company(lead(phone=None)).company_id
    db.upsert_company(lead(name="Reiser Insektenschutz", source="osm", source_ref="node-9", phone="08225 999999"))
    row = db.get_company(cid)
    assert row["name"] == "Schreinerei Reiser"
    assert (row["source"], row["source_ref"]) == ("google_places", "place-1")
    assert row["phone"] == "+498225999999"


# --- Kontakte ---

def test_contacts_deduplicated_per_source(db):
    cid = db.upsert_company(lead()).company_id
    ct = ContactInfo(kind=ContactKind.PHONE, value="+49822512345", raw="08225 12345",
                     origin=ContactOrigin.TEXT, source_url=URL + "kontakt", context="Tel. 08225 12345")
    assert db.add_contacts(cid, [ct, ct]) == 1
    assert db.add_contacts(cid, [ct.model_copy(update={"source_url": URL + "impressum"})]) == 1
    assert len(db.get_contacts(cid, "PHONE")) == 2


# --- Crawl / Analyse ---

def test_crawl_roundtrip(db):
    cid = db.upsert_company(lead()).company_id
    run_id = db.save_crawl(cid, good_crawl())
    loaded_id, loaded = db.latest_crawl(cid)
    assert loaded_id == run_id and loaded.status is CrawlStatus.SUCCESS


def test_report_for_bad_crawl_is_refused(db):
    """Kernregel: ein unzureichender Crawl darf nie als Analyse gespeichert werden."""
    cid = db.upsert_company(lead()).company_id
    run_id = db.save_crawl(cid, bad_crawl())
    with pytest.raises(NotAnalyzableError, match="EMPTY_CONTENT"):
        db.save_report(cid, run_id, report())
    assert db.conn.execute("SELECT COUNT(*) FROM analysis_reports").fetchone()[0] == 0


def test_report_for_foreign_crawl_is_refused(db):
    a = db.upsert_company(lead()).company_id
    b = db.upsert_company(lead(name="Andere", website="https://andere.de", source_ref="p2", address=None)).company_id
    run_id = db.save_crawl(a, good_crawl())
    with pytest.raises(NotAnalyzableError):
        db.save_report(b, run_id, report())


def test_report_roundtrip(db):
    cid = db.upsert_company(lead()).company_id
    rid = db.save_report(cid, db.save_crawl(cid, good_crawl()), report())
    loaded = db.get_report(rid)
    assert loaded.findings[0].issue_id == "copyright_outdated"
    assert loaded.findings[0].evidence == "© 2019 Reiser"


def test_transaction_rolls_back_completely(db):
    cid = db.upsert_company(lead()).company_id
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.save_crawl(cid, good_crawl())
            raise RuntimeError("Absturz mitten im Speichern")
    assert db.latest_crawl(cid) is None


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.save_crawl(999, good_crawl())


# --- Sperrliste ---

def test_blocklist(db):
    db.add_block("email", "Info@Reiser.de", "Widerspruch per Telefon")
    db.add_block("domain", "https://www.gesperrt.de/")
    assert db.is_blocked(email="info@reiser.de")
    assert db.is_blocked(email="chef@gesperrt.de")
    assert db.is_blocked(domain="gesperrt.de")
    assert not db.is_blocked(email="info@andere.de")


# --- Job-Schritte / Wiederaufnahme ---

def test_done_step_is_skipped(db):
    cid = db.upsert_company(lead()).company_id
    assert db.companies_needing(Step.CRAWL) == [cid]
    db.start_step(cid, Step.CRAWL)
    db.finish_step(cid, Step.CRAWL, StepStatus.DONE)
    assert db.is_step_done(cid, Step.CRAWL)
    assert db.companies_needing(Step.CRAWL) == []


def test_interrupted_step_is_resumed(db):
    """Prozess stirbt während 'running' → beim nächsten Lauf wieder dran."""
    cid = db.upsert_company(lead()).company_id
    db.start_step(cid, Step.CRAWL)
    reopened = Database(db.path)
    assert reopened.companies_needing(Step.CRAWL) == [cid]
    reopened.close()


def test_failed_step_retried_only_on_request_and_limited(db):
    cid = db.upsert_company(lead()).company_id
    for _ in range(2):
        db.start_step(cid, Step.CRAWL)
        db.finish_step(cid, Step.CRAWL, StepStatus.FAILED, "Timeout")
    assert db.companies_needing(Step.CRAWL) == []
    assert db.companies_needing(Step.CRAWL, include_failed=True, max_attempts=3) == [cid]
    db.start_step(cid, Step.CRAWL)
    db.finish_step(cid, Step.CRAWL, StepStatus.FAILED, "Timeout")
    assert db.companies_needing(Step.CRAWL, include_failed=True, max_attempts=3) == []


def test_manual_reset_for_single_lead(db):
    cid = db.upsert_company(lead()).company_id
    db.start_step(cid, Step.ANALYZE)
    db.finish_step(cid, Step.ANALYZE, StepStatus.DONE)
    db.reset_step(cid, Step.ANALYZE)
    assert db.companies_needing(Step.ANALYZE) == [cid]


def test_finish_without_start_fails(db):
    cid = db.upsert_company(lead()).company_id
    with pytest.raises(ValueError):
        db.finish_step(cid, Step.CRAWL, StepStatus.DONE)


def test_error_log(db):
    cid = db.upsert_company(lead()).company_id
    db.log_error(cid, Step.CRAWL, TimeoutError("15s überschritten"), temporary=True)
    errors = db.errors_for(cid)
    assert errors[0]["error_type"] == "TimeoutError" and errors[0]["temporary"] == 1
