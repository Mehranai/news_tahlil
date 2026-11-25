"""Create initial news schema

Revision ID: 0001
Revises:
Create Date: 2026-11-24 09:43:43.250492
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("rss_url", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_sources")),
        sa.UniqueConstraint("slug", name=op.f("uq_news_sources_slug")),
    )
    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["news_sources.id"], name=op.f("fk_articles_source_id_news_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint(
            "source_id", "canonical_url", name=op.f("uq_articles_source_id_canonical_url")
        ),
    )
    op.create_table(
        "fetch_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "complete",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("saved_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["news_sources.id"], name=op.f("fk_fetch_logs_source_id_news_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_logs")),
    )
    with op.batch_alter_table("fetch_logs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_fetch_logs_source_id"), ["source_id"], unique=False)

    op.create_table(
        "article_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("rss_summary", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "extraction_status",
            sa.Enum(
                "pending",
                "complete",
                "partial",
                "failed",
                name="extraction_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "analysis_status",
            sa.Enum(
                "pending",
                "complete",
                "failed",
                name="analysis_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_revisions_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_revisions")),
        sa.UniqueConstraint(
            "article_id", "content_hash", name=op.f("uq_article_revisions_article_id_content_hash")
        ),
    )
    with op.batch_alter_table("article_revisions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_article_revisions_content_hash"), ["content_hash"], unique=False
        )

    op.create_table(
        "classifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_revision_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("model_version", sa.String(length=160), nullable=True),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column(
            "relevance",
            sa.Enum(
                "direct",
                "indirect",
                "unrelated",
                "uncertain",
                name="relevance",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("related_entities", sa.JSON(), nullable=False),
        sa.Column("summary_fa", sa.Text(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name=op.f("ck_classifications_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["article_revision_id"],
            ["article_revisions.id"],
            name=op.f("fk_classifications_article_revision_id_article_revisions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classifications")),
    )
    with op.batch_alter_table("classifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_classifications_article_revision_id"),
            ["article_revision_id"],
            unique=False,
        )

    op.create_table(
        "human_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_revision_id", sa.Integer(), nullable=False),
        sa.Column("classification_id", sa.Integer(), nullable=True),
        sa.Column(
            "relevance",
            sa.Enum(
                "direct",
                "indirect",
                "unrelated",
                "uncertain",
                name="relevance",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_revision_id"],
            ["article_revisions.id"],
            name=op.f("fk_human_feedback_article_revision_id_article_revisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name=op.f("fk_human_feedback_classification_id_classifications"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_human_feedback")),
    )
    with op.batch_alter_table("human_feedback", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_human_feedback_article_revision_id"),
            ["article_revision_id"],
            unique=False,
        )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("classification_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("external_message_id", sa.String(length=100), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_notifications_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name=op.f("fk_notifications_classification_id_classifications"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "article_id", "channel", name=op.f("uq_notifications_article_id_channel")
        ),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    with op.batch_alter_table("human_feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_human_feedback_article_revision_id"))

    op.drop_table("human_feedback")
    with op.batch_alter_table("classifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_classifications_article_revision_id"))

    op.drop_table("classifications")
    with op.batch_alter_table("article_revisions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_article_revisions_content_hash"))

    op.drop_table("article_revisions")
    with op.batch_alter_table("fetch_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fetch_logs_source_id"))

    op.drop_table("fetch_logs")
    op.drop_table("articles")
    op.drop_table("news_sources")
