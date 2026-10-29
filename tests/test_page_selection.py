from modules.content_extractor import Link
from modules.html_store import HtmlStore
from modules.page_selection import categorize, has_link_to, select_subpages

HOME = "https://www.schreinerei.de/"


def L(url, text="", internal=True):
    return Link(url=url, text=text, internal=internal)


def test_one_page_per_category_in_priority_order():
    links = [L(HOME + "referenzen"), L(HOME + "ueber-uns"), L(HOME + "leistungen/kuechen"), L(HOME + "leistungen"),
             L(HOME + "impressum"), L(HOME + "kontakt"), L(HOME + "kontakt/anfahrt")]
    chosen = select_subpages(links, HOME, max_pages=10)
    assert [(p.category, p.url) for p in chosen] == [
        ("kontakt", HOME + "kontakt"), ("impressum", HOME + "impressum"), ("leistungen", HOME + "leistungen"),
        ("ueber_uns", HOME + "ueber-uns"), ("referenzen", HOME + "referenzen")]


def test_limit_respected():
    links = [L(HOME + "kontakt"), L(HOME + "impressum"), L(HOME + "leistungen")]
    assert [p.category for p in select_subpages(links, HOME, max_pages=2)] == ["kontakt", "impressum"]


def test_link_text_counts():
    assert select_subpages([L(HOME + "?page_id=12", "Über uns")], HOME, max_pages=5)[0].category == "ueber_uns"


def test_excluded_links():
    links = [L("https://facebook.com/kontakt", internal=False), L(HOME + "preise.pdf", "Leistungen PDF"),
             L(HOME + "datenschutz"), L(HOME + "wp-admin/kontakt"), L("mailto:info@schreinerei.de", "Kontakt"),
             L(HOME + "#kontakt"), L(HOME)]
    assert select_subpages(links, HOME, max_pages=10) == []


def test_www_and_fragment_variants_are_same_site():
    chosen = select_subpages([L("https://schreinerei.de/kontakt#form")], HOME, max_pages=5)
    assert chosen[0].url == "https://schreinerei.de/kontakt"


def test_already_crawled_and_found_categories_skipped():
    links = [L(HOME + "kontakt"), L(HOME + "impressum")]
    assert select_subpages(links, HOME, max_pages=5, already={HOME + "kontakt"}) [0].category == "impressum"
    assert select_subpages(links, HOME, max_pages=5, found_categories={"kontakt", "impressum"}) == []


def test_privacy_detected_but_not_crawled():
    links = [L(HOME + "datenschutzerklaerung", "Datenschutz")]
    assert categorize(links[0]) is None and has_link_to(links, "datenschutz|privacy")


def test_html_store(tmp_path):
    store = HtmlStore(tmp_path, max_bytes=1000)
    ref = store.save("<html>Grüße</html>")
    assert store.save("<html>Grüße</html>") == ref               # gleicher Inhalt, eine Datei
    assert store.load(ref) == "<html>Grüße</html>"
    assert store.save("x" * 2000) is None                         # zu groß: nicht gespeichert
    import pytest
    with pytest.raises(ValueError):
        store.load("../../etc/passwd")
