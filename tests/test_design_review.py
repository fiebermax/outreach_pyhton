"""Design-Prüfung mit echtem Headless-Chromium gegen den lokalen Testserver; KI simuliert."""

import asyncio
import json

from modules.browser import BrowserLimits
from modules.design_review import SYSTEM_PROMPT, load_review, rate_signals, review_site
from modules.llm import Usage

LIMITS = BrowserLimits(navigation_timeout_seconds=5, networkidle_timeout_seconds=1, allow_local=True)


class FakeLLM:
    model = "fake-model"

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def complete_json(self, system, user, max_tokens=1500, images=None):
        self.prompts.append((system, user))
        self.images = images
        return self.answer, Usage(100, 50, 0.001)


def review(url, tmp_path, llm=None):
    return asyncio.run(review_site(url, tmp_path, llm, limits=LIMITS))


def test_old_site_is_flagged_without_ai(site_server, tmp_path):
    r = review(site_server + "/design-old.html", tmp_path)
    assert r.error is None and r.verdict == "sehr veraltet"
    flags = " | ".join(r.red_flags)
    for part in ("keine Viewport-Angabe", "Tabellen", "<font>", "<center>", "Schmale Inhaltsspalte",
                 "Copyright-Jahr 2009", "Besucher Nr"):
        assert part in flags, part
    assert (tmp_path / "design" / "127.0.0.1" / "desktop.png").stat().st_size > 1000
    assert (tmp_path / "design" / "127.0.0.1" / "mobile.png").stat().st_size > 1000
    assert load_review(tmp_path, "127.0.0.1").verdict == "sehr veraltet"      # gespeichert und wieder lesbar


def test_modern_site_has_no_red_flags(site_server, tmp_path):
    r = review(site_server + "/design-modern.html", tmp_path)
    assert r.red_flags == [] and r.verdict == "keine Auffälligkeiten"
    assert "Für Handy und Computer angepasst (responsive)" in r.positives


def test_ai_verdict_is_used_and_prompt_is_design_only(site_server, tmp_path):
    llm = FakeLLM({"urteil": "veraltet", "note": 3, "geschaetztes_alter": "Stil ca. 2005–2010",
                   "gut": ["klare Telefonnummer"], "schlecht": ["Tabellenlayout", "nicht fürs Handy"],
                   "aufhaenger": "Ihre Seite ist am Handy kaum zu bedienen."})
    r = review(site_server + "/design-old.html", tmp_path, llm)
    assert (r.verdict, r.grade, r.ai_model) == ("veraltet", 3, "fake-model")
    assert r.bad == ["Tabellenlayout", "nicht fürs Handy"] and r.hook.startswith("Ihre Seite")
    system, user = llm.prompts[0]
    assert "Tippfehler" in system and "NICHT dein Thema" in system
    assert len(llm.images) == 2 and llm.images[0][:2] == b"\xff\xd8"          # Screenshots als JPEG an die KI
    assert "layout_tables" in user and "Schreinerei Huber" in user


def test_nonsense_ai_answer_falls_back_to_rules(site_server, tmp_path):
    r = review(site_server + "/design-old.html", tmp_path, FakeLLM({"urteil": "super toll", "note": "x"}))
    assert r.verdict == "sehr veraltet" and r.bad == r.red_flags


def test_unreachable_site_reports_error(site_server, tmp_path):
    r = review(site_server + "/forbidden", tmp_path)
    assert r.error and "Seite nicht ladbar" in r.error and r.desktop_png is None
    assert json.loads((tmp_path / "design" / "127.0.0.1" / "review.json").read_text())["error"]


def test_rules_only_count_measured_signals():
    flags, plus, points = rate_signals({"viewport_meta": "width=device-width", "media_queries": 3,
                                        "flex_grid_elements": 40, "hero_image": True, "webfonts": ["Inter"],
                                        "copyright_year": 2025}, {"horizontal_scroll_px": 0}, current_year=2026)
    assert flags == [] and points == 0 and len(plus) == 4
