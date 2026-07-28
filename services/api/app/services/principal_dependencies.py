"""FastAPI dependency boundary for the sole accepted access-token channel."""

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.access_token_authenticator import AccessAuthenticationError, AccessTokenAuthenticator, AuthenticatedPrincipal
from app.services.auth_dependencies import get_jwt_service


def get_access_token_authenticator(database: Session = Depends(get_db)) -> AccessTokenAuthenticator:
    """Compose the read-only validator from the caller-scoped session and JWT service."""

    return AccessTokenAuthenticator(database, get_jwt_service())


def get_current_principal(
    access_cookie: str | None = Cookie(default=None, alias="gearia_access"),
    authenticator: AccessTokenAuthenticator = Depends(get_access_token_authenticator),
) -> AuthenticatedPrincipal:
    """Return a validated principal or the single public protected-route failure."""

    try:
        return authenticator.authenticate(access_cookie)
    except AccessAuthenticationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required") from error
