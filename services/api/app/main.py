import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.routers.contents import router as contents_router
from app.routers.enrichment import router as enrichment_router
from app.routers.embeddings import router as embeddings_router
from app.routers.relationships import router as relationships_router
from app.routers.search import router as search_router
from app.routers.scout import router as scout_router
from app.routers.sources import router as sources_router

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting %s in %s", settings.app_name, settings.environment)
    yield
    logger.info("Stopping %s", settings.app_name)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
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
