import json
import logging

import pytest

from core.logging_setup import MASK, SecretMasker, setup_logging

# Zur Laufzeit zusammengesetzt, damit Secret-Scanner den Testwert nicht als echten Key melden.
FAKE_DEEPSEEK = "sk-" + "x" * 32
FAKE_GOOGLE = "AIza" + "B" * 35


@pytest.mark.parametrize("text", [
    f"Anfrage mit Key {FAKE_DEEPSEEK} fehlgeschlagen",
    f"https://places.googleapis.com/v1?key={FAKE_GOOGLE}",
    "SMTP login password=geheim123 abgelehnt",
    "Header Authorization: Bearer abc.def.ghi",
])
def test_patterns_masked(text):
    masked = SecretMasker()(text)
    for secret in (FAKE_DEEPSEEK, FAKE_GOOGLE, "geheim123", "abc.def.ghi"):
        assert secret not in masked
    assert MASK in masked


def test_configured_secret_masked_even_without_pattern():
    assert SecretMasker(["meinPasswortXYZ"])("login meinPasswortXYZ") == f"login {MASK}"


def test_handlers_mask_message_args_and_tracebacks(tmp_path, capsys):
    log_file = tmp_path / "out.jsonl"
    setup_logging("DEBUG", secrets=["supersecret99"], log_file=log_file)
    log = logging.getLogger("test")
    log.info("Key %s", "supersecret99", extra={"lead_id": 42, "step": "crawl"})
    try:
        raise RuntimeError("kaputt mit supersecret99")
    except RuntimeError:
        log.exception("Fehler")
    for handler in logging.getLogger().handlers:
        handler.flush()

    console = capsys.readouterr().err
    content = log_file.read_text()
    assert "supersecret99" not in console
    assert "supersecret99" not in content
    assert "lead_id=42" in console

    entries = [json.loads(line) for line in content.splitlines()]
    assert entries[0]["lead_id"] == "42" and entries[0]["step"] == "crawl"
    assert "RuntimeError" in entries[1]["exc"]


def test_setup_twice_does_not_duplicate_handlers(tmp_path):
    setup_logging("INFO", log_file=tmp_path / "a.jsonl")
    setup_logging("INFO", log_file=tmp_path / "a.jsonl")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_outreach", False)]
    assert len(ours) == 2
