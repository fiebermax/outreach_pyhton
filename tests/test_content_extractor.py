from pathlib import Path

import pytest

from models.schemas import ContactOrigin, ExtractionMethod
from modules.content_extractor import (
    Link, choose_text, extract_content, extract_emails, extract_phones, find_copyright,
)

SITES = Path(__file__).parent / "fixtures" / "sites"
URL = "https://schreinerei-beispiel.de/"


def load(name: str):
    return extract_content((SITES / name).read_text(encoding="utf-8", errors="replace"), URL, current_year=2026)


# --- Stufe 2: Struktur ---

def test_static_page_structure():
    c = load("static.html")
    assert c.title == "Schreinerei Beispiel – Möbel nach Maß in Jettingen"
    assert c.meta_description.startswith("Schreinerei für Einbauschränke")
    assert c.has_viewport_meta and c.lang == "de"
    assert [(h.level, h.text) for h in c.headings] == [(1, "Möbel nach Maß aus Meisterhand"), (2, "Unsere Leistungen")]
    assert "Einbauschränke für Dachschrägen" in c.visible_text
    assert "Telefon: 08225 12345" in c.visible_text      # Footer gehört zum sichtbaren Text


def test_scripts_and_styles_not_in_text():
    html = "<html><body><style>.x{color:red}</style><script>var geheim=1</script><p>Sichtbar</p></body></html>"
    c = extract_content(html, URL)
    assert c.visible_text == "Sichtbar"


def test_hidden_elements_not_in_text():
    html = ('<html><body><p>Sichtbar</p><div style="display: none">Versteckt</div>'
            '<div hidden>Auch versteckt</div><!-- Kommentar --></body></html>')
    assert extract_content(html, URL).visible_text == "Sichtbar"


def test_inline_elements_not_split_into_words():
    c = extract_content("<html><body><p>Wir <b>bauen</b> Möbel.</p></body></html>", URL)
    assert "bauen" in c.visible_text and "Möbel" in c.visible_text


# --- Stufe 3: Vergleich ---

def test_static_uses_trafilatura_without_footer():
    c = load("static.html")
    assert c.method is ExtractionMethod.TRAFILATURA
    assert "Meisterhand" in c.cleaned_text
    assert "© 2019" not in c.cleaned_text


def test_noscript_hint_is_not_content():
    """Trafilatura würde den <noscript>-Hinweis sonst als Seiteninhalt liefern."""
    c = load("js-only.html")
    assert "JavaScript" not in c.cleaned_text
    assert c.cleaned_text == ""
    assert c.method is ExtractionMethod.NONE
    assert any("noscript" in s for s in c.js_signals)
    assert any("#root" in s for s in c.js_signals)


def test_empty_page():
    c = load("empty.html")
    assert c.cleaned_text == "" and c.method is ExtractionMethod.NONE and c.title is None


@pytest.mark.parametrize("visible,main,method", [
    ("", None, ExtractionMethod.NONE),
    ("Kurzer Text", None, ExtractionMethod.HTML_PARSER),
    ("Kurzer Text", "kurz", ExtractionMethod.HTML_PARSER),
    ("a " * 10 + "wir bauen moebel " * 20, "wir bauen moebel " * 20, ExtractionMethod.TRAFILATURA),
    ("wir bauen moebel " * 20, "ganz andere woerter tauchen hier auf " * 5, ExtractionMethod.HTML_PARSER),
    ("leistung angebot " * 400, "leistung angebot " * 6, ExtractionMethod.HTML_PARSER),
])
def test_choose_text_rules(visible, main, method):
    assert choose_text(visible, main)[1] is method


# --- Kontakte ---

def test_static_contacts_with_origin():
    c = load("static.html")
    emails = {(e.value, e.origin) for e in c.emails}
    assert ("info@schreinerei-beispiel.de", ContactOrigin.LINK) in emails
    phones = c.phone_numbers
    assert [(p.value, p.origin) for p in phones] == [("+49822512345", ContactOrigin.TEXT)]
    assert "Telefon" in phones[0].context and phones[0].source_url == URL


