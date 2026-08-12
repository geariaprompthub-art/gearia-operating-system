"""Composition and HTTP rate-limit dependencies for the P3A organizations API."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_structured_logger
from app.db import get_db
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.auth_dependencies import get_rate_limiter
from app.services.organization_context_resolver import OrganizationContextResolver
from app.services.organization_metadata_application_service import OrganizationMetadataApplicationService
from app.services.organization_membership_application_service import OrganizationMembershipApplicationService
from app.services.organization_invitation_application_service import OrganizationInvitationApplicationService
from app.services.organization_invitation_read_application_service import OrganizationInvitationReadApplicationService
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.schemas.organization import InvitationCreateRequest
from app.services.organization_read_application_service import OrganizationReadApplicationService
from app.services.principal_dependencies import get_current_principal
from app.services.rate_limiter import RateLimitPolicy, RedisRateLimiter
from app.services.shared_organization_application_service import SharedOrganizationApplicationService
from app.services.organization_workspace_application_service import OrganizationWorkspaceApplicationService


@dataclass(frozen=True)
class OrganizationRateLimitPolicies:
    create_ip: RateLimitPolicy
    create_user: RateLimitPolicy
    update_ip: RateLimitPolicy
    update_user: RateLimitPolicy
    update_organization: RateLimitPolicy
    membership_update_ip: RateLimitPolicy
    membership_update_user: RateLimitPolicy
    membership_update_organization: RateLimitPolicy
    membership_revoke_ip: RateLimitPolicy
    membership_revoke_user: RateLimitPolicy
    membership_revoke_organization: RateLimitPolicy
    invitation_issue_ip: RateLimitPolicy
    invitation_issue_user: RateLimitPolicy
    invitation_issue_organization: RateLimitPolicy
    invitation_issue_email: RateLimitPolicy
    invitation_revoke_ip: RateLimitPolicy
    invitation_revoke_user: RateLimitPolicy
    invitation_revoke_organization: RateLimitPolicy
    invitation_accept_ip: RateLimitPolicy
    invitation_accept_user: RateLimitPolicy


def get_organization_rate_limit_policies() -> OrganizationRateLimitPolicies:
    settings = get_settings(); window = settings.auth_rate_limit_window_seconds
    return OrganizationRateLimitPolicies(
        RateLimitPolicy("organization:create:ip", settings.organization_create_ip_limit, window),
        RateLimitPolicy("organization:create:user", settings.organization_create_user_limit, window),
        RateLimitPolicy("organization:update:ip", settings.organization_update_ip_limit, window),
        RateLimitPolicy("organization:update:user", settings.organization_update_user_limit, window),
        RateLimitPolicy("organization:update:organization", settings.organization_update_organization_limit, window),
        RateLimitPolicy("organization:membership:update:ip", settings.organization_membership_update_ip_limit, window),
        RateLimitPolicy("organization:membership:update:user", settings.organization_membership_update_user_limit, window),
        RateLimitPolicy("organization:membership:update:organization", settings.organization_membership_update_organization_limit, window),
        RateLimitPolicy("organization:membership:revoke:ip", settings.organization_membership_revoke_ip_limit, window),
        RateLimitPolicy("organization:membership:revoke:user", settings.organization_membership_revoke_user_limit, window),
        RateLimitPolicy("organization:membership:revoke:organization", settings.organization_membership_revoke_organization_limit, window),
        RateLimitPolicy("organization:invitation:issue:ip", settings.organization_invitation_issue_ip_limit, window),
        RateLimitPolicy("organization:invitation:issue:user", settings.organization_invitation_issue_user_limit, window),
        RateLimitPolicy("organization:invitation:issue:organization", settings.organization_invitation_issue_organization_limit, window),
        RateLimitPolicy("organization:invitation:issue:email", settings.organization_invitation_issue_email_limit, window),
        RateLimitPolicy("organization:invitation:revoke:ip", settings.organization_invitation_revoke_ip_limit, window),
        RateLimitPolicy("organization:invitation:revoke:user", settings.organization_invitation_revoke_user_limit, window),
        RateLimitPolicy("organization:invitation:revoke:organization", settings.organization_invitation_revoke_organization_limit, window),
        RateLimitPolicy("organization:invitation:accept:ip", settings.organization_invitation_accept_ip_limit, window),
        RateLimitPolicy("organization:invitation:accept:user", settings.organization_invitation_accept_user_limit, window),
    )


def _consume(limiter: RedisRateLimiter, policy: RateLimitPolicy, identifier: str) -> None:
    decision = limiter.consume(policy, identifier)
    if not decision.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many organization requests", headers={"Retry-After": str(decision.retry_after), "Cache-Control": "no-store"})


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_organization_create_rate_limit(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    limiter: RedisRateLimiter = Depends(get_rate_limiter),
    policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies),
) -> None:
    _consume(limiter, policies.create_ip, _ip(request))
    _consume(limiter, policies.create_user, str(principal.user_id))


def require_organization_update_rate_limit(
    organization_id: UUID,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    limiter: RedisRateLimiter = Depends(get_rate_limiter),
    policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies),
) -> None:
    _consume(limiter, policies.update_ip, _ip(request))
    _consume(limiter, policies.update_user, str(principal.user_id))
    _consume(limiter, policies.update_organization, str(organization_id))


def require_membership_update_rate_limit(organization_id: UUID, request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter), policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies)) -> None:
    _consume(limiter, policies.membership_update_ip, _ip(request)); _consume(limiter, policies.membership_update_user, str(principal.user_id)); _consume(limiter, policies.membership_update_organization, str(organization_id))


def require_membership_revoke_rate_limit(organization_id: UUID, request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter), policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies)) -> None:
    _consume(limiter, policies.membership_revoke_ip, _ip(request)); _consume(limiter, policies.membership_revoke_user, str(principal.user_id)); _consume(limiter, policies.membership_revoke_organization, str(organization_id))


def require_invitation_issue_rate_limit(organization_id: UUID, payload: InvitationCreateRequest, request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter), policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies)) -> None:
    _consume(limiter, policies.invitation_issue_ip, _ip(request)); _consume(limiter, policies.invitation_issue_user, str(principal.user_id)); _consume(limiter, policies.invitation_issue_organization, str(organization_id)); _consume(limiter, policies.invitation_issue_email, payload.email)


def require_invitation_revoke_rate_limit(organization_id: UUID, request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter), policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies)) -> None:
    _consume(limiter, policies.invitation_revoke_ip, _ip(request)); _consume(limiter, policies.invitation_revoke_user, str(principal.user_id)); _consume(limiter, policies.invitation_revoke_organization, str(organization_id))


def require_invitation_accept_rate_limit(request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter), policies: OrganizationRateLimitPolicies = Depends(get_organization_rate_limit_policies)) -> None:
    _consume(limiter, policies.invitation_accept_ip, _ip(request)); _consume(limiter, policies.invitation_accept_user, str(principal.user_id))


def require_organization_workspace_create_rate_limit(organization_id: UUID, request: Request, principal: AuthenticatedPrincipal = Depends(get_current_principal), limiter: RedisRateLimiter = Depends(get_rate_limiter)) -> None:
    """Apply independent, non-sensitive limits to shared-workspace creation."""

    settings = get_settings()
    window = settings.auth_rate_limit_window_seconds
    _consume(limiter, RateLimitPolicy("organization:workspace:create:ip", settings.organization_workspace_create_ip_limit, window), _ip(request))
    _consume(limiter, RateLimitPolicy("organization:workspace:create:user", settings.organization_workspace_create_user_limit, window), str(principal.user_id))
    _consume(limiter, RateLimitPolicy("organization:workspace:create:organization", settings.organization_workspace_create_organization_limit, window), str(organization_id))


def _resolver(database: Session) -> OrganizationContextResolver:
    return OrganizationContextResolver(OrganizationRepository(database), OrganizationMembershipRepository(database))


def get_shared_organization_application_service(database: Session = Depends(get_db)) -> SharedOrganizationApplicationService:
    organizations = OrganizationRepository(database); memberships = OrganizationMembershipRepository(database)
    return SharedOrganizationApplicationService(database, UserRepository(database), organizations, memberships, WorkspaceRepository(database), OrganizationContextResolver(organizations, memberships), get_structured_logger("gearia.organizations"))


def get_organization_read_application_service(database: Session = Depends(get_db)) -> OrganizationReadApplicationService:
    return OrganizationReadApplicationService(OrganizationRepository(database), _resolver(database))


def get_organization_metadata_application_service(database: Session = Depends(get_db)) -> OrganizationMetadataApplicationService:
    organizations = OrganizationRepository(database)
    return OrganizationMetadataApplicationService(database, organizations, _resolver(database), structured_logger=get_structured_logger("gearia.organizations"))


def get_organization_membership_application_service(database: Session = Depends(get_db)) -> OrganizationMembershipApplicationService:
    organizations = OrganizationRepository(database); memberships = OrganizationMembershipRepository(database)
    return OrganizationMembershipApplicationService(database, organizations, memberships, OrganizationContextResolver(organizations, memberships), structured_logger=get_structured_logger("gearia.organizations"))


def get_organization_invitation_application_service(database: Session = Depends(get_db)) -> OrganizationInvitationApplicationService:
    pepper = get_settings().lifecycle_token_pepper
    if not pepper:
        raise RuntimeError("organization invitation service is not configured")
    return OrganizationInvitationApplicationService(database, pepper, FakeEmailDeliveryAdapter(), structured_logger=get_structured_logger("gearia.organizations"))


def get_organization_invitation_read_application_service(database: Session = Depends(get_db)) -> OrganizationInvitationReadApplicationService:
    from app.repositories.organization_invitation_repository import OrganizationInvitationRepository
    return OrganizationInvitationReadApplicationService(OrganizationInvitationRepository(database), _resolver(database))


def get_organization_workspace_application_service(database: Session = Depends(get_db)) -> OrganizationWorkspaceApplicationService:
    organizations = OrganizationRepository(database)
    memberships = OrganizationMembershipRepository(database)
    return OrganizationWorkspaceApplicationService(database, WorkspaceRepository(database), OrganizationContextResolver(organizations, memberships), get_structured_logger("gearia.organizations"))
