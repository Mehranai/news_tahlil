"""Load nonsecret project configuration separately from runtime environment values."""

import tomllib
from datetime import time
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    timezone: str
    times: Annotated[list[time], Field(min_length=1)]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("provide an IANA timezone such as Asia/Tehran") from None
        return value

    @field_validator("times")
    @classmethod
    def validate_times(cls, values: list[time]) -> list[time]:
        if any(value.tzinfo or value.second or value.microsecond for value in values):
            raise ValueError("times must be local and have minute precision")
        if len(set(values)) != len(values):
            raise ValueError("scheduled times must be unique")
        return sorted(values)


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    slug: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=50)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    rss_url: HttpUrl | None = None
    api_search_url: HttpUrl | None = None
    enabled: bool = True
    fetch_full_text: bool = False
    allow_classification: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self) -> "SourceSettings":
        if self.rss_url is None and self.api_search_url is None:
            raise ValueError("configure an RSS or API endpoint")
        return self


class CollectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 15
    attempts: Annotated[int, Field(ge=1, le=5)] = 3
    min_interval_seconds: Annotated[float, Field(ge=0, le=60)] = 1


class ClassificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, str_strip_whitespace=True)

    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=80)]
    protocol: Literal["openai_compatible", "ollama"] = "openai_compatible"
    base_url: HttpUrl
    model: Annotated[str, Field(min_length=1, max_length=160)]
    model_version: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    prompt_path: Path | None = None
    max_body_chars: Annotated[int, Field(ge=1000, le=120_000)] = 12_000
    max_output_tokens: Annotated[int, Field(ge=256, le=12_000)] = 1600
    timeout_seconds: Annotated[float, Field(gt=0, le=180)] = 60

    @field_validator("base_url")
    @classmethod
    def validate_api_base(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.query is not None or value.fragment is not None:
            raise ValueError("configure a base API URL without credentials, query, or fragment")
        return value


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    send_uncertain: bool = False
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 15


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schedule: ScheduleSettings
    sources: list[SourceSettings]
    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    classification: ClassificationSettings | None = None
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: list[SourceSettings]) -> list[SourceSettings]:
        if len({source.slug for source in values}) != len(values):
            raise ValueError("source slugs must be unique")
        return values


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEWS_",
        env_file=".env",
        env_file_encoding="utf-8-sig",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr = SecretStr("sqlite:///data/iran_news.db")
    log_level: LogLevel = "INFO"
    guardian_api_key: SecretStr | None = Field(None, validation_alias="GUARDIAN_API_KEY")
    llm_api_key: SecretStr | None = Field(None, validation_alias="LLM_API_KEY")
    telegram_bot_token: SecretStr | None = Field(None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: SecretStr | None = Field(None, validation_alias="TELEGRAM_CHAT_ID")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("provide a valid SQLAlchemy database URL") from None
        if url.get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("configure a SQLite or PostgreSQL database URL")
        return value


def load_project_settings(config_path: Path | None = None) -> ProjectSettings:
    """Read bundled defaults and replace any sections provided in a local TOML file."""
    with files("app.config").joinpath("defaults.toml").open("rb") as defaults:
        data = tomllib.load(defaults)

    path = config_path if config_path is not None else Path("config.toml")
    if config_path is not None or path.exists():
        with path.open("rb") as override:
            local = tomllib.load(override)
        if isinstance(local.get("schedule"), dict):
            local["schedule"] = {**data["schedule"], **local["schedule"]}
        data.update(local)

    settings = ProjectSettings.model_validate(data)
    if settings.classification and settings.classification.prompt_path:
        prompt_path = settings.classification.prompt_path
        if not prompt_path.is_absolute():
            settings.classification.prompt_path = path.parent / prompt_path
    return settings
