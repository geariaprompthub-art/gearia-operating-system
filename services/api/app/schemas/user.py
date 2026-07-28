"""Safe internal identity DTOs; no HTTP request contracts are defined here."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UserDTO(BaseModel):
    id: UUID
    email: str
    status: str
    email_verified_at: datetime | None
    token_version: int
    created_at: datetime
    updated_at: datetime


class CredentialResult(BaseModel):
    status: str
    user: UserDTO | None = None
    rehash_required: bool = False
