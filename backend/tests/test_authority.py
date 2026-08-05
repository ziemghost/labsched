"""Authority, the results plane, and the faults the system infers for itself.

The claim under test: an agent may schedule work but may not answer the
questions its own runs raise, except where the question is one of budget
rather than physical judgement.
"""
from __future__ import annotations

import pytest

from labsched import db, interventions
from labsched.auth import tokens
from labsched.faults import FaultKind
from labsched.runs import RunRequest, submit
from tests.conftest import one_op_run, read_only_run


async def _roles(org):
    """operator / engineer / owner / plain agent, all under one org."""
    wide = ["liquid_handler", "bli_reader", "incubator", "plate_reader"]
    made = {}
    for label, auths in [("operator", ["operator"]), ("engineer", ["engineer"]),
                         ("owner", ["sample_owner"]), ("agent", [])]:
        made[label] = await tokens.attenuate(
            org["id"], label, "agent",
            tokens.Caveats(wide, 4, 20_000, 300, 2000, tokens.default_expiry(5),
                           authorities=auths),
            token_id=f"tok-{label}")
    return made


async def _open_fault(h, org, kind, op):
    run = await one_op_run(org["id"], op, "op")
    await h.force_fault(kind)
    ok = await h.spin(20, until=lambda: _has_open())
    assert ok, f"{kind} never escalated"
    return run, (await h.open_interventions())[0]


async def _has_open():
    return await db.fetchval("select count(*) from interventions where state='open'") > 0


# -------------------------------------------------------------- authority ---

async def test_an_agent_cannot_answer_the_question_its_own_run_raised(h, org_token):
    roles = await _roles(org_token)
    run = await one_op_run(roles["agent"]["id"], "liquid_transfer", "transfer")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(20, until=_has_open)
    iv = (await h.open_interventions())[0]

    with pytest.raises(interventions.Forbidden):
        await interventions.resolve(iv["id"], "discard_abort",
                                    token_id=roles["agent"]["id"], reason="me")

    # And the intervention is untouched: a refused decision is not a decision.
    assert (await db.fetchval("select state from interventions where id=$1", iv["id"])) == "open"


async def test_destroying_a_plate_needs_the_sample_owner_not_the_operator(h, org_token):
    """The operator is the one standing next to the machine and still may not
    scrap a customer's plate."""
    roles = await _roles(org_token)
    _, iv = await _open_fault(h, org_token, FaultKind.SAMPLE_INTEGRITY_UNKNOWN,
                              "liquid_transfer")

    with pytest.raises(interventions.Forbidden):
        await interventions.resolve(iv["id"], "discard_abort",
                                    token_id=roles["operator"]["id"], reason="no")

    # But the operator may re-run the step, which is an operational call.
    out = await interventions.resolve(iv["id"], "redo_step",
                                      token_id=roles["operator"]["id"])
    assert out["basis"] == "authority:operator"


async def test_quarantining_needs_the_engineer(h, org_token):
    roles = await _roles(org_token)
    _, iv = await _open_fault(h, org_token, FaultKind.CALIBRATION_DRIFT, "bli_read")

    with pytest.raises(interventions.Forbidden):
        await interventions.resolve(iv["id"], "quarantine_device",
                                    token_id=roles["owner"]["id"])
    out = await interventions.resolve(iv["id"], "quarantine_device",
                                      token_id=roles["engineer"]["id"])
    assert out["basis"] == "authority:engineer"
    assert (await db.fetchval("select quarantined from devices where id=$1",
                              iv["device_id"])) is True


async def test_an_agent_may_answer_a_budget_question_for_its_own_run(h, org_token):
    """The payoff of the whole design: the human gate is a capability
    boundary, not a wall. Re-reading costs credits, and the token already
    encodes the budget, so the agent may decide it itself."""
    roles = await _roles(org_token)
    run = await one_op_run(roles["agent"]["id"], "bli_read", "read")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(20, until=_has_open)
    iv = (await h.open_interventions())[0]

    out = await interventions.resolve(iv["id"], "rerun_step",
                                      token_id=roles["agent"]["id"])
    assert out["basis"] == "agent_self_service"

    # ... but only for its own run, and only for that option.
    assert await h.spin(25, until=lambda: _settled(run["id"]))


async def test_an_agent_cannot_self_serve_someone_elses_run(h, org_token):
    roles = await _roles(org_token)
    stranger = await tokens.attenuate(
        org_token["id"], "stranger", "agent",
        tokens.Caveats(["bli_reader"], 2, 5_000, 300, 500, tokens.default_expiry(3),
                       authorities=[]),
        token_id="tok-stranger")

    await one_op_run(roles["agent"]["id"], "bli_read", "read")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(20, until=_has_open)
    iv = (await h.open_interventions())[0]

    with pytest.raises(interventions.Forbidden):
        await interventions.resolve(iv["id"], "rerun_step", token_id=stranger["id"])


