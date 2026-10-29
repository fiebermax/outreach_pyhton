"""Lead Finder mit simuliertem Google-Places-Server (keine echten API-Kosten)."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from core.errors import MissingCredentialError
from core.niche import NicheProfile
from modules.database import Database
from modules.lead_finder import (
    BudgetExhausted, GooglePlacesClient, PlacesConfig, export_leads, find_leads, haversine_km,
)

CENTER = {"latitude": 48.4085, "longitude": 10.4452}       # Jettingen-Scheppach (echte Koordinaten)


def place(pid, name, website=None, types=(), lat=48.41, lng=10.44, status="OPERATIONAL", label=None, phone=None):
    p = {"id": pid, "displayName": {"text": name}, "types": list(types), "businessStatus": status,
         "formattedAddress": "Hauptstraße 1, 89343 Jettingen-Scheppach, Deutschland",
         "addressComponents": [{"longText": "89343", "types": ["postal_code"]},
                               {"longText": "Jettingen-Scheppach", "types": ["locality", "political"]}],
         "location": {"latitude": lat, "longitude": lng}}
    if website:
        p["websiteUri"] = website
    if label:
        p["primaryTypeDisplayName"] = {"text": label}
    if phone:
        p["nationalPhoneNumber"] = phone
    return p


RESULTS = [
    place("p1", "Schreinerei Huber", "https://www.schreinerei-huber.de/", ["manufacturer"], phone="08225 12345"),
    place("p2", "Malerbetrieb Kraus", "https://www.firmen.tv/kraus_x1", ["painter"]),           # Verzeichnis
    place("p3", "Elektro Maier", "https://facebook.com/elektromaier", ["electrician"]),         # Social
    place("p4", "Zimmerei Ohne Web", None, ["manufacturer"]),
    place("p5", "Schreinerei Alt", "https://alt.de/", status="CLOSED_PERMANENTLY"),
    place("p6", "Schreinerei Weitweg", "https://weitweg.de/", lat=48.14, lng=11.58),             # München
    place("p7", "Hornbach Baumarkt", "https://hornbach.de/", ["home_improvement_store"]),
    place("p8", "Café Sonne", "https://cafe-sonne.de/", ["cafe"]),
]


class FakePlaces:
    def __init__(self, pages=None, status=200):
        self.calls = []
        self.pages = pages or {None: {"places": RESULTS}}
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        assert "places.websiteUri" in request.headers["X-Goog-FieldMask"]
        if self.status != 200:
            return httpx.Response(self.status, text="error")
        if body["textQuery"] == "Jettingen-Scheppach":
            return httpx.Response(200, json={"places": [{"id": "town", "location": CENTER}]})
        return httpx.Response(200, json=self.pages.get(body.get("pageToken"), {"places": []}))


def niche(**suche) -> NicheProfile:
    return NicheProfile.model_validate({
        "region": {"ort": "Jettingen-Scheppach", "radius_km": 20},
        "suche": suche or {},
        "branche": [{"name": "Schreiner/Tischler", "suchbegriffe": ["Schreinerei"], "schluesselwoerter": ["schreiner"]},
                    {"name": "Maler", "suchbegriffe": ["Maler"], "schluesselwoerter": ["maler"]},
                    {"name": "Elektro", "suchbegriffe": ["Elektriker"], "schluesselwoerter": ["elektro"]},
                    {"name": "Zimmerei", "suchbegriffe": ["Zimmerei"], "schluesselwoerter": ["zimmerei"]}],
        "ausschluss": {"namen_enthalten": ["hornbach"], "places_typen": ["home_improvement_store"]},
    })


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


def client_for(db, fake, **cfg):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return GooglePlacesClient("test-key", db, PlacesConfig(retries=0, **cfg), http=http)


def test_finds_and_filters(db):
    fake = FakePlaces()
    report = find_leads(db, client_for(db, fake), niche(), max_queries=1)
    names = {r["name"]: r for r in db.list_companies()}
    assert set(names) == {"Schreinerei Huber", "Malerbetrieb Kraus", "Elektro Maier", "Zimmerei Ohne Web"}
    assert report.skipped == {"dauerhaft geschlossen": 1, "außerhalb Umkreis": 1, "ausgeschlossen": 1,
                              "außerhalb Nische": 1}
    assert report.created == 4 and report.results == 8


def test_website_kinds_and_niche_saved(db):
    find_leads(db, client_for(db, FakePlaces()), niche(), max_queries=1)
    rows = {r["name"]: r for r in db.list_companies()}
    assert rows["Schreinerei Huber"]["website_kind"] == "own"
    assert rows["Malerbetrieb Kraus"]["website_kind"] == "directory"
    assert rows["Elektro Maier"]["website_kind"] == "directory"
    assert rows["Zimmerei Ohne Web"]["website_kind"] == "none"
    huber = rows["Schreinerei Huber"]
    assert huber["niche_branch"] == "Schreiner/Tischler" and "schreiner" in huber["niche_reason"]
    assert huber["postal_code"] == "89343" and huber["city"] == "Jettingen-Scheppach"
    assert huber["phone"] == "+49822512345" and huber["source_ref"] == "p1"
    assert 0 <= huber["distance_km"] < 2


def test_second_run_uses_cache_and_creates_no_duplicates(db):
    fake = FakePlaces()
    find_leads(db, client_for(db, fake), niche(), max_queries=1)
    calls_first = len(fake.calls)
    report = find_leads(db, client_for(db, fake), niche(), max_queries=1)
    assert len(fake.calls) == calls_first          # alles aus dem Cache
    assert report.api_requests == 0 and report.cache_hits == 2
    assert report.created == 0 and report.updated == 4
    assert len(db.list_companies()) == 4


def test_run_budget_stops_cleanly(db):
    report = find_leads(db, client_for(db, FakePlaces(), max_requests_per_run=2), niche(), max_queries=4)
    assert "Limit pro Lauf" in report.stopped_reason
    assert report.api_requests == 2                # Ort + 1 Suche


def test_monthly_budget_counts_in_db(db):
    for _ in range(5):
        db.log_api_call("google_places", "searchText", True)
    client = client_for(db, FakePlaces(), monthly_limit=5)
    with pytest.raises(BudgetExhausted, match="Monatslimit"):
        client.search_text("Schreinerei in Burgau")
    assert db.api_calls_this_month("google_places") == 5


def test_pagination_is_controlled(db):
    pages = {None: {"places": RESULTS[:1], "nextPageToken": "t2"}, "t2": {"places": [RESULTS[3]]}}
    fake = FakePlaces(pages)
    find_leads(db, client_for(db, fake), niche(max_seiten_pro_suche=1), max_queries=1)
    assert not any(c.get("pageToken") for c in fake.calls)
    fake2 = FakePlaces(pages)
    find_leads(db, client_for(db, fake2), niche(max_seiten_pro_suche=2), max_queries=1)
    assert any(c.get("pageToken") == "t2" for c in fake2.calls)


def test_api_denied_is_reported_not_crashing(db):
    report = find_leads(db, client_for(db, FakePlaces(status=403)), niche(), max_queries=2)
    assert report.stopped_reason and "verweigert" in report.errors[0]
    assert db.list_companies() == []


def test_missing_key():
    with pytest.raises(MissingCredentialError):
        GooglePlacesClient(None, None)


def test_no_invented_fields(db):
    """Google liefert keine E-Mail: das Feld bleibt leer statt geraten."""
    find_leads(db, client_for(db, FakePlaces()), niche(), max_queries=1)
    assert all(r["email"] is None for r in db.list_companies())


def test_export(db):
    find_leads(db, client_for(db, FakePlaces()), niche(), max_queries=1)
    exported = export_leads(db)
    assert len(exported) == 4 and json.dumps(exported)


def test_haversine():
    assert 50 < haversine_km(48.4085, 10.4452, 48.1374, 11.5755) < 95     # Jettingen → München ≈ 88 km
