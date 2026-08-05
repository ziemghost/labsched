"""asyncpg pool + a deliberately small migration runner.

No ORM: every scheduling decision here is a SQL statement you can read, so the
locking is auditable line by line.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Let asyncpg hand us jsonb as dicts instead of strings.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.dsn, min_size=2, max_size=16, init=_init_conn
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    p = await pool()
    return await p.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    p = await pool()
    return await p.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    p = await pool()
    return await p.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    p = await pool()
    return await p.execute(sql, *args)


# ------------------------------------------------------------------ migrate ---

async def migrate() -> list[str]:
    """Apply every unapplied .sql file in filename order. Returns what ran.

    Dedicated connection, statement cache off. A pooled connection caches
    prepared statements against relation OIDs, and DDL earlier in the same
    migration invalidates them, which surfaces as a later migration insisting a
    table it can see does not exist.
    """
    applied: list[str] = []
    conn = await asyncpg.connect(settings.dsn, statement_cache_size=0)
    try:
        await _init_conn(conn)
        await conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in await conn.fetch("select version from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            sql = path.read_text()
            # The initial migration also creates schema_migrations; tolerate that.
            sql = sql.replace("create table schema_migrations (",
                              "create table if not exists schema_migrations (")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "insert into schema_migrations(version) values ($1)", path.name
                )
            applied.append(path.name)
    finally:
        await conn.close()
    return applied


async def reset_schema() -> None:
    """Drop and recreate the public schema. Used by seed --reset and tests.

    The pool is torn down first: every pooled connection may hold prepared
    statements bound to relations that are about to stop existing, and reusing
    one afterwards fails in confusing ways.
    """
    conn = await asyncpg.connect(settings.dsn, statement_cache_size=0)
    try:
        await conn.execute("drop schema public cascade; create schema public;")
    finally:
        await conn.close()
    await close_pool()
    await migrate()
