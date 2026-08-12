"""Public P2A schemas; ORM and execution-context details stay internal."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceRead(BaseModel):
    """Minimal current-workspace representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class WorkspaceUpdate(BaseModel):
    """Allowed P2A update to the personal aggregate root."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)


class WorkspaceSourceCreate(BaseModel):
    """Link an existing canonical source to the current workspace."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID


class OrganizationWorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)


class OrganizationWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    organization_id: UUID
    name: str
    status: str
