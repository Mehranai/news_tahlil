"""Apply the article prompt and verify structured assessments before storage."""

import json
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.config.settings import ClassificationSettings
from app.intelligence.contracts import (
    ArticleInput,
    Assessment,
    ClassificationError,
    ClassificationResult,
    ClassifierIdentity,
    Relevance,
)
from app.intelligence.llm_api import ChatCompletionsAPI, ModelAPI, ModelHTTP, OllamaAPI
from app.preprocessing import clean_text

SCHEMA = Assessment.model_json_schema()


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str


def load_prompt(path: Path | None = None) -> Prompt:
    text = (
        files("app.intelligence.prompts")
        .joinpath("iran_relevance_v1.txt")
        .read_text(encoding="utf-8")
    )
    if path is not None:
        extra = path.read_text(encoding="utf-8-sig").strip()
        if not extra:
            raise ClassificationError("empty_prompt_guidance", halt_batch=True)
        text = f"{text}\nAdditional trusted project guidance:\n{extra}\n"
    return Prompt(text, f"v1-{sha256(text.encode('utf-8')).hexdigest()[:16]}")


class LLMClassifier:
    def __init__(self, settings: ClassificationSettings, api: ModelAPI, prompt: Prompt) -> None:
        self.settings = settings
        self.api = api
        self.prompt = prompt
        profile = settings.model_dump(
            mode="json",
            exclude={"prompt_path", "timeout_seconds"},
        )
        fingerprint = json.dumps(
            [profile, prompt.version, SCHEMA], sort_keys=True, ensure_ascii=True
        )
        self.identity = ClassifierIdentity(
            settings.provider,
            settings.model,
            prompt.version,
            sha256(fingerprint.encode("utf-8")).hexdigest(),
        )

    def classify(self, article: ArticleInput) -> ClassificationResult:
        title = article.title[:2000]
        summary = article.summary[:4000] if not article.body and article.summary else None
        body = article.body or ""
        limit = self.settings.max_body_chars
        truncated = len(body) > limit
        if truncated:
            tail = limit // 4
            segments = [body[: limit - tail], body[-tail:]]
        else:
            segments = [body] if body else []
        data = json.dumps(
            {
                "title": title,
                "summary": summary,
                "body_segments": segments,
                "body_truncated": truncated,
                "language": article.language,
            },
            ensure_ascii=False,
        )
        reply = self.api.complete(self.prompt.text, data, SCHEMA)
        try:
            assessment = Assessment.model_validate_json(reply.content)
        except ValidationError:
            raise ClassificationError("invalid_model_json") from None

        quoted_fields = [title, *segments]
        if summary:
            quoted_fields.append(summary)
        return ClassificationResult(
            _verify_evidence(assessment, quoted_fields),
            self.settings.model_version or reply.model_version,
            reply.input_tokens,
            reply.output_tokens,
        )


def build_classifier(
    settings: ClassificationSettings, client: httpx.Client, api_key: str | None
) -> LLMClassifier:
    if settings.base_url.host == "api.openai.com" and not api_key:
        raise ClassificationError("missing_llm_api_key", halt_batch=True)
    if settings.protocol == "ollama":
        api: ModelAPI = OllamaAPI(ModelHTTP(client, settings, None))
    else:
        if (
            api_key
            and settings.base_url.scheme != "https"
            and settings.base_url.host not in {"localhost", "127.0.0.1", "[::1]"}
        ):
            raise ClassificationError("llm_key_requires_https", halt_batch=True)
        api = ChatCompletionsAPI(ModelHTTP(client, settings, api_key))
    return LLMClassifier(settings, api, load_prompt(settings.prompt_path))


def _verify_evidence(assessment: Assessment, fields: list[str]) -> Assessment:
    inputs = [clean_text(field) for field in fields]
    quoted = [
        excerpt
        for excerpt in assessment.evidence
        if any(clean_text(excerpt) in field for field in inputs)
    ]
    positive = assessment.relevance in {Relevance.DIRECT, Relevance.INDIRECT}
    if len(quoted) != len(assessment.evidence) or (positive and not quoted):
        return assessment.model_copy(
            update={
                "relevance": Relevance.UNCERTAIN,
                "confidence": 0.0,
                "topic": "other",
                "reason": "شواهد تأییدپذیر برای نتیجهٔ مدل کافی نبود.",
                "evidence": quoted,
                "related_entities": [],
                "summary_fa": "خلاصهٔ این خبر به بازبینی انسانی نیاز دارد.",
                "needs_review": True,
            }
        )
    if assessment.relevance is Relevance.UNCERTAIN and not assessment.needs_review:
        return assessment.model_copy(update={"needs_review": True})
    return assessment
