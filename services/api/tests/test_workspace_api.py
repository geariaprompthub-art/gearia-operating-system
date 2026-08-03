"""HTTP contracts for P2A current-workspace boundaries."""

from uuid import UUID, uuid4

from app.models.source import Source
from app.models.content import Content
from app.models.user import User
from app.db import Base
from app.services.workspace_context import WorkspaceContext
from app.services.workspace_dependencies import get_workspace_context
from app.services.workspace_service import WorkspaceService
from test_main import TestingSessionLocal, client


def setup_function(_: object) -> None:
    """Reset the shared SQLite fixture regardless of test module execution order."""

    from test_main import test_engine

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def _workspace_context(marker: str) -> tuple[WorkspaceContext, UUID, UUID]:
    """Create two workspace-scoped canonical source fixtures in isolated SQLite state."""

    database = TestingSessionLocal()
    try:
        first_user = User(
            email=f"workspace-first-{marker}@test.local",
            email_normalized=f"workspace-first-{marker}@test.local",
            password_hash="hash",
        )
        second_user = User(
            email=f"workspace-second-{marker}@test.local",
            email_normalized=f"workspace-second-{marker}@test.local",
            password_hash="hash",
        )
        first_source = Source(name=f"workspace-source-first-{marker}", type="manual")
        second_source = Source(name=f"workspace-source-second-{marker}", type="manual")
        database.add_all([first_user, second_user, first_source, second_source])
        database.flush()
        first_workspace = WorkspaceService(database).provision_personal_workspace(first_user.id)
        WorkspaceService(database).provision_personal_workspace(second_user.id)
        database.commit()
        return WorkspaceContext(first_workspace.id, first_user.id), first_source.id, second_source.id
    finally:
        database.close()


def test_current_workspace_and_source_links_are_scoped_to_dependency_context() -> None:
    """The public API derives tenant scope from the dependency, never from request payload."""

    marker = uuid4().hex
    context, first_source_id, second_source_id = _workspace_context(marker)
    from app.main import app

    app.dependency_overrides[get_workspace_context] = lambda: context
    try:
        workspace = client.get("/workspaces/current")
        linked = client.post("/workspaces/current/sources", json={"source_id": str(first_source_id)})
        listed = client.get("/workspaces/current/sources")
        missing = client.post("/workspaces/current/sources", json={"source_id": str(uuid4())})
        removed = client.delete(f"/workspaces/current/sources/{first_source_id}")
        after_remove = client.get("/workspaces/current/sources")
    finally:
        app.dependency_overrides.pop(get_workspace_context, None)

    assert workspace.status_code == 200
    assert workspace.json()["id"] == str(context.workspace_id)
    assert linked.status_code == 201
    assert [item["id"] for item in linked.json()] == [str(first_source_id)]
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(first_source_id)]
    assert str(second_source_id) not in {item["id"] for item in listed.json()}
    assert missing.status_code == 404
    assert removed.status_code == 204
    assert after_remove.status_code == 200 and after_remove.json() == []


def test_workspace_update_rejects_unrecognized_or_invalid_public_input() -> None:
    """Workspace APIs keep a narrow, explicit schema and do not trust workspace IDs from clients."""

    context, _, _ = _workspace_context(uuid4().hex)
    from app.main import app

    app.dependency_overrides[get_workspace_context] = lambda: context
    try:
        valid = client.patch("/workspaces/current", json={"name": " Research Team "})
        extra = client.patch("/workspaces/current", json={"name": "Research", "workspace_id": str(uuid4())})
        blank = client.patch("/workspaces/current", json={"name": "   "})
    finally:
        app.dependency_overrides.pop(get_workspace_context, None)

    assert valid.status_code == 200
    assert valid.json()["name"] == "Research Team"
    assert extra.status_code == 422
    assert blank.status_code == 422


def test_workspace_contents_are_read_only_from_the_rebuildable_visibility_projection() -> None:
    """The workspace content route cannot enumerate canonical content outside its linked sources."""

    marker = uuid4().hex
    context, first_source_id, second_source_id = _workspace_context(marker)
    database = TestingSessionLocal()
    try:
        visible = Content(
            source_id=first_source_id,
            title="Visible workspace content",
            url=f"https://test/{marker}/visible",
            fingerprint=f"workspace-visible-{marker}",
            processing_status="processed",
        )
        hidden = Content(
            source_id=second_source_id,
            title="Hidden workspace content",
            url=f"https://test/{marker}/hidden",
            fingerprint=f"workspace-hidden-{marker}",
            processing_status="processed",
        )
        database.add_all([visible, hidden])
        database.commit()
        visible_id = visible.id
        hidden_id = hidden.id
    finally:
        database.close()
    from app.main import app

    app.dependency_overrides[get_workspace_context] = lambda: context
    try:
        assert client.post("/workspaces/current/sources", json={"source_id": str(first_source_id)}).status_code == 201
        response = client.get("/workspaces/current/contents")
    finally:
        app.dependency_overrides.pop(get_workspace_context, None)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(visible_id)]
    assert str(hidden_id) not in {item["id"] for item in response.json()}
