"""Reusable, non-router composition root for P1B authentication internals."""

from functools import lru_cache

import redis
from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.auth_service import AuthService
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.identity_service import IdentityService
from app.services.jwt_service import JWTService
from app.services.password_hasher import PasswordHashingService
from app.services.rate_limiter import RateLimitPolicy, RedisRateLimiter
from app.services.refresh_token_service import RefreshTokenService
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.registration_application_service import RegistrationApplicationService
from app.services.registration_service import RegistrationService
from app.services.email_verification_application_service import EmailVerificationApplicationService
from app.services.password_reset_application_service import PasswordResetApplicationService
from app.services.account_anonymization_application_service import AccountAnonymizationApplicationService
from app.services.workspace_service import WorkspaceService
from app.core.logging import get_structured_logger


@lru_cache
def get_password_hasher() -> PasswordHashingService:
    return PasswordHashingService()


@lru_cache
def get_jwt_service() -> JWTService:
    settings = get_settings()
    if not settings.auth_jwt_private_key or not settings.auth_jwt_public_key or not settings.auth_jwt_kid:
        raise RuntimeError("authentication signing keys are not configured")
    return JWTService(settings.auth_jwt_private_key, settings.auth_jwt_public_key, settings.auth_jwt_kid, settings.auth_jwt_issuer, settings.auth_jwt_audience, settings.auth_access_ttl_seconds, settings.auth_clock_skew_seconds)


@lru_cache
def get_refresh_token_service() -> RefreshTokenService:
    return RefreshTokenService()


@lru_cache
def get_csrf_service() -> CsrfService:
    return CsrfService()


def get_lifecycle_token_service(database: Session = Depends(get_db)) -> LifecycleTokenService:
    """Compose P2B opaque lifecycle challenges from a configured dedicated pepper."""

    pepper = get_settings().lifecycle_token_pepper
    if not pepper:
        raise RuntimeError("lifecycle token service is not configured")
    return LifecycleTokenService(database, pepper)


@lru_cache
def get_cookie_policy() -> CookiePolicy:
    settings = get_settings()
    return CookiePolicy(settings.auth_cookie_secure, settings.auth_cookie_samesite, settings.auth_cookie_domain, settings.auth_access_ttl_seconds, settings.auth_refresh_ttl_seconds)


@lru_cache
def get_redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def get_rate_limiter() -> RedisRateLimiter:
    return RedisRateLimiter(get_redis_client())


def get_auth_service(database: Session = Depends(get_db)) -> AuthService:
    """Build the future public auth orchestrator from explicit dependencies."""

    settings = get_settings()
    return AuthService(
        database,
        IdentityService(database, get_password_hasher()),
        get_jwt_service(),
        get_refresh_token_service(),
        get_cookie_policy(),
        get_csrf_service(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        get_rate_limiter(),
        RateLimitPolicy("auth:login", settings.auth_login_limit, settings.auth_rate_limit_window_seconds),
        settings.auth_refresh_ttl_seconds,
        RateLimitPolicy("auth:refresh", settings.auth_refresh_limit, settings.auth_rate_limit_window_seconds),
        settings.auth_access_ttl_seconds,
    )


@lru_cache
def get_email_delivery_adapter() -> FakeEmailDeliveryAdapter:
    """Use a no-network adapter until a later sprint authorizes a provider."""

    return FakeEmailDeliveryAdapter()


def get_registration_application_service(
    database: Session = Depends(get_db),
) -> RegistrationApplicationService:
    """Compose the public flow while leaving transaction ownership internal."""

    settings = get_settings()
    registration_service = RegistrationService(
        database,
        IdentityService(database, get_password_hasher()),
        WorkspaceService(database),
        get_lifecycle_token_service(database),
    )
    return RegistrationApplicationService(
        registration_service,
        get_rate_limiter(),
        RateLimitPolicy(
            "auth:register:ip",
            settings.auth_register_ip_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:register:email",
            settings.auth_register_email_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        get_email_delivery_adapter(),
        get_structured_logger("gearia.registration"),
    )


def get_email_verification_application_service(
    database: Session = Depends(get_db),
) -> EmailVerificationApplicationService:
    """Compose public verification with a request-owned database session."""

    settings = get_settings()
    return EmailVerificationApplicationService(
        database,
        get_lifecycle_token_service(database),
        get_rate_limiter(),
        RateLimitPolicy(
            "auth:verify:ip",
            settings.auth_verify_ip_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:verify:token",
            settings.auth_verify_token_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        get_structured_logger("gearia.email_verification"),
    )


def get_password_reset_application_service(
    database: Session = Depends(get_db),
) -> PasswordResetApplicationService:
    """Compose password reset from transaction-neutral P2B services and repositories."""

    settings = get_settings()
    return PasswordResetApplicationService(
        database,
        get_lifecycle_token_service(database),
        get_password_hasher(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        get_rate_limiter(),
        RateLimitPolicy(
            "auth:password-reset:request:ip",
            settings.auth_password_reset_request_ip_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:password-reset:request:email",
            settings.auth_password_reset_request_email_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:password-reset:confirm:ip",
            settings.auth_password_reset_confirm_ip_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:password-reset:confirm:token",
            settings.auth_password_reset_confirm_token_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        get_email_delivery_adapter(),
        structured_logger=get_structured_logger("gearia.password_reset"),
    )


def get_account_anonymization_application_service(
    database: Session = Depends(get_db),
) -> AccountAnonymizationApplicationService:
    """Compose authenticated account closure without exposing persistence to routers."""

    settings = get_settings()
    return AccountAnonymizationApplicationService(
        database,
        get_csrf_service(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        get_rate_limiter(),
        RateLimitPolicy(
            "auth:account-anonymization:ip",
            settings.auth_account_anonymization_ip_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        RateLimitPolicy(
            "auth:account-anonymization:user",
            settings.auth_account_anonymization_user_limit,
            settings.auth_rate_limit_window_seconds,
        ),
        get_structured_logger("gearia.account_anonymization"),
    )