async def test_an_irreversible_option_demands_a_written_reason(h, org_token):
    """Irreversible demands a reason unless the option waives it explicitly,
    which only the two `sample_integrity_unknown` answers do."""
    roles = await _roles(org_token)
    _, iv = await _open_fault(h, org_token, FaultKind.PLATE_STUCK, "liquid_transfer")

    with pytest.raises(interventions.InterventionError, match="requires a written reason"):
        await interventions.resolve(iv["id"], "plate_lost",
                                    token_id=roles["operator"]["id"])
    out = await interventions.resolve(iv["id"], "plate_lost",
                                      token_id=roles["operator"]["id"],
                                      reason="gripper jammed, plate cracked")
    assert out["resolution"] == "plate_lost"


async def test_a_stale_screen_is_rejected(h, org_token):
    """A 2am operator deciding against a page rendered twenty minutes ago,
    after the plate has moved."""
    roles = await _roles(org_token)
    _, iv = await _open_fault(h, org_token, FaultKind.PLATE_STUCK, "liquid_transfer")

    await interventions.acknowledge(iv["id"], "marc")   # bumps the version
    with pytest.raises(interventions.StaleVersion):
        await interventions.resolve(iv["id"], "freed_resume",
                                    token_id=roles["operator"]["id"],
                                    expected_version=iv["version"])

    fresh = await db.fetchrow("select * from interventions where id=$1", iv["id"])
    await interventions.resolve(iv["id"], "freed_resume",
                                token_id=roles["operator"]["id"],
                                expected_version=fresh["version"])


async def test_consequences_are_computed_not_prose(h, org_token):
    _, iv = await _open_fault(h, org_token, FaultKind.SAMPLE_INTEGRITY_UNKNOWN,
                              "liquid_transfer")

    discard = await interventions.consequences(iv["id"], "discard_abort")
    redo = await interventions.consequences(iv["id"], "redo_step")

    assert discard["plates_destroyed"] == 1 and discard["runs_aborted"] == 1
    # Irreversible, and one of the two options that waive the typed reason:
    # the question is already "you decide".
    assert discard["reversible"] is False and discard["requires_reason"] is False
    assert redo["plates_destroyed"] == 0 and redo["reversible"] is True
    assert redo["steps_requeued"] == 1, "re-running re-runs a step"
    # Net, not gross: this path refunds what the step paid before charging it
    # again, so the customer is out nothing. It used to advertise the full
    # amount while the ledger went 25 -> 0 -> 25.
    assert redo["credits_spent_again"] == 0
    assert discard["credits_released"] > 0, "aborting gives back what was not consumed"


# ---------------------------------------------------------- results plane ---

async def test_a_finished_step_produces_a_result_row(h, org_token):
    run = await read_only_run(org_token["id"], "res")
    assert await h.spin(25, until=lambda: _settled(run["id"]))

    result = await db.fetchrow("select * from results where run_id=$1", run["id"])
    assert result is not None
    assert result["calibration_epoch"] == 1
    assert result["state"] in ("released", "pending_qc")


async def test_done_does_not_mean_trustworthy(h, org_token):
    """The whole reason the result plane exists: the robot finished and the
    number may still be wrong."""
    run = await read_only_run(org_token["id"], "held")
    assert await h.spin(25, until=lambda: _settled(run["id"]))
    assert await h.run_state(run["id"]) == "done"

    await db.execute("update results set state='held', qc_verdict='fail' where run_id=$1",
                     run["id"])
    assert (await db.fetchval("select state from runs where id=$1", run["id"])) == "done"
    assert (await db.fetchval("select state from results where run_id=$1", run["id"])) == "held"


async def test_calibration_drift_reaches_backwards_over_delivered_results(h, org_token):
    """The most Adaptyv-shaped case: every step green, every number already
    released, and the instrument turns out to have been drifting."""
    roles = await _roles(org_token)
    finished = []
    for i in range(3):
        run = await read_only_run(org_token["id"], f"r{i}")
        assert await h.spin(25, until=lambda rid=run["id"]: _settled(rid))
        finished.append(run["id"])

    device = await db.fetchval("select device_id from results limit 1")
    epoch = await db.fetchval(
        "select calibration_epoch from results where device_id=$1 limit 1", device)
    await db.execute("update results set state='released' where device_id=$1", device)

    iid, pulled = await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "control chart went out of tolerance")
    assert iid, "no question was raised about results already delivered"
    assert pulled >= 1, "the caller is told how many results were pulled back"

    # Marking it again raises no second question about the same results.
    again, pulled_again = await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "same epoch, second report")
    assert again == iid and pulled_again == 0

    iv = await db.fetchrow("select * from interventions where id=$1", iid)
    assert iv["kind"] == FaultKind.RESULTS_SUSPECT
    assert len(iv["affected_result_ids"] if "affected_result_ids" in iv.keys()
               else iv["detail"]["affected_result_ids"]) >= 1
    # Released results are pulled back to held, not silently left delivered.
    held = await db.fetchval(
        "select count(*) from results where device_id=$1 and calibration_epoch=$2"
        " and state='held'", device, epoch)
    assert held >= 1

    await interventions.resolve(iid, "invalidate", token_id=roles["owner"]["id"],
                                reason="cannot stand behind these")
    assert (await db.fetchval(
        "select count(*) from results where device_id=$1 and calibration_epoch=$2"
        " and state='invalidated'", device, epoch)) >= 1


