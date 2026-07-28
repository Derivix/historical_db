"""
Database connection management using psycopg v3 connection pool.

Usage (sync):
    with get_connection() as conn:
        conn.execute(...)

Usage (async):
    async with get_async_connection() as conn:
        await conn.execute(...)

The pool is lazily initialised on first use and uses settings from app.config.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Generator

import psycopg
import psycopg_pool
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Sync pool (singleton per process)
# ---------------------------------------------------------------------------

_pool: psycopg_pool.ConnectionPool | None = None
_pool_lock = threading.Lock()


def _build_pool() -> psycopg_pool.ConnectionPool:
    cfg = get_settings()
    db = cfg.database
    logger.info("opening_connection_pool", dsn=_redact_dsn(db.dsn), min=db.pool_min, max=db.pool_max)
    pool = psycopg_pool.ConnectionPool(
        conninfo=db.dsn,
        min_size=db.pool_min,
        max_size=db.pool_max,
        open=True,
    )
    return pool


def get_pool() -> psycopg_pool.ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


@contextlib.contextmanager
def get_connection() -> Generator[psycopg.Connection, None, None]:
    """Yield a psycopg v3 connection from the pool (auto-commit off)."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    """Gracefully close the connection pool (call on application shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("connection_pool_closed")


# ---------------------------------------------------------------------------
# Raw connection (no pool) — useful for migrations / admin tasks
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def get_raw_connection(dsn: str | None = None) -> Generator[psycopg.Connection, None, None]:
    """Yield a raw psycopg connection bypassing the pool (autocommit=True)."""
    if dsn is None:
        dsn = get_settings().database.dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redact_dsn(dsn: str) -> str:
    """Remove password from DSN for logging."""
    import re
    return re.sub(r"(:)[^:@]+(@)", r"\1***\2", dsn)
