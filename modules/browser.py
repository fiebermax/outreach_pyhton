"""Stufe 4: Rendering im Headless-Browser (Playwright) und Layout-Messung.

Wird nur genutzt, wenn die HTTP-Stufen keinen ausreichenden Text liefern oder
JS-Hinweise vorliegen (Entscheidung: `quality_control.assess_page`).

Warten:
1. `domcontentloaded` (Pflicht), danach muss ein <body> existieren.
2. `networkidle` nur optional mit kurzem Timeout – viele Seiten werden nie "idle".
3. Optional: kurz warten, bis sichtbarer Text erscheint.

Layout: gemessen im Handy-Viewport (375 px). Ein Unterschied zwischen
Dokument- und Viewport-Breite ist nur ein Indiz; die Ursachen-Elemente werden
mitgespeichert, bewertet wird erst in der Analyse.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from playwright.async_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

from core.errors import BrowserCrashed, ContentTooLarge, FetchTimeout, InvalidUrlError, NetworkError, error_for_http_status
from core.normalize import normalize_url
from models.schemas import OverflowCulprit

MOBILE_VIEWPORT = {"width": 375, "height": 812}
OVERFLOW_TOLERANCE_PX = 1

# Sucht die Elemente, die über den rechten Rand ragen. Nur "Ursprünge": Elemente,
# deren Elternteil selbst noch in den Viewport passt – sonst stünde jedes Kind
# eines zu breiten Containers in der Liste.
_LAYOUT_JS = """
(tolerance) => {
  const vw = document.documentElement.clientWidth || window.innerWidth;
  const docW = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  // Was Nutzer tatsächlich erleben: Lässt sich die Seite seitlich scrollen?
  const sx = window.scrollX, sy = window.scrollY;
  window.scrollTo(1e6, sy);
  const maxScrollX = window.scrollX;
  window.scrollTo(sx, sy);
  const sel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    return s;
  };
  const clippedByAncestor = (el) => {
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'hidden' || ox === 'auto' || ox === 'scroll' || ox === 'clip') return true;
    }
    return false;
  };
  const culprits = [];
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.right <= vw + tolerance) continue;
    const st = getComputedStyle(el);
    if (st.position === 'fixed' || st.visibility === 'hidden' || st.display === 'none') continue;
    const parent = el.parentElement;
    if (parent && parent !== document.body && parent.getBoundingClientRect().right > vw + tolerance) continue;
    if (clippedByAncestor(el)) continue;
    culprits.push({selector: sel(el), right_edge: Math.round(r.right), width: Math.round(r.width)});
    if (culprits.length >= 20) break;
  }
  culprits.sort((a, b) => b.right_edge - a.right_edge);
  return {viewport_width: vw, document_width: docW, max_scroll_x: maxScrollX, culprits: culprits.slice(0, 5)};
}
"""


class ConsentPolicy(str, Enum):
    NONE = "none"                              # nie klicken, nur erkennen
    REJECT = "reject"                          # Standard: höchstens "Ablehnen/Nur notwendige"
    REJECT_THEN_ACCEPT = "reject_then_accept"  # nur bei ausdrücklicher Konfiguration


# Reihenfolge wichtig: "Nur notwendige akzeptieren" ist eine Ablehnung, kein Zustimmen.
_REJECT_RE = re.compile(r"ablehnen|nur (technisch )?notwendig|nur essenziell|nur erforderlich|weiter ohne|"
                        r"reject|decline|deny|refuse|necessary only|only necessary|essential only", re.I)
_SETTINGS_RE = re.compile(r"einstellung|anpassen|optionen|mehr info|details|infos|auswahl|"
                          r"settings|customi[sz]e|preferences|manage|more options", re.I)
_ACCEPT_RE = re.compile(r"akzeptier|zustimm|annehmen|einverstanden|alle erlauben|verstanden|"
                        r"accept|allow all|agree|got it", re.I)
_COOKIE_CONTEXT_RE = re.compile(r"cookie|consent|einwillig|datenschutzeinstellung|tracking", re.I)
_CLICKABLE = "button, [role=button], input[type=button], input[type=submit], a"

_BUTTONS_JS = """
(selector) => [...document.querySelectorAll(selector)].map((el, i) => {
  const r = el.getBoundingClientRect(), st = getComputedStyle(el);
  const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
  return {i, text: text.slice(0, 60), visible};
}).filter(b => b.visible && b.text && b.text.length <= 40)
"""


FRAME_EVAL_TIMEOUT_SECONDS = 3.0


async def _frame_eval(frame, expression: str, arg=None, timeout: float = FRAME_EVAL_TIMEOUT_SECONDS):
    """evaluate mit Zeitlimit. Playwright wartet sonst unbegrenzt auf Frames, die nie ein Dokument bekommen
    (echter Fall elektro-rick.base44.app: leeres iframe ohne URL ließ den Crawl hängen)."""
    try:
        return await asyncio.wait_for(frame.evaluate(expression, arg), timeout)
    except (asyncio.TimeoutError, PlaywrightError):
        return None


def classify_button(text: str) -> str:
    """'reject' | 'settings' | 'accept' | 'other' – nur nach Beschriftung, nie nach Farbe/Position."""
    if _REJECT_RE.search(text):
        return "reject"
    if _SETTINGS_RE.search(text):
        return "settings"
    if _ACCEPT_RE.search(text):
        return "accept"
    return "other"


@dataclass
class ConsentResult:
    policy: ConsentPolicy
    detected: bool = False
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (Art, Beschriftung)
    clicked: list[str] = field(default_factory=list)               # z. B. ["reject: Ablehnen"]
    content_without_consent: bool | None = None                    # None = nicht geprüft


@dataclass(frozen=True)
class BrowserLimits:
    navigation_timeout_seconds: float = 20.0
    networkidle_timeout_seconds: float = 3.0
    text_wait_seconds: float = 3.0
    max_html_bytes: int = 5_000_000
    allow_local: bool = False
    consent_policy: ConsentPolicy = ConsentPolicy.REJECT
    consent_wait_seconds: float = 1.5


@dataclass
class RenderResult:
    url: str
    final_url: str
    status: int | None
    html: str
    title: str | None
    viewport_width: int
    document_width: int
    horizontal_overflow: bool
    max_scroll_x: int = 0
    overflow_culprits: list[OverflowCulprit] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    waited_for: list[str] = field(default_factory=list)   # nachvollziehbar, worauf gewartet wurde
    consent: ConsentResult | None = None


class BrowserRenderer:
    """Ein Browser pro Lauf, ein frischer Kontext pro Seite (keine geteilten Cookies)."""

    def __init__(self, limits: BrowserLimits = BrowserLimits()):
        self.limits = limits
        self._pw = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> BrowserRenderer:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _consent_buttons(self, page) -> list[tuple[str, str, object, int]]:
        """Beschriftete Buttons in Frames, deren Text nach Cookie-Hinweis klingt: (Art, Text, Frame, Index)."""
        found = []
        for frame in page.frames:
            body = await _frame_eval(frame, "document.body ? document.body.innerText.slice(0, 20000) : ''")
            if body is None:
                continue                      # Frame ohne Dokument oder hängend: überspringen
            frame_hint = await _frame_eval(frame, "(document.title || '') + ' ' + (window.frameElement ? "
                                                  "(window.frameElement.title || window.frameElement.id || '') : '')") or ""
            if not _COOKIE_CONTEXT_RE.search(body + " " + frame_hint):
                continue
            for b in await _frame_eval(frame, _BUTTONS_JS, _CLICKABLE) or []:
                kind = classify_button(b["text"])
                if kind != "other":
                    found.append((kind, b["text"], frame, b["i"]))
        return found

    async def _handle_consent(self, page, content_ok: Callable[[str], bool] | None) -> ConsentResult:
        result = ConsentResult(self.limits.consent_policy)
        buttons = await self._consent_buttons(page)
        result.detected = bool(buttons)
        result.buttons = [(k, t) for k, t, _, _ in buttons]
        if not buttons:
            return result
        html = await page.content()
        result.content_without_consent = content_ok(html) if content_ok else None
        if result.content_without_consent or self.limits.consent_policy is ConsentPolicy.NONE:
            return result
        order = ["reject"] + (["accept"] if self.limits.consent_policy is ConsentPolicy.REJECT_THEN_ACCEPT else [])
        for kind in order:
            target = next(((t, f, i) for k, t, f, i in buttons if k == kind), None)
            if target is None:
                continue
            text, frame, index = target
            try:
                await frame.locator(_CLICKABLE).nth(index).click(timeout=3000)
            except PlaywrightError:
                continue
            result.clicked.append(f"{kind}: {text}")
            await page.wait_for_timeout(self.limits.consent_wait_seconds * 1000)
            if content_ok is None or content_ok(await page.content()):
                break
            buttons = await self._consent_buttons(page)
        return result

    async def render(self, url: str, content_ok: Callable[[str], bool] | None = None) -> RenderResult:
        """Rendert eine Seite. `content_ok(html)` entscheidet, ob ohne Banner-Klick genug Inhalt da ist."""
        url = normalize_url(url, allow_local=self.limits.allow_local)
        if self._browser is None or not self._browser.is_connected():
            raise BrowserCrashed("Browser nicht gestartet oder abgestürzt")
        lim = self.limits
        started = time.monotonic()
        context = await self._browser.new_context(viewport=MOBILE_VIEWPORT, java_script_enabled=True,
                                                  user_agent=None, locale="de-DE")
        waited: list[str] = []
        try:
            page = await context.new_page()
            # Videos/Audio nie laden; Bilder schon, weil sie das Layout (Overflow) beeinflussen.
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type == "media"
                             else route.continue_())
            try:
                response = await page.goto(url, wait_until="domcontentloaded",
                                           timeout=lim.navigation_timeout_seconds * 1000)
            except PlaywrightTimeout as exc:
                raise FetchTimeout(f"Browser: domcontentloaded nach {lim.navigation_timeout_seconds}s nicht erreicht") from exc
            except PlaywrightError as exc:
                text = str(exc)
                if "ERR_NAME_NOT_RESOLVED" in text or "ERR_CONNECTION" in text:
                    raise NetworkError(f"Browser: {text.splitlines()[0][:200]}") from exc
                if "Target closed" in text or "crashed" in text.lower():
                    raise BrowserCrashed(text[:200]) from exc
                raise NetworkError(f"Browser: {text.splitlines()[0][:200]}") from exc
            waited.append("domcontentloaded")

            status = response.status if response else None
            if status is not None:
                error = error_for_http_status(status, page.url)
                if error is not None:
                    raise error
            if await page.query_selector("body") is None:
                raise NetworkError("Browser: Seite ohne <body>")

            try:
                await page.wait_for_load_state("networkidle", timeout=lim.networkidle_timeout_seconds * 1000)
                waited.append("networkidle")
            except PlaywrightTimeout:
                waited.append(f"networkidle nicht erreicht ({lim.networkidle_timeout_seconds}s, ignoriert)")
            try:
                await page.wait_for_function("document.body && document.body.innerText.trim().length > 50",
                                             timeout=lim.text_wait_seconds * 1000)
                waited.append("Text sichtbar")
            except PlaywrightTimeout:
                waited.append(f"kein Text nach {lim.text_wait_seconds}s")

            consent = await self._handle_consent(page, content_ok)
            html = await page.content()
            if len(html.encode("utf-8", errors="ignore")) > lim.max_html_bytes:
                raise ContentTooLarge(f"gerendertes HTML größer als {lim.max_html_bytes} Bytes")
            layout = await _frame_eval(page.main_frame, _LAYOUT_JS, OVERFLOW_TOLERANCE_PX, timeout=10.0)
            if layout is None:
                raise BrowserCrashed("Layout-Messung nicht möglich (Zeitlimit)")
            return RenderResult(
                url=url, final_url=page.url, status=status, html=html, title=(await page.title()) or None,
                viewport_width=layout["viewport_width"], document_width=layout["document_width"],
                # Nur echtes seitliches Scrollen zählt; abgeschnittener Inhalt (overflow-x: hidden) nicht.
                horizontal_overflow=layout["max_scroll_x"] > OVERFLOW_TOLERANCE_PX,
                max_scroll_x=layout["max_scroll_x"],
                overflow_culprits=[OverflowCulprit(**c) for c in layout["culprits"]],
                elapsed_seconds=time.monotonic() - started, waited_for=waited, consent=consent,
            )
        except InvalidUrlError:
            raise
        except PlaywrightError as exc:
            raise BrowserCrashed(f"Browserfehler: {str(exc)[:200]}") from exc
        finally:
            await context.close()
