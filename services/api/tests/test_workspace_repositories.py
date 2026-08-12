"""PostgreSQL contracts for P2A tenant-scoped repositories and projection services."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from app.db import SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.models.user import User
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.repositories.workspace_source_repository import WorkspaceContextRequiredError, WorkspaceSourceRepository
from app.services.workspace_context import WorkspaceContext, WorkspaceExecutionContext
from app.services.workspace_service import WorkspaceService, WorkspaceSourceService
from app.services.workspace_visibility_projection_service import WorkspaceVisibilityProjectionService


def _content(source_id: UUID, marker: str) -> Content:
    return Content(
        source_id=source_id,
        title=f"P2A {marker}",
        url=f"https://test/p2a/{marker}",
        fingerprint=f"p2a-{marker}",
        processing_status="processed",
    )


def test_workspace_source_links_scope_visibility_and_projection_without_canonical_writes() -> None:
    """A projection is rebuilt from scoped links, is isolated, and remains reconstructible."""

    database = SessionLocal()
    marker = uuid4().hex
    try:
        first_user = User(
            email=f"p2a-first-{marker}@test.local",
            email_normalized=f"p2a-first-{marker}@test.local",
            password_hash="hash",
        )
        second_user = User(
            email=f"p2a-second-{marker}@test.local",
            email_normalized=f"p2a-second-{marker}@test.local",
            password_hash="hash",
        )
        source_a = Source(name=f"p2a-source-a-{marker}", type="manual")
        source_b = Source(name=f"p2a-source-b-{marker}", type="manual")
        database.add_all([first_user, second_user, source_a, source_b])
        database.flush()
        content_a_first = _content(source_a.id, f"{marker}-a-first")
        content_a_second = _content(source_a.id, f"{marker}-a-second")
        content_b = _content(source_b.id, f"{marker}-b")
        database.add_all([content_a_first, content_a_second, content_b])
        database.flush()

        workspace_service = WorkspaceService(database)
        first_workspace = workspace_service.provision_personal_workspace(first_user.id)
        second_workspace = workspace_service.provision_personal_workspace(second_user.id)
        first_context = WorkspaceContext(first_workspace.id, first_user.id)
        second_context = WorkspaceContext(second_workspace.id, second_user.id)
        visibility_repository = WorkspaceContentVisibilityRepository(database)
        projection_service = WorkspaceVisibilityProjectionService(database, visibility_repository)
        source_service = WorkspaceSourceService(
            database,
            WorkspaceSourceRepository(database),
            projection_service,
        )

        assert source_service.link(first_context, source_a.id) is True
        assert source_service.link(first_context, source_a.id) is False
        assert visibility_repository.list_content_ids(first_context) == sorted(
            [content_a_first.id, content_a_second.id]
        )
        assert visibility_repository.list_content_ids(second_context) == []
        assert projection_service.verify(first_context).consistent is True

        visibility_repository.replace(first_context, [content_a_first.id])
        assert projection_service.verify(first_context).consistent is False
        execution_context = WorkspaceExecutionContext.now(
            workspace_id=first_workspace.id,
            correlation_id=uuid4(),
            actor_type="system",
            actor_id=None,
            trigger="visibility_rebuild",
        )
        assert projection_service.rebuild(first_context, execution_context) == 2
        assert projection_service.verify(first_context).consistent is True

        assert source_service.unlink(first_context, source_a.id) is True
        assert visibility_repository.list_content_ids(first_context) == []
        assert database.get(Source, source_a.id) is source_a
        assert database.get(Content, content_a_first.id) is content_a_first
    finally:
        database.rollback()
        database.close()

    verification = SessionLocal()
    try:
        assert verification.scalar(
            text(
                "SELECT count(*) FROM workspaces w JOIN users u ON u.id = w.owner_user_id "
                "WHERE u.email_normalized IN (:first_email, :second_email)"
            ),
            {
                "first_email": f"p2a-first-{marker}@test.local",
                "second_email": f"p2a-second-{marker}@test.local",
            },
        ) == 0
    finally:
        verification.close()


def test_private_repositories_reject_missing_context_before_any_sql() -> None:
    """A repository never treats missing tenant scope as global read access."""

    database = SessionLocal()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        with pytest.raises(WorkspaceContextRequiredError):
            WorkspaceSourceRepository(database).list_sources(None)  # type: ignore[arg-type]
        with pytest.raises(WorkspaceContextRequiredError):
            WorkspaceContentVisibilityRepository(database).list_content_ids(None)  # type: ignore[arg-type]
        assert statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()


def test_workspace_projection_rejects_mismatched_job_context() -> None:
    """A background job cannot accidentally rebuild a different workspace's projection."""

    database = SessionLocal()
    try:
        user = User(email=f"p2a-context-{uuid4().hex}@test.local", email_normalized=f"p2a-context-{uuid4().hex}@test.local", password_hash="hash")
        # The normalized identity must match the visible email for this isolated fixture.
        user.email_normalized = user.email
        database.add(user)
        database.flush()
        workspace = WorkspaceService(database).provision_personal_workspace(user.id)
        context = WorkspaceContext(workspace.id, user.id)
        service = WorkspaceVisibilityProjectionService(database, WorkspaceContentVisibilityRepository(database))
        with pytest.raises(ValueError):
            service.rebuild(
                context,
                WorkspaceExecutionContext.now(
                    workspace_id=uuid4(),
                    correlation_id=uuid4(),
                    actor_type="system",
                    actor_id=None,
                    trigger="visibility_rebuild",
                ),
            )
    finally:
        database.rollback()
        database.close()
