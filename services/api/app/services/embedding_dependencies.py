"""FastAPI dependencies for lazy, overridable embedding generation."""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.embedding_service import EmbeddingService
from app.services.openai_embedding_provider import OpenAIEmbeddingProvider


def get_embedding_provider() -> object:
    """Return a lazy wrapper; no client or network operation occurs here."""

    return OpenAIEmbeddingProvider()


def get_embedding_service(
    database: Session = Depends(get_db), provider: object = Depends(get_embedding_provider)
) -> EmbeddingService:
    """Construct a service with dependencies explicit and overridable in HTTP tests."""

    return EmbeddingService(database, provider)  # type: ignore[arg-type]
