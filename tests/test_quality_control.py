from pathlib import Path

import pytest

from models.schemas import CrawlStatus, ExtractionMethod, PageResult
from modules.content_extractor import extract_content
from modules.quality_control import assess_page, assess_site, is_better, score_page

SITES = Path(__file__).parent / "fixtures" / "sites"
URL = "https://beispiel.de/"


def assess_fixture(name: str, rendered: bool = False):
    c = extract_content((SITES / name).read_text(encoding="utf-8", errors="replace"), URL)
    return assess_page(cleaned_text=c.cleaned_text, title=c.title, heading_texts=[h.text for h in c.headings],
                       js_signals=c.js_signals, rendered=rendered)


def text(words: int, distinct: bool = True) -> str:
    if distinct:
        return "\n".join(f"Satz {i} über unsere Arbeit in der Werkstatt Nummer{i}" for i in range(words // 8 + 1))
    return "\n".join("Gleiche Zeile immer wieder hier" for _ in range(words // 5 + 1))


# --- Fixture-Seiten: erwarteter Status (gelabelte Beispiele) ---

@pytest.mark.parametrize("name,status,needs_rendering", [
    ("static.html", CrawlStatus.SUCCESS, False),
    ("cookie-banner.html", CrawlStatus.PARTIAL_SUCCESS, False),   # Inhalt lesbar, aber kurz
    ("js-only.html", CrawlStatus.JS_RENDERING_REQUIRED, True),
    ("empty.html", CrawlStatus.EMPTY_CONTENT, True),             # leer ohne JS-Hinweis: Browser einmal versuchen
    ("captcha.html", CrawlStatus.BLOCKED, False),
    ("not-found.html", CrawlStatus.HTTP_ERROR, False),            # Soft-404
])
def test_fixture_status(name, status, needs_rendering):
    a = assess_fixture(name)
    assert a.status is status, a.reasons
    assert a.needs_rendering is needs_rendering


def test_empty_after_rendering_is_final():
    a = assess_fixture("js-only.html", rendered=True)
    assert a.status is CrawlStatus.EMPTY_CONTENT and not a.needs_rendering
    assert "nach Rendering" in a.failure_reason


def test_unusable_always_has_reason():
    for name in ("js-only.html", "empty.html", "captcha.html", "not-found.html"):
        a = assess_fixture(name)
        assert not a.status.is_usable and a.failure_reason


# --- Einzelregeln ---

def test_long_page_mentioning_404_is_not_error_page():
    a = assess_page(cleaned_text=text(300), title="Schreinerei Beispiel",
                    heading_texts=["Unsere Leistungen", "Fehler 404 vermeiden beim Umzug"])
    assert a.status is CrawlStatus.SUCCESS


def test_captcha_word_in_long_text_is_not_block():
    body = text(300) + "\nWir schützen unser Formular mit einem Captcha."
    assert assess_page(cleaned_text=body, title="Firma", heading_texts=["Kontakt"]).status is CrawlStatus.SUCCESS


@pytest.mark.parametrize("phrase", ["Diese Website ist im Aufbau", "Coming soon", "Diese Domain ist zu verkaufen"])
def test_placeholder_page(phrase):
    a = assess_page(cleaned_text=phrase + "\n" + text(20), title="Firma", heading_texts=[phrase])
    assert a.status is CrawlStatus.EMPTY_CONTENT and "Platzhalter" in a.failure_reason


@pytest.mark.parametrize("words,status", [(48, CrawlStatus.PARTIAL_SUCCESS), (56, CrawlStatus.SUCCESS)])
def test_success_word_boundary(words, status):
    body = " ".join(f"wort{i}" for i in range(words))
    assert assess_page(cleaned_text=body, title="Schreinerei Huber", heading_texts=["A"]).status is status


def test_thin_page_is_partial():
    a = assess_page(cleaned_text=text(30), title="Malerbetrieb Huber", heading_texts=["Willkommen"])
    assert a.status is CrawlStatus.PARTIAL_SUCCESS


def test_repetitive_page_scores_lower():
    distinct = assess_page(cleaned_text=text(200), title="Firma Huber", heading_texts=["A"])
    repeated = assess_page(cleaned_text=text(200, distinct=False), title="Firma Huber", heading_texts=["A"])
    assert repeated.score < distinct.score
    assert any("Wiederholungen" in r for r in repeated.reasons)


def test_moderate_text_with_js_signals_recommends_rendering():
    a = assess_page(cleaned_text=text(60), title="Firma Huber", heading_texts=["A"], js_signals=["leeres App-Element #root"])
    assert a.status.is_usable and a.needs_rendering


def test_min_quality_is_configurable():
    kwargs = dict(cleaned_text=text(60), title="Home", heading_texts=[])
    assert assess_page(**kwargs, min_quality=0.3).status is CrawlStatus.SUCCESS
    assert assess_page(**kwargs, min_quality=0.9).status is CrawlStatus.PARTIAL_SUCCESS


def test_score_components():
    score, parts = score_page(words=150, unique_ratio=1.0, heading_count=2, title="Schreinerei Huber")
    assert score == 1.0 and parts["length"] == 1.0
    assert score_page(150, 1.0, 2, "Home")[1]["title"] == 0.5        # generischer Titel
    assert score_page(0, 0.0, 0, None)[0] == 0.0


def test_is_better():
    empty = assess_fixture("js-only.html")
    good = assess_fixture("static.html")
    partial = assess_fixture("cookie-banner.html")
    assert is_better(good, empty) and not is_better(empty, good)
    assert is_better(good, partial) and not is_better(partial, good)


# --- Website-Ebene ---

def page(url: str, status: CrawlStatus) -> PageResult:
    if status.is_usable:
        return PageResult(url=url, crawl_status=status, cleaned_text="Text", extraction_method=ExtractionMethod.HTML_PARSER)
    return PageResult(url=url, crawl_status=status, failure_reason="Grund")


def test_site_all_good():
    assert assess_site([page(URL, CrawlStatus.SUCCESS), page(URL + "kontakt", CrawlStatus.SUCCESS)]).status is CrawlStatus.SUCCESS


def test_site_subpage_failed_is_partial():
    s = assess_site([page(URL, CrawlStatus.SUCCESS), page(URL + "impressum", CrawlStatus.TIMEOUT)])
    assert s.status is CrawlStatus.PARTIAL_SUCCESS and s.failed_pages == [URL + "impressum (TIMEOUT)"]


def test_site_home_failed_but_subpage_ok():
    s = assess_site([page(URL, CrawlStatus.JS_RENDERING_REQUIRED), page(URL + "kontakt", CrawlStatus.SUCCESS)])
    assert s.status is CrawlStatus.PARTIAL_SUCCESS


def test_site_nothing_usable_keeps_cause():
    """Ursache bleibt erhalten: blockiert ≠ Scraper-Fehler."""
    s = assess_site([page(URL, CrawlStatus.BLOCKED)])
    assert s.status is CrawlStatus.BLOCKED and s.status.is_site_inaccessible
    s = assess_site([page(URL, CrawlStatus.EMPTY_CONTENT)])
    assert s.status is CrawlStatus.EMPTY_CONTENT and not s.status.is_site_inaccessible


def test_site_without_pages():
    assert assess_site([]).status is CrawlStatus.FAILED
