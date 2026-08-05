"""The resumable event cursor, and the hole that makes "gap-free" a lie.

`audit.seq` is a `bigserial`: allocated at INSERT, visible at COMMIT. Those are
different moments in different orders, so a writer holding seq 5 can commit
after a writer holding seq 6. An agent that reads 6 and advances its cursor
never sees 5, silently, in production, under concurrency.
"""
from __future__ import annotations

import asyncio

import httpx

from labsched import db
from labsched.api import app
from tests.conftest import one_op_run


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def _insert(conn, run_id: str, action: str):
    await conn.execute(
        "insert into audit(actor, action, run_id, detail) values ('test',$2,$1,'{}'::jsonb)",
        run_id, action)


async def test_an_uncommitted_hole_is_not_skipped(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "cursor")
    await h.spin(3)

    async with client() as c:
        first = await c.get(f"/api/runs/{run['id']}/events")
    cursor = first.json()["cursor"]

    pool = await db.pool()
    slow = await pool.acquire()
    try:
        # `slow` takes a sequence number and holds its transaction open.
        tx = slow.transaction()
        await tx.start()
        await _insert(slow, run["id"], "held.by.slow.writer")

        # A second writer takes the next number and commits immediately.
        await db.execute(
            "insert into audit(actor, action, run_id, detail)"
            " values ('test','committed.first',$1,'{}'::jsonb)", run["id"])

        async with client() as c:
            page = await c.get(f"/api/runs/{run['id']}/events?since={cursor}")
        body = page.json()

        actions = [e["action"] for e in body["events"]]
        assert "committed.first" not in actions, (
            "the cursor stepped over an uncommitted sequence number; the event "
            "holding it would never have been delivered")
        assert body["cursor"] == cursor, "the cursor advanced past a hole"
        assert body["has_more"] is True

        await tx.commit()
    finally:
        await pool.release(slow)

    # Once it commits, both arrive, in order, exactly once.
    async with client() as c:
        page = await c.get(f"/api/runs/{run['id']}/events?since={cursor}")
    actions = [e["action"] for e in page.json()["events"]]
    assert actions[:2] == ["held.by.slow.writer", "committed.first"]


async def test_a_permanent_hole_does_not_stall_the_cursor(h, org_token):
    """A rolled-back transaction burns a sequence number forever. Waiting for
    it would wedge every agent following that run."""
    from labsched import api

    run = await one_op_run(org_token["id"], "bli_read", "rollback")
    await h.spin(3)

    async with client() as c:
        cursor = (await c.get(f"/api/runs/{run['id']}/events")).json()["cursor"]

    pool = await db.pool()
    conn = await pool.acquire()
    try:
        tx = conn.transaction()
        await tx.start()
        await _insert(conn, run["id"], "doomed")
        await tx.rollback()
    finally:
        await pool.release(conn)

    await db.execute(
        "insert into audit(actor, action, run_id, detail)"
        " values ('test','after.the.hole',$1,'{}'::jsonb)", run["id"])

    original = api.HOLE_GRACE_S
    api.HOLE_GRACE_S = 0.0          # the hole is now "old"
    try:
        async with client() as c:
            body = (await c.get(f"/api/runs/{run['id']}/events?since={cursor}")).json()
    finally:
        api.HOLE_GRACE_S = original

    assert "after.the.hole" in [e["action"] for e in body["events"]], \
        "a rolled-back sequence number wedged the cursor permanently"
