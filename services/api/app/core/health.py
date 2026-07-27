"""Local dependency checks used by readiness probes only."""

import socket
from urllib.parse import urlsplit

from sqlalchemy import text

from app.core.config import Settings
from app.db import engine


def check_postgres(settings: Settings) -> bool:
    """Run a bounded local PostgreSQL probe without revealing connection details."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def check_redis(settings: Settings) -> bool:
    """Perform a bounded Redis PING using the configured local dependency URL."""

    try:
        parsed = urlsplit(settings.redis_url)
        if parsed.scheme != "redis" or not parsed.hostname:
            return False
        with socket.create_connection(
            (parsed.hostname, parsed.port or 6379), timeout=settings.health_timeout_seconds
        ) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            return connection.recv(16).startswith(b"+PONG")
    except Exception:
        return False
