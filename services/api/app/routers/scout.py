"""Routes that execute the Scout ingestion pipeline."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.content import ScoutRunResult
from app.services.scout import ScoutService

router = APIRouter(prefix="/scout", tags=["scout"])


@router.post("/run", response_model=ScoutRunResult)
def run_scout(database: Session = Depends(get_db)) -> ScoutRunResult:
    """Run one RSS ingestion cycle for eligible sources."""

    sources_processed, contents_created = ScoutService(database).run()
    return ScoutRunResult(
        sources_processed=sources_processed,
        contents_created=contents_created,
    )
