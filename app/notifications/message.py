"""Render a Persian notification within the Telegram message size limit."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.intelligence.contracts import Relevance
from app.preprocessing import clean_text, normalize_url

MESSAGE_LIMIT = 4096

RELEVANCE_LABELS = {
    Relevance.DIRECT: "مستقیم",
    Relevance.INDIRECT: "غیرمستقیم",
    Relevance.UNCERTAIN: "نامشخص، نیازمند بررسی",
}
TOPIC_LABELS = {
    "politics": "سیاسی",
    "diplomacy": "دیپلماسی",
    "economy": "اقتصادی",
    "sanctions": "تحریم‌ها",
    "security": "امنیتی",
    "nuclear_program": "هسته‌ای",
    "technology": "فناوری",
    "sports": "ورزشی",
}


@dataclass(frozen=True)
class Alert:
    article_id: int
    classification_id: int
    source_name: str
    title: str
    original_url: str
    canonical_url: str
    published_at: datetime | None
    relevance: Relevance
    topic: str
    summary_fa: str
    reason: str


def render_message(alert: Alert, timezone: ZoneInfo) -> str:
    header = (
        "🔔 خبر نیازمند بررسی دربارهٔ ایران"
        if alert.relevance == Relevance.UNCERTAIN
        else "🔔 خبر جدید مرتبط با ایران"
    )
    published = (
        alert.published_at.astimezone(timezone).strftime("%Y-%m-%d %H:%M %Z")
        if alert.published_at
        else "نامشخص"
    )
    title = "📌 عنوان: "
    summary = "📝 خلاصه فارسی: "
    reason = "🧠 دلیل ارتباط: "
    lines = [
        header,
        f"📰 رسانه: {_clip(clean_text(alert.source_name), 120)}",
        title,
        f"🇮🇷 نوع ارتباط: {RELEVANCE_LABELS[alert.relevance]}",
        summary,
        reason,
        f"📂 دسته‌بندی: {TOPIC_LABELS.get(alert.topic, 'سایر')}",
        f"📅 تاریخ انتشار: {published}",
        "🔗 ",
    ]

    budget = MESSAGE_LIMIT - _length("\n\n".join(lines))
    # Leave space for the three text fields and always keep the link intact.
    for url in (alert.original_url, alert.canonical_url):
        if normalize_url(url) is not None and _length(url) <= budget - 90:
            break
    else:
        raise ValueError("invalid_notification_link")
    lines[-1] += url
    budget -= _length(url)
    fields = [(2, alert.title, 600), (4, alert.summary_fa, 1500), (5, alert.reason, 1500)]
    for index, (line, text, maximum) in enumerate(fields):
        clipped = _clip(clean_text(text), min(maximum, budget // (len(fields) - index)))
        lines[line] += clipped
        budget -= _length(clipped)
    return "\n\n".join(lines)


def _length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, maximum: int) -> str:
    encoded = text.encode("utf-16-le")
    if len(encoded) <= maximum * 2:
        return text
    return encoded[: (maximum - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
