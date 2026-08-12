"""P3A invitation HTTP boundary, deliberately free of token or ORM handling."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.schemas.auth import ErrorResponse
from app.schemas.organization import InvitationAcceptRequest, InvitationAcceptResponse, InvitationCreateRequest, InvitationIssueResponse, InvitationResponse
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import (
    get_organization_invitation_application_service,
    get_organization_invitation_read_application_service,
    require_invitation_accept_rate_limit,
    require_invitation_issue_rate_limit,
    require_invitation_revoke_rate_limit,
)
from app.services.organization_http_errors import organization_http_boundary
from app.services.organization_invitation_application_service import OrganizationInvitationApplicationService
from app.services.organization_invitation_read_application_service import OrganizationInvitationRead, OrganizationInvitationReadApplicationService
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf
from app.core.correlation import correlation_context

organization_router = APIRouter(prefix="/organizations", tags=["organization invitations"])
accept_router = APIRouter(prefix="/organization-invitations", tags=["organization invitations"])


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"; response.headers["Pragma"] = "no-cache"


def _response(item: OrganizationInvitationRead) -> InvitationResponse:
    return InvitationResponse(id=item.invitation_id, organization_id=item.organization_id, role=item.role, expires_at=item.expires_at, created_at=item.created_at)


@organization_router.get("/{organization_id}/invitations", response_model=list[InvitationResponse], responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
def list_invitations(organization_id: UUID, response: Response, principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationInvitationReadApplicationService = Depends(get_organization_invitation_read_application_service)) -> list[InvitationResponse]:
    items = organization_http_boundary(lambda: service.list_active(principal.user_id, organization_id))
    _private(response); return [_response(item) for item in items]


@organization_router.post("/{organization_id}/invitations", response_model=InvitationIssueResponse, status_code=status.HTTP_202_ACCEPTED, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def issue_invitation(organization_id: UUID, payload: InvitationCreateRequest, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_invitation_issue_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationInvitationApplicationService = Depends(get_organization_invitation_application_service)) -> InvitationIssueResponse:
    organization_http_boundary(lambda: service.issue(principal.user_id, organization_id, payload.email, payload.role, correlation_context.get()))
    _private(response); return InvitationIssueResponse()


@organization_router.delete("/{organization_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def revoke_invitation(organization_id: UUID, invitation_id: UUID, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_invitation_revoke_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationInvitationApplicationService = Depends(get_organization_invitation_application_service)) -> Response:
    organization_http_boundary(lambda: service.revoke(principal.user_id, organization_id, invitation_id))
    response.status_code = status.HTTP_204_NO_CONTENT; _private(response); return response


@accept_router.post("/accept", response_model=InvitationAcceptResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
def accept_invitation(payload: InvitationAcceptRequest, response: Response, _: None = Depends(require_authenticated_csrf), __: None = Depends(require_invitation_accept_rate_limit), principal: AuthenticatedPrincipal = Depends(get_current_principal), service: OrganizationInvitationApplicationService = Depends(get_organization_invitation_application_service)) -> InvitationAcceptResponse:
    organization_http_boundary(lambda: service.accept(principal.user_id, payload.token))
    _private(response); return InvitationAcceptResponse()
