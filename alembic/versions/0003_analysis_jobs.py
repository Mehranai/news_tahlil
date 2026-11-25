"""Track attempts per article revision and classifier configuration."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_revision_id", sa.Integer(), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
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
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=100), nullable=True),
        sa.Column("classification_id", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_revision_id"],
            ["article_revisions.id"],
            name=op.f("fk_analysis_jobs_article_revision_id_article_revisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name=op.f("fk_analysis_jobs_classification_id_classifications"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_jobs")),
        sa.UniqueConstraint(
            "article_revision_id",
            "profile_key",
            name=op.f("uq_analysis_jobs_article_revision_id_profile_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("analysis_jobs")
