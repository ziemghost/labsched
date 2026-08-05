"""Faults: the auto-recoverable ones stay silent, the ambiguous ones stop and
ask, and the answer a human gives actually moves the lab."""
from __future__ import annotations

import pytest

from labsched import db, interventions
from labsched.faults import HUMAN_FAULTS, FaultKind
from labsched.runs import RunRequest, submit
from tests.conftest import one_op_run, read_only_run, simple_run





async def _running(run_id):
    return await db.fetchval(
        "select count(*) from steps where run_id=$1 and state='running'", run_id) > 0


async def _settled(run_id):
    return await db.fetchval("select state from runs where id=$1", run_id) in (
        "done", "failed", "cancelled")


async def _has_intervention():
    return await db.fetchval("select count(*) from interventions where state='open'") > 0


# ------------------------------------------------------------ auto faults ---

async def test_offline_instrument_is_drained_and_the_step_moves(h, org_token):
    """A reader dying mid-read needs no human: the read is repeatable, so the
    plate comes out, the step is requeued, and the other reader picks it up."""
    run = await one_op_run(org_token["id"], "bli_read", "read")
    assert await h.spin(8, until=lambda: _running(run["id"]))
    first_device = await db.fetchval(
        "select device_id from steps where run_id=$1", run["id"])

    await h.driver.knock_offline(first_device, seconds=None, reason="power lost")
    assert await h.spin(25, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])

    assert await h.open_interventions() == [], "a repeatable read escalated needlessly"
    assert await h.run_state(run["id"]) == "done"
    final_device = await db.fetchval(
        "select device_id from steps where run_id=$1", run["id"])
    assert final_device != first_device, "did not fail over to the other instrument"
    assert (await db.fetchval("select state from devices where id=$1", first_device)) == "offline"
    await h.assert_no_orphan_locks()
    await h.assert_plate_in_one_place()


