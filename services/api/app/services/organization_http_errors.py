"""Sanitized HTTP translation for existing organization application services."""

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException, status

from app.services.organization_metadata_application_service import OrganizationMetadataError
from app.services.organization_membership_application_service import LastOrganizationOwnerError, OrganizationMembershipLifecycleError
from app.services.organization_invitation_application_service import OrganizationInvitationAlreadyMemberError, OrganizationInvitationError
from app.services.organization_read_application_service import OrganizationReadUnavailableError
from app.services.shared_organization_application_service import SharedOrganizationError, SharedOrganizationSlugConflictError
from app.services.organization_workspace_application_service import OrganizationWorkspaceError

Result = TypeVar("Result")
_PRIVATE_HEADERS = {"Cache-Control": "no-store"}


def organization_http_boundary(operation: Callable[[], Result]) -> Result:
    try:
        return operation()
    except OrganizationInvitationAlreadyMemberError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "Organization invitation cannot be processed", headers=_PRIVATE_HEADERS) from error
    except OrganizationWorkspaceError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization workspace access denied", headers=_PRIVATE_HEADERS) from error
    except OrganizationInvitationError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization invitation access denied", headers=_PRIVATE_HEADERS) from error
    except LastOrganizationOwnerError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "Organization owner requirement prevents this change", headers=_PRIVATE_HEADERS) from error
    except OrganizationMembershipLifecycleError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization membership access denied", headers=_PRIVATE_HEADERS) from error
    except SharedOrganizationSlugConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "Organization slug is unavailable", headers=_PRIVATE_HEADERS) from error
    except SharedOrganizationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid organization", headers=_PRIVATE_HEADERS) from error
    except OrganizationReadUnavailableError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found", headers=_PRIVATE_HEADERS) from error
    except OrganizationMetadataError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization access denied", headers=_PRIVATE_HEADERS) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Organization service unavailable", headers=_PRIVATE_HEADERS) from error
