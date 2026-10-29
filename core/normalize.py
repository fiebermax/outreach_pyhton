"""Normalisierung für Duplikaterkennung und saubere Speicherung.

Alle Funktionen sind rein (keine I/O) und deterministisch.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from core.errors import InvalidUrlError

_ALLOWED_SCHEMES = {"http", "https"}
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")


def normalize_url(url: str, *, allow_local: bool = False) -> str:
    """Bringt eine Website-URL in eine einheitliche Form oder wirft `InvalidUrlError`.

    - ergänzt fehlendes Schema (`www.x.de` → `https://www.x.de/`)
    - Host klein, IDN als Punycode, Standardport entfernt, Fragment entfernt
    - nur http/https; lokale/private Adressen nur mit `allow_local` (Tests)
    """
    if not url or not url.strip():
        raise InvalidUrlError("leere URL")
    url = url.strip()
    if "://" not in url:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) and not re.match(r"^[^:/]+:\d+", url):
            raise InvalidUrlError(f"nicht unterstütztes Schema: {url[:40]}")
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(f"nicht unterstütztes Schema: {scheme}")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise InvalidUrlError(f"kein Host: {url[:80]}")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidUrlError(f"ungültiger Hostname: {host}") from exc
    if not _HOST_RE.match(host):
        raise InvalidUrlError(f"ungültiger Hostname: {host}")

    if not allow_local:
        if host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
            raise InvalidUrlError(f"lokale Adresse: {host}")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise InvalidUrlError(f"Host ohne Domain-Endung: {host}") from None
        else:
            if not ip.is_global:
                raise InvalidUrlError(f"private/reservierte IP: {host}")

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError(f"ungültiger Port: {url[:80]}") from exc
    netloc = host
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def normalize_domain(url: str) -> str:
    """Registrierbare Vergleichsform: Host ohne `www.`, ohne Port. `https://WWW.Bäcker.de/x` → `xn--bcker-gra.de`."""
    host = urlsplit(normalize_url(url, allow_local=True)).hostname or ""
    return host[4:] if host.startswith("www.") else host


# Längere Formen zuerst, damit "gmbh & co. kg" nicht als "gmbh" + Rest endet.
_LEGAL_FORMS = [
    r"gmbh\s*&\s*co\.?\s*kg(aa)?", r"ug\s*\(haftungsbeschr(ä|ae)nkt\)", r"ug\s*\(hb\)",
    r"gmbh", r"mbh", r"ag", r"kgaa", r"kg", r"ohg", r"gbr", r"partg(\s*mbb)?", r"e\.\s*k\.?", r"e\.\s*kfm\.?",
    r"e\.\s*v\.?", r"eg", r"ug", r"inh\.?", r"inhaber(in)?",
]
_LEGAL_RE = re.compile(r"(?<![\w])(" + "|".join(_LEGAL_FORMS) + r")(?![\w])")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return text.translate(_UMLAUTS)


def normalize_company_name(name: str) -> str:
    """`Müller GmbH & Co. KG` → `mueller`; `Schreinerei Reiser e.K.` → `schreinerei reiser`."""
    text = _LEGAL_RE.sub(" ", _fold(name))
    text = text.replace("&", " und ").replace("+", " und ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bund\s*$", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


_STREET_SUFFIX = re.compile(r"(strasse|str\.?)(?=\s|$|\d)")


def address_key(address: str) -> str | None:
    """Vergleichsschlüssel `PLZ|straße hausnummer`, unabhängig von Ortsteil-Schreibweisen.

    `Burgauer Weg 14, 89343 Jettingen-Scheppach-Jettingen, Deutschland` → `89343|burgauer weg 14`.
    Ohne PLZ oder Straße: `None` (dann kein Adressvergleich möglich).
    """
    if not address:
        return None
    text = _fold(address)
    plz = re.search(r"\b(\d{5})\b", text)
    if not plz:
        return None
    street = text.split(",")[0] if "," in text else text[: plz.start()]
    street = street.replace(plz.group(1), "")
    street = _STREET_SUFFIX.sub("str", street)
    street = re.sub(r"(\d+)\s+([a-z])\b", r"\1\2", street)       # "2 a" → "2a"
    street = re.sub(r"[^\w\s]", " ", street)
    street = re.sub(r"\s+", " ", street).strip()
    if not street:
        return None
    return f"{plz.group(1)}|{street}"


def normalize_phone(raw: str, default_country_code: str = "49") -> str | None:
    """Telefonnummer in E.164-ähnliche Form: `08225 / 1234-5` → `+49822512345`. Unplausibel → `None`."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"\(0\)", "", text)                            # +49 (0) 8225 ...
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if plus:
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    elif digits.startswith("0"):
        number = default_country_code + digits[1:]
    else:
        return None                                              # ohne Vorwahl nicht eindeutig
    if not 8 <= len(number) <= 15:
        return None
    return "+" + number


_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")


def normalize_email(raw: str) -> str | None:
    if not raw:
        return None
    email = raw.strip().strip(".,;:<>()[]\"'").lower()
    if email.startswith("mailto:"):
        email = email[7:].split("?")[0]
    if not _EMAIL_RE.match(email) or ".." in email:
        return None
    return email
