"""The invariants that must not break: exclusivity, plate location, priority."""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from labsched import db
from labsched.runs import RunRequest, submit
from tests.conftest import one_op_run, read_only_run, simple_run


async def test_database_refuses_a_second_hold_on_one_device(h, org_token):
    """The guarantee is an index, not scheduler discipline. Try to break it by
    hand, with the scheduler out of the picture entirely."""
    # Two runs, so the plates differ and the only thing in contention is lh-1.
    run_a = await simple_run(org_token["id"], "r1")
    run_b = await simple_run(org_token["id"], "r2")

    async def reserve(res_id, run):
        await db.execute(
            "insert into reservations(id, run_id, step_id, device_id, sample_id, token_id)"
            " values ($1,$2,$3,'lh-1',$4,$5)",
            res_id, run["id"], run["steps"][0]["id"],
            run["steps"][0]["sample_id"], org_token["id"],
        )

    await reserve("res-a", run_a)
    with pytest.raises(asyncpg.UniqueViolationError):
        await reserve("res-b", run_b)

    # Releasing the instrument frees it for the next holder, and only then.
    await db.execute("update reservations set device_released_at=now() where id='res-a'")
    await reserve("res-b", run_b)


async def test_database_refuses_a_second_hold_on_one_plate(h, org_token):
    run = await simple_run(org_token["id"], "r1")
    sample = run["steps"][0]["sample_id"]
    await db.execute(
        "insert into reservations(id, run_id, step_id, device_id, sample_id, token_id)"
        " values ('res-a',$1,$2,'lh-1',$3,$4)",
        run["id"], run["steps"][0]["id"], sample, org_token["id"],
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "insert into reservations(id, run_id, step_id, device_id, sample_id, token_id)"
            " values ('res-b',$1,$2,'lh-2',$3,$4)",
            run["id"], run["steps"][1]["id"], sample, org_token["id"],
        )


async def test_concurrent_dispatch_never_double_books(h, org_token):
    """Twelve steps, all wanting a bli_reader, only two readers. Fire the
    reservation path at all of them at once and check the outcome."""
    runs = [await read_only_run(org_token["id"], f"ro{i}") for i in range(12)]
    step_ids = [r["steps"][0]["id"] for r in runs]

    results = await asyncio.gather(
        *(h.sched._try_reserve(sid) for sid in step_ids), return_exceptions=True
    )
    granted = sum(1 for r in results if r is True)
    violations = [r for r in results if isinstance(r, BaseException)
                  and not isinstance(r, asyncpg.UniqueViolationError)]

    assert violations == [], f"unexpected errors: {violations}"
    assert granted <= 2, f"{granted} reservations handed out for 2 instruments"
    await h.assert_no_double_booking()
    await h.assert_plate_in_one_place()


async def test_invariants_hold_across_a_busy_lab(h, org_token):
    """Eight competing runs, checked after every single tick."""
    for i in range(8):
        await simple_run(org_token["id"], f"run{i}", priority=i % 3)

    for _ in range(260):
        await h.sched.tick()
        await h.assert_no_double_booking()
        await h.assert_plate_in_one_place()
        await h.assert_no_orphan_locks()
        await asyncio.sleep(0.02)

    states = [r["state"] for r in await db.fetch("select state from runs")]
    assert states.count("done") >= 6, f"expected most runs to finish, got {states}"


