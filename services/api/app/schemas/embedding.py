"""Public embedding metadata; vectors are intentionally never serialized."""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
class EmbeddingRead(BaseModel):
    content_id: UUID; provider: str; model: str; dimensions: int; text_strategy_version: str
    content_hash: str | None; status: str; error_message: str | None; generated_at: datetime | None
    updated_at: datetime; is_stale: bool
    model_config=ConfigDict(from_attributes=True)
class EmbeddingBatchRequest(BaseModel):
    content_ids: list[UUID] | None = Field(default=None, max_length=500)
    limit: int=Field(default=50, ge=1, le=500); force: bool=False; dry_run: bool=False
class EmbeddingBatchResult(BaseModel):
    requested: int; processed: int; skipped: int; completed: int; failed: int; dry_run: bool; items: list[dict[str,object]]
