"""Cohort resolution is one decision, so it has to be one transaction.

The docstring said "transactionally" while the implementation looped a
function that opens its own transaction per member. Nothing in the UI reached
it, which is exactly how a false claim survives: dead code carrying a promise
nobody exercised. A failure halfway through left half a cohort resolved and
half open, from a screen whose whole pitch is that one answer covers all forty
plates.
"""
from __future__ import annotations

import pytest

from labsched import db, interventions
from labsched.faults import FaultKind
from tests.conftest import read_only_run


async def _cohort(h, org_token, n: int = 2):
    """Two out-of-range reads on the same instrument share a group key.

    That is the shape the batch UI exists for: same instrument, same question,
    asked once per plate.
    """
    # One reader, so both reads land on it and both questions share a group.
    await db.execute("delete from device_calibration_epochs where device_id='bli-2'")
    await db.execute("delete from devices where id='bli-2'")

    for i in range(n):
        await read_only_run(org_token["id"], f"cohort-{i}")
        await h.force_fault(FaultKind.UNEXPECTED_READING, device_id="bli-1")
    assert await h.spin(40, until=lambda: _open_count(n)), "cohort never formed"

    groups = await db.fetch(
        "select group_key, count(*) c from interventions where state='open'"
        " group by group_key order by c desc")
    assert groups[0]["c"] == n, f"expected one group of {n}, got {[dict(g) for g in groups]}"
    return groups[0]["group_key"]


async def _open_count(n: int) -> bool:
    return await db.fetchval(
        "select count(*) from interventions where state='open'") >= n


async def test_a_failing_member_leaves_the_whole_cohort_open(h, org_token, monkeypatch):
    group = await _cohort(h, org_token)
    before = await db.fetch(
        "select id, state from interventions where group_key=$1 order by id", group)

    real = interventions._apply
    calls = {"n": 0}

    async def explode(conn, iv, option, actor, transit_s):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("instrument bus died mid-cohort")
        return await real(conn, iv, option, actor, transit_s)

    monkeypatch.setattr(interventions, "_apply", explode)

    with pytest.raises(RuntimeError):
        await interventions.resolve_batch(
            group, "rerun_step", token_id="tok-org")

    after = await db.fetch(
        "select id, state from interventions where group_key=$1 order by id", group)
    assert [dict(r) for r in after] == [dict(r) for r in before], \
        "a half-applied cohort survived the failure"
    assert await db.fetchval(
        "select count(*) from audit where action='intervention.resolved'") == 0


async def test_a_clean_batch_resolves_every_member_once(h, org_token):
    group = await _cohort(h, org_token)
    n = await db.fetchval(
        "select count(*) from interventions where group_key=$1 and state='open'", group)
    assert n >= 1

    out = await interventions.resolve_batch(
        group, "rerun_step", token_id="tok-org")

    assert len(out["resolved"]) == n
    assert out["skipped"] == []
    assert await db.fetchval(
        "select count(*) from interventions where group_key=$1 and state='open'",
        group) == 0
    # One batch id over the whole cohort, so the audit reads as one decision.
    ids = await db.fetch(
        "select distinct batch_id from interventions where group_key=$1", group)
    assert [r["batch_id"] for r in ids] == [out["batch_id"]]
