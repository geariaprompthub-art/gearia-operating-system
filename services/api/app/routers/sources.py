"""Routes for managing source resources."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.source import Source
from app.schemas.source import SourceCreate, SourceRead, SourceUpdate

router = APIRouter(prefix="/sources", tags=["sources"])


def get_source_or_404(source_id: UUID, database: Session) -> Source:
    """Return a source or raise a consistent not-found response."""

    source = database.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


@router.get("", response_model=list[SourceRead])
def list_sources(database: Session = Depends(get_db)) -> list[Source]:
    """List all sources ordered by creation time."""

    return list(database.scalars(select(Source).order_by(Source.created_at)))


@router.post("", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source(payload: SourceCreate, database: Session = Depends(get_db)) -> Source:
    """Create and persist a source."""

    source = Source(**payload.model_dump())
    database.add(source)
    try:
        database.commit()
    except IntegrityError as error:
        database.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Source name already exists") from error
    database.refresh(source)
    return source


@router.get("/{source_id}", response_model=SourceRead)
def get_source(source_id: UUID, database: Session = Depends(get_db)) -> Source:
    """Get one source by UUID."""

    return get_source_or_404(source_id, database)


@router.put("/{source_id}", response_model=SourceRead)
def update_source(
    source_id: UUID, payload: SourceUpdate, database: Session = Depends(get_db)
) -> Source:
    """Update the fields provided for a source."""

    source = get_source_or_404(source_id, database)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    try:
        database.commit()
    except IntegrityError as error:
        database.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Source name already exists") from error
    database.refresh(source)
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: UUID, database: Session = Depends(get_db)) -> Response:
    """Delete a source by UUID."""

    source = get_source_or_404(source_id, database)
    database.delete(source)
    database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
