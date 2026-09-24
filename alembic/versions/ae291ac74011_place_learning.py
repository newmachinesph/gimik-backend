"""Open catalogue, private search history and durable activity-fit jobs."""

import sqlalchemy as sa
from alembic import op

revision = "ae291ac74011"
down_revision = "6dfe0ff40646"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("places", sa.Column("coordinates_refreshed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "catalog_places",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("place_id", sa.Uuid(), sa.ForeignKey("places.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column("license", sa.String(40), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        sa.Column("coordinate_kind", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_id"),
    )
    op.create_table(
        "place_assessments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("catalog_place_id", sa.Uuid(), sa.ForeignKey("catalog_places.id", ondelete="CASCADE"), nullable=False),
        sa.Column("someday_id", sa.Uuid(), sa.ForeignKey("somedays.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("verdict", sa.String(20)),
        sa.Column("confidence", sa.Float()),
        sa.Column("reason", sa.String(600)),
        sa.Column("model", sa.String(80)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("catalog_place_id", "someday_id", "evidence_hash"),
    )
    op.create_index("ix_place_assessments_queue", "place_assessments", ["status", "available_at"])
    op.create_table(
        "place_search_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query", sa.String(120), nullable=False),
        sa.Column("someday_id", sa.Uuid(), sa.ForeignKey("somedays.id", ondelete="SET NULL")),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_place_search_events_created_at", "place_search_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("place_search_events")
    op.drop_table("place_assessments")
    op.drop_table("catalog_places")
    op.drop_column("places", "coordinates_refreshed_at")
