"""Crash recovery.

The scheduler is killed at the three moments that actually hurt: while a job
is running, between declaring intent and recording the handle, and after the
instrument has forgotten the job entirely.
"""
from __future__ import annotations

import asyncio

from labsched import db
from labsched.runs import RunRequest, submit
from tests.conftest import one_op_run, read_only_run, simple_run


async def _until_running(run_id):
    async def check():
        return await db.fetchval(
            "select count(*) from steps where run_id=$1 and state='running'"
            " and job_handle is not null", run_id,
        ) > 0
    return check


async def test_run_survives_a_scheduler_restart(h, org_token):
    """Kill the process mid-step, start a fresh scheduler with a fresh driver
    object, and the run finishes. Nothing was held in memory to lose."""
    run = await simple_run(org_token["id"], "survivor")

    assert await h.spin(8, until=await _until_running(run["id"])), "step never started"
    handle_before = await db.fetchval(
        "select job_handle from steps where run_id=$1 and state='running'", run["id"]
    )
    assert handle_before

    await h.restart()

    # The new scheduler re-adopts the in-flight job rather than restarting it.
    await h.spin(2)
    handle_after = await db.fetchval(
        "select job_handle from steps where run_id=$1 and state in ('running','done')"
        " and job_handle is not null order by idx limit 1", run["id"]
    )
    assert handle_after == handle_before, "restart re-submitted the job instead of adopting it"

    ok = await h.spin(25, until=lambda: _run_done(run["id"]))
    assert await h.run_state(run["id"]) == "done", await h.step_states(run["id"])
    await h.assert_no_orphan_locks()
    await h.assert_plate_in_one_place()


async def _run_done(run_id):
    return await db.fetchval("select state from runs where id=$1", run_id) == "done"


async def test_crash_between_intent_and_handle_adopts_the_job(h, org_token):
    """We write 'running' before calling start(). If we die in that window the
    step has no handle, so recovery has to ask the instrument whether it is
    already working on this step, not submit it a second time."""
    run = await read_only_run(org_token["id"], "adopt")
    assert await h.spin(8, until=await _until_running(run["id"]))

    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    real_handle = step["job_handle"]
    # Simulate the crash: the handle never made it to disk.
    await db.execute("update steps set job_handle=null where id=$1", step["id"])

    await h.restart()
    await h.spin(1.5)

    recovered = await db.fetchval("select job_handle from steps where id=$1", step["id"])
    assert recovered == real_handle, "did not re-adopt the running job"

    jobs = await db.fetchval(
        "select count(*) from device_jobs where step_id=$1", step["id"]
    )
    assert jobs == 1, "the operation was submitted twice"

    await h.spin(20, until=lambda: _run_done(run["id"]))
    assert await h.run_state(run["id"]) == "done"
    await h.assert_no_orphan_locks()


async def test_lost_handle_on_an_idempotent_step_is_requeued(h, org_token):
    """A read that we cannot account for is safe to simply do again."""
    run = await one_op_run(org_token["id"], "bli_read", "reread")
    assert await h.spin(8, until=await _until_running(run["id"]))

    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    # Instrument forgot the job and we forgot the handle: nobody knows.
    await db.execute("update device_jobs set forgotten=true where step_id=$1", step["id"])
    await db.execute("update steps set job_handle=null where id=$1", step["id"])

    await h.restart()
    await h.spin(20, until=lambda: _run_done(run["id"]))

    assert await h.open_interventions() == [], "a repeatable read should not need a human"
    assert await h.run_state(run["id"]) == "done"
    assert (await db.fetchval("select attempt from steps where id=$1", step["id"])) >= 1
    await h.assert_no_orphan_locks()


async def test_lost_handle_on_a_transfer_asks_a_human(h, org_token):
    """The one the whole design exists for: a liquid transfer with an unknown
    outcome must not be retried and must not be assumed done."""
    run = await one_op_run(org_token["id"], "liquid_transfer", "transfer")
    assert await h.spin(8, until=await _until_running(run["id"]))

    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    await db.execute("update device_jobs set forgotten=true where step_id=$1", step["id"])
    await db.execute("update steps set job_handle=null where id=$1", step["id"])

    await h.restart()
    await h.spin(4)

    ivs = await h.open_interventions()
    assert len(ivs) == 1, f"expected one intervention, got {[dict(i) for i in ivs]}"
    assert ivs[0]["kind"] == "sample_integrity_unknown"
    assert await h.run_state(run["id"]) == "blocked_on_intervention"
    assert (await db.fetchval("select state from steps where id=$1", step["id"])) \
        == "blocked_on_human"

    # And it is still held: the plate is in the machine.
    held = await h.held_reservations()
    assert len(held) == 1 and held[0]["device_released_at"] is None
    await h.assert_plate_in_one_place()


async def test_restart_does_not_strand_a_reserved_but_unstarted_step(h, org_token):
    """Crash after the reservation, before the plate lands. The plate keeps
    moving, the reservation is still valid, and the step starts normally."""
    run = await simple_run(org_token["id"], "strand")
    await h.sched.tick()

    scheduled = await db.fetchval(
        "select count(*) from steps where run_id=$1 and state='scheduled'", run["id"]
    )
    assert scheduled == 1, "expected a reservation on the first tick"

    await h.restart()
    await h.spin(25, until=lambda: _run_done(run["id"]))
    assert await h.run_state(run["id"]) == "done", await h.step_states(run["id"])
    await h.assert_no_orphan_locks()


async def test_the_loop_survives_a_tick_error_it_cannot_even_log(h, org_token):
    """The failure that actually happened: Postgres restarted under a running
    demo, the tick raised, and the handler's own `tick.error` write raised the
    same way, out of the `except` block, out of `run_forever`, and the lab
    sat frozen at tick 92,495 while the API kept serving its last known state.

    The audit line is expendable. The loop is not.
    """
    run = await simple_run(org_token["id"], "blip")
    sched = h.sched
    real_tick, outages = sched.tick, {"left": 3}

    async def flaky_tick():
        if outages["left"]:
            outages["left"] -= 1
            raise ConnectionError("[Errno 2] No such file or directory")
        await real_tick()

    async def audit_is_also_down(*a, **kw):
        raise ConnectionError("[Errno 2] No such file or directory")

    sched.tick = flaky_tick
    sched._audit = audit_is_also_down

    task = asyncio.create_task(sched.run_forever())
    try:
        for _ in range(200):                       # ~10s at tick_interval_s=0.05
            if sched.ticks > 0 and not outages["left"]:
                break
            await asyncio.sleep(0.05)
    finally:
        sched.stop()
        await asyncio.wait_for(task, 5)

    assert not task.cancelled()
    assert task.exception() is None, "the loop left through its own error handler"
    assert sched.error_count == 3, "every failed tick should still be counted"
    assert sched.ticks > 0, "the loop never resumed real work after the outage"
    assert sched.last_error is None, "a recovered loop must not stay red"
