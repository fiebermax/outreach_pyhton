"""Modul A: Unternehmen finden (Google Places API, Text Search "New").

- Suchanfragen kommen aus `niche.toml` (Branche × Ort × Suchbegriff).
- Jede API-Antwort wird zwischengespeichert (Standard 30 Tage); ein zweiter Lauf kostet nichts.
- Kostenbremse: Höchstzahl Abfragen pro Lauf und pro Kalendermonat (in der DB gezählt).
- Umkreis: Treffer außerhalb `radius_km` um den Hauptort werden verworfen (Luftlinie).
- Dauerhaft geschlossene Betriebe werden verworfen.
- Keine erfundenen Daten: Felder, die Google nicht liefert, bleiben leer.

Nutzungsbedingungen Google Maps Platform: `place_id` darf dauerhaft gespeichert werden;
andere Inhalte nur begrenzt zwischengespeichert – deshalb Cache-Ablauf und erneutes Abfragen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field

import httpx

from core.errors import (
    InvalidResponseError, MissingCredentialError, OutreachError, PermanentError, error_for_http_status,
)
from core.niche import NicheProfile, classify
from core.retry import retry_call
from models.schemas import Lead
from modules.database import Database

log = logging.getLogger(__name__)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PROVIDER = "google_places"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.websiteUri", "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.types",
    "places.primaryType", "places.primaryTypeDisplayName", "places.businessStatus", "places.location",
    "nextPageToken",
])


class BudgetExhausted(PermanentError):
    """Abfragelimit (Lauf oder Monat) erreicht."""


@dataclass(frozen=True)
class PlacesConfig:
    max_requests_per_run: int = 30
    monthly_limit: int = 800
    cache_days: float = 30.0
    timeout_seconds: float = 20.0
    retries: int = 2


@dataclass
class FinderReport:
    queries: int = 0
    api_requests: int = 0
    cache_hits: int = 0
    results: int = 0
    created: int = 0
    updated: int = 0
    skipped: Counter = field(default_factory=Counter)
    by_branch: Counter = field(default_factory=Counter)
    website_kinds: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _component(place: dict, kind: str) -> str | None:
    for comp in place.get("addressComponents", []):
        if kind in comp.get("types", []):
            return comp.get("longText") or comp.get("shortText")
    return None


class GooglePlacesClient:
    def __init__(self, api_key: str | None, db: Database, config: PlacesConfig = PlacesConfig(),
                 http: httpx.Client | None = None):
        if not api_key:
            raise MissingCredentialError("GOOGLE_PLACES_API_KEY ist nicht gesetzt.")
        self._key = api_key
        self.db = db
        self.config = config
        self.http = http or httpx.Client(timeout=config.timeout_seconds)
        self.requests_this_run = 0
        self.cache_hits = 0

    def _check_budget(self) -> None:
        if self.requests_this_run >= self.config.max_requests_per_run:
            raise BudgetExhausted(f"Limit pro Lauf erreicht ({self.config.max_requests_per_run} Abfragen)")
        used = self.db.api_calls_this_month(PROVIDER)
        if used >= self.config.monthly_limit:
            raise BudgetExhausted(f"Monatslimit erreicht ({used}/{self.config.monthly_limit} Abfragen)")

    def search_text(self, query: str, page_size: int = 20, page_token: str | None = None,
                    near: tuple[float, float, float] | None = None) -> dict:
        """`near` = (lat, lng, Radius km): Treffer in diesem Umkreis bevorzugen."""
        body = {"textQuery": query, "pageSize": page_size, "languageCode": "de", "regionCode": "DE"}
        if near:
            body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                                "radius": min(50_000.0, near[2] * 1000)}}
        if page_token:
            body["pageToken"] = page_token
        key = "places:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cached = self.db.cache_get(key, self.config.cache_days)
        if cached is not None:
            self.cache_hits += 1
            return json.loads(cached)

        self._check_budget()

        def call() -> dict:
            response = self.http.post(PLACES_URL, json=body,
                                      headers={"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": FIELD_MASK})
            error = error_for_http_status(response.status_code, "places:searchText")
            if error is not None:
                detail = response.text[:300]
                if response.status_code == 403:
                    raise MissingCredentialError(f"Places API verweigert (Key/Abrechnung/API-Freischaltung prüfen): {detail}")
                error.args = (f"{error} – {detail}",)
                raise error
            try:
                return response.json()
            except ValueError as exc:
                raise InvalidResponseError(f"Places-Antwort ist kein JSON: {response.text[:200]}") from exc

        self.requests_this_run += 1
        try:
            data = retry_call(call, attempts=self.config.retries + 1, base_delay=2.0, label="places")
        except OutreachError:
            self.db.log_api_call(PROVIDER, "searchText", success=False)
            raise
        self.db.log_api_call(PROVIDER, "searchText", success=True)
        self.db.cache_put(key, PROVIDER, json.dumps(data))
        return data

    def locate(self, place_name: str, near: tuple[float, float] | None = None) -> tuple[float, float] | None:
        """Koordinaten eines Ortes (für den Umkreisfilter), bevorzugt in der Nähe von `near`. Gecacht."""
        places = self.search_text(place_name, page_size=1, near=(*near, 50) if near else None).get("places", [])
        loc = places[0].get("location") if places else None
        return (loc["latitude"], loc["longitude"]) if loc else None


def place_to_lead(place: dict, niche: NicheProfile, center: tuple[float, float] | None) -> Lead:
    name = place.get("displayName", {}).get("text") or ""
    match = classify(niche, name, place.get("types", []), place.get("primaryTypeDisplayName", {}).get("text"))
    distance = None
    loc = place.get("location")
    if center and loc:
        distance = round(haversine_km(center[0], center[1], loc["latitude"], loc["longitude"]), 1)
    return Lead(
        name=name, category=place.get("primaryTypeDisplayName", {}).get("text"),
        address=place.get("formattedAddress"), city=_component(place, "locality"),
        postal_code=_component(place, "postal_code"), website=place.get("websiteUri"),
        phone=place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
        source=PROVIDER, source_ref=place.get("id"), niche_branch=match.branche, niche_fit=match.fit,
        niche_reason=match.reason, distance_km=distance, business_status=place.get("businessStatus"),
    )


def find_leads(db: Database, client: GooglePlacesClient, niche: NicheProfile, *,
               max_queries: int | None = None, include_out_of_niche: bool = False) -> FinderReport:
    """Führt die Nischen-Suchanfragen aus und speichert passende Unternehmen. Fehler stoppen nur die Anfrage."""
    report = FinderReport()
    try:
        home = (niche.region.lat, niche.region.lng) if niche.region.lat is not None else None
        center = client.locate(niche.region.ort, near=home)
    except OutreachError as exc:
        report.errors.append(f"Ort {niche.region.ort} nicht auffindbar: {exc}")
        report.stopped_reason = str(exc)
        return report
    if center is None:
        report.errors.append(f"Ort {niche.region.ort} nicht gefunden – Umkreisfilter deaktiviert")
    elif home and haversine_km(*home, *center) > 100:
        report.errors.append(f"Achtung: „{niche.region.ort}“ liegt {haversine_km(*home, *center):.0f} km von zu Hause "
                             "entfernt – falscher Ort? Genauer angeben, z. B. mit PLZ.")

    queries = niche.search_queries()[: max_queries or None]
    for branche, query in queries:
        token = None
        for _page in range(niche.suche.max_seiten_pro_suche):
            try:
                data = client.search_text(query, niche.suche.max_treffer_pro_suche, token,
                                          near=(*center, niche.region.radius_km) if center else None)
            except BudgetExhausted as exc:
                report.stopped_reason = str(exc)
                break
            except OutreachError as exc:
                report.errors.append(f"{query}: {type(exc).__name__}: {str(exc)[:150]}")
                break
            report.queries += 1
            for place in data.get("places", []):
                report.results += 1
                if place.get("businessStatus") == "CLOSED_PERMANENTLY":
                    report.skipped["dauerhaft geschlossen"] += 1
                    continue
                try:
                    lead = place_to_lead(place, niche, center)
                except ValueError as exc:            # z. B. ungültige Website-URL bei Google
                    report.errors.append(f"{place.get('id')}: Daten ungültig: {str(exc)[:120]}")
                    continue
                if lead.distance_km is not None and lead.distance_km > niche.region.radius_km:
                    report.skipped["außerhalb Umkreis"] += 1
                    continue
                if lead.niche_fit == 0 and not include_out_of_niche:
                    report.skipped["außerhalb Nische" if "ausgeschlossen" not in (lead.niche_reason or "")
                                   else "ausgeschlossen"] += 1
                    continue
                result = db.upsert_company(lead)
                report.created += result.created
                report.updated += not result.created
                report.by_branch[lead.niche_branch or "–"] += 1
                report.website_kinds[db.get_company(result.company_id)["website_kind"]] += 1
            token = data.get("nextPageToken")
            if not token:
                break
        if report.stopped_reason:
            break
    report.api_requests, report.cache_hits = client.requests_this_run, client.cache_hits
    return report


def export_leads(db: Database) -> list[dict]:
    """Alle Unternehmen als JSON-fähige Liste (für Exporte)."""
    return [dict(row) for row in db.list_companies()]
