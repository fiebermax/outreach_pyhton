"""Strukturiertes Logging mit Maskierung von Zugangsdaten.

Konsole: lesbare Zeilen. Datei (`logs/outreach.jsonl`): eine JSON-Zeile pro Eintrag.
Maskiert werden die konfigurierten Geheimnisse und typische Schlüsselmuster,
auch in Exception-Tracebacks.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

MASK = "***"

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                 # DeepSeek/OpenAI-Stil
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),             # Google API Key
    re.compile(r"(?i)(password|passwort|api[_-]?key|token|secret)(\s*[=:]\s*)([^\s,;&\"']+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)(\S+)"),
]

_EXTRA_FIELDS = ("lead_id", "url", "step", "status")


class SecretMasker:
    def __init__(self, secrets: Iterable[str] = ()):
        # Längste zuerst, damit Teilstrings nicht vorher ersetzt werden.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        for pattern in _PATTERNS:
            if pattern.groups >= 2:
                text = pattern.sub(lambda m: "".join(m.groups()[:-1]) + MASK, text)
            else:
                text = pattern.sub(MASK, text)
        return text


class MaskingFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker, fmt: str | None = None):
        super().__init__(fmt or "%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self._mask = masker

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{k}={getattr(record, k)}" for k in _EXTRA_FIELDS if hasattr(record, k))
        return self._mask(f"{line} {extras}" if extras else line)


class JsonFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker):
        super().__init__()
        self._mask = masker

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": self._mask(record.getMessage()),
        }
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                entry[key] = self._mask(str(getattr(record, key)))
        if record.exc_info:
            entry["exc"] = self._mask(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    log_file: Path | None = None,
) -> SecretMasker:
    """Richtet Root-Logger ein. Mehrfacher Aufruf ersetzt die Handler statt sie zu verdoppeln."""
    masker = SecretMasker(secrets)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_outreach", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(MaskingFormatter(masker))
    console._outreach = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter(masker))
        file_handler._outreach = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # Bibliotheken loggen sonst ganze URLs inkl. Query-Parametern auf INFO.
    for noisy in ("httpx", "httpcore", "trafilatura"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    return masker


def setup_from_settings(settings) -> SecretMasker:
    return setup_logging(settings.log_level, settings.secret_values(), settings.logs_dir / "outreach.jsonl")
