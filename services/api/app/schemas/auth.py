"""Public HTTP contracts for the first authentication flow only."""

from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class LoginRequest(BaseModel):
    """Credentials accepted by the public login endpoint."""

    email: StrictStr = Field(min_length=1, max_length=320)
    password: StrictStr = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class LoginUser(BaseModel):
    """Minimal public identity returned after a successful login."""

    id: UUID
    email: str

    model_config = ConfigDict(extra="forbid")


class LoginResponse(BaseModel):
    """Successful login response; credentials and tokens live only in cookies."""

    user: LoginUser

    model_config = ConfigDict(extra="forbid")


class RefreshResponse(BaseModel):
    """Token refresh acknowledgement; all token material remains in cookies."""

    status: str = "authenticated"

    model_config = ConfigDict(extra="forbid")


class CurrentUserResponse(BaseModel):
    """Minimal public projection for the currently authenticated user."""

    id: UUID
    email: str
    status: str
    email_verified_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    """Stable sanitized error envelope for public authentication failures."""

    detail: str

    model_config = ConfigDict(extra="forbid")
