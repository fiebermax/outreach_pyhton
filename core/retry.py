"""Begrenzte Wiederholungen mit exponentiellem Backoff.

Wiederholt wird nur `TemporaryError`. Dauerhafte und unbekannte Fehler
werden sofort weitergereicht.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from core.errors import RateLimited, TemporaryError

T = TypeVar("T")
log = logging.getLogger(__name__)


def backoff_delay(attempt: int, base: float, maximum: float, jitter: float = 0.0) -> float:
    """Wartezeit vor Versuch `attempt + 1` (attempt beginnt bei 1)."""
    delay = min(maximum, base * (2 ** (attempt - 1)))
    if jitter:
        delay += random.uniform(0, jitter * delay)
    return delay


def _delay_for(exc: TemporaryError, attempt: int, base: float, maximum: float, jitter: float) -> float:
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        return min(maximum, max(0.0, exc.retry_after))
    return backoff_delay(attempt, base, maximum, jitter)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "",
) -> T:
    if attempts < 1:
        raise ValueError("attempts muss >= 1 sein")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TemporaryError as exc:
            if attempt == attempts:
                raise
            delay = _delay_for(exc, attempt, base_delay, max_delay, jitter)
            log.warning("Versuch %d/%d fehlgeschlagen%s: %s – neuer Versuch in %.1fs",
                        attempt, attempts, f" [{label}]" if label else "", exc, delay)
            sleep(delay)
    raise AssertionError("unerreichbar")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    label: str = "",
) -> T:
    if attempts < 1:
        raise ValueError("attempts muss >= 1 sein")
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except TemporaryError as exc:
            if attempt == attempts:
                raise
            delay = _delay_for(exc, attempt, base_delay, max_delay, jitter)
            log.warning("Versuch %d/%d fehlgeschlagen%s: %s – neuer Versuch in %.1fs",
                        attempt, attempts, f" [{label}]" if label else "", exc, delay)
            await sleep(delay)
    raise AssertionError("unerreichbar")
