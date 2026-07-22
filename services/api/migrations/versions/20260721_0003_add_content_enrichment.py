"""Add deterministic enrichment fields to contents.

Revision ID: 20260721_0003
Revises: 20260720_0002
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260721_0003"
down_revision = "20260720_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add structured enrichment fields while preserving existing content."""

    op.add_column("contents", sa.Column("category", sa.String(length=100), nullable=True))
    op.add_column("contents", sa.Column("topics", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("contents", sa.Column("keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("contents", sa.Column("relevance_score", sa.Integer(), nullable=True))
    op.add_column("contents", sa.Column("processing_status", sa.String(length=20), nullable=False, server_default="pending"))
    op.add_column("contents", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("contents", sa.Column("processing_error", sa.Text(), nullable=True))
    op.create_index("ix_contents_category", "contents", ["category"])
    op.create_index("ix_contents_relevance_score", "contents", ["relevance_score"])
    op.create_index("ix_contents_processing_status", "contents", ["processing_status"])
    op.create_check_constraint("ck_contents_relevance_score_range", "contents", "relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 100)")


def downgrade() -> None:
    """Remove deterministic enrichment fields."""

    op.drop_constraint("ck_contents_relevance_score_range", "contents", type_="check")
    op.drop_index("ix_contents_processing_status", table_name="contents")
    op.drop_index("ix_contents_relevance_score", table_name="contents")
    op.drop_index("ix_contents_category", table_name="contents")
    op.drop_column("contents", "processing_error")
    op.drop_column("contents", "processed_at")
    op.drop_column("contents", "processing_status")
    op.drop_column("contents", "relevance_score")
    op.drop_column("contents", "keywords")
    op.drop_column("contents", "topics")
    op.drop_column("contents", "category")
