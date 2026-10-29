"""Zielnische laden und Unternehmen zuordnen (`niche.toml`).

Die Zuordnung ist deterministisch und begründet: Jede Entscheidung nennt das
Schlüsselwort oder die Kategorie, die sie ausgelöst hat.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from config import PROJECT_ROOT
from core.normalize import normalize_company_name
from models.schemas import FindingCategory


class Region(BaseModel):
    ort: str = Field(min_length=2)
    weitere_orte: list[str] = Field(default_factory=list)
    radius_km: float = Field(20, gt=0, le=200)

    @property
    def orte(self) -> list[str]:
        return [self.ort, *[o for o in self.weitere_orte if o != self.ort]]


class Suche(BaseModel):
    max_treffer_pro_suche: int = Field(20, ge=1, le=20)
    max_seiten_pro_suche: int = Field(1, ge=1, le=3)


class Branche(BaseModel):
    name: str
    suchbegriffe: list[str] = Field(min_length=1)
    schluesselwoerter: list[str] = Field(default_factory=list)
    places_typen: list[str] = Field(default_factory=list)
    prioritaet: float = Field(1.0, ge=0, le=1)

    @field_validator("schluesselwoerter")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.lower() for v in value]


class Ausschluss(BaseModel):
    namen_enthalten: list[str] = Field(default_factory=list)
    places_typen: list[str] = Field(default_factory=list)


class NicheProfile(BaseModel):
    region: Region
    suche: Suche = Suche()
    branche: list[Branche] = Field(min_length=1)
    ausschluss: Ausschluss = Ausschluss()
    angebot: dict[FindingCategory, float] = Field(default_factory=dict)

    def search_queries(self) -> list[tuple[str, str]]:
        """(Branche, Suchtext) für jede Kombination aus Ort und Suchbegriff, Kernbranchen zuerst."""
        queries = []
        for branche in sorted(self.branche, key=lambda b: -b.prioritaet):
            for ort in self.region.orte:
                for begriff in branche.suchbegriffe:
                    queries.append((branche.name, f"{begriff} in {ort}"))
        return queries

    def offer_fit(self, category: FindingCategory) -> float:
        """Wie gut passt ein Befund dieser Kategorie zum eigenen Angebot? Unbekannt = 0,5."""
        return self.angebot.get(category, 0.5)


@dataclass(frozen=True)
class NicheMatch:
    branche: str | None
    fit: float                 # 0 = außerhalb, sonst Priorität der Branche
    reason: str
    excluded: bool = False


def _padded(text: str) -> str:
    # Leerzeichen an den Rändern, damit Schlüsselwörter wie "bau " auch am Ende greifen.
    return f" {text.lower()} "


def classify(profile: NicheProfile, name: str, places_types: list[str] | None = None,
             category_label: str | None = None) -> NicheMatch:
    """Ordnet ein Unternehmen einer Branche der Nische zu.

    Reihenfolge: Ausschluss → Schlüsselwort im Namen → Schlüsselwort in der Kategorie → Places-Typ.
    Bei mehreren Treffern gewinnt die Branche mit der höchsten Priorität.
    """
    places_types = places_types or []
    lowered = _padded(name)
    folded = _padded(normalize_company_name(name))
    for word in profile.ausschluss.namen_enthalten:
        if word.lower() in lowered or word.lower() in folded:
            return NicheMatch(None, 0.0, f"ausgeschlossen: Name enthält „{word.strip()}“", excluded=True)
    for t in places_types:
        if t in profile.ausschluss.places_typen:
            return NicheMatch(None, 0.0, f"ausgeschlossen: Kategorie {t}", excluded=True)

    label = _padded(category_label or "")
    candidates: list[tuple[float, int, Branche, str]] = []
    for branche in profile.branche:
        for word in branche.schluesselwoerter:
            if word in lowered or word in folded:
                candidates.append((branche.prioritaet, 3, branche, f"Name enthält „{word.strip()}“"))
                break
            if word in label:
                candidates.append((branche.prioritaet, 2, branche, f"Kategorie „{category_label}“ enthält „{word.strip()}“"))
                break
        else:
            hit = next((t for t in places_types if t in branche.places_typen), None)
            if hit:
                candidates.append((branche.prioritaet, 1, branche, f"Google-Kategorie {hit}"))
    if not candidates:
        return NicheMatch(None, 0.0, "keine Zielbranche erkannt")
    # Stärkeres Signal (Name > Kategorie > Typ) vor Priorität, damit "Malerbetrieb" nicht als Bau gilt.
    _, _, branche, reason = max(candidates, key=lambda c: (c[1], c[0]))
    return NicheMatch(branche.name, branche.prioritaet, reason)


def load_niche(path: Path | str | None = None) -> NicheProfile:
    path = Path(path) if path else PROJECT_ROOT / "niche.toml"
    with open(path, "rb") as fh:
        return NicheProfile.model_validate(tomllib.load(fh))
