"""Append-only audit log.

`log` always takes an explicit connection so that an audit row lands in the
same transaction as the state change it describes. If the change rolls back so
does its log line, so the log never claims something that did not happen.
"""
from __future__ import annotations

from typing import Any

import asyncpg


async def log(
    conn: asyncpg.Connection,
    actor: str,
    action: str,
    *,
    run_id: str | None = None,
    step_id: str | None = None,
    device_id: str | None = None,
    sample_id: str | None = None,
    token_id: str | None = None,
    intervention_id: str | None = None,
    **detail: Any,
) -> None:
    await conn.execute(
        """
        insert into audit(actor, action, run_id, step_id, device_id, sample_id,
                          token_id, intervention_id, detail)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        actor, action, run_id, step_id, device_id, sample_id, token_id,
        intervention_id, detail or {},
    )
