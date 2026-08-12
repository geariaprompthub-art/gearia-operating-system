"""Thin organization-workspace HTTP boundary."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.schemas.auth import ErrorResponse
from app.schemas.workspace import OrganizationWorkspaceCreate, OrganizationWorkspaceRead
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import get_organization_workspace_application_service, require_organization_workspace_create_rate_limit
from app.services.organization_http_errors import organization_http_boundary
from app.services.organization_workspace_application_service import OrganizationWorkspaceApplicationService, OrganizationWorkspaceRead as Read
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf

router = APIRouter(prefix="/organizations", tags=["organization workspaces"])


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _response(item: Read) -> OrganizationWorkspaceRead:
    return OrganizationWorkspaceRead(id=item.workspace_id, organization_id=item.organization_id, name=item.name, status=item.status)


@router.get("/{organization_id}/workspaces", response_model=list[OrganizationWorkspaceRead], responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
def list_workspaces(organization_id: UUID, response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationWorkspaceApplicationService = Depends(get_organization_workspace_application_service)) -> list[OrganizationWorkspaceRead]:
    _private(response)
    return [_response(item) for item in organization_http_boundary(lambda: service.list_accessible(principal.user_id, organization_id))]


@router.post("/{organization_id}/workspaces", response_model=OrganizationWorkspaceRead, status_code=status.HTTP_201_CREATED, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}})
def create_workspace(organization_id: UUID, payload: OrganizationWorkspaceCreate, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_organization_workspace_create_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationWorkspaceApplicationService = Depends(get_organization_workspace_application_service)) -> OrganizationWorkspaceRead:
    _private(response)
    return _response(organization_http_boundary(lambda: service.create_shared(principal.user_id, organization_id, payload.name)))


@router.get("/{organization_id}/workspaces/{workspace_id}", response_model=OrganizationWorkspaceRead, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
def get_workspace(organization_id: UUID, workspace_id: UUID, response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationWorkspaceApplicationService = Depends(get_organization_workspace_application_service)) -> OrganizationWorkspaceRead:
    _private(response)
    return _response(organization_http_boundary(lambda: service.get_accessible(principal.user_id, organization_id, workspace_id)))
