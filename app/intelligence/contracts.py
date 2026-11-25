"""Provider-independent inputs, results, and errors for relevance classifiers."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator


class Relevance(StrEnum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    relevance: Relevance
    confidence: float
    topic: str
    reason: str
    evidence: list[str]
    related_entities: list[str]
    summary_fa: str
    needs_review: bool

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value):
            raise ValueError("topic must be a lowercase label")
        return value

    @field_validator("reason", "summary_fa")
    @classmethod
    def validate_prose(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 1500:
            raise ValueError("prose must contain 1 to 1500 characters")
        return value

    @field_validator("summary_fa")
    @classmethod
    def validate_summary_script(cls, value: str) -> str:
        if not re.search(r"[آ-غف-يپچژکگی]", value):
            raise ValueError("summary_fa must contain Persian-script text")
        return value

    @field_validator("evidence", "related_entities")
    @classmethod
    def validate_excerpts(cls, values: list[str]) -> list[str]:
        if len(values) > 12 or any(not 1 <= len(value.strip()) <= 500 for value in values):
            raise ValueError("provide at most 12 nonempty items up to 500 characters each")
        return list(dict.fromkeys(value.strip() for value in values))


@dataclass(frozen=True)
class ArticleInput:
    title: str
    summary: str | None
    body: str | None
    language: str | None


@dataclass(frozen=True)
class ClassifierIdentity:
    provider: str
    model: str
    prompt_version: str
    profile_key: str


@dataclass(frozen=True)
class ClassificationResult:
    assessment: Assessment
    model_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ClassificationError(Exception):
    """A safe error code suitable for logs and persistent processing state."""

    def __init__(self, code: str, *, halt_batch: bool = False) -> None:
        self.code = code
        self.halt_batch = halt_batch
        super().__init__(code)


class Classifier(Protocol):
    @property
    def identity(self) -> ClassifierIdentity: ...

    def classify(self, article: ArticleInput) -> ClassificationResult: ...