async def test_priority_wins_and_fifo_breaks_ties(h, org_token):
    """One instrument, three contenders. Highest priority goes first; equal
    priorities go in submission order."""
    # Narrow the fleet to one reader by taking the others out of service,
    # which is what actually happens in a lab, and keeps their history.
    await db.execute(
        "update devices set quarantined=true, state='offline' where id <> 'bli-1'")

    low = await read_only_run(org_token["id"], "low")
    await db.execute("update runs set priority=1 where id=$1", low["id"])
    high = await read_only_run(org_token["id"], "high")
    await db.execute("update runs set priority=9 where id=$1", high["id"])
    mid_a = await read_only_run(org_token["id"], "mid_a")
    mid_b = await read_only_run(org_token["id"], "mid_b")
    await db.execute("update runs set priority=5 where id = any($1::text[])",
                     [mid_a["id"], mid_b["id"]])

    order = []
    for _ in range(200):
        await h.sched.tick()
        for row in await db.fetch(
            "select run_id from reservations order by acquired_at, id"
        ):
            if row["run_id"] not in order:
                order.append(row["run_id"])
        if len(order) == 4:
            break
        await asyncio.sleep(0.02)

    assert order == [high["id"], mid_a["id"], mid_b["id"], low["id"]], (
        "expected priority desc then FIFO, got "
        + str([await db.fetchval("select name from runs where id=$1", r) for r in order])
    )


async def test_a_plate_is_never_reserved_while_moving(h, org_token):
    """A plate on the mover is not a plate you can schedule onto."""
    run = await simple_run(org_token["id"], "moving")
    await db.execute(
        "update samples set state='in_transit', location_kind='transit',"
        " location_device_id=null, transit_to='inc-1', transit_started_at=now(),"
        " transit_eta = now() + interval '30 seconds' where id=$1",
        run["steps"][0]["sample_id"],
    )
    got = await h.sched._try_reserve(run["steps"][0]["id"])
    assert got is False
    assert await h.held_reservations() == []


async def test_step_graph_must_be_a_dag(h, org_token):
    from labsched.runs import AdmissionError

    read = {"op": "bli_read", "with": {"target": "T"}}
    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(
            name="cyclic", token_id=org_token["id"],
            steps=[{**read, "name": "a", "after": [1]},
                   {**read, "name": "b", "after": [0]}]))
    assert any(p.code == "cycle" for p in exc.value.problems)
    assert exc.value.status == 422

    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(
            name="selfdep", token_id=org_token["id"],
            steps=[{**read, "name": "a", "after": [0]}]))
    assert any(p.code == "self_dependency" for p in exc.value.problems)


async def test_dependencies_are_respected(h, org_token):
    run = await simple_run(org_token["id"], "ordered")
    seen = []
    for _ in range(160):
        await h.sched.tick()
        for s in await db.fetch(
            "select name, state, started_at from steps where run_id=$1"
            " and started_at is not null order by started_at", run["id"]
        ):
            if s["name"] not in seen:
                seen.append(s["name"])
        if await h.run_state(run["id"]) == "done":
            break
        await asyncio.sleep(0.02)

    assert await h.run_state(run["id"]) == "done"
    assert seen == ["prep", "incubate", "read"]


async def test_the_fleet_does_not_grow_when_an_instrument_is_asked_two_questions(
        h, org_token):
    """`/api/state` left-joined open interventions onto devices, so a machine
    with two open questions came back twice: the header counted six instruments
    as seven and the floor drew a second tile under a duplicate key."""
    import httpx
    from labsched import interventions
    from labsched.api import app
    from labsched.faults import FaultKind

    device = await db.fetchval("select id from devices order by id limit 1")
    run = await one_op_run(org_token["id"], "bli_read", "twice")
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for kind in (FaultKind.CALIBRATION_DRIFT, FaultKind.UNEXPECTED_READING):
                await interventions.open_intervention(
                    conn, kind=kind, run_id=run["id"], step_id=None,
                    device_id=device, sample_id=None, actor="test",
                    detail={"note": "two questions, one machine"})

    fleet = await db.fetchval("select count(*) from devices")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        state = (await c.get("/api/state")).json()

    ids = [d["id"] for d in state["devices"]]
    assert len(ids) == fleet, f"{fleet} instruments came back as {len(ids)}: {ids}"
    assert len(set(ids)) == len(ids), f"duplicate instrument tiles: {ids}"


