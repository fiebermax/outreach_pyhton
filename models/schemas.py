"""Pydantic-Datenmodelle der Pipeline.

Die Modelle erzwingen die Grundregeln des Projekts schon beim Erzeugen:
- ein nicht verwertbarer Crawl braucht einen Fehlergrund,
- ein Befund braucht einen Beleg, ein gemessener Befund zusätzlich Messwerte,
- ein E-Mail-Entwurf braucht mindestens einen zugrunde liegenden Befund.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.errors import InvalidUrlError
from core.normalize import normalize_email, normalize_phone, normalize_url


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, use_enum_values=False, validate_assignment=True)


# --------------------------------------------------------------------------- Enums

class CrawlStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    JS_RENDERING_REQUIRED = "JS_RENDERING_REQUIRED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_URL = "INVALID_URL"
    FAILED = "FAILED"

    @property
    def is_usable(self) -> bool:
        """Darf das Ergebnis analysiert werden?"""
        return self in (CrawlStatus.SUCCESS, CrawlStatus.PARTIAL_SUCCESS)

    @property
    def is_site_inaccessible(self) -> bool:
        """Website technisch nicht zugänglich (kein Fehler des Scrapers)."""
        return self in (CrawlStatus.BLOCKED, CrawlStatus.TIMEOUT, CrawlStatus.HTTP_ERROR, CrawlStatus.INVALID_URL)


class ExtractionMethod(str, Enum):
    NONE = "NONE"
    HTML_PARSER = "HTML_PARSER"        # BeautifulSoup/lxml, sichtbarer Text
    TRAFILATURA = "TRAFILATURA"        # Haupttext-Extraktion
    PLAYWRIGHT = "PLAYWRIGHT"          # gerendert im Browser


class ContactKind(str, Enum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"


class ContactOrigin(str, Enum):
    TEXT = "TEXT"                      # nur als Text auf der Seite
    LINK = "LINK"                      # anklickbar (mailto:/tel:)
    DIRECTORY = "DIRECTORY"            # aus Unternehmensverzeichnis (z. B. Google Places)


class EvidenceType(str, Enum):
    MEASURED = "MEASURED"
    DIRECTLY_OBSERVED = "DIRECTLY_OBSERVED"
    AI_INTERPRETATION = "AI_INTERPRETATION"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FindingCategory(str, Enum):
    ACCESSIBILITY = "ACCESSIBILITY"    # Erreichbarkeit, HTTPS, Weiterleitungen
    LINKS = "LINKS"
    METADATA = "METADATA"
    MOBILE = "MOBILE"
    CONTACT = "CONTACT"
    LEGAL = "LEGAL"                    # Impressum/Datenschutz auffindbar
    PERFORMANCE = "PERFORMANCE"
    MAINTENANCE = "MAINTENANCE"        # z. B. veraltetes Copyright-Jahr
    SPELLING = "SPELLING"
    GRAMMAR = "GRAMMAR"
    CLARITY = "CLARITY"                # Verständlichkeit, unklare Leistungen
    CONSISTENCY = "CONSISTENCY"        # widersprüchliche Angaben
    CALL_TO_ACTION = "CALL_TO_ACTION"
    NAVIGATION = "NAVIGATION"


# --------------------------------------------------------------------------- Leads

class Lead(_Model):
    """Ein Unternehmen, wie es aus einer Datenquelle kommt."""

    name: str = Field(min_length=1, max_length=300)
    category: str | None = None
    address: str | None = None
    city: str | None = None
    postal_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    source: str = Field(min_length=1)                 # z. B. "google_places"
    source_ref: str | None = None                     # z. B. place_id
    collected_at: datetime = Field(default_factory=utcnow)
    # Anreicherung (Lead Finder)
    niche_branch: str | None = None
    niche_fit: float | None = Field(default=None, ge=0, le=1)
    niche_reason: str | None = None
    distance_km: float | None = Field(default=None, ge=0)
    business_status: str | None = None                # z. B. OPERATIONAL, CLOSED_PERMANENTLY

    @field_validator("website", mode="before")
    @classmethod
    def _website(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            return normalize_url(value)
        except InvalidUrlError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        email = normalize_email(value)
        if email is None:
            raise ValueError(f"ungültige E-Mail-Adresse: {value!r}")
        return email

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value):
        # Rohwert behalten, wenn nicht normalisierbar: lieber unformatiert als verloren.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_phone(value) or value.strip()


class ContactInfo(_Model):
    """Eine gefundene E-Mail oder Telefonnummer mit Fundstelle."""

    kind: ContactKind
    value: str = Field(min_length=3)                  # normalisiert
    raw: str                                          # wie auf der Seite
    origin: ContactOrigin
    source_url: str
    context: str = Field(default="", max_length=300)  # Textumgebung zur Nachprüfung

    @property
    def is_link(self) -> bool:
        return self.origin is ContactOrigin.LINK


# --------------------------------------------------------------------------- Crawl

class Heading(_Model):
    level: int = Field(ge=1, le=6)
    text: str


class OverflowCulprit(_Model):
    """DOM-Element, das über den Viewport hinausragt (Indiz, kein Urteil)."""

    selector: str
    right_edge: float
    width: float


class PageResult(_Model):
    """Extraktionsergebnis einer einzelnen Seite (Felder gemäß Spezifikation 6.4)."""

    url: str
    final_url: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    page_title: str | None = None
    meta_description: str | None = None
    raw_html_reference: str | None = None             # Dateipfad, nicht der Inhalt
    cleaned_text: str = ""
    headings: list[Heading] = Field(default_factory=list)
    emails: list[ContactInfo] = Field(default_factory=list)
    phone_numbers: list[ContactInfo] = Field(default_factory=list)
    contact_links: list[str] = Field(default_factory=list)
    footer_text: str | None = None
    copyright_year: int | None = Field(default=None, ge=1990, le=2100)
    copyright_text: str | None = None                 # Fundstelle, z. B. "© 2019 Schreinerei Beispiel"
    viewport_width: int | None = Field(default=None, gt=0)
    document_width: int | None = Field(default=None, gt=0)
    horizontal_overflow: bool | None = None           # None = nicht gemessen
    overflow_culprits: list[OverflowCulprit] = Field(default_factory=list)
    # Messwerte für deterministische Prüfungen (Phase 6)
    has_viewport_meta: bool | None = None
    has_h1: bool | None = None
    internal_links: list[str] = Field(default_factory=list)   # max. 150, für Linkprüfung
    redirect_chain: list[str] = Field(default_factory=list)   # "301 http://… " je Sprung
    response_seconds: float | None = Field(default=None, ge=0)
    html_bytes: int | None = Field(default=None, ge=0)
    word_count: int | None = Field(default=None, ge=0)        # sichtbarer Text, auch wenn unbrauchbar
    extraction_method: ExtractionMethod = ExtractionMethod.NONE
    extraction_quality: float = Field(default=0.0, ge=0, le=1)
    crawl_status: CrawlStatus
    failure_reason: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _consistent(self) -> PageResult:
        if not self.crawl_status.is_usable and not self.failure_reason:
            raise ValueError(f"crawl_status {self.crawl_status.value} braucht failure_reason")
        if self.crawl_status.is_usable:
            if not self.cleaned_text.strip():
                raise ValueError("verwertbarer Crawl ohne Text ist widersprüchlich")
            if self.extraction_method is ExtractionMethod.NONE:
                raise ValueError("verwertbarer Crawl braucht eine Extraktionsmethode")
        if self.horizontal_overflow and (self.viewport_width is None or self.document_width is None):
            raise ValueError("Overflow ohne Messwerte ist nicht belegt")
        return self


class CrawlResult(_Model):
    """Ergebnis für eine ganze Website (Startseite + ausgewählte Unterseiten)."""

    site_url: str
    status: CrawlStatus
    pages: list[PageResult] = Field(default_factory=list)
    failure_reason: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CrawlResult:
        if not self.status.is_usable and not self.failure_reason:
            raise ValueError(f"status {self.status.value} braucht failure_reason")
        if self.status.is_usable and not self.usable_pages:
            raise ValueError("verwertbarer Site-Crawl ohne verwertbare Seite")
        return self

    @property
    def usable_pages(self) -> list[PageResult]:
        return [p for p in self.pages if p.crawl_status.is_usable]

    def contacts(self, kind: ContactKind) -> list[ContactInfo]:
        """Alle Kontakte einer Art, je Wert einmal; ein anklickbarer Fund hat Vorrang."""
        best: dict[str, ContactInfo] = {}
        for page in self.pages:
            for contact in page.emails + page.phone_numbers:
                if contact.kind is not kind:
                    continue
                known = best.get(contact.value)
                if known is None or (contact.is_link and not known.is_link):
                    best[contact.value] = contact
        return list(best.values())


# --------------------------------------------------------------------------- Analyse

class Finding(_Model):
    """Ein einzelner, belegter Befund (Felder gemäß Spezifikation 8.4)."""

    issue_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_.\-]+$")
    category: FindingCategory
    severity: Severity
    description: str = Field(min_length=10)
    evidence: str = Field(min_length=3)               # Zitat oder Beobachtung
    source_url: str
    evidence_type: EvidenceType
    confidence: float = Field(ge=0, le=1)
    business_relevance: Severity
    suggested_improvement: str | None = None
    measurement: dict[str, float | int | str | bool] | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _evidence_rules(self) -> Finding:
        if self.evidence_type is EvidenceType.MEASURED and not self.measurement:
            raise ValueError("gemessener Befund braucht Messwerte (measurement)")
        return self

    @property
    def is_verified(self) -> bool:
        """Deterministisch belegt, nicht nur KI-Einschätzung."""
        return self.evidence_type is not EvidenceType.AI_INTERPRETATION


class AnalysisReport(_Model):
    site_url: str
    findings: list[Finding] = Field(default_factory=list)
    analysis_quality: float = Field(ge=0, le=1)
    ai_model: str | None = None
    ai_cost_eur: float = Field(default=0.0, ge=0)
    discarded_findings: int = Field(default=0, ge=0)  # verworfen mangels Beleg
    created_at: datetime = Field(default_factory=utcnow)


class LeadScore(_Model):
    score: float = Field(ge=0, le=100)
    components: dict[str, float] = Field(default_factory=dict)
    disqualified: bool = False
    disqualification_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _reasons(self) -> LeadScore:
        if self.disqualified and not self.disqualification_reasons:
            raise ValueError("Disqualifikation braucht einen Grund")
        if self.disqualified and self.score != 0:
            raise ValueError("disqualifizierter Lead hat Score 0")
        return self


class EmailDraft(_Model):
    recipient: str
    subject: str = Field(min_length=3, max_length=150)
    body: str = Field(min_length=50, max_length=3000)
    finding_ids: list[str] = Field(min_length=1)      # ohne Befund kein Entwurf
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("recipient", mode="before")
    @classmethod
    def _recipient(cls, value):
        email = normalize_email(value or "")
        if email is None:
            raise ValueError(f"ungültiger Empfänger: {value!r}")
        return email
