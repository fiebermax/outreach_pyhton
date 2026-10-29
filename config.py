"""Zentrale Konfiguration aus Umgebungsvariablen bzw. `.env`.

Zugangsdaten sind optional beim Laden. Fehlen sie, meldet erst das Modul,
das sie braucht, einen klaren Fehler über `Settings.require_secret()`.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.errors import MissingCredentialError

PROJECT_ROOT = Path(__file__).resolve().parent


class MailMode(str, Enum):
    DRAFT = "draft"
    SMTP = "smtp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Datenquellen / KI ---
    google_places_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-chat"
    deepseek_vision_model: str = "deepseek-flash"     # sieht Screenshots (Design-Urteil)
    deepseek_base_url: str = "https://api.deepseek.com"
    ai_max_cost_eur: float = Field(2.0, ge=0)
    # Annahme, bitte mit der aktuellen DeepSeek-Preisliste abgleichen (€ pro 1 Mio. Token)
    deepseek_price_input_per_mtok_eur: float = Field(0.30, ge=0)
    deepseek_price_output_per_mtok_eur: float = Field(1.20, ge=0)

    # --- E-Mail ---
    mail_mode: MailMode = MailMode.DRAFT
    # Zweiter, unabhängiger Schalter: SMTP-Modus allein versendet noch nichts.
    send_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    sender_address: str | None = None
    sender_name: str = "Maximilian Fieber"
    sender_website: str | None = "fieberki.de"
    # Postfach-Abgleich (IONOS: imap.ionos.de, Gmail: imap.gmail.com + App-Passwort)
    imap_host: str | None = None
    imap_port: int = Field(993, ge=1, le=65535)
    imap_user: str | None = None
    imap_password: SecretStr | None = None

    # --- Speicher ---
    database_path: Path = Path("data/outreach.db")
    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")

    # --- Crawler ---
    crawl_timeout_seconds: float = Field(15.0, gt=0, le=120)
    crawl_max_bytes: int = Field(5_000_000, gt=0)
    crawl_max_pages: int = Field(8, ge=1, le=50)
    crawl_max_depth: int = Field(2, ge=0, le=5)
    crawl_max_seconds_per_site: float = Field(90.0, gt=0)
    crawl_concurrency: int = Field(4, ge=1, le=32)
    crawl_retries: int = Field(2, ge=0, le=5)
    min_extraction_quality: float = Field(0.5, ge=0, le=1)

    # Cookie-Banner: none = nie klicken, reject = höchstens "Ablehnen", reject_then_accept = notfalls zustimmen
    consent_policy: str = Field("reject", pattern="^(none|reject|reject_then_accept)$")

    # --- Google Places (Kostenbremse) ---
    places_max_requests_per_run: int = Field(30, ge=1, le=500)
    places_monthly_limit: int = Field(800, ge=1)
    places_cache_days: float = Field(30, ge=0, le=30)   # Google erlaubt nur begrenztes Zwischenspeichern

    # --- Outreach ---
    contact_cooldown_days: int = Field(365, ge=0)   # Sperrfrist nach jedem Kontakt

    # --- Betrieb ---
    log_level: str = "INFO"

    @field_validator("database_path", "data_dir", "logs_dir")
    @classmethod
    def _relative_to_project(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unbekanntes LOG_LEVEL: {value}")
        return value

    @field_validator("google_places_api_key", "deepseek_api_key", "smtp_password", "imap_password", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def require_secret(self, field: str) -> str:
        """Liefert den Klartext eines Schlüssels oder einen verständlichen Fehler."""
        value = getattr(self, field)
        if value is None:
            raise MissingCredentialError(
                f"{field.upper()} ist nicht gesetzt. In .env oder den Umgebungsvariablen eintragen."
            )
        return value.get_secret_value()

    def secret_values(self) -> list[str]:
        """Alle gesetzten Geheimnisse, damit das Logging sie maskieren kann."""
        secrets = [self.google_places_api_key, self.deepseek_api_key, self.smtp_password, self.imap_password]
        return [s.get_secret_value() for s in secrets if s is not None]

    @property
    def smtp_allowed(self) -> bool:
        return self.mail_mode is MailMode.SMTP and self.send_enabled


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
