"""Public HTTP contracts for the first authentication flow only."""

from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.services.email_normalization import InvalidEmailError, normalize_email


class LoginRequest(BaseModel):
    """Credentials accepted by the public login endpoint."""

    email: StrictStr = Field(min_length=1, max_length=320)
    password: StrictStr = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(BaseModel):
    """Strict public registration input, independent from internal DTOs."""

    email: StrictStr = Field(min_length=1, max_length=320)
    password: StrictStr = Field(min_length=12, max_length=128)

    model_config = ConfigDict(extra="forbid")

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        try:
            return normalize_email(value).email
        except InvalidEmailError as error:
            raise ValueError("email is invalid") from error


class RegisterResponse(BaseModel):
    """Uniform acknowledgement that never reveals identity or challenge state."""

    status: str = "registration_received"

    model_config = ConfigDict(extra="forbid")


class EmailVerificationConfirmRequest(BaseModel):
    """Strict opaque challenge accepted by public email verification."""

    token: StrictStr = Field(min_length=1, max_length=256)

    model_config = ConfigDict(extra="forbid")


class EmailVerificationConfirmResponse(BaseModel):
    """Stable acknowledgement without account, token, or session state."""

    status: str = "email_verified"

    model_config = ConfigDict(extra="forbid")


class PasswordResetRequest(BaseModel):
    """Strict public input for an anonymous reset request."""

    email: StrictStr = Field(min_length=1, max_length=320)

    model_config = ConfigDict(extra="forbid")

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        try:
            return normalize_email(value).email
        except InvalidEmailError as error:
            raise ValueError("email is invalid") from error


class PasswordResetRequestResponse(BaseModel):
    """Uniform response that does not disclose account state."""

    status: str = "password_reset_requested"

    model_config = ConfigDict(extra="forbid")


class PasswordResetConfirmRequest(BaseModel):
    """Strict opaque token and replacement password input."""

    token: StrictStr = Field(min_length=1, max_length=256)
    password: StrictStr = Field(min_length=12, max_length=128)

    model_config = ConfigDict(extra="forbid")


class PasswordResetConfirmResponse(BaseModel):
    """Stable response without authentication material."""

    status: str = "password_reset_completed"

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


class AccountDeletionRequest(BaseModel):
    """Explicit, strict acknowledgement for an irreversible account closure."""

    confirmation: StrictStr = Field(min_length=6, max_length=6)

    model_config = ConfigDict(extra="forbid")

    @field_validator("confirmation")
    @classmethod
    def validate_confirmation(cls, value: str) -> str:
        if value != "DELETE":
            raise ValueError("invalid account deletion confirmation")
        return value


class ErrorResponse(BaseModel):
    """Stable sanitized error envelope for public authentication failures."""

    detail: str

    model_config = ConfigDict(extra="forbid")
