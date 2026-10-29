"""Paket 3.8: Rohes HTML als Datei ablegen, in der Datenbank nur die Referenz.

Dateiname = Hash des Inhalts, damit identische Seiten nur einmal Platz brauchen.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class HtmlStore:
    def __init__(self, root: Path, max_bytes: int = 2_000_000):
        self.root = Path(root)
        self.max_bytes = max_bytes

    def save(self, html: str) -> str | None:
        """Speichert und gibt den Pfad relativ zu `root` zurück; zu groß → None (nicht gespeichert)."""
        data = html.encode("utf-8", errors="replace")
        if len(data) > self.max_bytes:
            return None
        digest = hashlib.sha256(data).hexdigest()[:24]
        relative = f"{digest[:2]}/{digest}.html"
        path = self.root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)          # atomar: nie halbe Dateien
        return relative

    def load(self, reference: str) -> str:
        path = (self.root / reference).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Referenz zeigt außerhalb des HTML-Speichers")
        return path.read_text(encoding="utf-8")
