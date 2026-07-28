"""Disposable PostgreSQL migration and P1B model-schema parity coverage."""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from app.core.config import get_settings
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession


def _run_alembic(database_url: URL, operation: str, revision: str) -> None:
    """Run Alembic against an isolated database and restore process settings."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        (command.upgrade if operation == "upgrade" else command.downgrade)(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def _drop_database(admin_url: URL, database_name: str) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()"), {"name": database_name})
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
    finally:
        engine.dispose()


def test_p1b_migration_is_reversible_and_matches_auth_models() -> None:
    """Validate upgrade, downgrade, re-upgrade and the P1B physical contract."""
    primary = make_url(get_settings().database_url)
    name = f"gearia_p1b_migration_{uuid4().hex}"
    admin, temporary = primary.set(database="postgres"), primary.set(database=name)
    creator = create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        with creator.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        _run_alembic(temporary, "upgrade", "20260728_0008")
        engine = create_engine(temporary)
        try:
            inspector = inspect(engine)
            assert {"auth_sessions", "auth_refresh_tokens", "users", "contents"} <= set(inspector.get_table_names())
            assert {column["name"] for column in inspector.get_columns("auth_sessions")} == set(AuthSession.__table__.columns.keys())
            assert {column["name"] for column in inspector.get_columns("auth_refresh_tokens")} == set(AuthRefreshToken.__table__.columns.keys())
            assert {foreign["options"].get("ondelete") for foreign in inspector.get_foreign_keys("auth_refresh_tokens")} >= {"CASCADE", "SET NULL"}
            assert "uq_auth_refresh_tokens_hash" in {item["name"] for item in inspector.get_unique_constraints("auth_refresh_tokens")}
            assert {"ix_auth_sessions_active", "ix_auth_sessions_user_id", "ix_auth_refresh_tokens_family_id"} <= {item["name"] for item in inspector.get_indexes("auth_sessions")} | {item["name"] for item in inspector.get_indexes("auth_refresh_tokens")}
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260728_0008"
        finally:
            engine.dispose()
        _run_alembic(temporary, "downgrade", "20260727_0007")
        engine = create_engine(temporary)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT to_regclass('public.auth_sessions')")) is None
                assert connection.scalar(text("SELECT to_regclass('public.auth_refresh_tokens')")) is None
                assert connection.scalar(text("SELECT to_regclass('public.users')")) == "users"
                assert connection.scalar(text("SELECT to_regclass('public.contents')")) == "contents"
        finally:
            engine.dispose()
        _run_alembic(temporary, "upgrade", "20260728_0008")
        engine = create_engine(temporary)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260728_0008"
        finally:
            engine.dispose()
    finally:
        creator.dispose()
        _drop_database(admin, name)
