import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import configure_logging
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


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def _reranking_error_response(_: Request, __: Exception, status_code: int, detail: str) -> JSONResponse:
    """Return the stable public representation for a sanitized reranking failure."""

    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.exception_handler(RerankingProviderConfigurationError)
async def handle_reranking_configuration_error(
    request: Request, error: RerankingProviderConfigurationError
) -> JSONResponse:
    """Hide provider configuration details from public responses."""

    return _reranking_error_response(request, error, 500, "Reranking service is not configured")


@app.exception_handler(RerankingProviderUnavailableError)
async def handle_reranking_unavailable_error(
    request: Request, error: RerankingProviderUnavailableError
) -> JSONResponse:
    """Map a temporary provider outage to the public service-unavailable contract."""

    return _reranking_error_response(request, error, 503, "Reranking service is temporarily unavailable")


@app.exception_handler(RerankingProviderResponseError)
async def handle_reranking_response_error(
    request: Request, error: RerankingProviderResponseError
) -> JSONResponse:
    """Map a malformed provider response without exposing its contents."""

    return _reranking_error_response(request, error, 502, "Reranking service returned an invalid response")


@app.exception_handler(RerankingPipelineHydrationError)
async def handle_reranking_hydration_error(
    request: Request, error: RerankingPipelineHydrationError
) -> JSONResponse:
    """Map invalid read-model hydration to the stable public error contract."""

    return _reranking_error_response(request, error, 500, "Search result hydration failed")
app.include_router(sources_router)
app.include_router(contents_router)
app.include_router(enrichment_router)
app.include_router(embeddings_router)
app.include_router(relationships_router)
app.include_router(search_router)
app.include_router(scout_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