async def test_other_runs_affected_can_exceed_one(h, org_token):
    """The count was over the runs *holding* the instrument, which a partial
    unique index allows exactly one of, so the number sold as
    the multi-tenancy story was structurally 0 or 1. Taking a machine out
    reaches everything queued that it could have run."""
    from labsched import interventions

    mine = await one_op_run(org_token["id"], "bli_read", "mine")
    for i in range(3):
        await one_op_run(org_token["id"], "bli_read", f"theirs{i}")
    device = await db.fetchval("select id from devices where kind='bli_reader' limit 1")

    assert await interventions.other_runs_on_device(device, mine["id"]) >= 2, \
        "a shared instrument reported reaching at most one other run"


async def test_the_held_badge_counts_the_table_not_the_page(h, org_token):
    """`held_result_count` summed the 120-row payload. `held` is the one state
    that moves backwards in time, since a number is called into question when
    its epoch is, so the badge fell as the lab got
    busier, with nothing resolved, while the rows it stopped counting also
    became unreachable in the Results tab."""
    import httpx
    from labsched import interventions
    from labsched.api import app

    run = await one_op_run(org_token["id"], "bli_read", "badge")
    assert await h.spin(30, until=lambda: db.fetchval(
        "select 1 from results where run_id=$1", run["id"]))
    res = await db.fetchrow("select * from results where run_id=$1", run["id"])
    iid, held = await interventions.mark_epoch_suspect(
        res["device_id"], res["calibration_epoch"], "token:tok-engineer", "audit")
    assert held == 1

    # Bury it: 140 newer results, which is what an afternoon of runs does.
    step = await db.fetchrow("select * from steps where id=$1", res["step_id"])
    for i in range(140):
        sid = f"{step['id']}-filler{i}"
        await db.execute(
            "insert into steps(id, run_id, idx, name, capability, duration_s,"
            " credit_cost, sample_id, state)"
            " values ($1,$2,$3,'filler',$4,1,0,$5,'done')",
            sid, step["run_id"], 100 + i, step["capability"], step["sample_id"])
        await db.execute(
            "insert into results(id, run_id, step_id, sample_id, device_id,"
            " calibration_epoch, payload, state, created_at)"
            " values ($1,$2,$3,$4,$5,$6,'{}'::jsonb,'released',"
            "         now() + make_interval(secs => $7))",
            f"res-filler{i}", step["run_id"], sid, step["sample_id"],
            res["device_id"], res["calibration_epoch"], float(i + 1))

    truth = await db.fetchval("select count(*) from results where state='held'")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        state = (await c.get("/api/state")).json()

    assert state["held_result_count"] == truth, (
        f"badge said {state['held_result_count']} numbers are in question, "
        f"{truth} are")
    assert res["id"] in [r["id"] for r in state["results"]], \
        "the held result the Results tab exists to show fell out of the payload"


async def test_only_the_option_that_takes_the_instrument_out_reaches_other_runs(
        h, org_token):
    """`other_runs_affected` is served in a per-option consequence payload, so
    it has to be a consequence. Computed once from the device, it printed the
    same red chip under 'Release with a caveat', an option that annotates
    result rows and touches no instrument."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "bli_read", "reach")
    for i in range(2):
        await one_op_run(org_token["id"], "bli_read", f"queued{i}")
    assert await h.spin(30, until=lambda: db.fetchval(
        "select 1 from results where run_id=$1", run["id"]))
    res = await db.fetchrow("select * from results where run_id=$1", run["id"])

    iid, _ = await interventions.mark_epoch_suspect(
        res["device_id"], res["calibration_epoch"], "token:tok-engineer", "audit")
    for option in ("accept_with_caveat", "invalidate", "requeue"):
        cons = await interventions.consequences(iid, option)
        assert cons["other_runs_affected"] == 0, (
            f"'{option}' takes no instrument out of service and claimed to "
            f"reach {cons['other_runs_affected']} other runs")

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            drift = await interventions.open_intervention(
                conn, kind=FaultKind.CALIBRATION_DRIFT, run_id=res["run_id"],
                step_id=None, device_id=res["device_id"], sample_id=None,
                actor="qc", detail={"epoch": res["calibration_epoch"]})
    taken = await interventions.consequences(drift, "quarantine_device")
    assert taken["other_runs_affected"] >= 1, \
        "taking the instrument out reported reaching nobody"
