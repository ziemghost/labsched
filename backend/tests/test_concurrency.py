"""`max_concurrent` means what it says.

Biscuit checks the shape of a run at admission, how wide it could get.
This is the other half: the live ledger at dispatch, enforced per ancestor over
its whole subtree so a project cannot mint ten agents and get ten times its
allowance. Nothing tested the actual ceiling, and it was one too high: the
count is taken before the insert, so `held > cap` permitted `cap + 1`.
"""
from __future__ import annotations

from labsched import db
from labsched.auth import tokens
from tests.conftest import one_op_run


async def _open_reservations(token_ids: list[str]) -> int:
    return await db.fetchval(
        "select count(*) from reservations where token_id = any($1::text[])"
        " and (device_released_at is null or sample_released_at is null)",
        token_ids) or 0


async def _capped_child(parent: dict, cap: int, label: str = "capped",
                        days: int = 3) -> dict:
    # `days` shrinks at each generation on purpose: a child may not outlive its
    # parent, so the same window measured a moment later is a widening.
    return await tokens.attenuate(
        parent["id"], label, "agent",
        tokens.Caveats(
            allowed_kinds=list(parent["allowed_kinds"]), max_concurrent=cap,
            max_wallclock_s=10_000, max_run_credits=200, budget_credits=5_000,
            expires_at=tokens.default_expiry(days),
            authorities=["operator"],
        ),
    )


async def test_a_token_never_holds_more_than_its_cap(h, org_token):
    tok = await _capped_child(org_token, cap=1)
    for i in range(3):
        await one_op_run(tok["id"], "bli_read", f"r{i}")

    peak = 0
    for _ in range(40):
        await h.sched.tick()
        peak = max(peak, await _open_reservations([tok["id"]]))

    assert peak <= 1, f"a token capped at 1 held {peak} reservations at once"
    assert peak == 1, "nothing was ever dispatched; this test would pass vacuously"


async def test_the_cap_binds_the_whole_subtree(h, org_token):
    """A parent capped at 2 must still cap 2 when the work is spread over two
    children that are each allowed 2."""
    parent = await _capped_child(org_token, cap=2, label="project")
    a = await _capped_child(parent, cap=2, label="agent-a", days=2)
    b = await _capped_child(parent, cap=2, label="agent-b", days=2)

    for i in range(3):
        await one_op_run(a["id"], "bli_read", f"a{i}")
        await one_op_run(b["id"], "bli_read", f"b{i}")

    peak = 0
    for _ in range(40):
        await h.sched.tick()
        peak = max(peak, await _open_reservations([parent["id"], a["id"], b["id"]]))

    assert peak <= 2, f"a subtree capped at 2 held {peak} reservations at once"
    assert peak == 2, "nothing was ever dispatched; this test would pass vacuously"


async def test_work_still_flows_once_a_reservation_frees(h, org_token):
    """The cap must throttle, not deadlock."""
    tok = await _capped_child(org_token, cap=1)
    runs = [await one_op_run(tok["id"], "bli_read", f"r{i}") for i in range(3)]

    async def all_settled():
        return await db.fetchval(
            "select count(*) from runs where id = any($1::text[])"
            " and state in ('done','failed','cancelled')",
            [r["id"] for r in runs]) == len(runs)

    assert await h.spin(40, until=all_settled), "the cap stalled the queue"


async def test_a_requeued_step_gets_its_credits_back(h, org_token):
    """A step going back on the queue has not consumed the instrument time it
    paid for, and it will be charged again when it is dispatched. The
    automatic path refunded; the seven human-ordered requeues did not, so
    quarantining an instrument billed the customer twice for work that never
    happened, against a budget that is a real enforced cap.
    """
    from labsched import interventions

    tok = await _capped_child(org_token, cap=4, label="billed")
    await one_op_run(tok["id"], "bli_read", "billed-run")
    assert await h.spin(20, until=lambda: db.fetchval(
        "select count(*) from reservations where token_id=$1", tok["id"]))

    spent_once = await db.fetchval(
        "select credits_spent from tokens where id=$1", tok["id"])
    assert spent_once > 0, "nothing was charged; this test would pass vacuously"

    device = await db.fetchval(
        "select device_id from reservations where token_id=$1 order by acquired_at desc limit 1",
        tok["id"])
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await interventions.quarantine_device(
                conn, device, "token:tok-engineer", "engineer took it out of service")

    after_requeue = await db.fetchval(
        "select credits_spent from tokens where id=$1", tok["id"])
    assert after_requeue == 0, (
        f"quarantine kept {after_requeue} credits for a step it requeued")

    # And the lineage was credited too, not just the leaf.
    assert await db.fetchval(
        "select credits_spent from tokens where id='tok-org'") == 0
