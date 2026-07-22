"""Idempotent embedding generation service."""
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.content import Content
from app.models.content_embedding import ContentEmbedding
from app.services.embedding_provider import EmbeddingProvider, build_content_embedding_text, content_hash
from app.services.openai_embedding_provider import OpenAIEmbeddingProvider
class EmbeddingService:
    def __init__(self, database: Session, provider: EmbeddingProvider | None = None) -> None: self.db=database; self.provider=provider
    def generate(self, content_id, force=False, dry_run=False) -> dict[str, object]:
        content=self.db.get(Content,content_id)
        if not content: raise LookupError("Content not found")
        text=build_content_embedding_text(content); digest=content_hash(text)
        row=self.db.scalar(select(ContentEmbedding).where(ContentEmbedding.content_id==content_id))
        if row and self.is_stale(row, digest):
            if not dry_run: row.embedding_status = "stale"; self.db.commit()
        if row and row.content_hash==digest and row.embedding_status=="completed" and not force: return {"content_id":content_id,"status":"skipped","content_hash":digest,"dry_run":dry_run}
        if dry_run: return {"content_id":content_id,"status":"would_generate","content_hash":digest,"dry_run":True}
        row=row or ContentEmbedding(content_id=content_id)
        row.embedding_status="processing"; row.processing_error=None; self.db.add(row); self.db.flush()
        try:
            vector=(self.provider or OpenAIEmbeddingProvider()).embed_text(text)
            if len(vector)!=1536: raise RuntimeError("Invalid embedding dimensions")
            row.embedding=vector; row.content_hash=digest; row.embedding_status="completed"; row.embedded_at=datetime.now(UTC)
            self.db.commit(); return {"content_id":content_id,"status":"completed","content_hash":digest,"dry_run":False}
        except Exception as error:
            row.embedding_status="failed"; row.processing_error="Embedding provider failed"; self.db.commit(); raise RuntimeError("Embedding generation failed") from error
    def generate_batch(self, limit: int=50, force: bool=False, dry_run: bool=False, content_ids: list[object] | None=None) -> dict[str, object]:
        if content_ids is not None:
            unique=list(dict.fromkeys(content_ids)); contents=list(self.db.scalars(select(Content).where(Content.id.in_(unique)).order_by(Content.created_at.asc(),Content.id.asc())))
        else: contents=list(self.db.scalars(select(Content).order_by(Content.created_at.asc(),Content.id.asc()).limit(limit)))
        results=[]
        for content in contents:
            try: results.append(self.generate(content.id,force,dry_run))
            except RuntimeError: results.append({"content_id":content.id,"status":"failed","dry_run":dry_run})
        return {"requested":len(unique) if content_ids is not None else len(contents),"processed":len(contents),"skipped":sum(x["status"]=="skipped" for x in results),"completed":sum(x["status"] in {"completed", "would_generate"} for x in results),"failed":sum(x["status"]=="failed" for x in results),"dry_run":dry_run,"items":results}
    @staticmethod
    def is_stale(row: ContentEmbedding, current_hash: str) -> bool:
        """One reusable validity rule: only matching completed records are usable."""
        return row.embedding_status != "completed" or row.content_hash != current_hash
