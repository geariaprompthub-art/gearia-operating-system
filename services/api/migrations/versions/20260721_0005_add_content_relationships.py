"""Add deterministic content relationships.

Revision ID: 20260721_0005
Revises: 20260721_0004
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260721_0005"
down_revision = "20260721_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create explainable canonical relationship storage and PostgreSQL trigram support."""

    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    json_type = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()
    op.create_table(
        "content_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("related_content_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=False),
        sa.Column("score_breakdown", json_type, nullable=False),
        sa.Column("shared_topics", json_type, nullable=False),
        sa.Column("shared_keywords", json_type, nullable=False),
        sa.Column("same_category", sa.Boolean(), nullable=False),
        sa.Column("same_source", sa.Boolean(), nullable=False),
        sa.Column("text_similarity", sa.Numeric(6, 5), nullable=False),
        sa.Column("published_distance_days", sa.Integer(), nullable=True),
        sa.Column("reasons", json_type, nullable=False),
        sa.Column("algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["contents.id"], name="fk_content_relationships_content_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_content_id"], ["contents.id"], name="fk_content_relationships_related_content_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_content_relationships"),
        sa.UniqueConstraint("content_id", "related_content_id", name="uq_content_relationships_pair"),
        sa.CheckConstraint("content_id <> related_content_id", name="ck_content_relationships_distinct_contents"),
        sa.CheckConstraint("content_id < related_content_id", name="ck_content_relationships_canonical_pair"),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="ck_content_relationships_score_range"),
    )
    op.create_index("ix_content_relationships_content_id", "content_relationships", ["content_id"])
    op.create_index("ix_content_relationships_related_content_id", "content_relationships", ["related_content_id"])
    op.create_index("ix_content_relationships_content_score", "content_relationships", ["content_id", "score"])
    op.create_index("ix_content_relationships_related_score", "content_relationships", ["related_content_id", "score"])
    op.create_index("ix_content_relationships_algorithm_version", "content_relationships", ["algorithm_version"])
    op.create_index("ix_content_relationships_calculated_at", "content_relationships", ["calculated_at"])


def downgrade() -> None:
    """Drop only Sprint 06 objects; pg_trgm can be shared and remains installed."""

    op.drop_table("content_relationships")
