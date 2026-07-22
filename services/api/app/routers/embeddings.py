"""Embedding generation and audit endpoints."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only
from app.db import get_db
from app.models.content_embedding import ContentEmbedding
from app.models.content import Content
from app.services.embedding_service import EmbeddingService
from app.services.embedding_dependencies import get_embedding_service
from app.services.embedding_provider import build_content_embedding_text, content_hash
from app.schemas.embedding import EmbeddingBatchRequest, EmbeddingBatchResult, EmbeddingRead
router=APIRouter(prefix="/embeddings",tags=["embeddings"])
@router.post("/contents/{content_id}/generate")
def generate(content_id: UUID, force: bool=False, dry_run: bool=False, service: EmbeddingService=Depends(get_embedding_service)) -> dict[str,object]:
    try: return service.generate(content_id,force,dry_run)
    except LookupError as error: raise HTTPException(404,str(error)) from error
    except RuntimeError as error: raise HTTPException(503,"Embedding provider unavailable") from error
@router.post("/generate", response_model=EmbeddingBatchResult)
def generate_batch(request: EmbeddingBatchRequest=Body(default_factory=EmbeddingBatchRequest), service: EmbeddingService=Depends(get_embedding_service)) -> dict[str,object]:
    return service.generate_batch(request.limit,request.force,request.dry_run,request.content_ids)
@router.get("/contents/{content_id}", response_model=EmbeddingRead)
def get_embedding(content_id: UUID,database: Session=Depends(get_db)) -> dict[str,object]:
    row=database.scalar(select(ContentEmbedding).where(ContentEmbedding.content_id==content_id))
    if not row: raise HTTPException(404,"Embedding not found")
    content=database.get(Content, content_id)
    return _public(row, content)
@router.get("")
def list_embeddings(embedding_status: Literal["pending", "processing", "completed", "failed", "stale"] | None=Query(default=None, alias="status"), provider: str|None=None, model: str|None=None, stale: bool|None=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100), database: Session=Depends(get_db)) -> dict[str,object]:
    statement=select(ContentEmbedding)
    if embedding_status: statement=statement.where(ContentEmbedding.embedding_status==embedding_status)
    if provider: statement=statement.where(ContentEmbedding.provider==provider)
    if model: statement=statement.where(ContentEmbedding.model==model)
    # Stale is derived from the current content hash, so it must filter before pagination.
    # This metadata-only scan is acceptable at the current small operational volume.
    rows=list(database.scalars(statement.options(load_only(
        ContentEmbedding.id, ContentEmbedding.content_id, ContentEmbedding.provider,
        ContentEmbedding.model, ContentEmbedding.dimensions, ContentEmbedding.content_hash,
        ContentEmbedding.text_strategy_version, ContentEmbedding.embedding_status,
        ContentEmbedding.processing_error, ContentEmbedding.embedded_at,
        ContentEmbedding.created_at, ContentEmbedding.updated_at,
    )).order_by(ContentEmbedding.created_at.asc(),ContentEmbedding.id.asc())))
    items=[_public(r, database.get(Content,r.content_id)) for r in rows]
    if stale is not None: items=[item for item in items if item["is_stale"]==stale]
    total=len(items); start=(page-1)*page_size
    return {"page":page,"page_size":page_size,"total":total,"items":items[start:start+page_size]}
def _public(row: ContentEmbedding, content: Content | None) -> dict[str,object]:
    current=content_hash(build_content_embedding_text(content)) if content else ""
    stale=EmbeddingService.is_stale(row,current)
    return {"content_id":row.content_id,"provider":row.provider,"model":row.model,"dimensions":row.dimensions,"text_strategy_version":row.text_strategy_version,"content_hash":row.content_hash,"status":row.embedding_status,"error_message":row.processing_error,"generated_at":row.embedded_at,"updated_at":row.updated_at,"is_stale":stale}
