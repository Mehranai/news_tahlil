"""Keep alternate links associated with their original article."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["news_sources.id"],
            name=op.f("fk_article_aliases_source_id_news_sources"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_aliases_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_aliases")),
        sa.UniqueConstraint("source_id", "url", name=op.f("uq_article_aliases_source_id_url")),
    )
    op.create_index(op.f("ix_article_aliases_article_id"), "article_aliases", ["article_id"])


def downgrade() -> None:
    op.drop_table("article_aliases")
