import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import configure_logging, get_structured_logger
from app.core import health
from app.middleware.request_correlation import install_request_correlation
from app.middleware.security_headers import install_security_headers
from app.core.structured_logging import SafeStructuredLogger
from app.routers.contents import router as contents_router
from app.routers.enrichment import router as enrichment_router
from app.routers.embeddings import router as embeddings_router
from app.routers.relationships import router as relationships_router
from app.routers.search import router as search_router
from app.routers.scout import router as scout_router
from app.routers.sources import router as sources_router
from app.routers.auth import router as auth_router
from app.routers.workspaces import router as workspaces_router
from app.routers.organizations import router as organizations_router
from app.routers.organization_invitations import accept_router as organization_invitation_accept_router, organization_router as organization_invitation_router
from app.routers.organization_workspaces import router as organization_workspaces_router
from app.services.hybrid_reranking_pipeline import RerankingPipelineHydrationError
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting %s in %s", settings.app_name, settings.environment)
    yield
    logger.info("Stopping %s", settings.app_name)


def _reranking_error_response(_: Request, __: Exception, status_code: int, detail: str) -> JSONResponse:
    """Return the stable public representation for a sanitized reranking failure."""

    return JSONResponse(status_code=status_code, content={"detail": detail})


async def handle_reranking_configuration_error(
    request: Request, error: RerankingProviderConfigurationError
) -> JSONResponse:
    """Hide provider configuration details from public responses."""

    return _reranking_error_response(request, error, 500, "Reranking service is not configured")


async def handle_reranking_unavailable_error(
    request: Request, error: RerankingProviderUnavailableError
) -> JSONResponse:
    """Map a temporary provider outage to the public service-unavailable contract."""

    return _reranking_error_response(request, error, 503, "Reranking service is temporarily unavailable")


async def handle_reranking_response_error(
    request: Request, error: RerankingProviderResponseError
) -> JSONResponse:
    """Map a malformed provider response without exposing its contents."""

    return _reranking_error_response(request, error, 502, "Reranking service returned an invalid response")


async def handle_reranking_hydration_error(
    request: Request, error: RerankingPipelineHydrationError
) -> JSONResponse:
    """Map invalid read-model hydration to the stable public error contract."""

    return _reranking_error_response(request, error, 500, "Search result hydration failed")


async def handle_request_validation_error(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    """Sanitize only anonymous registration validation, which can include a password."""

    if request.url.path == "/auth/register":
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid registration request"},
            headers={"Cache-Control": "no-store"},
        )
    if request.url.path == "/auth/email-verification/confirm":
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid email verification request"},
            headers={"Cache-Control": "no-store"},
        )
    if request.url.path in {"/auth/password-reset/request", "/auth/password-reset/confirm"}:
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid password reset request"},
            headers={"Cache-Control": "no-store"},
        )
    if request.url.path == "/auth/me" and request.method == "DELETE":
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid account deletion request"},
            headers={"Cache-Control": "no-store"},
        )
    return await request_validation_exception_handler(request, error)
def create_app(*, structured_logger: SafeStructuredLogger | None = None) -> FastAPI:
    """Compose an isolated FastAPI application with the production defaults."""

    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        debug=settings.debug,
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
    )
    if settings.structured_logging_enabled:
        install_request_correlation(
            application,
            structured_logger or get_structured_logger("gearia.http"),
        )
    install_security_headers(application)

    application.add_exception_handler(
        RerankingProviderConfigurationError,
        handle_reranking_configuration_error,
    )
    application.add_exception_handler(
        RerankingProviderUnavailableError,
        handle_reranking_unavailable_error,
    )
    application.add_exception_handler(
        RerankingProviderResponseError,
        handle_reranking_response_error,
    )
    application.add_exception_handler(
        RerankingPipelineHydrationError,
        handle_reranking_hydration_error,
    )
    application.add_exception_handler(RequestValidationError, handle_request_validation_error)
    application.include_router(sources_router)
    application.include_router(contents_router)
    application.include_router(enrichment_router)
    application.include_router(embeddings_router)
    application.include_router(relationships_router)
    application.include_router(search_router)
    application.include_router(scout_router)
    application.include_router(auth_router)
    application.include_router(workspaces_router)
    application.include_router(organizations_router)
    application.include_router(organization_invitation_router)
    application.include_router(organization_invitation_accept_router)
    application.include_router(organization_workspaces_router)

    @application.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        """Report process liveness without touching infrastructure dependencies."""

        return {"status": "alive"}

    @application.get("/health/ready", tags=["health"])
    async def health_ready() -> JSONResponse:
        """Report readiness based solely on mandatory local dependencies."""

        dependencies = {"postgres": "ok" if health.check_postgres(settings) else "unavailable"}
        if settings.redis_required:
            dependencies["redis"] = "ok" if health.check_redis(settings) else "unavailable"
        ready = all(value == "ok" for value in dependencies.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "dependencies": dependencies},
        )

    return application


app = create_app()
