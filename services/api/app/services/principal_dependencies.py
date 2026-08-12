"""FastAPI dependency boundary for the sole accepted access-token channel."""

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.access_token_authenticator import AccessAuthenticationError, AccessTokenAuthenticator, AuthenticatedPrincipal
from app.services.auth_dependencies import get_jwt_service
from app.services.auth_dependencies import get_csrf_service
from app.repositories.auth_session_repository import AuthSessionRepository
from app.services.csrf_service import CsrfService


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


def require_authenticated_csrf(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    csrf_cookie: str | None = Cookie(default=None, alias="gearia_csrf"),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    database: Session = Depends(get_db),
    csrf_service: CsrfService = Depends(get_csrf_service),
) -> None:
    """Validate the current session-bound CSRF pair without domain authorization."""

    session = AuthSessionRepository(database).get_active_for_principal(principal.session_id, principal.user_id)
    if session is None or not csrf_service.valid_pair(csrf_cookie, csrf_header, session.csrf_secret_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
