"""What an expired SLA does, and more importantly what it does not do.

The sweep is the one place where the scheduler acts on an open question by
itself, so it is the one place where "the safe default" has to be checked
rather than asserted in a docstring. It used to park every affected plate on
expiry, including the plate of a `plate_stuck` fault, whose whole message is
that the plate is in a gripper and a person has to open the door. The plate
walked itself back to storage ninety seconds later and the audit recorded it as
an applied resolution.
"""
from __future__ import annotations

import pytest

from labsched import db
from labsched.faults import HUMAN_FAULTS, FaultKind
from tests.conftest import one_op_run

OP_FOR = {
    FaultKind.PLATE_STUCK: "liquid_transfer",
    FaultKind.SAMPLE_INTEGRITY_UNKNOWN: "liquid_transfer",
    FaultKind.UNEXPECTED_READING: "bli_read",
    FaultKind.CALIBRATION_DRIFT: "bli_read",
}


async def _open_intervention(h, kind: str):
    await one_op_run("tok-org", OP_FOR[kind], "op")
    await h.force_fault(kind)
    assert await h.spin(20, until=lambda: db.fetchval(
        "select count(*) from interventions where state='open'")), f"{kind} never opened"
    return await db.fetchrow("select * from interventions where state='open'")


async def _expire(intervention_id: str):
    await db.execute(
        "update interventions set expires_at = now() - interval '1 second' where id=$1",
        intervention_id)


@pytest.mark.parametrize("kind", [FaultKind.PLATE_STUCK, FaultKind.SAMPLE_INTEGRITY_UNKNOWN])
async def test_expiry_does_not_move_a_plate_the_question_holds(h, org_token, kind):
    iv = await _open_intervention(h, kind)
    assert HUMAN_FAULTS[kind].hold_sample, "this test is about held plates"

    before = await db.fetchrow("select * from samples where id=$1", iv["sample_id"])
    await _expire(iv["id"])
    await h.sched.sweep_slas()

    after = await db.fetchrow("select * from samples where id=$1", iv["sample_id"])
    assert after["location_kind"] == before["location_kind"], "held plate was moved by a timer"
    assert after["location_device_id"] == before["location_device_id"]
    assert after["state"] == before["state"]

    still = await db.fetchrow("select * from interventions where id=$1", iv["id"])
    assert still["state"] == "open", "an expiry answered the question"
    assert still["detail"]["escalated"] is True


async def test_expiry_still_parks_a_plate_the_question_does_not_hold(h, org_token):
    """The converse, so the fix is not just "do nothing on expiry".

    In practice a fault that does not hold its plate parked it when it opened,
    so this pass is belt and braces, but it still has to happen,
    and it has to be recorded, or "park and hold" is only the hold half.
    """
    kind = FaultKind.CALIBRATION_DRIFT
    iv = await _open_intervention(h, kind)
    assert not HUMAN_FAULTS[kind].hold_sample

    await _expire(iv["id"])
    await h.sched.sweep_slas()

    after = await db.fetchrow("select * from samples where id=$1", iv["sample_id"])
    assert after["location_kind"] in ("transit", "storage")
    assert after["location_device_id"] is None

    row = await db.fetchrow(
        "select * from audit where action='intervention.escalated' and intervention_id=$1",
        iv["id"])
    assert row["detail"]["parked_samples"] == [iv["sample_id"]]


async def test_the_audit_does_not_claim_an_option_was_applied(h, org_token):
    iv = await _open_intervention(h, FaultKind.PLATE_STUCK)
    await _expire(iv["id"])
    await h.sched.sweep_slas()

    row = await db.fetchrow(
        "select * from audit where action='intervention.escalated' and intervention_id=$1",
        iv["id"])
    assert row is not None, "escalation was not audited"
    detail = row["detail"]
    assert "applied" not in detail, "escalation logged as though it resolved something"
    assert detail["policy"] == "park_and_hold"
    assert detail["still_held"] == {"device": True, "sample": True}
    assert detail["parked_samples"] == []


async def test_an_unanswered_question_keeps_escalating(h, org_token):
    """The first fix escalated once and then bumped the deadline by another
    120s, a countdown the UI and every agent watched tick to zero and then do
    nothing, forever. A question nobody has answered should keep getting
    louder."""
    iv = await _open_intervention(h, FaultKind.PLATE_STUCK)

    for expected in (1, 2, 3):
        await _expire(iv["id"])
        await h.sched.sweep_slas()
        row = await db.fetchrow("select * from interventions where id=$1", iv["id"])
        assert row["detail"]["escalations"] == expected, \
            f"escalation {expected} never fired"
        assert row["state"] == "open"

    assert await db.fetchval(
        "select count(*) from audit where action='intervention.escalated'"
        " and intervention_id=$1", iv["id"]) == 3


async def test_park_and_hold_is_a_policy_not_a_choosable_option(h, org_token):
    """`park_and_hold` is deliberately not a key on any option list: an SLA
    escalates, it never decides. If it ever becomes one, this fails."""
    for spec in HUMAN_FAULTS.values():
        assert spec.escalation_policy not in [o.key for o in spec.options], (
            f"{spec.kind}: the escalation policy is also a resolvable option, which "
            "means an expiry could take it")


async def test_expiry_leaves_a_plate_that_is_in_a_machine_right_now(h, org_token):
    """`holds.sample` is a property of the fault kind; the affected set of a
    backwards-reaching question is every plate the instrument ever touched --
    including one a different, live step is running. Parking those walked a
    plate out of a machine mid-step: the floor tile said "busy, holds this
    plate, step running" while the plate said "parked, storage"."""
    from labsched import interventions
    from tests.conftest import one_op_run

    working = await one_op_run(org_token["id"], "incubate", "still-running")

    async def in_a_machine():
        # Not merely reserved: physically on the instrument. A scheduled step's
        # plate spends the transit window in `transit`, which is a different
        # thing to leave alone.
        return await db.fetchval(
            "select 1 from reservations r join steps s on s.id = r.step_id"
            "  join samples sm on sm.id = r.sample_id"
            " where s.run_id=$1 and r.sample_released_at is null"
            "   and s.state in ('scheduled','running')"
            "   and sm.location_kind = 'device'", working["id"])
    assert await h.spin(30, until=in_a_machine), "no plate ever went into a machine"

    plate, device = await db.fetchrow(
        "select r.sample_id, r.device_id from reservations r"
        "  join steps s on s.id = r.step_id"
        " where s.run_id=$1 and r.sample_released_at is null", working["id"])

    # A question that does not hold plates, reaching backwards over a set that
    # happens to include the one in the machine. Opened directly rather than
    # through a second run, so no further ticks can end the live step first.
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            iid = await interventions.open_intervention(
                conn, kind=FaultKind.RESULTS_SUSPECT, run_id=working["id"],
                step_id=None, device_id=device, sample_id=None,
                actor="operator", detail={"epoch": 1},
                extra_samples=[plate], extra_runs=[working["id"]])
    assert not HUMAN_FAULTS[FaultKind.RESULTS_SUSPECT].hold_sample

    await _expire(iid)
    await h.sched.sweep_slas()

    sample = await db.fetchrow("select * from samples where id=$1", plate)
    assert sample["location_kind"] == "device" and \
        sample["location_device_id"] == device, \
        "an overdue question walked a plate out of an instrument mid-step"
    row = await db.fetchrow(
        "select * from audit where action='intervention.escalated'"
        "   and intervention_id=$1", iid)
    assert plate in (row["detail"].get("samples_in_use") or []), \
        "the escalation did not record what it left where it was"
