"""Rauchtest: Paketstruktur ist importierbar und Kernabhängigkeiten sind installiert."""

import importlib

import pytest

PROJECT_MODULES = [
    "modules.lead_finder",
    "modules.scraper",
    "modules.content_extractor",
    "modules.quality_control",
    "modules.analyzer",
    "modules.website_checks",
    "modules.lead_scoring",
    "modules.mailer",
    "modules.database",
    "services.crawl_service",
    "services.analysis_service",
    "services.outreach_service",
    "models.schemas",
]

DEPENDENCIES = ["pydantic", "pydantic_settings", "httpx", "bs4", "lxml", "trafilatura", "playwright"]


@pytest.mark.parametrize("name", PROJECT_MODULES + DEPENDENCIES)
def test_importable(name):
    importlib.import_module(name)


def test_env_file_is_ignored():
    from pathlib import Path

    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text()
    assert ".env" in gitignore.splitlines()
