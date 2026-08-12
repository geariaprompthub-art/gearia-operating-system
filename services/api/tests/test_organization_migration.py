"""Disposable PostgreSQL contract for the P3A organizational persistence layer."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db import Base
from app.models import Organization, OrganizationInvitation, OrganizationMembership, Workspace


def _run_alembic(database_url: URL, operation: str, revision: str) -> None:
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        (command.upgrade if operation == "upgrade" else command.downgrade)(config, revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


def _drop_database(admin_url: URL, database_name: str) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        engine.dispose()


def _assert_integrity_error(connection, statement: str, parameters: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text(statement), parameters)


def test_organization_migration_backfills_and_enforces_postgresql_contract() -> None:
    """Exercise P3A physical constraints and reversible legacy-workspace backfill."""

    primary_url = make_url(get_settings().database_url)
    database_name = f"gearia_p3a_organizations_{uuid4().hex}"
    admin_url = primary_url.set(database="postgres")
    temporary_url = primary_url.set(database=database_name)
    creator = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        assert {"organizations", "organization_memberships", "organization_invitations"}.issubset(Base.metadata.tables)
        assert "organization_id" in Workspace.__table__.c
        assert Organization.__tablename__ == "organizations"
        assert OrganizationMembership.__tablename__ == "organization_memberships"
        assert OrganizationInvitation.__tablename__ == "organization_invitations"
        with creator.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        _run_alembic(temporary_url, "upgrade", "20260730_0010")
        user_id, workspace_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        engine = create_engine(temporary_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (id, email, email_normalized, password_hash, status, token_version, "
                        "failed_login_count, created_at, updated_at) VALUES "
                        "(:id, :email, :normalized, 'hash', 'active', 1, 0, :now, :now)"
                    ),
                    {"id": user_id, "email": "legacy@example.com", "normalized": "legacy@example.com", "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, owner_user_id, name, status, created_at, updated_at) VALUES "
                        "(:id, :owner_id, 'Legacy workspace', 'active', :now, :now)"
                    ),
                    {"id": workspace_id, "owner_id": user_id, "now": now},
                )
        finally:
            engine.dispose()

        _run_alembic(temporary_url, "upgrade", "20260805_0011")
        _run_alembic(temporary_url, "upgrade", "20260810_0012")
        engine = create_engine(temporary_url)
        try:
            with engine.begin() as connection:
                columns = {column["name"]: column for column in inspect(connection).get_columns("workspaces")}
                assert not columns["organization_id"]["nullable"]
                assert columns["owner_user_id"]["nullable"]
                indexes = {index["name"] for index in inspect(connection).get_indexes("workspaces")}
                assert "uq_workspaces_personal_owner_user_active" in indexes
                assert "uq_workspaces_owner_user" not in {
                    constraint["name"] for constraint in inspect(connection).get_unique_constraints("workspaces")
                }
                index_definition = connection.scalar(
                    text(
                        "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                        "WHERE indexrelid = 'uq_workspaces_personal_owner_user_active'::regclass"
                    )
                )
                assert index_definition is not None
                assert "WHERE (owner_user_id IS NOT NULL)" in index_definition
                shared_id = uuid4()
                connection.execute(
                    text("INSERT INTO organizations (id, kind, name, slug, status, created_at, updated_at) VALUES (:id, 'shared', 'Shared', :slug, 'active', :now, :now)"),
                    {"id": shared_id, "slug": f"shared-{uuid4().hex}", "now": now},
                )
                for _ in range(2):
                    connection.execute(
                        text("INSERT INTO workspaces (id, organization_id, owner_user_id, name, status, created_at, updated_at) VALUES (:id, :organization_id, NULL, 'General', 'active', :now, :now)"),
                        {"id": uuid4(), "organization_id": shared_id, "now": now},
                    )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO workspaces (id, organization_id, owner_user_id, name, status, created_at, updated_at) VALUES (:id, :organization_id, :owner_user_id, 'Duplicate', 'active', :now, :now)",
                    {"id": uuid4(), "organization_id": connection.scalar(text("SELECT organization_id FROM workspaces WHERE id = :id"), {"id": workspace_id}), "owner_user_id": user_id, "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO workspaces (id, organization_id, owner_user_id, name, status, created_at, updated_at) VALUES (:id, NULL, NULL, 'Invalid', 'active', :now, :now)",
                    {"id": uuid4(), "now": now},
                )
                connection.execute(text("DELETE FROM workspaces WHERE organization_id = :organization_id"), {"organization_id": shared_id})
                connection.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": shared_id})
        finally:
            engine.dispose()
        _run_alembic(temporary_url, "downgrade", "20260805_0011")
        _run_alembic(temporary_url, "upgrade", "20260810_0012")
        engine = create_engine(temporary_url)
        try:
            with engine.connect() as connection:
                columns = {column["name"] for column in inspect(connection).get_columns("workspaces")}
                assert "organization_id" in columns
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260810_0012"
                row = connection.execute(
                    text(
                        "SELECT w.organization_id, o.kind, o.personal_owner_user_id, m.role "
                        "FROM workspaces w JOIN organizations o ON o.id = w.organization_id "
                        "JOIN organization_memberships m ON m.organization_id = o.id "
                        "WHERE w.id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                ).one()
                organization_id = row[0]
                membership_id = connection.scalar(
                    text("SELECT id FROM organization_memberships WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                assert row[1:] == ("personal", user_id, "owner")
                assert connection.scalar(text("SELECT count(*) FROM workspaces WHERE organization_id IS NULL")) == 0

                _assert_integrity_error(
                    connection,
                    "INSERT INTO organizations (id, kind, name, slug, status, created_at, updated_at) "
                    "VALUES (:id, 'unknown', 'Invalid kind', 'invalid-kind', 'active', :now, :now)",
                    {"id": uuid4(), "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organizations (id, kind, name, slug, status, created_at, updated_at) "
                    "VALUES (:id, 'shared', 'Invalid status', 'invalid-status', 'unknown', :now, :now)",
                    {"id": uuid4(), "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organizations (id, kind, name, slug, status, personal_owner_user_id, created_at, updated_at) "
                    "VALUES (:id, 'personal', 'Duplicate', 'duplicate-personal', 'active', :user_id, :now, :now)",
                    {"id": uuid4(), "user_id": user_id, "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_memberships (id, organization_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :organization_id, :user_id, 'invalid', :now, :now)",
                    {"id": uuid4(), "organization_id": organization_id, "user_id": user_id, "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_memberships (id, organization_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :organization_id, :user_id, 'owner', :now, :now)",
                    {"id": uuid4(), "organization_id": organization_id, "user_id": user_id, "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO organization_memberships (id, organization_id, user_id, role, created_at, updated_at, revoked_at) "
                        "VALUES (:id, :organization_id, :user_id, 'member', :now, :now, :now)"
                    ),
                    {"id": uuid4(), "organization_id": organization_id, "user_id": user_id, "now": now},
                )

                invitation_id = uuid4()
                invitation = {
                    "id": invitation_id,
                    "organization_id": organization_id,
                    "email": "invitee@example.com",
                    "token_hash": "a" * 64,
                    "expires_at": now + timedelta(hours=1),
                    "membership_id": membership_id,
                    "now": now,
                }
                connection.execute(
                    text(
                        "INSERT INTO organization_invitations "
                        "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, created_by_membership_id, created_at) "
                        "VALUES (:id, :organization_id, :email, 'member', :token_hash, :expires_at, :membership_id, :now)"
                    ),
                    invitation,
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_memberships (id, organization_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :organization_id, :user_id, 'member', :now, :now)",
                    {"id": uuid4(), "organization_id": uuid4(), "user_id": user_id, "now": now},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_invitations "
                    "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, created_by_membership_id, created_at) "
                    "VALUES (:id, :organization_id, :email, 'admin', :token_hash, :expires_at, :membership_id, :now)",
                    {**invitation, "id": uuid4(), "token_hash": "b" * 64},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_invitations "
                    "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, created_by_membership_id, created_at) "
                    "VALUES (:id, :organization_id, 'invalid-role@example.com', 'owner', :token_hash, :expires_at, :membership_id, :now)",
                    {**invitation, "id": uuid4(), "token_hash": "c" * 64},
                )
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_invitations "
                    "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, accepted_at, invalidated_at, created_by_membership_id, created_at) "
                    "VALUES (:id, :organization_id, 'terminal@example.com', 'member', :token_hash, :expires_at, :now, :now, :membership_id, :now)",
                    {**invitation, "id": uuid4(), "token_hash": "c" * 64},
                )
                connection.execute(text("UPDATE organization_invitations SET accepted_at = :now WHERE id = :id"), {"now": now, "id": invitation_id})
                _assert_integrity_error(
                    connection,
                    "INSERT INTO organization_invitations "
                    "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, created_by_membership_id, created_at) "
                    "VALUES (:id, :organization_id, 'other@example.com', 'member', :token_hash, :expires_at, :membership_id, :now)",
                    {**invitation, "id": uuid4()},
                )
                connection.execute(
                    text(
                        "INSERT INTO organization_invitations "
                        "(id, organization_id, invited_email_normalized, role, token_hash, expires_at, created_by_membership_id, created_at) "
                        "VALUES (:id, :organization_id, :email, 'member', :token_hash, :expires_at, :membership_id, :now)"
                    ),
                    {**invitation, "id": uuid4(), "token_hash": "d" * 64},
                )
        finally:
            engine.dispose()

        _run_alembic(temporary_url, "downgrade", "20260730_0010")
        engine = create_engine(temporary_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT to_regclass('public.organizations')")) is None
                columns = {column["name"] for column in inspect(connection).get_columns("workspaces")}
                assert "owner_user_id" in columns and "organization_id" not in columns
        finally:
            engine.dispose()
        _run_alembic(temporary_url, "upgrade", "20260805_0011")
    finally:
        creator.dispose()
        _drop_database(admin_url, database_name)


def test_workspace_ownership_bridge_downgrade_fails_closed_for_shared_workspace() -> None:
    """Do not manufacture a personal owner while reverting a shared workspace."""

    primary_url = make_url(get_settings().database_url)
    database_name = f"gearia_p3a_shared_downgrade_{uuid4().hex}"
    admin_url = primary_url.set(database="postgres")
    temporary_url = primary_url.set(database=database_name)
    creator = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    now = datetime.now(UTC)
    try:
        with creator.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        _run_alembic(temporary_url, "upgrade", "20260810_0012")
        engine = create_engine(temporary_url)
        try:
            shared_id = uuid4()
            workspace_id = uuid4()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO organizations (id, kind, name, slug, status, created_at, updated_at) "
                        "VALUES (:id, 'shared', 'Shared', :slug, 'active', :now, :now)"
                    ),
                    {"id": shared_id, "slug": f"shared-{uuid4().hex}", "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO workspaces "
                        "(id, organization_id, owner_user_id, name, status, created_at, updated_at) "
                        "VALUES (:id, :organization_id, NULL, 'General', 'active', :now, :now)"
                    ),
                    {"id": workspace_id, "organization_id": shared_id, "now": now},
                )
            with pytest.raises(RuntimeError, match="shared workspaces exist"):
                _run_alembic(temporary_url, "downgrade", "20260805_0011")
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260810_0012"
                assert connection.scalar(
                    text("SELECT owner_user_id IS NULL FROM workspaces WHERE id = :id"),
                    {"id": workspace_id},
                )
        finally:
            engine.dispose()
    finally:
        creator.dispose()
        _drop_database(admin_url, database_name)


def test_workspace_ownership_bridge_rejects_concurrent_duplicate_personal_workspaces() -> None:
    """The partial physical uniqueness constraint serializes duplicate personal inserts."""

    primary_url = make_url(get_settings().database_url)
    database_name = f"gearia_p3a_workspace_race_{uuid4().hex}"
    admin_url = primary_url.set(database="postgres")
    temporary_url = primary_url.set(database=database_name)
    creator = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    now = datetime.now(UTC)
    try:
        with creator.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        _run_alembic(temporary_url, "upgrade", "20260810_0012")
        engine = create_engine(temporary_url)
        owner_id, organization_id = uuid4(), uuid4()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (id, email, email_normalized, password_hash, status, token_version, "
                        "failed_login_count, created_at, updated_at) VALUES "
                        "(:id, :email, :email, 'hash', 'active', 1, 0, :now, :now)"
                    ),
                    {"id": owner_id, "email": f"{owner_id.hex}@example.com", "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO organizations (id, kind, name, slug, status, personal_owner_user_id, created_at, updated_at) "
                        "VALUES (:id, 'personal', 'Personal', :slug, 'active', :owner_id, :now, :now)"
                    ),
                    {"id": organization_id, "slug": f"personal-{owner_id.hex}", "owner_id": owner_id, "now": now},
                )
            barrier = Barrier(2)

            def insert_workspace() -> bool:
                try:
                    with engine.begin() as connection:
                        barrier.wait(timeout=5)
                        connection.execute(
                            text(
                                "INSERT INTO workspaces "
                                "(id, organization_id, owner_user_id, name, status, created_at, updated_at) "
                                "VALUES (:id, :organization_id, :owner_id, 'Personal', 'active', :now, :now)"
                            ),
                            {
                                "id": uuid4(),
                                "organization_id": organization_id,
                                "owner_id": owner_id,
                                "now": now,
                            },
                        )
                    return True
                except IntegrityError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda _: insert_workspace(), range(2)))
            assert outcomes.count(True) == 1
            assert outcomes.count(False) == 1
            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT count(*) FROM workspaces WHERE owner_user_id = :owner_id"),
                    {"owner_id": owner_id},
                ) == 1
        finally:
            engine.dispose()
    finally:
        creator.dispose()
        _drop_database(admin_url, database_name)
