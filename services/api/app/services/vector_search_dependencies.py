"""Explicit, overridable dependencies for vector retrieval."""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.repositories.vector_search_repository import VectorSearchRepository
from app.services.embedding_dependencies import get_embedding_provider
from app.services.vector_search_service import VectorSearchService


def get_vector_search_service(
    database: Session = Depends(get_db), provider: object = Depends(get_embedding_provider)
) -> VectorSearchService:
    """Build the vector-search service only for POST vector search requests."""

    return VectorSearchService(VectorSearchRepository(database), provider)  # type: ignore[arg-type]
