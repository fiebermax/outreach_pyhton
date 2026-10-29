import pytest
from pydantic import ValidationError

from core.niche import NicheProfile, classify, load_niche
from models.schemas import FindingCategory


@pytest.fixture(scope="module")
def niche():
    return load_niche()


# Echte Places-Ergebnisse "Handwerker in Jettingen-Scheppach" (29.10.2026): Name, Google-Typen, erwartete Branche.
REAL = [
    ("WEST-Dienstleistungen", ["service"], None),
    ("Martin Schaffer", ["general_contractor", "service"], "Bau/Sanierung"),
    ("kbs GmbH Komplette Bausanierung", ["general_contractor", "service"], "Bau/Sanierung"),
    ("Josef Gay", ["manufacturer", "service"], None),                      # Zimmerei, aber Name verrät es nicht
    ("Malerbetrieb Kallup", ["painter", "service"], "Maler/Stuckateur"),
    ("FARBTEC | Maler in Jettingen-Scheppach", ["painter", "service"], "Maler/Stuckateur"),
    ("Ewald und Sohn GmbH", ["plumber", "general_contractor", "home_goods_store", "store"], "Sanitär/Heizung"),
    ("Farbeffekt - Kompetent | Zuverlässig", [], "Maler/Stuckateur"),
    ("Schreinerei & Insektenschutz Reiser Jettingen", ["manufacturer", "service"], "Schreiner/Tischler"),
    ("Primotec-e.K", ["plumber", "general_contractor"], "Sanitär/Heizung"),
    ("Franz Wegenmayer", ["manufacturer", "service"], None),
    ("KT Metall - Inh. Tino Kastl", ["general_contractor", "service"], "Bau/Sanierung"),
    ("H.B.S Verputzer/Maler", ["painter", "service"], "Maler/Stuckateur"),
    ("Gebäudetechnik Gilg", ["electrician", "service"], "Elektro"),
    ("Mayer Elektrotechnik", ["electrician", "service"], "Elektro"),
    ("Zimmerei S. Hins | Holzbau - Spenglerei", ["manufacturer", "service"], "Zimmerei/Holzbau"),
    ("Konrad Bigelmayr Malerei Burgau", ["painter", "service"], "Maler/Stuckateur"),
    ("Zaunbau Mahler Verkauf&Montage", ["general_contractor", "service"], "Bau/Sanierung"),
    ("Malermeisterbetrieb Kiran", ["painter", "service"], "Maler/Stuckateur"),
]


@pytest.mark.parametrize("name,types,expected", REAL)
def test_real_companies(niche, name, types, expected):
    match = classify(niche, name, types)
    assert match.branche == expected, match.reason
    assert (match.fit > 0) == (expected is not None)
    assert match.reason


def test_name_beats_google_type(niche):
    """N&H Bau ist bei Google 'painter', der Name sagt Bau – der Name ist das stärkere Signal."""
    assert classify(niche, "N&H Bau", ["painter", "general_contractor"]).reason.startswith("Name")


def test_priority_breaks_ties(niche):
    # Zimmerei (1.0) und Spenglerei (0.8) im Namen → Zimmerei
    assert classify(niche, "Zimmerei und Spenglerei Huber").branche == "Zimmerei/Holzbau"


def test_town_name_not_misread(niche):
    assert classify(niche, "Elektro Müller Bad Wörishofen").branche == "Elektro"
    assert classify(niche, "Café Bad Wörishofen").branche is None


@pytest.mark.parametrize("name,types", [("Hornbach Baumarkt Ulm", []), ("BAUHAUS Augsburg", []),
                                        ("Holz Fachmarkt", ["home_improvement_store"])])
def test_exclusions(niche, name, types):
    match = classify(niche, name, types)
    assert match.excluded and match.fit == 0 and "ausgeschlossen" in match.reason


def test_category_label_used(niche):
    assert classify(niche, "Huber GmbH", category_label="Schreinerei").branche == "Schreiner/Tischler"


def test_search_queries_core_first(niche):
    queries = niche.search_queries()
    assert queries[0] == ("Schreiner/Tischler", "Schreinerei in Jettingen-Scheppach")
    assert ("Bau/Sanierung", "Bauunternehmen in Krumbach") in queries
    assert queries[-1][0] == "Bau/Sanierung"                              # niedrigste Priorität zuletzt
    assert len(queries) == len({q for _, q in queries})


def test_offer_fit(niche):
    assert niche.offer_fit(FindingCategory.MOBILE) == 1.0
    assert niche.offer_fit(FindingCategory.LEGAL) == 0.4


def test_invalid_profile_rejected():
    with pytest.raises(ValidationError):
        NicheProfile.model_validate({"region": {"ort": "X"}, "branche": []})
    with pytest.raises(ValidationError):
        NicheProfile.model_validate({"region": {"ort": "X"}, "branche": [{"name": "A", "suchbegriffe": ["a"]}],
                                     "angebot": {"ERFUNDEN": 1.0}})
