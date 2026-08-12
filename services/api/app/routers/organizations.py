"""P3A organizations HTTP boundary: no SQL, ORM, or transaction ownership."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.schemas.auth import ErrorResponse
from app.schemas.organization import MembershipResponse, MembershipUpdateRequest, OrganizationCreateRequest, OrganizationCreateResponse, OrganizationResponse, OrganizationUpdateRequest
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import (
    get_organization_metadata_application_service,
    get_organization_read_application_service,
    get_organization_membership_application_service,
    get_shared_organization_application_service,
    require_organization_create_rate_limit,
    require_organization_update_rate_limit,
    require_membership_revoke_rate_limit,
    require_membership_update_rate_limit,
)
from app.services.organization_http_errors import organization_http_boundary
from app.services.organization_metadata_application_service import OrganizationMetadataApplicationService
from app.services.organization_membership_application_service import MembershipResult, OrganizationMembershipApplicationService
from app.services.organization_read_application_service import OrganizationReadApplicationService
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf
from app.services.shared_organization_application_service import SharedOrganizationApplicationService

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _response(item: object) -> OrganizationResponse:
    return OrganizationResponse(
        id=item.organization_id,
        kind=item.kind,
        name=item.name,
        slug=item.slug,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _membership_response(item: MembershipResult) -> MembershipResponse:
    return MembershipResponse(id=item.membership_id, organization_id=item.organization_id, user_id=item.user_id, role=item.role, created_at=item.created_at)


@router.get("", response_model=list[OrganizationResponse], responses={401: {"model": ErrorResponse}})
def list_organizations(response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationReadApplicationService = Depends(get_organization_read_application_service)) -> list[OrganizationResponse]:
    items = organization_http_boundary(lambda: service.list_accessible(principal.user_id))
    _private(response)
    return [_response(item) for item in items]


@router.post("", response_model=OrganizationCreateResponse, status_code=status.HTTP_201_CREATED, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def create_organization(
    payload: OrganizationCreateRequest,
    response: Response,
    _: None = Depends(require_authenticated_csrf),
    __: None = Depends(require_organization_create_rate_limit),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    service: SharedOrganizationApplicationService = Depends(get_shared_organization_application_service),
) -> OrganizationCreateResponse:
    result = organization_http_boundary(lambda: service.create(principal.user_id, payload.name, payload.slug))
    _private(response)
    return OrganizationCreateResponse(
        organization=OrganizationResponse(id=result.organization_id, kind="shared", name=result.name, slug=result.slug, status="active", created_at=result.created_at, updated_at=result.updated_at),
        owner_membership_id=result.owner_membership_id,
        initial_workspace_id=result.workspace_id,
    )


@router.get("/{organization_id}", response_model=OrganizationResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def get_organization(organization_id: UUID, response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationReadApplicationService = Depends(get_organization_read_application_service)) -> OrganizationResponse:
    item = organization_http_boundary(lambda: service.get_accessible(principal.user_id, organization_id))
    _private(response)
    return _response(item)


@router.patch("/{organization_id}", response_model=OrganizationResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def update_organization(
    organization_id: UUID,
    payload: OrganizationUpdateRequest,
    response: Response,
    _: None = Depends(require_authenticated_csrf),
    __: None = Depends(require_organization_update_rate_limit),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    service: OrganizationMetadataApplicationService = Depends(get_organization_metadata_application_service),
) -> OrganizationResponse:
    item = organization_http_boundary(lambda: service.update_name(principal.user_id, organization_id, payload.name))
    _private(response)
    return _response(item)


@router.get("/{organization_id}/memberships", response_model=list[MembershipResponse], responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
def list_memberships(organization_id: UUID, response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationMembershipApplicationService = Depends(get_organization_membership_application_service)) -> list[MembershipResponse]:
    """List only active memberships after service-owned context/policy resolution."""

    items = organization_http_boundary(lambda: service.list_active(principal.user_id, organization_id))
    _private(response)
    return [_membership_response(item) for item in items]


@router.patch("/{organization_id}/memberships/{membership_id}", response_model=MembershipResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def update_membership(organization_id: UUID, membership_id: UUID, payload: MembershipUpdateRequest, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_membership_update_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationMembershipApplicationService = Depends(get_organization_membership_application_service)) -> MembershipResponse:
    """Delegate every transition and last-owner invariant to the lifecycle service."""

    item = organization_http_boundary(lambda: service.change_role(principal.user_id, organization_id, membership_id, payload.role))
    _private(response)
    return _membership_response(item)


@router.delete("/{organization_id}/memberships/{membership_id}", status_code=status.HTTP_204_NO_CONTENT, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def revoke_membership(organization_id: UUID, membership_id: UUID, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_membership_revoke_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationMembershipApplicationService = Depends(get_organization_membership_application_service)) -> Response:
    """Soft-revoke through the lifecycle service; no physical deletion occurs here."""

    organization_http_boundary(lambda: service.revoke(principal.user_id, organization_id, membership_id))
    response.status_code = status.HTTP_204_NO_CONTENT
    _private(response)
    return response
