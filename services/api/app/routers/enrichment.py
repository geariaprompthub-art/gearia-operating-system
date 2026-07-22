"""Routes for deterministic content enrichment."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.content import EnrichmentRunResult
from app.services.enrichment_service import EnrichmentService

router = APIRouter(prefix="/enrichment", tags=["enrichment"])


@router.post("/run", response_model=EnrichmentRunResult, summary="Enrich pending content")
def run_enrichment(
    limit: int = Query(default=50, ge=1, le=500, description="Maximum pending contents to process."),
    database: Session = Depends(get_db),
) -> EnrichmentRunResult:
    """Process pending content oldest first without retrying processed or failed records."""

    found, processed, failed = EnrichmentService(database).run_pending(limit)
    return EnrichmentRunResult(
        contents_found=found,
        contents_processed=processed,
        contents_failed=failed,
    )
