from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import (
    RuntimeSettings,
    ScheduleSettings,
    SourceSettings,
    load_project_settings,
)


def test_default_schedule_and_source_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_project_settings()

    assert settings.schedule.timezone == "Asia/Tehran"
    assert settings.schedule.times == [time(hour) for hour in (8, 11, 14, 17, 20)]
    assert [source.slug for source in settings.sources] == ["bbc", "guardian", "aljazeera", "dw"]
    guardian = settings.sources[1]
    assert str(guardian.api_search_url) == "https://content.guardianapis.com/search"
    enabled = {source.slug for source in settings.sources if source.enabled}
    assert enabled == {"bbc", "aljazeera", "dw"}
    assert not guardian.allow_classification


def test_schedule_override_preserves_other_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "custom.toml"
    config_path.write_text('[schedule]\ntimes = ["21:15", "08:30"]\n', encoding="utf-8")

    settings = load_project_settings(config_path)

    assert settings.schedule.timezone == "Asia/Tehran"
    assert settings.schedule.times == [time(8, 30), time(21, 15)]
    assert len(settings.sources) == 4


def test_source_settings_allow_api_without_an_rss_feed() -> None:
    source = SourceSettings.model_validate(
        {
            "slug": "example",
            "name": "Example",
            "api_search_url": "https://example.test/search",
        }
    )

    assert source.rss_url is None
    with pytest.raises(ValidationError, match="configure an RSS or API endpoint"):
        SourceSettings(slug="example", name="Example")


@pytest.mark.parametrize(
    ("timezone", "times", "error"),
    [
        ("Asia/Tehrann", ["08:00"], "IANA timezone"),
        ("Asia/Tehran", ["08:00", "08:00"], "must be unique"),
        ("Asia/Tehran", ["08:00:01"], "minute precision"),
        ("Asia/Tehran", ["08:00+03:30"], "must be local"),
    ],
)
def test_reject_invalid_schedule(timezone: str, times: list[str], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        ScheduleSettings.model_validate({"timezone": timezone, "times": times})


def test_environment_overrides_dotenv_and_masks_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "NEWS_LOG_LEVEL=WARNING\nTELEGRAM_BOT_TOKEN=test-file-token\n", encoding="utf-8"
    )
    monkeypatch.setenv("NEWS_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    settings = RuntimeSettings()

    assert settings.log_level == "DEBUG"
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "test-file-token"
    assert "test-file-token" not in repr(settings)


def test_invalid_database_url_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_DATABASE_URL", "not-a-url-test-password")

    with pytest.raises(ValidationError, match="valid SQLAlchemy") as error:
        RuntimeSettings()

    assert "not-a-url-test-password" not in str(error.value)