async def test_quarantine_for_drift_closes_the_epoch(h, org_token):
    roles = await _roles(org_token)
    _, iv = await _open_fault(h, org_token, FaultKind.CALIBRATION_DRIFT, "bli_read")
    device = iv["device_id"]

    await interventions.resolve(iv["id"], "quarantine_device",
                                token_id=roles["engineer"]["id"])

    closed = await db.fetchrow(
        "select * from device_calibration_epochs where device_id=$1 and epoch=1", device)
    assert closed["verdict"] == "suspect" and closed["ended_at"] is not None
    # A new epoch is open, so work after the fix is not tarred with the old one.
    assert await db.fetchval(
        "select count(*) from device_calibration_epochs where device_id=$1"
        " and ended_at is null", device) == 1


async def test_qc_derives_drift_from_control_values(h, org_token):
    """The one fault nothing reports.

    The instrument keeps answering heartbeats, keeps completing jobs and keeps
    reporting success. It just starts returning control values off its
    baseline. Every step stays green. The only way to catch it is to compare
    the instrument against its own history.
    """
    device = "bli-1"
    await db.execute("update devices set quarantined=true, state='offline'"
                     " where id <> $1 and 'bli_read' = any(capabilities)", device)

    # Build a believable control history from real reads.
    for i in range(5):
        run = await read_only_run(org_token["id"], f"hist{i}")
        assert await h.spin(25, until=lambda rid=run["id"]: _settled(rid))

    healthy = await db.fetch(
        "select control_value from results where device_id=$1 and control_value is not null",
        device)
    assert len(healthy) >= 4, "the instrument never reported a control value"

    # Now it starts drifting. Nothing is reported as an error.
    await h.driver.set_degraded(device)

    # Silent means silent: the instrument still answers `ok`, and nothing has
    # marked it suspect yet. If the heartbeat gave it away, the control chart
    # below would be confirming a known fact rather than discovering one.
    hb = await h.driver.heartbeat(device)
    assert hb.health.value == "ok", "a silently drifting instrument reported degraded"
    await h.sched.sweep_heartbeats()
    assert not await db.fetchval("select suspect from devices where id=$1", device), \
        "the instrument was marked suspect before a single reading was compared"

    run = await read_only_run(org_token["id"], "drifted")

    async def drift_noticed():
        return await db.fetchval(
            "select count(*) from interventions where device_id=$1 and kind=$2"
            " and state='open'", device, FaultKind.CALIBRATION_DRIFT) > 0

    assert await h.spin(30, until=drift_noticed), "QC never noticed the drift"

    # The step itself succeeded, which is the point. Execution is green and
    # the run is nonetheless held, because the number is in question.
    assert (await db.fetchval(
        "select state from steps where run_id=$1", run["id"])) == "done"
    assert await h.run_state(run["id"]) == "blocked_on_intervention"

    iv = await db.fetchrow(
        "select * from interventions where device_id=$1 and kind=$2 and state='open'",
        device, FaultKind.CALIBRATION_DRIFT)
    assert iv is not None, "QC did not notice the instrument drifting"
    assert iv["detail"]["detected_by"] == "control chart"
    assert iv["detail"]["deviation"] > 0.15

    audited = await db.fetchrow(
        "select * from audit where action='fault.derived' and device_id=$1", device)
    assert audited is not None, "a derived fault must be auditable as derived"

    # The result that triggered it is held, not released.
    held = await db.fetchval(
        "select state from results where run_id=$1", run["id"])
    assert held == "held", f"a suspect number was released anyway ({held})"


async def test_a_control_within_tolerance_releases_the_result(h, org_token):
    device = "bli-1"
    await db.execute("update devices set quarantined=true, state='offline'"
                     " where id <> $1 and 'bli_read' = any(capabilities)", device)

    for i in range(6):
        run = await read_only_run(org_token["id"], f"ok{i}")
        assert await h.spin(25, until=lambda rid=run["id"]: _settled(rid))

    assert await db.fetchval(
        "select count(*) from interventions where kind=$1 and state='open'",
        FaultKind.CALIBRATION_DRIFT) == 0, "a steady instrument was flagged"
    assert await db.fetchval(
        "select count(*) from results where state='released'") >= 4


async def _settled(run_id):
    return await db.fetchval("select state from runs where id=$1", run_id) in (
        "done", "failed", "cancelled")
