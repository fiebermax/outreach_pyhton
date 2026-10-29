"""Fehlertypen der Pipeline.

Grundregel: Nur `TemporaryError` wird automatisch wiederholt. Alles andere
(auch unbekannte Exceptions) gilt als dauerhaft, damit nichts endlos läuft.
"""

from __future__ import annotations


class OutreachError(Exception):
    """Basisklasse aller fachlichen Fehler."""


class TemporaryError(OutreachError):
    """Fehler, bei dem ein späterer Versuch Erfolg haben kann."""


class PermanentError(OutreachError):
    """Fehler, bei dem eine Wiederholung nichts ändert."""


# --- temporär ---------------------------------------------------------------

class NetworkError(TemporaryError):
    """Verbindung fehlgeschlagen (DNS, Reset, Abbruch)."""


class FetchTimeout(TemporaryError):
    """Zeitlimit überschritten."""


class ServerError(TemporaryError):
    """HTTP 5xx."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(TemporaryError):
    """HTTP 429 oder API-Kontingent; `retry_after` in Sekunden, falls bekannt."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BrowserCrashed(TemporaryError):
    """Browserprozess abgestürzt oder Seite geschlossen."""


# --- dauerhaft --------------------------------------------------------------

class InvalidUrlError(PermanentError):
    """URL ist syntaktisch oder inhaltlich unbrauchbar."""


class HttpClientError(PermanentError):
    """HTTP 4xx (außer 429)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BlockedError(PermanentError):
    """Zugriff verweigert (Captcha, Bot-Schutz). Wird nie umgangen oder wiederholt."""


class ContentTooLarge(PermanentError):
    """Antwort überschreitet das Größenlimit."""


class UnsupportedContentType(PermanentError):
    """Kein HTML (z. B. PDF, Bild)."""


class InvalidResponseError(PermanentError):
    """Antwort einer API ist nicht im erwarteten Format (z. B. kaputtes JSON)."""


class MissingCredentialError(PermanentError):
    """Benötigter Zugangsschlüssel ist nicht konfiguriert."""


class CostLimitExceeded(PermanentError):
    """KI-Kostenlimit erreicht."""


class SendNotAllowed(PermanentError):
    """Versand verweigert: nicht freigegeben, gesperrt oder bereits versendet."""


def is_temporary(exc: BaseException) -> bool:
    return isinstance(exc, TemporaryError)


def error_for_http_status(status: int, url: str = "", retry_after: float | None = None) -> OutreachError | None:
    """Ordnet einen HTTP-Status einem Fehlertyp zu; `None` bei 2xx/3xx."""
    if status < 400:
        return None
    where = f" ({url})" if url else ""
    if status == 429:
        return RateLimited(f"HTTP 429{where}", retry_after=retry_after)
    if status >= 500:
        return ServerError(f"HTTP {status}{where}", status=status)
    return HttpClientError(f"HTTP {status}{where}", status=status)
