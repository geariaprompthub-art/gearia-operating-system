"""Pydantic schemas for source resources."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    """Payload required to create a source."""

    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool = True


class SourceUpdate(BaseModel):
    """Payload accepted to update an existing source."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None


class SourceRead(BaseModel):
    """Serialized source resource."""

    id: UUID
    name: str
    type: str
    url: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
