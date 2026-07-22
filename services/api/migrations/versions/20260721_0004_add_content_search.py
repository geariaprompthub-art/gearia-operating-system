"""Add PostgreSQL full-text search index for contents.

Revision ID: 20260721_0004
Revises: 20260721_0003
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "20260721_0004"
down_revision = "20260721_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create an accent-insensitive weighted tsvector maintained by a trigger."""

    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("ALTER TABLE contents ADD COLUMN search_vector tsvector")
    op.execute(
        """
        CREATE FUNCTION content_search_vector_update() RETURNS trigger AS $$
        BEGIN
          NEW.search_vector :=
            setweight(to_tsvector('simple', unaccent(coalesce(NEW.title, ''))), 'A') ||
            setweight(to_tsvector('simple', unaccent(coalesce((SELECT string_agg(keyword_item.value, ' ') FROM jsonb_array_elements_text(coalesce(NEW.keywords, '[]'::jsonb)) AS keyword_item(value)), ''))), 'B') ||
            setweight(to_tsvector('simple', unaccent(coalesce((SELECT string_agg(topic_item.value, ' ') FROM jsonb_array_elements_text(coalesce(NEW.topics, '[]'::jsonb)) AS topic_item(value)), ''))), 'B') ||
            setweight(to_tsvector('simple', unaccent(coalesce(NEW.summary, ''))), 'C') ||
            setweight(to_tsvector('simple', unaccent(coalesce(NEW.category, ''))), 'D');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER content_search_vector_trigger
        BEFORE INSERT OR UPDATE OF title, summary, category, topics, keywords
        ON contents
        FOR EACH ROW EXECUTE FUNCTION content_search_vector_update();
        """
    )
    # Updating an indexed source field fires the trigger and backfills every existing row.
    op.execute("UPDATE contents SET title = title")
    op.execute("CREATE INDEX ix_contents_search_vector_gin ON contents USING GIN (search_vector)")
    op.create_index("ix_contents_language", "contents", ["language"])
    op.create_index("ix_contents_published_at", "contents", ["published_at"])
    op.create_index("ix_contents_created_at", "contents", ["created_at"])


def downgrade() -> None:
    """Remove FTS objects; unaccent remains because it may be shared by other objects."""

    op.drop_index("ix_contents_created_at", table_name="contents")
    op.drop_index("ix_contents_published_at", table_name="contents")
    op.drop_index("ix_contents_language", table_name="contents")
    op.execute("DROP INDEX ix_contents_search_vector_gin")
    op.execute("DROP TRIGGER content_search_vector_trigger ON contents")
    op.execute("DROP FUNCTION content_search_vector_update()")
    op.execute("ALTER TABLE contents DROP COLUMN search_vector")
