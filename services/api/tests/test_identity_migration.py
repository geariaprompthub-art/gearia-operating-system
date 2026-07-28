"""Disposable PostgreSQL validation for the P1A users migration."""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from app.core.config import get_settings


def _run_alembic(database_url: URL, operation: str, revision: str) -> None:
    """Execute one Alembic operation against the temporary database."""
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    try:
        command_config = Config("alembic.ini")
        if operation == "upgrade":
            command.upgrade(command_config, revision)
        else:
            command.downgrade(command_config, revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


def _drop_database(admin_url: URL, database_name: str) -> None:
    """Drop only the temporary database after terminating its own connections."""
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
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
    finally:
        engine.dispose()


def test_users_migration_upgrade_downgrade_and_reupgrade() -> None:
    """Prove the P1A migration is reversible without touching legacy tables."""
    primary_url = make_url(get_settings().database_url)
    database_name = f"gearia_p1a_migration_test_{uuid4().hex}"
    admin_url = primary_url.set(database="postgres")
    temporary_url = primary_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        _run_alembic(temporary_url, "upgrade", "20260721_0006")
        temporary_engine = create_engine(temporary_url)
        try:
            with temporary_engine.connect() as connection:
                assert connection.scalar(text("SELECT to_regclass('public.users')")) is None
                assert connection.scalar(text("SELECT to_regclass('public.contents')")) == "contents"
        finally:
            temporary_engine.dispose()

        _run_alembic(temporary_url, "upgrade", "20260727_0007")
        temporary_engine = create_engine(temporary_url)
        try:
            with temporary_engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users")) == 0
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == "20260727_0007"
        finally:
            temporary_engine.dispose()

        _run_alembic(temporary_url, "downgrade", "20260721_0006")
        temporary_engine = create_engine(temporary_url)
        try:
            with temporary_engine.connect() as connection:
                assert connection.scalar(text("SELECT to_regclass('public.users')")) is None
                assert connection.scalar(text("SELECT to_regclass('public.contents')")) == "contents"
        finally:
            temporary_engine.dispose()

        _run_alembic(temporary_url, "upgrade", "20260727_0007")
        temporary_engine = create_engine(temporary_url)
        try:
            with temporary_engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users")) == 0
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == "20260727_0007"
        finally:
            temporary_engine.dispose()
    finally:
        admin_engine.dispose()
        _drop_database(admin_url, database_name)
