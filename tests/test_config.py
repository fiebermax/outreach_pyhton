import pytest
from pydantic import ValidationError

from config import PROJECT_ROOT, MailMode, Settings
from core.errors import MissingCredentialError


def make(**kwargs) -> Settings:
    """Settings ohne echte .env, damit Tests nicht von lokalen Keys abhängen."""
    return Settings(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GOOGLE_PLACES_API_KEY", "DEEPSEEK_API_KEY", "MAIL_MODE", "SEND_ENABLED", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_safe():
    s = make()
    assert s.mail_mode is MailMode.DRAFT
    assert s.send_enabled is False
    assert s.smtp_allowed is False


def test_smtp_mode_alone_does_not_allow_sending():
    assert make(mail_mode="smtp").smtp_allowed is False
    assert make(mail_mode="smtp", send_enabled=True).smtp_allowed is True


def test_missing_key_loads_but_fails_on_use():
    s = make()
    with pytest.raises(MissingCredentialError, match="DEEPSEEK_API_KEY"):
        s.require_secret("deepseek_api_key")


def test_empty_string_counts_as_missing():
    with pytest.raises(MissingCredentialError):
        make(deepseek_api_key="  ").require_secret("deepseek_api_key")


def test_secret_not_in_repr():
    s = make(deepseek_api_key="sk-test-not-real-1234567890")
    assert "sk-test-not-real" not in repr(s)
    assert s.require_secret("deepseek_api_key") == "sk-test-not-real-1234567890"
    assert s.secret_values() == ["sk-test-not-real-1234567890"]


def test_reads_env_variables(monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.setenv("SEND_ENABLED", "true")
    assert make().smtp_allowed is True


def test_env_file_with_inline_comment(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MAIL_MODE=draft            # draft | smtp\nCRAWL_MAX_PAGES=3\n")
    s = Settings(_env_file=env)
    assert s.mail_mode is MailMode.DRAFT
    assert s.crawl_max_pages == 3


def test_relative_paths_resolve_to_project_root():
    assert make().database_path == PROJECT_ROOT / "data" / "outreach.db"


@pytest.mark.parametrize("field,value", [
    ("crawl_timeout_seconds", 0),
    ("min_extraction_quality", 1.5),
    ("crawl_concurrency", 0),
    ("log_level", "LOUD"),
    ("mail_mode", "fax"),
])
def test_invalid_values_rejected(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_env_example_matches_settings():
    """Jede Variable in .env.example muss ein Feld in Settings haben (Tippfehler-Schutz)."""
    names = [
        line.split("=", 1)[0].strip().lower()
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    unknown = [n for n in names if n not in Settings.model_fields]
    assert not unknown, unknown
