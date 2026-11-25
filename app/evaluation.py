"""Measure multiclass relevance and the binary decision to announce a story."""

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from app.intelligence.contracts import ArticleInput, ClassificationError, Classifier, Relevance

POSITIVE = {Relevance.DIRECT, Relevance.INDIRECT}


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def metrics(pairs: Iterable[tuple[Relevance, Relevance]]) -> dict:
    matrix = {label: dict.fromkeys(Relevance, 0) for label in Relevance}
    for expected, predicted in pairs:
        matrix[expected][predicted] += 1

    total = sum(sum(row.values()) for row in matrix.values())
    per_label = {}
    for label in Relevance:
        correct = matrix[label][label]
        actual = sum(matrix[label].values())
        predicted = sum(row[label] for row in matrix.values())
        per_label[label] = {
            "support": actual,
            "precision": _ratio(correct, predicted),
            "recall": _ratio(correct, actual),
            "f1": _ratio(2 * correct, actual + predicted),
        }
    f1_scores = [score["f1"] for score in per_label.values() if score["f1"] is not None]

    tp = sum(matrix[actual][predicted] for actual in POSITIVE for predicted in POSITIVE)
    actual_positive = sum(sum(matrix[label].values()) for label in POSITIVE)
    predicted_positive = sum(sum(row[label] for label in POSITIVE) for row in matrix.values())
    fp, fn = predicted_positive - tp, actual_positive - tp
    indirect = matrix[Relevance.INDIRECT]
    return {
        "samples": total,
        "accuracy": _ratio(sum(matrix[label][label] for label in Relevance), total),
        "precision": _ratio(tp, predicted_positive),
        "recall": _ratio(tp, actual_positive),
        "f1": _ratio(2 * tp, actual_positive + predicted_positive),
        "macro_f1": sum(f1_scores) / len(f1_scores) if f1_scores else None,
        "false_positive_rate": _ratio(fp, total - actual_positive),
        "false_negative_rate": _ratio(fn, actual_positive),
        "indirect_relevance_recall": per_label[Relevance.INDIRECT]["recall"],
        "indirect_detection_recall": _ratio(
            sum(indirect[label] for label in POSITIVE), sum(indirect.values())
        ),
        "per_label": per_label,
        "confusion_matrix": matrix,
    }


class Fixture(BaseModel):
    title: str = Field(min_length=1)
    summary: str | None = None
    body: str | None = None
    language: str | None = None
    relevance: Relevance


def evaluate_fixtures(path: Path, classifier: Classifier) -> tuple[dict, int]:
    cases = TypeAdapter(list[Fixture]).validate_json(path.read_bytes())
    if not cases:
        raise ValueError("Provide at least one synthetic case")
    pairs = []
    outcomes = []
    for index, sample in enumerate(cases, 1):
        outcome = {"case": index, "title": sample.title, "expected": sample.relevance}
        try:
            result = classifier.classify(
                ArticleInput(sample.title, sample.summary, sample.body, sample.language)
            )
        except ClassificationError as exc:
            outcomes.append({**outcome, "error": exc.code})
            if exc.halt_batch:
                break
        else:
            predicted = result.assessment.relevance
            pairs.append((sample.relevance, predicted))
            outcomes.append({**outcome, "predicted": predicted})
    return {
        "dataset": "synthetic",
        "cases": len(cases),
        "failed_cases": len(outcomes) - len(pairs),
        "unattempted_cases": len(cases) - len(outcomes),
        "classifier": asdict(classifier.identity),
        "metrics": metrics(pairs),
        "outcomes": outcomes,
    }, int(len(pairs) != len(cases))
