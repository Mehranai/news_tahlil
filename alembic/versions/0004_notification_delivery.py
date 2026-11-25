"""Track Telegram retries and unresolved delivery attempts."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

LEGACY_STATUS = "status IN ('pending', 'sent', 'failed')"
DELIVERY_STATUS = "status IN ('pending', 'sending', 'sent', 'failed', 'unknown')"


def upgrade() -> None:
    with op.batch_alter_table("notifications", copy_from=_offline_source(current=False)) as table:
        table.drop_constraint(op.f("ck_notifications_status"), type_="check")
        table.create_check_constraint(op.f("ck_notifications_status"), DELIVERY_STATUS)
        table.add_column(sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
        table.add_column(sa.Column("claim_token", sa.String(length=32), nullable=True))
        table.add_column(sa.Column("lease_until", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE notifications SET status = 'unknown' WHERE last_error = 'delivery_unknown'"
        " AND status = 'failed'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE notifications SET status = 'failed', last_error = 'delivery_unknown'"
        " WHERE status IN ('sending', 'unknown')"
    )
    with op.batch_alter_table("notifications", copy_from=_offline_source(current=True)) as table:
        table.drop_constraint(op.f("ck_notifications_status"), type_="check")
        table.create_check_constraint(op.f("ck_notifications_status"), LEGACY_STATUS)
        table.drop_column("lease_until")
        table.drop_column("claim_token")
        table.drop_column("next_attempt_at")


def _offline_source(*, current: bool) -> sa.Table | None:
    context = op.get_context()
    if not context.as_sql or context.dialect.name != "sqlite":
        return None

    # An offline SQLite batch migration cannot reflect the table it will copy.
    metadata = sa.MetaData()
    articles = sa.Table("articles", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    classifications = sa.Table(
        "classifications", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    table = sa.Table(
        "notifications",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey(
                articles.c.id,
                name="fk_notifications_article_id_articles",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "classification_id",
            sa.Integer(),
            sa.ForeignKey(
                classifications.c.id,
                name="fk_notifications_classification_id_classifications",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("external_message_id", sa.String(length=100)),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime()),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint("article_id", "channel", name="uq_notifications_article_id_channel"),
        sa.CheckConstraint(
            DELIVERY_STATUS if current else LEGACY_STATUS, name="ck_notifications_status"
        ),
    )
    if current:
        table.append_column(sa.Column("next_attempt_at", sa.DateTime()))
        table.append_column(sa.Column("claim_token", sa.String(length=32)))
        table.append_column(sa.Column("lease_until", sa.DateTime()))
    return table
