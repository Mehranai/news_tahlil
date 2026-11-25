"""Store human label history and compare model decisions against the latest labels."""

from collections import defaultdict
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.selectable import ScalarSelect

from app.evaluation import metrics
from app.intelligence.contracts import Relevance
from app.storage.models import (
    AnalysisJob,
    Article,
    ArticleRevision,
    Classification,
    HumanFeedback,
    NewsSource,
)


def latest_feedback_id() -> ScalarSelect[int]:
    feedback = aliased(HumanFeedback)
    return (
        select(feedback.id)
        .where(feedback.article_revision_id == ArticleRevision.id)
        .order_by(feedback.created_at.desc(), feedback.id.desc())
        .limit(1)
        .correlate(ArticleRevision)
        .scalar_subquery()
    )


def latest_classification_id() -> ScalarSelect[int]:
    classification = aliased(Classification)
    return (
        select(classification.id)
        .where(classification.article_revision_id == ArticleRevision.id)
        .order_by(classification.analyzed_at.desc(), classification.id.desc())
        .limit(1)
        .correlate(ArticleRevision)
        .scalar_subquery()
    )


@dataclass(frozen=True)
class EvaluatedModel:
    provider: str
    model: str
    model_version: str | None
    prompt_version: str
    profile_key: str | None


class EvaluationStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def record(self, revision_id: int, relevance: Relevance, note: str | None) -> dict | None:
        with self.sessions.begin() as session:
            revision = session.execute(
                select(ArticleRevision.id, latest_classification_id()).where(
                    ArticleRevision.id == revision_id
                )
            ).one_or_none()
            if revision is None:
                return None
            row = HumanFeedback(
                article_revision_id=revision_id,
                classification_id=revision[1],
                relevance=relevance,
                note=note,
            )
            session.add(row)
            session.flush()
            return {"feedback_id": row.id, "revision_id": revision_id, "human_label": relevance}

    def review(
        self,
        *,
        revision_id: int | None,
        limit: int,
        needs_review: bool,
        related_only: bool = False,
    ) -> list[dict]:
        statement = (
            select(
                ArticleRevision.id.label("revision_id"),
                NewsSource.slug.label("source"),
                ArticleRevision.title,
                Article.canonical_url.label("url"),
                Classification.id.label("classification_id"),
                Classification.relevance.label("model_label"),
                Classification.model,
                Classification.confidence,
                Classification.needs_review,
                HumanFeedback.relevance.label("human_label"),
            )
            .select_from(ArticleRevision)
            .join(Article, Article.id == ArticleRevision.article_id)
            .join(NewsSource, NewsSource.id == Article.source_id)
            .outerjoin(Classification, Classification.id == latest_classification_id())
            .outerjoin(HumanFeedback, HumanFeedback.id == latest_feedback_id())
            .order_by(ArticleRevision.last_seen_at.desc(), ArticleRevision.id.desc())
            .limit(limit)
        )
        if revision_id is not None:
            statement = statement.where(ArticleRevision.id == revision_id).add_columns(
                ArticleRevision.rss_summary,
                ArticleRevision.body_text,
                Classification.reason,
                Classification.evidence,
                HumanFeedback.note.label("human_note"),
            )
        elif needs_review:
            statement = statement.where(
                HumanFeedback.id.is_(None),
                Classification.needs_review.is_not(False),
            )
        elif related_only:
            statement = statement.where(
                Classification.relevance.in_([Relevance.DIRECT, Relevance.INDIRECT])
            )
        with self.sessions() as session:
            return [dict(row) for row in session.execute(statement).mappings()]

    def evaluate(self, *, common_only: bool) -> dict:
        predictions = (
            select(
                ArticleRevision.id,
                HumanFeedback.relevance,
                Classification.relevance,
                Classification.provider,
                Classification.model,
                Classification.model_version,
                Classification.prompt_version,
                AnalysisJob.profile_key,
            )
            .select_from(ArticleRevision)
            .join(HumanFeedback, HumanFeedback.id == latest_feedback_id())
            .outerjoin(Classification, Classification.article_revision_id == ArticleRevision.id)
            .outerjoin(AnalysisJob, AnalysisJob.classification_id == Classification.id)
            .order_by(Classification.analyzed_at.desc(), Classification.id.desc())
        )
        expected = {}
        profiles: dict[EvaluatedModel, dict[int, Relevance]] = defaultdict(dict)
        with self.sessions() as session:
            for revision_id, label, relevance, *identity in session.execute(predictions):
                expected[revision_id] = label
                if relevance is not None:
                    profiles[EvaluatedModel(*identity)].setdefault(revision_id, relevance)
        common = (
            set.intersection(*(set(profile) for profile in profiles.values()))
            if profiles
            else set()
        )
        models = []
        for model, predictions_by_id in profiles.items():
            samples = [
                (revision_id, expected[revision_id], predicted)
                for revision_id, predicted in predictions_by_id.items()
                if not common_only or revision_id in common
            ]
            mismatches = [
                {"revision_id": revision_id, "expected": actual, "predicted": predicted}
                for revision_id, actual, predicted in samples
                if actual != predicted
            ]
            models.append(
                {
                    **asdict(model),
                    "predicted_labeled_revisions": len(predictions_by_id),
                    "metrics": metrics((actual, predicted) for _, actual, predicted in samples),
                    "mismatches": len(mismatches),
                    "mismatch_examples": mismatches[:20],
                }
            )
        return {
            "dataset": "human_feedback",
            "labeled_revisions": len(expected),
            "common_revisions": len(common),
            "common_only": common_only,
            "models": models,
        }
