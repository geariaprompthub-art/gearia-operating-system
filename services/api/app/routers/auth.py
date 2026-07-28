"""Public authentication endpoints implemented incrementally by P1B phases."""

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.schemas.auth import CurrentUserResponse, ErrorResponse, LoginRequest, LoginResponse, LoginUser, RefreshResponse
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.auth_dependencies import get_auth_service
from app.services.principal_dependencies import get_current_principal
from app.services.auth_service import (
    AccountStatusError,
    AuthService,
    InvalidCredentialsError,
    InvalidCsrfError,
    InvalidRefreshError,
    LoginRateLimitedError,
    RefreshError,
    RefreshRateLimitedError,
    RefreshReuseDetectedError,
    InvalidLogoutCsrfError,
    InvalidLogoutSessionError,
    LogoutError,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
        403: {"model": ErrorResponse, "description": "Account cannot sign in"},
        429: {"model": ErrorResponse, "description": "Login rate limited"},
        500: {"model": ErrorResponse, "description": "Authentication unavailable"},
    },
    summary="Create an authenticated browser session",
)
def login(
    payload: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    """Authenticate credentials and emit only centralized browser cookies."""

    try:
        result = service.login(payload.email, payload.password)
    except InvalidCredentialsError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from error
    except AccountStatusError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account cannot sign in") from error
    except LoginRateLimitedError as error:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts") from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication service unavailable") from error

    service.cookie_policy.set_tokens(response, result.access_token, result.refresh_token, result.csrf_token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return LoginResponse(user=LoginUser(id=result.user_id, email=result.email))


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid refresh state"},
        403: {"model": ErrorResponse, "description": "Invalid CSRF token"},
        429: {"model": ErrorResponse, "description": "Refresh rate limited"},
        500: {"model": ErrorResponse, "description": "Authentication unavailable"},
    },
    summary="Rotate the authenticated browser refresh token",
)
def refresh(
    request: Request,
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias="gearia_refresh"),
    csrf_cookie: str | None = Cookie(default=None, alias="gearia_csrf"),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    service: AuthService = Depends(get_auth_service),
) -> RefreshResponse | JSONResponse:
    """Rotate cookies using an opaque HttpOnly refresh token and bound CSRF pair."""

    try:
        result = service.refresh(
            refresh_cookie,
            csrf_cookie,
            csrf_header,
            request.client.host if request.client else "unknown",
        )
    except RefreshRateLimitedError as error:
        return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS, content={"detail": "Too many refresh attempts"}, headers={"Retry-After": str(error.retry_after)})
    except InvalidCsrfError as error:
        return _cleared_refresh_failure(service, status.HTTP_403_FORBIDDEN, "Refresh failed")
    except (InvalidRefreshError, RefreshReuseDetectedError) as error:
        return _cleared_refresh_failure(service, status.HTTP_401_UNAUTHORIZED, "Refresh failed")
    except RefreshError as error:
        return _cleared_refresh_failure(service, status.HTTP_401_UNAUTHORIZED, "Refresh failed")
    except Exception as error:
        return _cleared_refresh_failure(service, status.HTTP_500_INTERNAL_SERVER_ERROR, "Authentication service unavailable")

    service.cookie_policy.set_tokens(response, result.access_token, result.refresh_token, result.csrf_token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return RefreshResponse()


def _cleared_refresh_failure(service: AuthService, status_code: int, detail: str) -> JSONResponse:
    """Build a terminal refresh failure after applying the central clear policy."""

    response = JSONResponse(status_code=status_code, content={"detail": detail})
    service.cookie_policy.clear(response)
    return response


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    responses={401: {"model": ErrorResponse, "description": "Authentication required"}},
    summary="Return the currently authenticated user",
)
def current_user(principal: AuthenticatedPrincipal = Depends(get_current_principal)) -> CurrentUserResponse:
    """Expose a minimal projection of a read-only authenticated principal."""

    return CurrentUserResponse(
        id=principal.user_id,
        email=principal.email,
        status=principal.user_status,
        email_verified_at=principal.email_verified_at,
        created_at=principal.created_at,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Invalid CSRF token"},
        500: {"model": ErrorResponse, "description": "Authentication unavailable"},
    },
    summary="Revoke the current authenticated browser session",
)
def logout(
    response: Response,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    csrf_cookie: str | None = Cookie(default=None, alias="gearia_csrf"),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    service: AuthService = Depends(get_auth_service),
) -> None:
    """Revoke only the authenticated session and clear cookies centrally."""

    try:
        service.logout(principal, csrf_cookie, csrf_header)
    except InvalidLogoutCsrfError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Logout failed") from error
    except (InvalidLogoutSessionError, LogoutError) as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required") from error
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication service unavailable") from error

    service.cookie_policy.clear(response)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