@pytest.mark.parametrize("href,expected", [
    ("tel:+49822512345", "+49822512345"),
    ("tel:+%2049%2015221678098", "+4915221678098"),            # URL-kodiert (echtes Beispiel)
    ("tel:+49%20(0)%20714114733399", "+49714114733399"),
    ('tel:01743810360">0174 3810360', "+491743810360"),       # kaputter Link einer echten Seite
])
def test_tel_link_variants(href, expected):
    phones = extract_phones("", [Link(url=href, text="", internal=False)], URL)
    assert [(p.value, p.origin) for p in phones] == [(expected, ContactOrigin.LINK)]
    assert phones[0].raw == href[4:]                          # Original bleibt zur Prüfung erhalten


def test_tel_link_detected():
    html = '<html><body><a href="tel:+49822512345">08225 / 12345</a></body></html>'
    origins = {(p.value, p.origin) for p in extract_content(html, URL).phone_numbers}
    assert ("+49822512345", ContactOrigin.LINK) in origins


@pytest.mark.parametrize("text,expected", [
    ("Telefon: 08225 12345", ["+49822512345"]),
    ("Tel. 0 82 25 / 123 45", ["+49822512345"]),
    ("Mobil: 0171 1234567", ["+491711234567"]),
    ("Rufen Sie an: +49 (0) 8225 12345", ["+49822512345"]),
    ("Fax: 08225 12346", []),                          # Fax ist keine Telefonnummer
    ("89343 Jettingen-Scheppach", []),                 # Postleitzahl
    ("Öffnungszeiten ab 01.02.2024", []),              # Datum
    ("Seit 1998 über 0815 Lösungen hinaus", []),       # Zahl ohne Kennzeichnung
    ("Tel. 08225 12345\n89343 Jettingen", ["+49822512345"]),   # PLZ der Folgezeile nicht anhängen
    ("Tel.: 08225 12345, 89343 Jettingen", ["+49822512345"]),
    # echte Schreibweisen aus Jettingen-Scheppach
    ("Mobil: + 49 15221678098", ["+4915221678098"]),
    ("Telefon\n0174/3810360", ["+491743810360"]),
    ("Telefon:\n0174/3810360", ["+491743810360"]),
])
def test_phone_detection_is_conservative(text, expected):
    assert [p.value for p in extract_phones(text, [], URL)] == expected


@pytest.mark.parametrize("text,expected", [
    ("Schreiben Sie an info@firma.de.", ["info@firma.de"]),
    ("info [at] firma [dot] de", ["info@firma.de"]),
    ("kontakt(at)firma.de", ["kontakt@firma.de"]),
    ("Kein Kontakt angegeben", []),
])
def test_email_detection(text, expected):
    assert [e.value for e in extract_emails(text, [], URL)] == expected


def test_page_without_email():
    assert load("cookie-banner.html").emails == []


def test_contact_links_internal_only():
    html = ('<html><body><a href="/kontakt.html">Kontakt</a><a href="impressum">Impressum</a>'
            '<a href="https://andere-seite.de/kontakt">Fremd</a><a href="/leistungen">Leistungen</a></body></html>')
    assert extract_content(html, URL).contact_links == [URL + "impressum", URL + "kontakt.html"]


# --- Footer / Copyright ---

def test_static_copyright():
    c = load("static.html")
    assert c.copyright_year == 2019
    assert c.copyright_text == "© 2019"
    assert "© 2019 Schreinerei Beispiel" in c.footer_text


@pytest.mark.parametrize("text,year", [
    ("© 2019 Firma", 2019),
    ("Copyright 2015 – 2023 Firma GmbH", 2023),
    ("(c) 2021", 2021),
    ("© Schreinerei Beispiel 2020", 2020),
    ("© 2015-2019 und © 2022", 2022),
    ("© 2099 Tippfehler", None),                       # Zukunft: unplausibel
    ("Gegründet 2019", None),                          # kein Copyright-Vermerk
    ("", None),
])
def test_copyright_years(text, year):
    assert find_copyright([text], current_year=2026)[0] == year


def test_current_year_not_hardcoded():
    assert find_copyright(["© 2030"], current_year=2029)[0] == 2030
    assert find_copyright(["© 2030"], current_year=2026)[0] is None


def test_footer_by_class():
    html = '<html><body><p>Inhalt</p><div class="site-footer">© 2020 Firma</div></body></html>'
    c = extract_content(html, URL, current_year=2026)
    assert c.footer_text == "© 2020 Firma" and c.copyright_year == 2020