async def test_a_hung_instrument_times_out_and_fails_over(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.DEVICE_TIMEOUT)

    assert await h.spin(30, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"

    kinds = [a["detail"].get("kind") for a in
             await db.fetch("select detail from audit where action='fault.auto'")]
    assert FaultKind.DEVICE_TIMEOUT in kinds
    assert (await db.fetchval("select attempt from steps where run_id=$1", run["id"])) >= 1
    await h.assert_no_orphan_locks()


async def test_a_transient_comms_error_is_retried_not_escalated(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.COMMS_ERROR)

    assert await h.spin(25, until=lambda: _settled(run["id"]))
    assert await h.run_state(run["id"]) == "done"
    assert await h.open_interventions() == []
    retried = await db.fetch("select * from audit where action='step.start_retry'")
    assert retried, "no evidence the submission was retried"


async def test_a_step_that_runs_out_of_attempts_fails_the_run(h, org_token):
    """Auto-recovery is bounded. Three instruments, all hostile, and the run
    fails rather than looping forever."""
    run = await one_op_run(org_token["id"], "bli_read", "read")
    for _ in range(6):
        await h.force_fault(FaultKind.DEVICE_TIMEOUT)

    assert await h.spin(40, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "failed"
    await h.assert_no_orphan_locks()
    await h.assert_plate_in_one_place()


# ----------------------------------------------------------- human faults ---

# Which operation makes each fault plausible. `results_suspect` is derived by
# looking backwards rather than raised by an instrument, so it is not injectable
# and is tested separately.
OP_FOR = {
    FaultKind.SAMPLE_INTEGRITY_UNKNOWN: "liquid_transfer",
    FaultKind.BATCH_DESTROYED: "incubate",
    FaultKind.UNEXPECTED_READING: "bli_read",
    FaultKind.PLATE_STUCK: "liquid_transfer",
    FaultKind.CALIBRATION_DRIFT: "bli_read",
}
INJECTABLE_HUMAN = list(OP_FOR)


@pytest.mark.parametrize("kind", INJECTABLE_HUMAN)
async def test_each_human_fault_opens_the_right_intervention(h, org_token, kind):
    run = await one_op_run(org_token["id"], OP_FOR[kind], "op")
    await h.force_fault(kind)
    assert await h.spin(20, until=_has_intervention), f"{kind} never escalated"

    iv = (await h.open_interventions())[0]
    spec = HUMAN_FAULTS[kind]
    assert iv["kind"] == kind
    assert [o["key"] for o in iv["options"]] == [o.key for o in spec.options]
    assert all(o["consequence"] for o in iv["options"]), "options must state consequences"
    assert iv["holds"]["device"] == spec.hold_device
    assert iv["holds"]["sample"] == spec.hold_sample
    assert iv["holds"]["rationale"]

    assert await h.run_state(run["id"]) == "blocked_on_intervention"
    assert (await db.fetchval("select state from steps where run_id=$1", run["id"])) \
        == "blocked_on_human"

    # The declared hold policy is what actually happened.
    res = await db.fetchrow("select * from reservations where run_id=$1", run["id"])
    assert (res["device_released_at"] is None) == spec.hold_device
    assert (res["sample_released_at"] is None) == spec.hold_sample

    await h.assert_plate_in_one_place()


@pytest.mark.parametrize("kind", INJECTABLE_HUMAN)
async def test_a_blocked_run_makes_no_further_progress_on_its_own(h, org_token, kind):
    """No timeout, no default, no guess. It waits."""
    run = await one_op_run(org_token["id"], OP_FOR[kind], "op")
    await h.force_fault(kind)
    assert await h.spin(20, until=_has_intervention)

    await h.spin(4)
    assert await h.run_state(run["id"]) == "blocked_on_intervention"
    assert len(await h.open_interventions()) == 1


async def test_rejects_an_option_that_does_not_belong_to_the_fault(h, org_token):
    run = await one_op_run(org_token["id"], "incubate", "inc")
    await h.force_fault(FaultKind.BATCH_DESTROYED)
    assert await h.spin(20, until=_has_intervention)
    iv = (await h.open_interventions())[0]

    with pytest.raises(interventions.InterventionError, match="not an option"):
        await interventions.resolve(iv["id"], "quarantine_device", token_id="tok-org", reason="test")


async def test_an_intervention_cannot_be_resolved_twice(h, org_token):
    run = await one_op_run(org_token["id"], "incubate", "inc")
    await h.force_fault(FaultKind.BATCH_DESTROYED)
    assert await h.spin(20, until=_has_intervention)
    iv = (await h.open_interventions())[0]

    await interventions.resolve(iv["id"], "abort_run", token_id="tok-org", reason="test")

    # Re-resolving is not an error to be retried; it is a conflict
    # carrying the prior decision, so a caller can tell "someone beat me to it"
    # from "my request was bad".
    with pytest.raises(interventions.AlreadyResolved) as exc:
        await interventions.resolve(iv["id"], "abort_run", token_id="tok-org",
                                    reason="test")
    assert exc.value.intervention["resolution"] == "abort_run"
    assert exc.value.intervention["resolved_by_token"] == "tok-org"


# --------------------------------------------- resolutions drive the lab ---

async def test_quarantine_really_takes_the_instrument_out_of_service(h, org_token):
    """Not a button that closes a ticket: the device goes offline, its work
    moves, and the scheduler stops choosing it."""
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.CALIBRATION_DRIFT)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    device = iv["device_id"]
    await interventions.resolve(iv["id"], "quarantine_device", token_id="tok-org", reason="test")

    dev = await db.fetchrow("select * from devices where id=$1", device)
    assert dev["quarantined"] is True and dev["state"] == "offline"

    assert await h.spin(25, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"
    assert (await db.fetchval("select device_id from steps where run_id=$1", run["id"])) \
        != device, "work was scheduled back onto a quarantined instrument"
    await h.assert_no_orphan_locks()


async def test_accepting_a_drifted_reading_keeps_the_instrument_running(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.CALIBRATION_DRIFT)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    device = iv["device_id"]
    await interventions.resolve(iv["id"], "ignore_continue", token_id="tok-org", reason="test")
    await h.spin(6, until=lambda: _settled(run["id"]))

    dev = await db.fetchrow("select * from devices where id=$1", device)
    assert dev["quarantined"] is False
    assert dev["suspect"] is True, "an instrument we chose to trust anyway must stay flagged"
    assert await h.run_state(run["id"]) == "done"
    result = await db.fetchval("select result from steps where run_id=$1", run["id"])
    assert result.get("accepted_with_drift") is True


async def test_discarding_a_sample_destroys_it_and_aborts_the_run(h, org_token):
    run = await simple_run(org_token["id"], "doomed")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    await interventions.resolve(iv["id"], "discard_abort", token_id="tok-org", reason="test")
    await h.spin(6)

    assert await h.run_state(run["id"]) == "failed"
    sample = await db.fetchrow("select * from samples where id=$1", iv["sample_id"])
    assert sample["state"] == "destroyed"
    assert sample["location_kind"] == "storage", "a destroyed plate must not sit in a machine"
    assert await h.held_reservations() == []
    await h.assert_no_orphan_locks()


async def test_accept_and_continue_completes_the_step_and_the_run_proceeds(h, org_token):
    run = await simple_run(org_token["id"], "accepted")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    await interventions.resolve(iv["id"], "accept_continue", token_id="tok-org", reason="test")

    assert await h.spin(30, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"
    states = await h.step_states(run["id"])
    assert states == {"prep": "done", "incubate": "done", "read": "done"}
    result = await db.fetchval(
        "select result from steps where run_id=$1 and idx=0", run["id"])
    assert result.get("accepted_by_operator") is True


async def test_redo_step_reruns_it_from_scratch(h, org_token):
    run = await simple_run(org_token["id"], "redone")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    await interventions.resolve(iv["id"], "redo_step", token_id="tok-org", reason="test")

    assert await h.spin(35, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"
    step = await db.fetchrow("select * from steps where run_id=$1 and idx=0", run["id"])
    assert step["attempt"] >= 1, "the step was not actually re-run"
    assert step["result"].get("accepted_by_operator") is None


async def test_reprep_issues_a_fresh_plate_and_restarts_the_run(h, org_token):
    run = await simple_run(org_token["id"], "reprep")
    await h.force_fault(FaultKind.BATCH_DESTROYED)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    old_sample = iv["sample_id"]
    await interventions.resolve(iv["id"], "reprep_restart", token_id="tok-org", reason="test")

    old = await db.fetchrow("select * from samples where id=$1", old_sample)
    assert old["state"] == "destroyed"

    new_sample = await db.fetchval(
        "select distinct sample_id from steps where run_id=$1", run["id"])
    assert new_sample != old_sample, "the run was restarted onto the cooked plate"
    fresh = await db.fetchrow("select * from samples where id=$1", new_sample)
    assert fresh["state"] in ("parked", "ok", "in_transit")

    assert await h.spin(40, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"
    await h.assert_no_orphan_locks()
    await h.assert_plate_in_one_place()


async def test_a_lost_plate_aborts_the_run_and_frees_the_instrument(h, org_token):
    run = await simple_run(org_token["id"], "jammed")
    await h.force_fault(FaultKind.PLATE_STUCK)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    device = iv["device_id"]
    assert (await db.fetchval("select state from devices where id=$1", device)) == "faulted"

    await interventions.resolve(iv["id"], "plate_lost", token_id="tok-org", reason="test")
    await h.spin(6)

    assert await h.run_state(run["id"]) == "failed"
    assert (await db.fetchval("select state from samples where id=$1", iv["sample_id"])) \
        == "destroyed"
    dev = await db.fetchrow("select * from devices where id=$1", device)
    assert dev["state"] == "idle", f"instrument left {dev['state']} after the jam was cleared"
    assert await h.held_reservations() == []


async def test_freeing_a_stuck_plate_resumes_the_run(h, org_token):
    run = await simple_run(org_token["id"], "freed")
    await h.force_fault(FaultKind.PLATE_STUCK)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    await interventions.resolve(iv["id"], "freed_resume", token_id="tok-org", reason="test")

    assert await h.spin(35, until=lambda: _settled(run["id"])), \
        await h.step_states(run["id"])
    assert await h.run_state(run["id"]) == "done"
    await h.assert_no_orphan_locks()


async def test_rereading_excludes_the_instrument_that_misread(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    bad_device = iv["device_id"]
    await interventions.resolve(iv["id"], "rerun_step", token_id="tok-org", reason="test")

    assert await h.spin(25, until=lambda: _settled(run["id"]))
    assert await h.run_state(run["id"]) == "done"
    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    assert bad_device in step["tried_devices"]
    assert step["device_id"] != bad_device


async def test_every_resolution_is_attributed_in_the_audit_log(h, org_token):
    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(20, until=_has_intervention)

    iv = (await h.open_interventions())[0]
    await interventions.resolve(iv["id"], "accept_reading", token_id="tok-org",
                                reason="looks real, controls agree")

    row = await db.fetchrow(
        "select * from audit where action='intervention.resolved' and intervention_id=$1",
        iv["id"])
    assert row is not None
    # The credential is what is recorded, not a name someone typed.
    assert row["actor"] == "token:tok-org"
    assert row["token_id"] == "tok-org"
    assert row["detail"]["resolution"] == "accept_reading"
    assert row["detail"]["reason"] == "looks real, controls agree"
    # And the computed blast radius is recorded with the decision.
    assert "consequences" in row["detail"]

    stored = await db.fetchrow("select * from interventions where id=$1", iv["id"])
    assert stored["resolved_by_token"] == "tok-org" and stored["state"] == "resolved"
    assert stored["resolution_reason"] == "looks real, controls agree"


# Every option that destroys a plate, and the fault that offers it. These are
# the irreversible, sample-owner buttons, whose printed number an
# operator has the least opportunity to check.
DESTROYING_OPTIONS = [
    (FaultKind.SAMPLE_INTEGRITY_UNKNOWN, "discard_abort"),
    (FaultKind.PLATE_STUCK, "plate_lost"),
    (FaultKind.BATCH_DESTROYED, "abort_run"),
    (FaultKind.BATCH_DESTROYED, "reprep_restart"),
]


@pytest.mark.parametrize("kind,option", DESTROYING_OPTIONS)
async def test_the_panel_promises_exactly_the_plates_the_button_destroys(
        h, org_token, kind, option):
    """`plates_destroyed` was `len(affected_sample_ids)`, neither what
    `_apply` does nor what the samples table says. On `batch_destroyed` it was
    wrong in both directions at once: the cohort is destroyed when the question
    *opens*, so `abort_run` advertised a plate and destroyed none, while
    `reprep_restart` advertised one and re-prepped every plate of the run.

    A three-plate run makes the two candidate answers differ, so this cannot
    pass by coincidence. The property is the general one: the number printed
    under an irreversible button equals the rows that actually become
    `destroyed` when it is pressed.
    """
    run, _ = await submit(RunRequest(
        name="promise", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plate_count=3))

    await h.force_fault(kind)
    assert await h.spin(60, until=_has_intervention), f"{kind} never escalated"
    iv = (await h.open_interventions())[0]
    assert iv["kind"] == kind

    plates = await db.fetchval(
        "select count(distinct sample_id) from steps where run_id=$1", run["id"])
    assert plates == 3, f"expected a three-plate run, got {plates}"

    promised = (await interventions.consequences(iv["id"], option))["plates_destroyed"]

    async def destroyed() -> set[str]:
        return {r["id"] for r in
                await db.fetch("select id from samples where state='destroyed'")}

    before = await destroyed()
    await interventions.resolve(iv["id"], option, token_id="tok-org", reason="test")
    newly = await destroyed() - before

    assert promised == len(newly), (
        f"{kind}/{option}: the panel promised {promised} plate(s) destroyed and the "
        f"button destroyed {len(newly)} ({sorted(newly)}); already destroyed before "
        f"the press: {sorted(before)}")

    # The same number is written into the permanent resolution record, so it is
    # not only a preview that was wrong.
    row = await db.fetchrow(
        "select detail from audit where action='intervention.resolved'"
        "   and intervention_id=$1", iv["id"])
    assert row["detail"]["consequences"]["plates_destroyed"] == len(newly)


async def test_the_two_options_that_waive_a_reason_do_not_demand_one(h, org_token):
    """A typed reason is demanded by anything irreversible, which is the right
    default but not a law. `accept_continue` and `discard_abort` opt out: the
    question is already "you decide", and a box the operator fills with "ok" is
    friction that buys no record. Reversibility is unchanged, so both keep the
    red styling and the confirm step.
    """
    run = await one_op_run(org_token["id"], "liquid_transfer", "waived")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(20, until=_has_intervention)
    iv = (await h.open_interventions())[0]

    cons = await interventions.consequences(iv["id"], "accept_continue")
    assert cons["reversible"] is False, "the option is still irreversible"
    assert cons["requires_reason"] is False, "and the panel has to say so"

    await interventions.resolve(iv["id"], "accept_continue", token_id="tok-org")
    stored = await db.fetchrow("select * from interventions where id=$1", iv["id"])
    assert stored["state"] == "resolved" and stored["resolution_reason"] is None
