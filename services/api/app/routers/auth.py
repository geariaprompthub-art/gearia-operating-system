"""Public authentication endpoints implemented incrementally by P1B phases."""

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.schemas.auth import AccountDeletionRequest, CurrentUserResponse, EmailVerificationConfirmRequest, EmailVerificationConfirmResponse, ErrorResponse, LoginRequest, LoginResponse, LoginUser, PasswordResetConfirmRequest, PasswordResetConfirmResponse, PasswordResetRequest, PasswordResetRequestResponse, RefreshResponse, RegisterRequest, RegisterResponse
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.auth_dependencies import get_auth_service
from app.services.auth_dependencies import get_registration_application_service
from app.services.auth_dependencies import get_email_verification_application_service
from app.services.auth_dependencies import get_password_reset_application_service
from app.services.auth_dependencies import get_account_anonymization_application_service
from app.services.auth_dependencies import get_cookie_policy
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
from app.services.registration_application_service import (
    RegistrationApplicationService,
    RegistrationRateLimitedError,
    RegistrationUnavailableError,
)
from app.services.email_verification_application_service import (
    EmailVerificationApplicationService,
    EmailVerificationRateLimitedError,
    EmailVerificationUnavailableError,
)
from app.services.password_reset_application_service import (
    PasswordResetApplicationService,
    PasswordResetRateLimitedError,
    PasswordResetUnavailableError,
)
from app.services.account_anonymization_application_service import (
    AccountAnonymizationApplicationService,
    AccountAnonymizationCsrfError,
    AccountAnonymizationRateLimitedError,
    AccountAnonymizationUnavailableError,
)
from app.services.cookie_policy import CookiePolicy
from app.core.correlation import correlation_context

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        429: {"model": ErrorResponse, "description": "Password-reset rate limited"},
        500: {"model": ErrorResponse, "description": "Password-reset unavailable"},
    },
    summary="Request password reset without revealing account state",
)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    response: Response,
    service: PasswordResetApplicationService = Depends(get_password_reset_application_service),
) -> PasswordResetRequestResponse | JSONResponse:
    """Request an anonymous reset token without creating authentication state."""

    try:
        service.request(
            payload.email,
            request.client.host if request.client else "unknown",
            correlation_context.get(),
        )
    except PasswordResetRateLimitedError as error:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many password reset attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )
    except PasswordResetUnavailableError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Password reset service unavailable"},
            headers={"Cache-Control": "no-store"},
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return PasswordResetRequestResponse()


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetConfirmResponse,
    status_code=status.HTTP_200_OK,
    responses={
        429: {"model": ErrorResponse, "description": "Password-reset rate limited"},
        500: {"model": ErrorResponse, "description": "Password-reset unavailable"},
    },
    summary="Confirm an opaque password-reset token",
)
def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    request: Request,
    response: Response,
    service: PasswordResetApplicationService = Depends(get_password_reset_application_service),
) -> PasswordResetConfirmResponse | JSONResponse:
    """Replace credentials and revoke prior authentication without logging in."""

    try:
        service.confirm(
            payload.token,
            payload.password,
            request.client.host if request.client else "unknown",
        )
    except PasswordResetRateLimitedError as error:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many password reset attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )
    except PasswordResetUnavailableError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Password reset service unavailable"},
            headers={"Cache-Control": "no-store"},
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return PasswordResetConfirmResponse()


@router.post(
    "/email-verification/confirm",
    response_model=EmailVerificationConfirmResponse,
    status_code=status.HTTP_200_OK,
    responses={
        429: {"model": ErrorResponse, "description": "Email verification rate limited"},
        500: {"model": ErrorResponse, "description": "Email verification unavailable"},
    },
    summary="Confirm an opaque email-verification challenge",
)
def confirm_email_verification(
    payload: EmailVerificationConfirmRequest,
    request: Request,
    response: Response,
    service: EmailVerificationApplicationService = Depends(get_email_verification_application_service),
) -> EmailVerificationConfirmResponse | JSONResponse:
    """Confirm anonymously without creating a session, cookies, or access token."""

    try:
        service.confirm(payload.token, request.client.host if request.client else "unknown")
    except EmailVerificationRateLimitedError as error:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many email verification attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )
    except EmailVerificationUnavailableError:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Email verification service unavailable"},
            headers={"Cache-Control": "no-store"},
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return EmailVerificationConfirmResponse()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        429: {"model": ErrorResponse, "description": "Registration rate limited"},
        500: {"model": ErrorResponse, "description": "Registration unavailable"},
    },
    summary="Request account registration without revealing account state",
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    service: RegistrationApplicationService = Depends(get_registration_application_service),
) -> RegisterResponse | JSONResponse:
    """Accept anonymous registration; no CSRF token is needed before a session exists."""

    try:
        service.submit(
            payload.email,
            payload.password,
            request.client.host if request.client else "unknown",
            correlation_context.get(),
        )
    except RegistrationRateLimitedError as error:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many registration attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )
    except RegistrationUnavailableError as error:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Registration service unavailable"},
            headers={"Cache-Control": "no-store"},
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return RegisterResponse()


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


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Invalid CSRF token"},
        422: {"model": ErrorResponse, "description": "Invalid deletion confirmation"},
        429: {"model": ErrorResponse, "description": "Account deletion rate limited"},
        500: {"model": ErrorResponse, "description": "Account deletion unavailable"},
    },
    summary="Irreversibly anonymize the current account and block its workspace",
)
def delete_current_account(
    payload: AccountDeletionRequest,
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    csrf_cookie: str | None = Cookie(default=None, alias="gearia_csrf"),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    service: AccountAnonymizationApplicationService = Depends(
        get_account_anonymization_application_service
    ),
    cookie_policy: CookiePolicy = Depends(get_cookie_policy),
) -> Response:
    """Own no SQL: invoke the application transaction then clear cookies centrally."""

    try:
        service.anonymize_account(
            principal,
            csrf_cookie,
            csrf_header,
            request.client.host if request.client else "unknown",
        )
    except AccountAnonymizationRateLimitedError as error:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many account deletion attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )
    except AccountAnonymizationCsrfError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account deletion failed",
            headers={"Cache-Control": "no-store"},
        ) from error
    except AccountAnonymizationUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account deletion unavailable",
            headers={"Cache-Control": "no-store"},
        ) from error

    cookie_policy.clear(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


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
