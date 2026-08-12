"""Strict public HTTP contracts for the first P3A organizations endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class OrganizationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    slug: StrictStr


class OrganizationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name is invalid")
        return value


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: UUID
    kind: str
    name: str
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization: OrganizationResponse
    owner_membership_id: UUID
    initial_workspace_id: UUID


class MembershipUpdateRequest(BaseModel):
    """The lifecycle service remains the sole authority over role transitions."""

    model_config = ConfigDict(extra="forbid")
    role: StrictStr

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"owner", "admin", "member"}:
            raise ValueError("role is invalid")
        return value


class MembershipResponse(BaseModel):
    """Active membership projection without user contact or audit internals."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    created_at: datetime


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: StrictStr = Field(min_length=1, max_length=320)
    role: StrictStr

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"member", "admin"}:
            raise ValueError("role is invalid")
        return value


class InvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: StrictStr = Field(min_length=1, max_length=256)


class InvitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    organization_id: UUID
    role: str
    expires_at: datetime
    created_at: datetime


class InvitationIssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = "invitation_created"


class InvitationAcceptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = "invitation_processed"
