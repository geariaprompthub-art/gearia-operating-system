"""Unit contracts for explicit workspace execution boundaries."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.workspace_context import WorkspaceContext, WorkspaceExecutionContext


def test_workspace_context_is_an_explicit_immutable_private_boundary() -> None:
    """Private repository callers must carry both workspace and authenticated user IDs."""

    context = WorkspaceContext(workspace_id=uuid4(), user_id=uuid4())

    assert context.workspace_id
    assert context.user_id
    with pytest.raises(Exception):
        context.workspace_id = uuid4()  # type: ignore[misc]


def test_workspace_execution_context_requires_canonical_metadata() -> None:
    """Background work cannot execute with implicit tenancy or an ambiguous trigger."""

    context = WorkspaceExecutionContext.now(
        workspace_id=uuid4(),
        correlation_id=uuid4(),
        actor_type="system",
        actor_id=None,
        trigger="visibility_rebuild",
    )

    assert context.timestamp.tzinfo is not None
    with pytest.raises(ValueError):
        WorkspaceExecutionContext(
            workspace_id=uuid4(),
            correlation_id=uuid4(),
            actor_type=" user ",
            actor_id=uuid4(),
            trigger="visibility_rebuild",
            timestamp=datetime.now(UTC),
        )
    with pytest.raises(ValueError):
        WorkspaceExecutionContext(
            workspace_id=uuid4(),
            correlation_id=uuid4(),
            actor_type="system",
            actor_id=None,
            trigger="",
            timestamp=datetime.now(UTC),
        )
    with pytest.raises(ValueError):
        WorkspaceExecutionContext(
            workspace_id=uuid4(),
            correlation_id=uuid4(),
            actor_type="system",
            actor_id=None,
            trigger="visibility_rebuild",
            timestamp=datetime.now(),
        )
