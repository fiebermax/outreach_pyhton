"""Stufen 2 und 3: Inhalte aus HTML extrahieren.

- Stufe 2 (`HTML_PARSER`): sichtbarer Text der ganzen Seite mit BeautifulSoup/lxml.
- Stufe 3 (`TRAFILATURA`): Haupttext ohne Navigation/Footer.
Beide werden verglichen; `choose_text()` dokumentiert die Entscheidungsregel.

Kontakte, Footer und Copyright kommen immer aus dem vollständigen sichtbaren
Text, weil die Haupttext-Extraktion den Footer absichtlich entfernt.

Reine Funktionen ohne Netzwerkzugriff – testbar mit lokalen HTML-Dateien.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

import trafilatura
from bs4 import BeautifulSoup, Comment, Tag

from core.normalize import normalize_email, normalize_phone
from models.schemas import ContactInfo, ContactKind, ContactOrigin, ExtractionMethod, Heading

# Elemente ohne sichtbaren Text. <noscript> wird separat als JS-Signal ausgewertet.
_INVISIBLE_TAGS = ["script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "head"]
_SPA_ROOT_IDS = {"root", "app", "__next", "__nuxt", "___gatsby", "svelte"}

_EMAIL_TEXT = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*(?:@|\[at\]|\(at\)|\{at\}|\s\[@\]\s)\s*[a-zA-Z0-9\-]+"
    r"(?:\s*(?:\.|\[dot\]|\(dot\)|\{dot\})\s*[a-zA-Z0-9\-]+)*\s*(?:\.|\[dot\]|\(dot\)|\{dot\})\s*[a-zA-Z]{2,}"
)
# Telefonnummern nur mit Kennzeichnung davor (Tel., Telefon, Mobil …) oder im internationalen Format.
# Bewusst konservativ: lieber eine Nummer übersehen als eine Postleitzahl oder ein Datum als Telefon melden.
_PHONE_LABEL = r"(?:tel(?:efon)?|fon|phone|mobil|handy|mobile|ruf|t)\.?\s*(?:nr\.?|nummer)?\s*[:.]?\s*"
_PHONE_NUMBER = r"((?:\+\s?|00)\d{1,3}[\s\-/.]?(?:\(0\)\s?)?\d{2,5}(?:\s?[\-/.]?\s?\d{1,8}){1,4}|\(?0(?:\s?\d){2,5}\)?(?:\s?[\-/.]?\s?\d{1,8}){1,4})"
_PHONE_WITH_LABEL = re.compile(r"(?<![\w])" + _PHONE_LABEL + _PHONE_NUMBER, re.I)
_PHONE_INTERNATIONAL = re.compile(r"(?<![\w+])((?:\+\s?49|0049)[\s\-/.]?(?:\(0\)\s?)?\d{2,5}(?:\s?[\-/.]?\s?\d{1,8}){1,4})")
_LABEL_ALONE = re.compile(r"(?im)^\s*(tel(?:efon)?|fon|phone|mobil|handy|mobile)\.?\s*:?\s*\n\s*")
_FAX_BEFORE = re.compile(r"fax\.?\s*[:.]?\s*$", re.I)

_COPYRIGHT = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:by\s+)?(?:[^\d\n]{0,40}?)?((?:19|20)\d{2})(?:\s*[-–—/]\s*((?:19|20)\d{2}))?",
    re.I,
)

_RELEVANT_LINK_WORDS = ("kontakt", "contact", "impressum", "imprint", "datenschutz", "privacy", "anfahrt")


@dataclass
class Link:
    url: str
    text: str
    internal: bool


@dataclass
class ExtractedContent:
    title: str | None
    meta_description: str | None
    lang: str | None
    has_viewport_meta: bool
    headings: list[Heading]
    visible_text: str                    # Stufe 2: ganze Seite
    main_text: str | None                # Stufe 3: Trafilatura
    cleaned_text: str                    # gewählter Text
    method: ExtractionMethod
    choice_reason: str
    footer_text: str | None
    copyright_year: int | None
    copyright_text: str | None
    emails: list[ContactInfo]
    phone_numbers: list[ContactInfo]
    links: list[Link]
    contact_links: list[str]             # interne Links zu Kontakt/Impressum/Datenschutz
    noscript_text: str
    js_signals: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- Hilfsfunktionen

def _clean_whitespace(text: str) -> str:
    lines = (re.sub(r"[ \t ​]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _short(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _visible_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for name in _INVISIBLE_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(_is_hidden):
        tag.decompose()
    return soup


def _text_of(node) -> str:
    # Blockelemente sollen Zeilen ergeben, Inline-Elemente nicht zerreißen.
    return _clean_whitespace(node.get_text("\n"))


def _context(text: str, start: int, end: int, radius: int = 60) -> str:
    return _short(text[max(0, start - radius): end + radius])


# --------------------------------------------------------------------------- Kontakte

def deobfuscate_email(raw: str) -> str:
    text = re.sub(r"\s*(?:\[at\]|\(at\)|\{at\}|\[@\])\s*", "@", raw, flags=re.I)
    text = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\})\s*", ".", text, flags=re.I)
    return re.sub(r"\s*@\s*", "@", text)


def extract_emails(text: str, links: list[Link], page_url: str) -> list[ContactInfo]:
    found: dict[tuple[str, ContactOrigin], ContactInfo] = {}
    for link in links:
        if link.url.lower().startswith("mailto:"):
            value = normalize_email(link.url)
            if value:
                found.setdefault((value, ContactOrigin.LINK), ContactInfo(
                    kind=ContactKind.EMAIL, value=value, raw=link.url[:200], origin=ContactOrigin.LINK,
                    source_url=page_url, context=_short(link.text or value)))
    for match in _EMAIL_TEXT.finditer(text):
        value = normalize_email(deobfuscate_email(match.group(0)))
        if value:
            found.setdefault((value, ContactOrigin.TEXT), ContactInfo(
                kind=ContactKind.EMAIL, value=value, raw=match.group(0), origin=ContactOrigin.TEXT,
                source_url=page_url, context=_context(text, match.start(), match.end())))
    return list(found.values())


def extract_phones(text: str, links: list[Link], page_url: str) -> list[ContactInfo]:
    found: dict[tuple[str, ContactOrigin], ContactInfo] = {}
    for link in links:
        if link.url.lower().startswith("tel:"):
            raw = link.url[4:]
            # URL-Kodierung auflösen (tel:+%2049...) und nur den Nummernteil nehmen –
            # manche Seiten haben kaputte Links wie tel:0174381">0174 381; raw bleibt zur Prüfung erhalten.
            number = re.match(r"[\d+\s()/.\-]*", unquote(raw)).group(0)
            value = normalize_phone(number)
            if value:
                found.setdefault((value, ContactOrigin.LINK), ContactInfo(
                    kind=ContactKind.PHONE, value=value, raw=raw, origin=ContactOrigin.LINK,
                    source_url=page_url, context=_short(link.text or raw)))
    # "Telefon" und Nummer stehen oft in getrennten Elementen und damit auf zwei Zeilen.
    text = _LABEL_ALONE.sub(lambda m: m.group(1) + ": ", text)
    # Zeilenweise: Eine Nummer endet am Zeilenende, sonst würde z. B. die PLZ der Folgezeile angehängt.
    for line in text.splitlines():
        for pattern in (_PHONE_WITH_LABEL, _PHONE_INTERNATIONAL):
            for match in pattern.finditer(line):
                if _FAX_BEFORE.search(line[max(0, match.start() - 12): match.start()]):
                    continue
                raw = match.group(1).strip()
                if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}", raw):   # Datum
                    continue
                value = normalize_phone(raw)
                if value:
                    found.setdefault((value, ContactOrigin.TEXT), ContactInfo(
                        kind=ContactKind.PHONE, value=value, raw=raw, origin=ContactOrigin.TEXT,
                        source_url=page_url, context=_context(line, match.start(), match.end())))
    return list(found.values())


# --------------------------------------------------------------------------- Footer / Copyright

def find_footer(soup: BeautifulSoup) -> str | None:
    candidates = soup.find_all("footer")
    if not candidates:
        candidates = soup.find_all(
            lambda t: isinstance(t, Tag) and t.name in ("div", "section")
            and re.search(r"footer|fusszeile|site-info|copyright", " ".join(t.get("class", [])) + " " + (t.get("id") or ""), re.I)
        )
    if not candidates:
        return None
    text = _text_of(candidates[-1])
    return text or None


def find_copyright(texts: list[str | None], current_year: int | None = None) -> tuple[int | None, str | None]:
    """Jüngstes plausibles Copyright-Jahr und die Fundstelle. Erst Footer, dann Seitentext.

    Ein Bereich wie "2015–2023" zählt mit seinem Endjahr.
    """
    current_year = current_year or date.today().year
    for text in texts:
        if not text:
            continue
        best: tuple[int, str] | None = None
        for match in _COPYRIGHT.finditer(text):
            years = [int(y) for y in match.groups() if y]
            year = max(years)
            if not 1990 <= year <= current_year + 1:
                continue
            if best is None or year > best[0]:
                best = (year, _short(match.group(0), 120))
        if best:
            return best
    return None, None


# --------------------------------------------------------------------------- Textwahl

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w{3,}", text.casefold())}


def choose_text(visible: str, main: str | None) -> tuple[str, ExtractionMethod, str]:
    """Regel (dokumentiert in docs/qualitaet.md):

    1. Kein sichtbarer Text und kein Haupttext → NONE.
    2. Haupttext fehlt oder < 80 Zeichen → sichtbarer Text (HTML_PARSER).
    3. Haupttext enthält Wörter, die im sichtbaren Text nicht vorkommen (Überlappung < 90 %) → HTML_PARSER,
       denn dann stammt er aus unsichtbaren Quellen.
    4. Haupttext deckt < 15 % des sichtbaren Texts ab und der sichtbare Text ist lang → HTML_PARSER
       (kleine Firmenseiten: Trafilatura verwirft sonst Leistungslisten).
    5. Sonst Haupttext (TRAFILATURA).
    """
    visible = visible.strip()
    main = (main or "").strip()
    if not visible and not main:
        return "", ExtractionMethod.NONE, "weder sichtbarer Text noch Haupttext"
    if len(main) < 80:
        return visible, ExtractionMethod.HTML_PARSER, f"Haupttext zu kurz ({len(main)} Zeichen)"
    main_tokens, visible_tokens = _tokens(main), _tokens(visible)
    overlap = len(main_tokens & visible_tokens) / max(1, len(main_tokens))
    if overlap < 0.9:
        return visible, ExtractionMethod.HTML_PARSER, f"Haupttext passt nicht zum sichtbaren Text (Überlappung {overlap:.0%})"
    coverage = len(main) / max(1, len(visible))
    if coverage < 0.15 and len(visible) > 1500:
        return visible, ExtractionMethod.HTML_PARSER, f"Haupttext deckt nur {coverage:.0%} ab"
    return main, ExtractionMethod.TRAFILATURA, f"Haupttext {len(main)} von {len(visible)} Zeichen, Überlappung {overlap:.0%}"


# --------------------------------------------------------------------------- Hauptfunktion

def _js_signals(raw_soup: BeautifulSoup, visible_text: str, noscript_text: str) -> list[str]:
    signals = []
    if noscript_text:
        signals.append(f"noscript-Hinweis: {_short(noscript_text, 80)}")
    for element_id in _SPA_ROOT_IDS:
        node = raw_soup.find(id=element_id)
        if node is not None and not node.get_text(strip=True):
            signals.append(f"leeres App-Element #{element_id}")
    scripts = len(raw_soup.find_all("script"))
    if scripts and len(visible_text) < 200:
        signals.append(f"{scripts} Skripte bei nur {len(visible_text)} Zeichen Text")
    return signals


def extract_content(html: str, page_url: str, current_year: int | None = None) -> ExtractedContent:
    raw_soup = BeautifulSoup(html, "lxml")

    title_tag = raw_soup.find("title")
    title = _short(title_tag.get_text(" "), 300) if title_tag else None
    desc_tag = raw_soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta_description = _short(desc_tag.get("content", ""), 500) if desc_tag else None
    viewport = raw_soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}) is not None
    html_tag = raw_soup.find("html")
    lang = html_tag.get("lang") if html_tag else None
    noscript_text = _clean_whitespace(" ".join(n.get_text(" ") for n in raw_soup.find_all("noscript")))

    base = raw_soup.find("base", href=True)
    base_url = urljoin(page_url, base["href"]) if base else page_url
    host = urlsplit(page_url).hostname or ""
    links: list[Link] = []
    for a in raw_soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        url = href if href.lower().startswith(("mailto:", "tel:")) else urljoin(base_url, href)
        target_host = urlsplit(url).hostname or ""
        internal = target_host.removeprefix("www.") == host.removeprefix("www.")
        links.append(Link(url=url, text=_short(a.get_text(" "), 120), internal=internal))

    soup = _visible_soup(html)
    headings = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = _short(tag.get_text(" "), 300)
        if text:
            headings.append(Heading(level=int(tag.name[1]), text=text))
    body = soup.body or soup
    visible_text = _text_of(body)
    footer_text = find_footer(soup)

    # Trafilatura bekommt HTML ohne <noscript>, sonst gilt "Bitte JavaScript aktivieren" als Inhalt.
    for tag in raw_soup.find_all("noscript"):
        tag.decompose()
    main_text = trafilatura.extract(str(raw_soup), url=page_url, include_comments=False, include_tables=True)
    main_text = _clean_whitespace(main_text) if main_text else None

    cleaned, method, reason = choose_text(visible_text, main_text)
    year, year_text = find_copyright([footer_text, visible_text], current_year)

    contact_links = sorted({
        l.url for l in links
        if l.internal and any(w in (l.url + " " + l.text).lower() for w in _RELEVANT_LINK_WORDS)
    })

    return ExtractedContent(
        title=title or None, meta_description=meta_description or None, lang=lang, has_viewport_meta=viewport,
        headings=headings[:100], visible_text=visible_text, main_text=main_text, cleaned_text=cleaned,
        method=method, choice_reason=reason, footer_text=footer_text, copyright_year=year,
        copyright_text=year_text, emails=extract_emails(visible_text, links, page_url),
        phone_numbers=extract_phones(visible_text, links, page_url), links=links, contact_links=contact_links,
        noscript_text=noscript_text, js_signals=_js_signals(BeautifulSoup(html, "lxml"), visible_text, noscript_text),
    )
