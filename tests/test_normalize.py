import pytest

from core.errors import InvalidUrlError
from core.normalize import (
    address_key, normalize_company_name, normalize_domain, normalize_email, normalize_phone, normalize_url,
)


@pytest.mark.parametrize("raw,expected", [
    ("www.schreinerei-reiser.de", "https://www.schreinerei-reiser.de/"),
    ("HTTP://Example.DE:80/Kontakt#top", "http://example.de/Kontakt"),
    ("https://example.de:443", "https://example.de/"),
    ("https://example.de:8443/x?a=1", "https://example.de:8443/x?a=1"),
    ("  https://bücher.de  ", "https://xn--bcher-kva.de/"),
    ("example.de/leistungen", "https://example.de/leistungen"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "ftp://example.de", "javascript:alert(1)", "mailto:a@b.de",
    "https://", "http://localhost/", "http://192.168.0.10/", "http://127.0.0.1:8000/",
    "http://intranet/", "https://exa mple.de",
])
def test_invalid_urls_rejected(raw):
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)


def test_local_allowed_for_tests():
    assert normalize_url("http://127.0.0.1:8000/a", allow_local=True) == "http://127.0.0.1:8000/a"


@pytest.mark.parametrize("a,b", [
    ("https://www.example.de/", "example.de"),
    ("http://EXAMPLE.de:8080/impressum", "https://example.de"),
])
def test_same_domain(a, b):
    assert normalize_domain(a) == normalize_domain(b) == "example.de"


@pytest.mark.parametrize("raw,expected", [
    ("Müller GmbH & Co. KG", "mueller"),
    ("Schreinerei & Insektenschutz Reiser Jettingen", "schreinerei und insektenschutz reiser jettingen"),
    ("Zimmerei Holzbau Martin Schuster", "zimmerei holzbau martin schuster"),
    ("Elektro Maier e.K.", "elektro maier"),
    ("Malerbetrieb Straßer UG (haftungsbeschränkt)", "malerbetrieb strasser"),
    ("Agentur Gaus AG", "agentur gaus"),            # "ag" nur als eigenes Wort entfernt
    ("  SCHMID  bau-GmbH ", "schmid bau"),
])
def test_company_name(raw, expected):
    assert normalize_company_name(raw) == expected


def test_company_name_variants_match():
    assert normalize_company_name("Müller GmbH") == normalize_company_name("MUELLER gmbh.")


@pytest.mark.parametrize("raw,expected", [
    ("Burgauer Weg 14, 89343 Jettingen-Scheppach-Jettingen, Deutschland", "89343|burgauer weg 14"),
    ("Vorderer Höges 2 a, 89343 Jettingen-Scheppach, Deutschland", "89343|vorderer hoeges 2a"),
    ("Hauptstraße 5, 89343 Jettingen-Scheppach", "89343|hauptstr 5"),
    ("Hauptstr. 5, 89343 Jettingen", "89343|hauptstr 5"),
    ("Jettingen-Scheppach", None),
    ("", None),
])
def test_address_key(raw, expected):
    assert address_key(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("08225 / 1234-5", "+49822512345"),
    ("+49 (0) 8225 12345", "+49822512345"),
    ("0049 8225 12345", "+49822512345"),
    ("+43 1 234567", "+431234567"),
    ("12345", None),
    ("", None),
])
def test_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Info@Firma.de", "info@firma.de"),
    ("mailto:info@firma.de?subject=Hallo", "info@firma.de"),
    ("<info@firma.de>.", "info@firma.de"),
    ("info@firma", None),
    ("info@@firma.de", None),
    ("a..b@firma.de", None),
])
def test_email(raw, expected):
    assert normalize_email(raw) == expected
