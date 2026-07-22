"""Add fixed OpenAI embeddings for semantic retrieval.

Revision ID: 20260721_0006
Revises: 20260721_0005
"""
from alembic import op
import sqlalchemy as sa

revision = "20260721_0006"
down_revision = "20260721_0005"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""CREATE TABLE content_embeddings (
      id uuid PRIMARY KEY, content_id uuid NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
      embedding vector(1536), provider varchar(50) NOT NULL DEFAULT 'openai',
      model varchar(100) NOT NULL DEFAULT 'text-embedding-3-small', dimensions integer NOT NULL DEFAULT 1536,
      content_hash varchar(64), text_strategy_version varchar(50) NOT NULL DEFAULT 'content-text-v1',
      embedding_status varchar(20) NOT NULL DEFAULT 'pending', processing_error text, embedded_at timestamptz,
      created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
      CONSTRAINT uq_content_embeddings_identity UNIQUE(content_id, provider, model, text_strategy_version),
      CONSTRAINT uq_content_embeddings_content UNIQUE(content_id),
      CONSTRAINT ck_content_embeddings_dimensions CHECK(dimensions = 1536),
      CONSTRAINT ck_content_embeddings_provider CHECK(provider = 'openai'),
      CONSTRAINT ck_content_embeddings_model CHECK(model = 'text-embedding-3-small'),
      CONSTRAINT ck_content_embeddings_strategy CHECK(text_strategy_version = 'content-text-v1'),
      CONSTRAINT ck_content_embeddings_hash CHECK(content_hash IS NULL OR char_length(content_hash) = 64),
      CONSTRAINT ck_content_embeddings_completed CHECK(embedding_status <> 'completed' OR (embedding IS NOT NULL AND embedded_at IS NOT NULL))
    )""")
    op.create_index("ix_content_embeddings_content_id", "content_embeddings", ["content_id"])
    op.create_index("ix_content_embeddings_status", "content_embeddings", ["embedding_status"])
def downgrade() -> None:
    op.drop_table("content_embeddings")
