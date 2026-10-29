"""SQLite-Datenzugriff.

Nur Speichern, Laden und Abgleichen – keine Analyse- oder Versandlogik.
Schema-Versionen laufen über `PRAGMA user_version`; neue Migrationen werden
ans Ende von `_MIGRATIONS` angehängt, nie bestehende geändert.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from core.normalize import address_key, normalize_company_name, normalize_domain
from models.schemas import AnalysisReport, ContactInfo, CrawlResult, Lead

# Domains, unter denen viele fremde Firmen eine Seite haben. Sie taugen nicht
# als Identitätsmerkmal, sonst würden alle Facebook-Seiten zu einer Firma.
SHARED_PLATFORM_DOMAINS = {
    "facebook.com", "m.facebook.com", "instagram.com", "linkedin.com", "xing.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "google.com", "sites.google.com", "business.site", "g.page", "goo.gl",
    "linktr.ee", "wa.me", "gelbeseiten.de", "dasoertliche.de", "11880.com", "yelp.de", "yelp.com",
    "myhammer.de", "jimdo.com", "wix.com", "ebay-kleinanzeigen.de", "kleinanzeigen.de",
}


# Pfade, die noch als "Startseite" gelten.
_ROOT_PATHS = {"", "/", "/index.html", "/index.php", "/home", "/de", "/de/", "/start"}


# Freemail-Domains identifizieren keine Firma (gmail.com gehört nicht dem Maler).
_FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "t-online.de", "outlook.com", "hotmail.com",
    "hotmail.de", "yahoo.de", "yahoo.com", "icloud.com", "me.com", "aol.com", "freenet.de", "posteo.de", "mail.de",
    "online.de", "arcor.de", "live.de", "outlook.de",
}


def identity_domain(website: str | None) -> str | None:
    """Domain als Identitätsmerkmal – nur wenn die URL auf eine Startseite zeigt.

    Verzeichnisprofile wie `firmen.tv/schuster-zimmerei_edyswg` oder
    `fensterbau.io/guenzburg/7213-...` liegen auf fremden Hosts mit vielen
    Firmen; deren Domain würde verschiedene Firmen zusammenführen.
    """
    if not website:
        return None
    if urlsplit(website).path.rstrip("/").lower() not in {p.rstrip("/") for p in _ROOT_PATHS}:
        return None
    domain = normalize_domain(website)
    if domain in SHARED_PLATFORM_DOMAINS or any(domain.endswith("." + d) for d in SHARED_PLATFORM_DOMAINS):
        return None
    return domain


def website_kind(website: str | None) -> str:
    """'own' = eigene Startseite, 'directory' = Verzeichnis-/Social-Profil, 'none' = keine Angabe."""
    if not website:
        return "none"
    return "own" if identity_domain(website) else "directory"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"     # bewusst nicht ausgeführt, z. B. keine Website


class Step(str, Enum):
    CRAWL = "crawl"
    ANALYZE = "analyze"
    SCORE = "score"
    DRAFT = "draft"
    SEND = "send"


class Channel(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    VISIT = "visit"
    LETTER = "letter"
    SOCIAL = "social"
    OTHER = "other"


class Outcome(str, Enum):
    SENT = "sent"                      # verschickt/angerufen, noch keine Reaktion erfasst
    NO_REPLY = "no_reply"
    REPLIED = "replied"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    MEETING = "meeting"
    CUSTOMER = "customer"
    BOUNCED = "bounced"
    DO_NOT_CONTACT = "do_not_contact"  # Widerspruch: nie wieder kontaktieren


# Ergebnisse, nach denen niemand mehr unaufgefordert kontaktiert wird.
_STOP_OUTCOMES = {Outcome.NOT_INTERESTED, Outcome.DO_NOT_CONTACT}


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str


class NotAnalyzableError(ValueError):
    """Analyse zu einem nicht verwertbaren Crawl soll gespeichert werden."""


@dataclass(frozen=True)
class UpsertResult:
    company_id: int
    created: bool
    matched_by: str | None   # "domain" | "source_ref" | "name_address" | None (neu)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_TS = "created_at TEXT NOT NULL, updated_at TEXT NOT NULL"

_MIGRATIONS: list[str] = [
    # --- v1 ---------------------------------------------------------------
    f"""
    CREATE TABLE companies (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        category TEXT,
        address TEXT,
        address_key TEXT,
        city TEXT,
        postal_code TEXT,
        phone TEXT,
        email TEXT,
        website TEXT,
        domain TEXT,
        source TEXT NOT NULL,
        source_ref TEXT,
        collected_at TEXT NOT NULL,
        {_TS}
    );
    CREATE UNIQUE INDEX ux_companies_domain ON companies(domain) WHERE domain IS NOT NULL;
    CREATE UNIQUE INDEX ux_companies_source_ref ON companies(source, source_ref) WHERE source_ref IS NOT NULL;
    CREATE UNIQUE INDEX ux_companies_name_address ON companies(name_key, address_key) WHERE address_key IS NOT NULL;

    CREATE TABLE contacts (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('EMAIL', 'PHONE')),
        value TEXT NOT NULL,
        raw TEXT NOT NULL,
        origin TEXT NOT NULL,
        source_url TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '',
        {_TS},
        UNIQUE (company_id, kind, value, source_url)
    );

    CREATE TABLE crawl_runs (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        site_url TEXT NOT NULL,
        status TEXT NOT NULL,
        failure_reason TEXT,
        usable INTEGER NOT NULL CHECK (usable IN (0, 1)),
        result_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        {_TS}
    );
    CREATE INDEX ix_crawl_runs_company ON crawl_runs(company_id, id);

    CREATE TABLE analysis_reports (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
        analysis_quality REAL NOT NULL,
        ai_model TEXT,
        ai_cost_eur REAL NOT NULL DEFAULT 0,
        discarded_findings INTEGER NOT NULL DEFAULT 0,
        {_TS}
    );

    CREATE TABLE findings (
        id INTEGER PRIMARY KEY,
        report_id INTEGER NOT NULL REFERENCES analysis_reports(id) ON DELETE CASCADE,
        issue_id TEXT NOT NULL,
        category TEXT NOT NULL,
        severity TEXT NOT NULL,
        evidence_type TEXT NOT NULL,
        confidence REAL NOT NULL,
        finding_json TEXT NOT NULL,
        {_TS}
    );

    CREATE TABLE lead_scores (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        report_id INTEGER REFERENCES analysis_reports(id),
        score REAL NOT NULL,
        disqualified INTEGER NOT NULL CHECK (disqualified IN (0, 1)),
        score_json TEXT NOT NULL,
        {_TS}
    );

    CREATE TABLE email_drafts (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        report_id INTEGER REFERENCES analysis_reports(id),
        recipient TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        finding_ids_json TEXT NOT NULL,
        eml_path TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'failed', 'cancelled')),
        {_TS}
    );
    -- höchstens eine versendete Nachricht pro Firma und Empfänger
    CREATE UNIQUE INDEX ux_drafts_sent_once ON email_drafts(company_id, recipient) WHERE status = 'sent';

    CREATE TABLE send_attempts (
        id INTEGER PRIMARY KEY,
        draft_id INTEGER NOT NULL REFERENCES email_drafts(id) ON DELETE CASCADE,
        success INTEGER NOT NULL CHECK (success IN (0, 1)),
        error TEXT,
        {_TS}
    );

    CREATE TABLE blocklist (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('email', 'domain')),
        value TEXT NOT NULL,
        reason TEXT,
        {_TS},
        UNIQUE (kind, value)
    );

    CREATE TABLE job_steps (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        step TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        started_at TEXT,
        finished_at TEXT,
        {_TS},
        UNIQUE (company_id, step)
    );

    CREATE TABLE error_log (
        id INTEGER PRIMARY KEY,
        company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
        step TEXT,
        error_type TEXT NOT NULL,
        message TEXT NOT NULL,
        temporary INTEGER NOT NULL CHECK (temporary IN (0, 1)),
        {_TS}
    );
    """,
    # --- v2: Kontakthistorie (auch Kontakte außerhalb des Systems) -----------
    f"""
    ALTER TABLE companies ADD COLUMN do_not_contact INTEGER NOT NULL DEFAULT 0 CHECK (do_not_contact IN (0, 1));
    ALTER TABLE companies ADD COLUMN do_not_contact_reason TEXT;

    CREATE TABLE contact_events (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK (channel IN ('email', 'phone', 'visit', 'letter', 'social', 'other')),
        outcome TEXT NOT NULL CHECK (outcome IN ('sent', 'no_reply', 'replied', 'interested', 'not_interested',
                                                 'meeting', 'customer', 'bounced', 'do_not_contact')),
        occurred_at TEXT NOT NULL,
        recipient TEXT,
        draft_id INTEGER REFERENCES email_drafts(id),
        note TEXT,
        source TEXT NOT NULL CHECK (source IN ('system', 'manual', 'import')),
        {_TS}
    );
    CREATE INDEX ix_contact_events_company ON contact_events(company_id, occurred_at);
    CREATE INDEX ix_companies_name_key ON companies(name_key);
    """,
    # --- v3: Nische, Art der Website, API-Cache und Nutzungszähler ---------
    f"""
    ALTER TABLE companies ADD COLUMN niche_branch TEXT;
    ALTER TABLE companies ADD COLUMN niche_fit REAL;
    ALTER TABLE companies ADD COLUMN niche_reason TEXT;
    ALTER TABLE companies ADD COLUMN website_kind TEXT CHECK (website_kind IN ('own', 'directory', 'none'));
    ALTER TABLE companies ADD COLUMN distance_km REAL;
    ALTER TABLE companies ADD COLUMN business_status TEXT;

    CREATE TABLE api_cache (
        id INTEGER PRIMARY KEY,
        cache_key TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        response_json TEXT NOT NULL,
        {_TS}
    );

    CREATE TABLE api_calls (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        success INTEGER NOT NULL CHECK (success IN (0, 1)),
        {_TS}
    );
    CREATE INDEX ix_api_calls_provider ON api_calls(provider, created_at);
    """,
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: Transaktionen werden explizit über `transaction()` gesteuert.
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._in_tx = False
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- Grundlagen

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        for version, script in enumerate(_MIGRATIONS, start=1):
            if self.schema_version >= version:
                continue
            # executescript committet selbst; Version wird im selben Skript gesetzt.
            try:
                self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Alles-oder-nichts. Verschachtelte Aufrufe laufen in der äußeren Transaktion mit."""
        if self._in_tx:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    # ------------------------------------------------------------- Unternehmen

    def _find_company(self, domain: str | None, lead: Lead, name_key: str, addr_key: str | None):
        c = self.conn
        # Quell-ID zuerst: Sie identifiziert genau einen Eintrag der Quelle.
        if lead.source_ref:
            row = c.execute("SELECT id FROM companies WHERE source = ? AND source_ref = ?",
                            (lead.source, lead.source_ref)).fetchone()
            if row:
                return row["id"], "source_ref"
        if domain:
            row = c.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
            if row:
                return row["id"], "domain"
        if addr_key:
            row = c.execute("SELECT id FROM companies WHERE name_key = ? AND address_key = ?",
                            (name_key, addr_key)).fetchone()
            if row:
                return row["id"], "name_address"
        return None, None

    def upsert_company(self, lead: Lead) -> UpsertResult:
        """Legt ein Unternehmen an oder aktualisiert das bekannte.

        Abgleich in dieser Reihenfolge: Quell-ID → Domain → Name + Adresse.
        Vorhandene Werte werden nie durch leere überschrieben; eine andere Quelle füllt nur Lücken.
        """
        domain = identity_domain(lead.website)
        name_key = normalize_company_name(lead.name) or lead.name.casefold()
        addr_key = address_key(lead.address or "")
        now = _now()
        fields = {
            "name": lead.name, "name_key": name_key, "category": lead.category, "address": lead.address,
            "address_key": addr_key, "city": lead.city, "postal_code": lead.postal_code, "phone": lead.phone,
            "email": lead.email, "website": lead.website, "domain": domain, "source": lead.source,
            "source_ref": lead.source_ref, "collected_at": lead.collected_at.isoformat(),
            "niche_branch": lead.niche_branch, "niche_fit": lead.niche_fit, "niche_reason": lead.niche_reason,
            "website_kind": website_kind(lead.website), "distance_km": lead.distance_km,
            "business_status": lead.business_status,
        }
        with self.transaction() as c:
            company_id, matched_by = self._find_company(domain, lead, name_key, addr_key)
            if company_id is None:
                cols = ", ".join(fields) + ", created_at, updated_at"
                marks = ", ".join("?" for _ in range(len(fields) + 2))
                cur = c.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", (*fields.values(), now, now))
                return UpsertResult(cur.lastrowid, True, None)

            current = c.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            if current["source"] == lead.source:
                updates = {k: v for k, v in fields.items() if v is not None and current[k] != v}
            else:
                # Andere Quelle: nur Lücken füllen, Identität (Name, Quelle) bleibt beim ersten Fund.
                updates = {k: v for k, v in fields.items()
                           if v is not None and current[k] is None and k not in ("source", "source_ref")}
            for unique_col in ("domain", "address_key", "source_ref"):
                if unique_col in updates and self._would_collide(company_id, unique_col, updates):
                    updates.pop(unique_col)
                    if unique_col == "address_key":
                        updates.pop("address", None)
            if updates:
                assignments = ", ".join(f"{k} = ?" for k in updates)
                c.execute(f"UPDATE companies SET {assignments}, updated_at = ? WHERE id = ?",
                          (*updates.values(), now, company_id))
            return UpsertResult(company_id, False, matched_by)

    def _would_collide(self, company_id: int, column: str, updates: dict) -> bool:
        """Würde das Update einen UNIQUE-Index mit einem anderen Datensatz verletzen?"""
        if column == "domain":
            sql, args = "SELECT id FROM companies WHERE domain = ? AND id != ?", (updates["domain"], company_id)
        elif column == "address_key":
            name_key = updates.get("name_key") or self.get_company(company_id)["name_key"]
            sql = "SELECT id FROM companies WHERE name_key = ? AND address_key = ? AND id != ?"
            args = (name_key, updates["address_key"], company_id)
        else:
            source = self.get_company(company_id)["source"]
            sql = "SELECT id FROM companies WHERE source = ? AND source_ref = ? AND id != ?"
            args = (source, updates["source_ref"], company_id)
        return self.conn.execute(sql, args).fetchone() is not None

    def get_company(self, company_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    def list_companies(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM companies ORDER BY id").fetchall()

    # ------------------------------------------------------------- Kontakte

    def add_contacts(self, company_id: int, contacts: list[ContactInfo]) -> int:
        """Speichert Kontakte mit Fundstelle; bekannte (gleicher Wert + Seite) werden übersprungen."""
        now = _now()
        added = 0
        with self.transaction() as c:
            for ct in contacts:
                cur = c.execute(
                    "INSERT OR IGNORE INTO contacts (company_id, kind, value, raw, origin, source_url, context,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (company_id, ct.kind.value, ct.value, ct.raw, ct.origin.value, ct.source_url, ct.context, now, now),
                )
                added += cur.rowcount
        return added

    def get_contacts(self, company_id: int, kind: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM contacts WHERE company_id = ?", [company_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    # ------------------------------------------------------------- Crawls

    def save_crawl(self, company_id: int, result: CrawlResult) -> int:
        now = _now()
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO crawl_runs (company_id, site_url, status, failure_reason, usable, result_json,"
                " started_at, finished_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, result.site_url, result.status.value, result.failure_reason,
                 int(result.status.is_usable), result.model_dump_json(), result.started_at.isoformat(),
                 result.finished_at.isoformat() if result.finished_at else None, now, now),
            )
            return cur.lastrowid

    def latest_crawl(self, company_id: int) -> tuple[int, CrawlResult] | None:
        row = self.conn.execute(
            "SELECT id, result_json FROM crawl_runs WHERE company_id = ? ORDER BY id DESC LIMIT 1", (company_id,)
        ).fetchone()
        return (row["id"], CrawlResult.model_validate_json(row["result_json"])) if row else None

    # ------------------------------------------------------------- Analyse

    def save_report(self, company_id: int, crawl_run_id: int, report: AnalysisReport) -> int:
        """Speichert Bericht + Befunde atomar. Verweigert Berichte zu unbrauchbaren Crawls."""
        run = self.conn.execute(
            "SELECT company_id, status, usable FROM crawl_runs WHERE id = ?", (crawl_run_id,)
        ).fetchone()
        if run is None or run["company_id"] != company_id:
            raise NotAnalyzableError(f"Crawl {crawl_run_id} gehört nicht zu Unternehmen {company_id}")
        if not run["usable"]:
            raise NotAnalyzableError(f"Crawl {crawl_run_id} hat Status {run['status']} und ist nicht analysierbar")
        now = _now()
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO analysis_reports (company_id, crawl_run_id, analysis_quality, ai_model, ai_cost_eur,"
                " discarded_findings, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, crawl_run_id, report.analysis_quality, report.ai_model, report.ai_cost_eur,
                 report.discarded_findings, now, now),
            )
            report_id = cur.lastrowid
            for f in report.findings:
                c.execute(
                    "INSERT INTO findings (report_id, issue_id, category, severity, evidence_type, confidence,"
                    " finding_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, f.issue_id, f.category.value, f.severity.value, f.evidence_type.value,
                     f.confidence, f.model_dump_json(), now, now),
                )
            return report_id

    def get_report(self, report_id: int) -> AnalysisReport | None:
        row = self.conn.execute("SELECT * FROM analysis_reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return None
        findings = [
            json.loads(r["finding_json"])
            for r in self.conn.execute("SELECT finding_json FROM findings WHERE report_id = ? ORDER BY id", (report_id,))
        ]
        site_url = self.conn.execute("SELECT site_url FROM crawl_runs WHERE id = ?", (row["crawl_run_id"],)).fetchone()[0]
        return AnalysisReport(site_url=site_url, findings=findings, analysis_quality=row["analysis_quality"],
                              ai_model=row["ai_model"], ai_cost_eur=row["ai_cost_eur"],
                              discarded_findings=row["discarded_findings"])

    # ------------------------------------------------------------- Sperrliste

    def add_block(self, kind: str, value: str, reason: str | None = None) -> None:
        value = value.strip().lower()
        if kind == "domain":
            value = normalize_domain(value)
        now = _now()
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO blocklist (kind, value, reason, created_at, updated_at)"
                      " VALUES (?, ?, ?, ?, ?)", (kind, value, reason, now, now))

    def is_blocked(self, email: str | None = None, domain: str | None = None) -> bool:
        checks: list[tuple[str, str]] = []
        if email:
            email = email.strip().lower()
            checks += [("email", email), ("domain", email.rsplit("@", 1)[-1])]
        if domain:
            checks.append(("domain", normalize_domain(domain)))
        for kind, value in checks:
            if self.conn.execute("SELECT 1 FROM blocklist WHERE kind = ? AND value = ?", (kind, value)).fetchone():
                return True
        return False

    # ------------------------------------------------------------- Job-Schritte

    def step_status(self, company_id: int, step: Step) -> StepStatus | None:
        row = self.conn.execute("SELECT status FROM job_steps WHERE company_id = ? AND step = ?",
                                (company_id, step.value)).fetchone()
        return StepStatus(row["status"]) if row else None

    def is_step_done(self, company_id: int, step: Step) -> bool:
        return self.step_status(company_id, step) in (StepStatus.DONE, StepStatus.SKIPPED)

    def start_step(self, company_id: int, step: Step) -> None:
        now = _now()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO job_steps (company_id, step, status, attempts, started_at, created_at, updated_at)"
                " VALUES (?, ?, 'running', 1, ?, ?, ?)"
                " ON CONFLICT (company_id, step) DO UPDATE SET status = 'running', attempts = attempts + 1,"
                " started_at = excluded.started_at, finished_at = NULL, updated_at = excluded.updated_at",
                (company_id, step.value, now, now, now),
            )

    def finish_step(self, company_id: int, step: Step, status: StepStatus, error: str | None = None) -> None:
        if status in (StepStatus.PENDING, StepStatus.RUNNING):
            raise ValueError("finish_step braucht einen Endstatus")
        now = _now()
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE job_steps SET status = ?, last_error = ?, finished_at = ?, updated_at = ?"
                " WHERE company_id = ? AND step = ?",
                (status.value, error, now, now, company_id, step.value),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Schritt {step.value} für Unternehmen {company_id} wurde nie gestartet")

    def reset_step(self, company_id: int, step: Step) -> None:
        """Für gezielte Wiederholung: Schritt wieder auf 'pending' setzen (Versuchszähler bleibt)."""
        with self.transaction() as c:
            c.execute("UPDATE job_steps SET status = 'pending', updated_at = ? WHERE company_id = ? AND step = ?",
                      (_now(), company_id, step.value))

    def companies_needing(self, step: Step, include_failed: bool = False, max_attempts: int = 3) -> list[int]:
        """IDs, bei denen der Schritt noch aussteht.

        'running' zählt als ausstehend: Ein Lauf, der dort abgebrochen ist, wird wieder aufgenommen.
        Fehlgeschlagene nur mit `include_failed` und solange `max_attempts` nicht erreicht ist.
        """
        statuses = ["pending", "running"] + (["failed"] if include_failed else [])
        marks = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"""SELECT c.id FROM companies c
                LEFT JOIN job_steps j ON j.company_id = c.id AND j.step = ?
                WHERE j.id IS NULL OR (j.status IN ({marks}) AND (j.status != 'failed' OR j.attempts < ?))
                ORDER BY c.id""",
            (step.value, *statuses, max_attempts),
        ).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------- Fehlerprotokoll

    def log_error(self, company_id: int | None, step: Step | None, exc: BaseException, temporary: bool) -> None:
        now = _now()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO error_log (company_id, step, error_type, message, temporary, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (company_id, step.value if step else None, type(exc).__name__, str(exc)[:2000], int(temporary), now, now),
            )

    def errors_for(self, company_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM error_log WHERE company_id = ? ORDER BY id", (company_id,)).fetchall()

    # ------------------------------------------------------------- Kontakthistorie

    def record_contact(
        self,
        company_id: int,
        channel: Channel,
        outcome: Outcome,
        *,
        occurred_at: datetime | None = None,
        recipient: str | None = None,
        note: str | None = None,
        draft_id: int | None = None,
        source: str = "manual",
    ) -> int:
        """Hält einen Kontakt fest. Ablehnung/Widerspruch sperrt das Unternehmen dauerhaft."""
        now = _now()
        when = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO contact_events (company_id, channel, outcome, occurred_at, recipient, draft_id, note,"
                " source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, channel.value, outcome.value, when, recipient.lower() if recipient else None,
                 draft_id, note, source, now, now),
            )
            if outcome in _STOP_OUTCOMES:
                self.set_do_not_contact(company_id, f"{outcome.value} am {when[:10]}" + (f": {note}" if note else ""))
                if recipient:
                    self.add_block("email", recipient, outcome.value)
            return cur.lastrowid

    def set_do_not_contact(self, company_id: int, reason: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE companies SET do_not_contact = 1, do_not_contact_reason = ?, updated_at = ? WHERE id = ?",
                      (reason, _now(), company_id))

    def contact_history(self, company_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contact_events WHERE company_id = ? ORDER BY occurred_at DESC, id DESC", (company_id,)
        ).fetchall()

    def may_contact(
        self, company_id: int, email: str | None = None, cooldown_days: int = 365, now: datetime | None = None
    ) -> ContactDecision:
        """Darf dieses Unternehmen (unaufgefordert) kontaktiert werden? Prüft alles, was die DB weiß.

        Das ist keine rechtliche Freigabe – die prüft der Mailer zusätzlich (Paket 8.3).
        """
        company = self.get_company(company_id)
        if company is None:
            return ContactDecision(False, "Unternehmen unbekannt")
        if company["do_not_contact"]:
            return ContactDecision(False, f"gesperrt: {company['do_not_contact_reason'] or 'ohne Angabe'}")
        if self.is_blocked(email=email, domain=company["domain"]):
            return ContactDecision(False, "Empfänger oder Domain auf Sperrliste")
        last = self.conn.execute(
            "SELECT outcome, occurred_at, channel FROM contact_events WHERE company_id = ?"
            " ORDER BY occurred_at DESC, id DESC LIMIT 1", (company_id,)
        ).fetchone()
        if last is not None:
            if last["outcome"] in (Outcome.CUSTOMER.value, Outcome.INTERESTED.value, Outcome.MEETING.value,
                                   Outcome.REPLIED.value):
                return ContactDecision(False, f"laufender Kontakt ({last['outcome']} am {last['occurred_at'][:10]})")
            age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last["occurred_at"])
            if age.days < cooldown_days:
                return ContactDecision(False, f"zuletzt kontaktiert am {last['occurred_at'][:10]} per {last['channel']}"
                                              f" (Sperrfrist {cooldown_days} Tage)")
        pending = self.conn.execute(
            "SELECT id FROM email_drafts WHERE company_id = ? AND status = 'draft'", (company_id,)
        ).fetchone()
        if pending is not None:
            return ContactDecision(False, f"offener Entwurf #{pending['id']} existiert bereits")
        return ContactDecision(True, "kein Hinderungsgrund in der Datenbank")

    def contact_overview(self) -> list[sqlite3.Row]:
        """Alle jemals kontaktierten Unternehmen mit letztem Kontakt – für die Übersicht."""
        return self.conn.execute(
            """SELECT c.id, c.name, c.domain, c.do_not_contact, e.occurred_at AS last_contact, e.channel, e.outcome,
                      (SELECT COUNT(*) FROM contact_events x WHERE x.company_id = c.id) AS contacts
               FROM companies c
               JOIN contact_events e ON e.id = (SELECT id FROM contact_events y WHERE y.company_id = c.id
                                                ORDER BY occurred_at DESC, id DESC LIMIT 1)
               ORDER BY e.occurred_at DESC"""
        ).fetchall()

    def find_company(self, *, website: str | None = None, email: str | None = None,
                     name: str | None = None) -> int | None:
        """Sucht ein bekanntes Unternehmen über Domain, E-Mail(-Domain) oder exakten normalisierten Namen."""
        if website and identity_domain(website):
            row = self.conn.execute("SELECT id FROM companies WHERE domain = ?", (identity_domain(website),)).fetchone()
            if row:
                return row["id"]
        if email:
            email = email.strip().lower()
            row = self.conn.execute(
                "SELECT company_id AS id FROM contacts WHERE kind = 'EMAIL' AND value = ?"
                " UNION SELECT id FROM companies WHERE email = ?"
                " UNION SELECT company_id FROM contact_events WHERE recipient = ? LIMIT 1",
                (email, email, email)).fetchone()
            if row:
                return row["id"]
            domain = email.rsplit("@", 1)[-1]
            if domain not in _FREEMAIL_DOMAINS:
                row = self.conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
                if row:
                    return row["id"]
        if name:
            rows = self.conn.execute("SELECT id FROM companies WHERE name_key = ?",
                                     (normalize_company_name(name),)).fetchall()
            if len(rows) == 1:          # mehrdeutige Namen nicht raten
                return rows[0]["id"]
        return None

    # ------------------------------------------------------------- API-Cache und -Nutzung

    def cache_get(self, key: str, max_age_days: float) -> str | None:
        row = self.conn.execute("SELECT response_json, updated_at FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])
        return row["response_json"] if age.total_seconds() <= max_age_days * 86400 else None

    def cache_put(self, key: str, provider: str, response_json: str) -> None:
        now = _now()
        with self.transaction() as c:
            c.execute("INSERT INTO api_cache (cache_key, provider, response_json, created_at, updated_at)"
                      " VALUES (?, ?, ?, ?, ?) ON CONFLICT (cache_key) DO UPDATE SET"
                      " response_json = excluded.response_json, updated_at = excluded.updated_at",
                      (key, provider, response_json, now, now))

    def log_api_call(self, provider: str, endpoint: str, success: bool) -> None:
        now = _now()
        with self.transaction() as c:
            c.execute("INSERT INTO api_calls (provider, endpoint, success, created_at, updated_at)"
                      " VALUES (?, ?, ?, ?, ?)", (provider, endpoint, int(success), now, now))

    def api_calls_this_month(self, provider: str, now: datetime | None = None) -> int:
        month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
        return self.conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = ? AND substr(created_at, 1, 7) = ?",
                                 (provider, month)).fetchone()[0]

