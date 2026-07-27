import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import configure_logging, get_structured_logger
from app.middleware.request_correlation import install_request_correlation
from app.core.structured_logging import SafeStructuredLogger
from app.routers.contents import router as contents_router
from app.routers.enrichment import router as enrichment_router
from app.routers.embeddings import router as embeddings_router
from app.routers.relationships import router as relationships_router
from app.routers.search import router as search_router
from app.routers.scout import router as scout_router
from app.routers.sources import router as sources_router
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
def create_app(*, structured_logger: SafeStructuredLogger | None = None) -> FastAPI:
    """Compose an isolated FastAPI application with the production defaults."""

    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    if settings.structured_logging_enabled:
        install_request_correlation(
            application,
            structured_logger or get_structured_logger("gearia.http"),
        )

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
    application.include_router(sources_router)
    application.include_router(contents_router)
    application.include_router(enrichment_router)
    application.include_router(embeddings_router)
    application.include_router(relationships_router)
    application.include_router(search_router)
    application.include_router(scout_router)

    @application.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
