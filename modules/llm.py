"""Austauschbarer KI-Client (Standard: DeepSeek, OpenAI-kompatible Chat-API).

Kosten werden aus den gemeldeten Token-Zahlen berechnet. Die Preise sind
Konfigurationswerte (Annahme, bitte mit der aktuellen Preisliste abgleichen),
keine fest eingebauten Fakten. Vor jedem Aufruf wird das Kostenlimit geprüft.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from core.errors import (
    CostLimitExceeded, InvalidResponseError, MissingCredentialError, error_for_http_status,
)
from core.retry import retry_call

log = logging.getLogger(__name__)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_eur: float = 0.0


class LLMClient(Protocol):
    model: str

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> tuple[dict, Usage]:
        """Liefert ein JSON-Objekt. Wirft InvalidResponseError, wenn keins zurückkommt."""


@dataclass
class CostTracker:
    limit_eur: float
    spent_eur: float = 0.0

    def check(self) -> None:
        if self.spent_eur >= self.limit_eur:
            raise CostLimitExceeded(f"KI-Kostenlimit erreicht ({self.spent_eur:.4f} € von {self.limit_eur:.2f} €)")

    def add(self, usage: Usage) -> None:
        self.spent_eur += usage.cost_eur


def parse_json_object(text: str) -> dict:
    """Akzeptiert reines JSON oder JSON in einem ```-Block; alles andere ist ungültig."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise InvalidResponseError(f"keine JSON-Antwort: {text[:120]!r}") from exc
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc2:
            raise InvalidResponseError(f"JSON nicht lesbar: {exc2}") from exc2
    if not isinstance(data, dict):
        raise InvalidResponseError("JSON-Antwort ist kein Objekt")
    return data


class DeepSeekClient:
    def __init__(self, api_key: str | None, *, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com", tracker: CostTracker | None = None,
                 price_input_per_mtok_eur: float = 0.30, price_output_per_mtok_eur: float = 1.20,
                 timeout_seconds: float = 60.0, retries: int = 2, http: httpx.Client | None = None):
        if not api_key:
            raise MissingCredentialError("DEEPSEEK_API_KEY ist nicht gesetzt.")
        self._key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.tracker = tracker or CostTracker(limit_eur=2.0)
        self.prices = (price_input_per_mtok_eur, price_output_per_mtok_eur)
        self.retries = retries
        self.http = http or httpx.Client(timeout=timeout_seconds)

    def _call(self, payload: dict) -> dict:
        response = self.http.post(f"{self.base_url}/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {self._key}"})
        error = error_for_http_status(response.status_code, "deepseek")
        if error is not None:
            if response.status_code in (401, 403):
                raise MissingCredentialError("DeepSeek lehnt den API-Key ab (401/403).")
            raise error
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidResponseError("DeepSeek-Antwort ist kein JSON") from exc

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> tuple[dict, Usage]:
        self.tracker.check()
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        raw = retry_call(lambda: self._call(payload), attempts=self.retries + 1, base_delay=2.0, label="deepseek")
        usage_raw = raw.get("usage") or {}
        usage = Usage(usage_raw.get("prompt_tokens", 0), usage_raw.get("completion_tokens", 0))
        usage.cost_eur = (usage.prompt_tokens * self.prices[0] + usage.completion_tokens * self.prices[1]) / 1e6
        self.tracker.add(usage)
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidResponseError(f"unerwartetes Antwortformat: {str(raw)[:150]}") from exc
        return parse_json_object(content or ""), usage
