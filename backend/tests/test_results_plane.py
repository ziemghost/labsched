"""The results plane has to drain.

`run.state = done` not implying "the number is trustworthy" is the idea this
project is proudest of, and it only means something if a result that nothing is
wrong with actually leaves the queue. `liquid_transfer` and `incubate` have
no control well, and their results used to sit in
`pending_qc` forever because the QC pass returned before releasing them. A
queue that only ever grows is not a review queue, it is a leak with a badge on
it.
"""
from __future__ import annotations

from labsched import db
from tests.conftest import one_op_run, simple_run


async def _states() -> dict[str, str]:
    rows = await db.fetch(
        "select r.id, r.state, s.op from results r join steps s on s.id = r.step_id")
    return {r["op"]: r["state"] for r in rows}


async def test_operations_without_a_control_well_still_leave_the_queue(h, org_token):
    run = await simple_run(org_token["id"], "three-step")
    assert await h.spin(30, until=lambda: _done(run["id"]))

    stuck = await db.fetch(
        "select r.id, s.op from results r join steps s on s.id = r.step_id"
        " where r.state = 'pending_qc'")
    assert stuck == [], f"results stranded in pending_qc: {[dict(s) for s in stuck]}"

    states = await _states()
    assert states.get("liquid_transfer") == "released"
    assert states.get("incubate") == "released"


async def _done(run_id: str) -> bool:
    return await db.fetchval("select state from runs where id=$1", run_id) in (
        "done", "failed", "cancelled")


async def test_a_transfer_does_not_report_an_absorbance_control(h, org_token):
    """A liquid handler has no detector. It used to report `CONTROL 1.004` and
    a QC pass, on the Results tab, next to the reads."""
    run = await one_op_run(org_token["id"], "liquid_transfer", "prep")
    assert await h.spin(30, until=lambda: _done(run["id"]))

    row = await db.fetchrow(
        "select r.* from results r join steps s on s.id = r.step_id"
        " where s.run_id=$1", run["id"])
    assert row is not None
    assert row["control_value"] is None, "a liquid handler reported a control well"
    assert row["state"] == "released"
    assert row["qc_note"] == "operation has no control well"


async def test_a_step_completed_by_a_human_decision_leaves_the_queue(h, org_token):
    """`_complete_step` is the *other* producer of result rows, and it was
    missed the first time the plane was fixed. A number a person explicitly
    accepted has had the only review it will ever get, so it must not sit in
    `pending_qc` waiting for a machine check that will never run."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "bli_read", "read")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(30, until=lambda: db.fetchval(
        "select count(*) from interventions where state='open'"))

    iv = await db.fetchrow("select * from interventions where state='open'")
    await interventions.resolve(iv["id"], "accept_reading", token_id="tok-org")

    row = await db.fetchrow(
        "select r.* from results r join steps s on s.id = r.step_id"
        " where s.run_id=$1", run["id"])
    assert row is not None, "accepting a reading recorded no result"
    assert row["state"] == "released", f"stranded in {row['state']}"
    # `warn`, not `pass`: it was accepted despite an open question, and the
    # note says who accepted it.
    assert row["qc_verdict"] == "warn"
    assert "accepted" in (row["qc_note"] or "")
    assert "tok-org" in (row["qc_note"] or "")


async def test_a_pending_result_has_no_verdict(h, org_token):
    """A verdict is what QC concluded. Before it runs there is nothing to
    conclude, and the column used to default to 'pass'."""
    await db.execute(
        "insert into runs(id, name, priority, state, token_id, allowed_kinds)"
        " values ('run-x','x',0,'pending','tok-org','{}')")
    await db.execute(
        "insert into samples(id, label, state, location_kind)"
        " values ('plate-x','x','ok','storage')")
    await db.execute(
        "insert into steps(id, run_id, idx, name, capability, duration_s,"
        " credit_cost, sample_id, state) values"
        " ('step-x','run-x',0,'x','bli_read',1,1,'plate-x','done')")
    await db.execute(
        "insert into results(id, run_id, step_id, sample_id, device_id, payload)"
        " values ('res-x','run-x','step-x','plate-x','bli-1','{}'::jsonb)")

    row = await db.fetchrow("select * from results where id='res-x'")
    assert row["state"] == "pending_qc"
    assert row["qc_verdict"] is None


async def test_a_read_does_report_one(h, org_token):
    """The converse: the control chart is the whole derived-QC story, so reads
    must keep reporting controls."""
    run = await one_op_run(org_token["id"], "bli_read", "read")
    assert await h.spin(30, until=lambda: _done(run["id"]))

    row = await db.fetchrow(
        "select r.* from results r join steps s on s.id = r.step_id"
        " where s.run_id=$1", run["id"])
    assert row is not None and row["control_value"] is not None


async def test_re_running_after_a_suspect_epoch_actually_re_runs(h, org_token):
    """"Re-run the affected steps" invalidated the results and requeued
    nothing: every step behind a result is `done` by construction, and
    `requeue_step` refuses `done` steps. The customer was left with a run
    marked done whose only number had been withdrawn and a re-run that did not
    exist."""
    from labsched import interventions

    run = await one_op_run(org_token["id"], "bli_read", "suspect")
    assert await h.spin(30, until=lambda: _done(run["id"]))

    device = await db.fetchval("select device_id from results where run_id=$1", run["id"])
    epoch = await db.fetchval(
        "select calibration_epoch from results where run_id=$1", run["id"])
    await db.execute("update results set state='released' where run_id=$1", run["id"])

    iid, pulled = await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "control chart out of tolerance")
    assert iid and pulled >= 1

    before = await db.fetchval("select credits_spent from tokens where id='tok-org'")
    await interventions.resolve(iid, "requeue", token_id=org_token["id"],
                                reason="cannot stand behind these numbers")

    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    assert step["state"] != "done", "the step was never actually requeued"
    assert step["finished_at"] is None and step["result"] is None

    # "Costs their credits again": the instrument time was really consumed, so
    # this is not refunded, and the re-run will be charged when it dispatches.
    assert await db.fetchval(
        "select credits_spent from tokens where id='tok-org'") == before

    assert await h.spin(40, until=lambda: _done(run["id"])), "the re-run never completed"
    fresh = await db.fetchval(
        "select count(*) from results where run_id=$1 and state <> 'invalidated'",
        run["id"])
    assert fresh >= 1, "the re-run produced no new number"


async def test_aborting_a_run_actually_releases_the_credits_it_advertises(h, org_token):
    """The consequence panel printed "N credits released" on every abort and
    the ledger never moved: the only refund sites were the requeue paths."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "liquid_transfer", "abort-me")
    await h.force_fault(FaultKind.SAMPLE_INTEGRITY_UNKNOWN)
    assert await h.spin(30, until=lambda: db.fetchval(
        "select count(*) from interventions where state='open'"))

    iv = await db.fetchrow("select * from interventions where state='open'")
    counts = await interventions.consequences(iv["id"], "discard_abort")
    advertised = counts["credits_released"]
    assert advertised > 0, "nothing was charged; this test would pass vacuously"

    before = await db.fetchval("select credits_spent from tokens where id='tok-org'")
    await interventions.resolve(iv["id"], "discard_abort", token_id=org_token["id"],
                                reason="plate discarded")
    after = await db.fetchval("select credits_spent from tokens where id='tok-org'")

    assert before - after == advertised, (
        f"panel promised {advertised} credits back, ledger moved {before - after}")


async def test_a_reservation_is_refunded_at_most_once(h, org_token):
    from labsched import interventions

    run = await one_op_run(org_token["id"], "bli_read", "once")
    assert await h.spin(20, until=lambda: db.fetchval(
        "select count(*) from reservations where run_id=$1", run["id"]))

    step_id = await db.fetchval("select id from steps where run_id=$1", run["id"])
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            first = await interventions.refund_step(conn, step_id, "test")
            second = await interventions.refund_step(conn, step_id, "test again")
    assert first > 0 and second == 0


async def test_accepting_drift_actually_releases_the_numbers(h, org_token):
    """"Accept drift, keep running · results are released with a caveat" only
    ever released the fault's own step result, which the QC-derived form of
    this question does not have, because it is raised about the instrument.
    The numbers it was about stayed held with no open question attached: a
    badge counting up next to "no decisions pending"."""
    from labsched import interventions
    from labsched.faults import FaultKind

    device = "bli-1"
    await db.execute("update devices set quarantined=true, state='offline'"
                     " where id <> $1 and 'bli_read' = any(capabilities)", device)

    for i in range(5):
        run = await one_op_run(org_token["id"], "bli_read", f"base{i}")
        assert await h.spin(20, until=lambda rid=run["id"]: _done(rid))

    await h.driver.set_degraded(device)
    await one_op_run(org_token["id"], "bli_read", "drifted")
    assert await h.spin(40, until=lambda: db.fetchval(
        "select count(*) from interventions where kind=$1 and state='open'",
        FaultKind.CALIBRATION_DRIFT)), "QC never derived the drift"

    iv = await db.fetchrow(
        "select * from interventions where kind=$1 and state='open'",
        FaultKind.CALIBRATION_DRIFT)
    assert iv["step_id"] is None, "this test is about the instrument-level question"
    held_before = await db.fetchval(
        "select count(*) from results where device_id=$1 and state='held'", device)
    assert held_before >= 1, "QC held nothing; this test would pass vacuously"

    await interventions.resolve(iv["id"], "ignore_continue", token_id=org_token["id"])

    stranded = await db.fetchval(
        "select count(*) from results where device_id=$1 and state='held'", device)
    assert stranded == 0, f"{stranded} result(s) left held with no question open"
    note = await db.fetchval(
        "select qc_note from results where device_id=$1 and state='released'"
        " order by released_at desc limit 1", device)
    assert "drift accepted" in (note or ""), note


async def test_a_consumed_reservation_is_never_refunded(h, org_token):
    """A step can hold more than one reservation once a re-run is possible.
    The refund used to stamp every unrefunded row and pay one, so aborting
    mid-re-run promised both attempts and moved the credits of one."""
    from labsched import interventions

    run = await one_op_run(org_token["id"], "bli_read", "twice")
    assert await h.spin(30, until=lambda: _done(run["id"]))

    charged_once = await db.fetchval(
        "select credits_spent from tokens where id='tok-org'")
    step_id = await db.fetchval("select id from steps where run_id=$1", run["id"])

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            back = await interventions.refund_step(conn, step_id, "test")
    assert back == 0, "a reservation consumed by a finished step was refunded"
    assert await db.fetchval(
        "select credits_spent from tokens where id='tok-org'") == charged_once


async def _drift_question(h, org_token, device="bli-1"):
    """Drive QC into deriving a drift question about the instrument."""
    from labsched.faults import FaultKind

    await db.execute("update devices set quarantined=true, state='offline'"
                     " where id <> $1 and 'bli_read' = any(capabilities)", device)
    for i in range(5):
        run = await one_op_run(org_token["id"], "bli_read", f"base{i}")
        assert await h.spin(20, until=lambda rid=run["id"]: _done(rid))

    await h.driver.set_degraded(device)
    await one_op_run(org_token["id"], "bli_read", "drifted")
    assert await h.spin(40, until=lambda: db.fetchval(
        "select count(*) from interventions where kind=$1 and state='open'",
        FaultKind.CALIBRATION_DRIFT)), "QC never derived the drift"
    return await db.fetchrow(
        "select * from interventions where kind=$1 and state='open'",
        FaultKind.CALIBRATION_DRIFT)


async def test_quarantining_for_drift_hands_the_numbers_to_a_new_question(h, org_token):
    """Quarantine answers "is the instrument drifting" and deliberately does
    not answer "what about the numbers it already produced". That second
    question has to be raised: the code said it was "raised separately"
    while nothing raised it, so the numbers sat held with nothing open."""
    from labsched import interventions
    from labsched.faults import FaultKind

    iv = await _drift_question(h, org_token)
    await interventions.resolve(iv["id"], "quarantine_device",
                                token_id="tok-org", reason="drift confirmed")

    await h.assert_no_stranded_results()
    follow_up = await db.fetchrow(
        "select * from interventions where kind=$1 and state='open'",
        FaultKind.RESULTS_SUSPECT)
    assert follow_up is not None, "the numbers were left with no question about them"
    assert follow_up["detail"]["epoch"] == iv["detail"]["epoch"]


async def test_accepting_drift_leaves_nothing_held(h, org_token):
    from labsched import interventions

    iv = await _drift_question(h, org_token)
    await interventions.resolve(iv["id"], "ignore_continue", token_id="tok-org")
    await h.assert_no_stranded_results()


async def test_a_question_only_releases_its_own_results(h, org_token):
    """The release used to be keyed on device and current epoch, so it reached
    results another still-open question was holding, leaving that question
    open over numbers it no longer held."""
    from labsched import interventions

    iv = await _drift_question(h, org_token)
    device = iv["device_id"]
    epoch = iv["detail"]["epoch"]

    # A second question over the same instrument and epoch, raised by hand.
    other, pulled = await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "operator review")
    assert other and pulled >= 1

    await interventions.resolve(iv["id"], "ignore_continue", token_id="tok-org")

    still = await db.fetchval(
        "select count(*) from results where held_by=$1 and state='held'", other)
    assert still >= 1, "another question's results were released out from under it"
    await h.assert_no_stranded_results()


async def test_re_prep_gives_back_what_the_run_paid(h, org_token):
    """`reprep_restart` restarts every step, and every step is charged again
    when it re-dispatches. Not refunding billed the run twice, while
    `abort_run`, the sibling option on the same question, refunded in full."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "incubate", "reprep")
    await h.force_fault(FaultKind.BATCH_DESTROYED)
    assert await h.spin(30, until=lambda: db.fetchval(
        "select count(*) from interventions where state='open'"))

    charged = await db.fetchval("select credits_spent from tokens where id='tok-org'")
    assert charged > 0, "nothing was charged; this test would pass vacuously"

    iv = await db.fetchrow("select * from interventions where state='open'")
    await interventions.resolve(iv["id"], "reprep_restart", token_id=org_token["id"],
                                reason="fresh plates, run it again")

    assert await db.fetchval(
        "select credits_spent from tokens where id='tok-org'") == 0, (
        "restarting kept the credits for steps it is about to charge again")


async def test_marking_an_epoch_suspect_closes_it(h, org_token):
    """Stamping the verdict without closing the epoch left it *current*, so
    results produced afterwards were stamped with an epoch already declared
    untrustworthy, and delivered clean."""
    from labsched import interventions

    run = await one_op_run(org_token["id"], "bli_read", "epoch")
    assert await h.spin(30, until=lambda: _done(run["id"]))
    device = await db.fetchval("select device_id from results where run_id=$1", run["id"])
    epoch = await db.fetchval(
        "select calibration_epoch from results where run_id=$1", run["id"])

    await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "control chart reviewed")

    row = await db.fetchrow(
        "select * from device_calibration_epochs where device_id=$1 and epoch=$2",
        device, epoch)
    assert row["verdict"] == "suspect"
    assert row["ended_at"] is not None, "a suspect epoch was left open and current"

    pool = await db.pool()
    async with pool.acquire() as conn:
        assert await interventions.current_epoch(conn, device) > epoch, \
            "new work would still be stamped with the suspect epoch"


async def test_one_question_cannot_dispose_of_anothers_numbers(h, org_token):
    """`mark_epoch_suspect` refuses to *take* results another open question is
    holding, then used to hand their ids to the new question anyway. Resolving
    it disposed of numbers it did not hold and left the first question open
    over an empty set. held -> open stayed true the whole way through, which
    is why the old invariant did not see it."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "bli_read", "poach")
    assert await h.spin(30, until=lambda: _done(run["id"]))
    res = await db.fetchrow("select * from results where run_id=$1", run["id"])
    device, epoch = res["device_id"], res["calibration_epoch"]

    # What QC's control chart does when it fires: a question that holds the
    # number until someone decides about it.
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            drift = await interventions.open_intervention(
                conn, kind=FaultKind.CALIBRATION_DRIFT, run_id=res["run_id"],
                step_id=None, device_id=device, sample_id=None, actor="qc",
                detail={"epoch": epoch, "note": "control chart"})
            await conn.execute(
                "update results set state='held', qc_verdict='fail', held_by=$2"
                " where id=$1", res["id"], drift)

    # An operator now marks the same epoch suspect from the Results tab.
    suspect, _ = await interventions.mark_epoch_suspect(
        device, epoch, "token:tok-engineer", "audit")
    if suspect is not None:
        listed = (await db.fetchrow(
            "select * from interventions where id=$1", suspect))["detail"]
        assert res["id"] not in (listed.get("affected_result_ids") or []), \
            "a question listed a result it had just refused to take"
        await interventions.resolve(suspect, "accept_with_caveat",
                                    token_id=org_token["id"])

    # And the endpoint has the fact it needs to say which of the two empty
    # cases this is, rather than "no results were produced" about an epoch the
    # operator is looking at a result row from.
    assert await interventions.results_held_elsewhere(device, epoch) == 1

    after = await db.fetchrow("select * from results where id=$1", res["id"])
    assert after["held_by"] == drift and after["state"] == "held", \
        "another question's resolution disposed of a number it did not hold"
    assert await db.fetchval(
        "select state from interventions where id=$1", drift) == "open"

    # And the question that does hold it can still let it go.
    await interventions.resolve(drift, "ignore_continue", token_id=org_token["id"])
    assert await db.fetchval(
        "select state from results where id=$1", res["id"]) == "released"


async def test_restarting_reports_the_money_it_moves(h, org_token):
    """`reprep_restart` is the one requeue path that re-runs *finished* steps.
    Their reservations are consumed, so the refund cannot pay them and they are
    charged again, while the panel, keying on the option's name, advertised
    "0 released, 0 spent again" next to a sibling that reported its refund."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await simple_run(org_token["id"], name="money")

    async def past_prep():
        return await db.fetchval(
            "select 1 from steps where run_id=$1 and name='prep' and state='done'",
            run["id"])
    assert await h.spin(30, until=past_prep), "no step finished; nothing consumed"

    device = await db.fetchval(
        "select device_id from steps where run_id=$1 and name='incubate'", run["id"])
    await h.force_fault(FaultKind.BATCH_DESTROYED, device_id=device)
    assert await h.spin(30, until=lambda: db.fetchval(
        "select id from interventions where kind=$1 and state='open'",
        FaultKind.BATCH_DESTROYED))
    iv = await db.fetchrow(
        "select * from interventions where kind=$1 and state='open'",
        FaultKind.BATCH_DESTROYED)

    consumed = await db.fetchval(
        "select coalesce(sum(credits),0) from reservations where run_id=$1"
        "  and consumed_at is not null and refunded_at is null", run["id"])
    unconsumed = await db.fetchval(
        "select coalesce(sum(credits),0) from reservations where run_id=$1"
        "  and consumed_at is null and refunded_at is null", run["id"])
    assert consumed > 0 and unconsumed > 0, "test needs both kinds of charge"

    cons = await interventions.consequences(iv["id"], "reprep_restart")
    assert cons["credits_released"] == unconsumed, \
        "the refund it performs was reported as nothing"
    assert cons["credits_spent_again"] >= consumed, \
        "instrument time already paid for is charged again and advertised free"

    spent_before = await db.fetchval(
        "select credits_spent from tokens where id=$1", org_token["id"])
    await interventions.resolve(iv["id"], "reprep_restart",
                                token_id=org_token["id"], reason="fresh plates")
    spent_after = await db.fetchval(
        "select credits_spent from tokens where id=$1", org_token["id"])
    assert spent_before - spent_after == cons["credits_released"], \
        "the ledger and the panel disagree about the refund"


async def test_quarantine_condemns_the_epoch_the_question_names(h, org_token):
    """Resolving a drift question with `quarantine_device` read the epoch out of
    `detail` and then rolled whatever epoch was current, so an instrument
    that had rolled since detection got a clean epoch stamped suspect and the
    suspect one left unmarked."""
    from labsched import interventions
    from labsched.faults import FaultKind

    run = await one_op_run(org_token["id"], "bli_read", "epoch-drift")
    assert await h.spin(30, until=lambda: _done(run["id"]))
    res = await db.fetchrow("select * from results where run_id=$1", run["id"])
    device, epoch = res["device_id"], res["calibration_epoch"]

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            drift = await interventions.open_intervention(
                conn, kind=FaultKind.CALIBRATION_DRIFT, run_id=res["run_id"],
                step_id=None, device_id=device, sample_id=None, actor="qc",
                detail={"epoch": epoch, "note": "control chart"})
            # The instrument rolls on for its own reasons before anyone answers
            # the question. Returning to service does exactly this.
            await interventions.roll_epoch(conn, device, "good", "returned to service")
            moved_on = await interventions.current_epoch(conn, device)
    assert moved_on > epoch

    await interventions.resolve(drift, "quarantine_device", token_id=org_token["id"],
                                reason="drift confirmed")

    verdicts = {r["epoch"]: r["verdict"] for r in await db.fetch(
        "select epoch, verdict from device_calibration_epochs where device_id=$1", device)}
    assert verdicts[epoch] == "suspect", \
        "the epoch the question was about was left unmarked"
    assert verdicts.get(moved_on) != "suspect", \
        "an epoch nobody questioned was condemned instead"


async def test_the_panel_prices_the_re_run_it_will_actually_do(h, org_token):
    """`_apply` withdraws what this question holds; the estimate read the
    snapshot id list. A number that left the question's custody, which
    `_reprep_and_restart` does since it is scoped by run, was still priced and
    counted, on an irreversible option."""
    from labsched import interventions

    run = await one_op_run(org_token["id"], "bli_read", "priced")
    assert await h.spin(30, until=lambda: _done(run["id"]))
    res = await db.fetchrow("select * from results where run_id=$1", run["id"])

    iid, pulled = await interventions.mark_epoch_suspect(
        res["device_id"], res["calibration_epoch"], "token:tok-engineer", "audit")
    assert pulled == 1
    priced = await interventions.consequences(iid, "requeue")
    assert priced["steps_requeued"] == 1 and priced["credits_spent_again"] > 0

    # What a restart of that run does to the number this question holds.
    await db.execute(
        "update results set state='invalidated', held_by=null,"
        " invalidated_reason='run restarted after re-prep' where id=$1", res["id"])

    after = await interventions.consequences(iid, "requeue")
    assert after["steps_requeued"] == 0 and after["credits_spent_again"] == 0, \
        "the panel priced a re-run of a number this question no longer holds"

    await interventions.resolve(iid, "requeue", token_id=org_token["id"],
                                reason="re-run what is left")
    assert await db.fetchval(
        "select state from steps where run_id=$1", run["id"]) == "done", \
        "a step was put back on the floor that the panel said would not be"


async def test_every_option_that_refunds_says_so(h, org_token):
    """`credits_released` was a list of option names while `credits_spent_again`
    was measured, so four options that really do refund through
    `requeue_step`'s default reported giving back nothing, beside option text
    promising the refund. The panel's two numbers now come from the same
    predicate the ledger pays on."""
    from labsched import interventions
    from labsched.faults import FaultKind

    await one_op_run(org_token["id"], "bli_read", "refunded")
    await h.force_fault(FaultKind.UNEXPECTED_READING)
    assert await h.spin(30, until=lambda: db.fetchval(
        "select count(*) from interventions where state='open'"))
    iv = await db.fetchrow("select * from interventions where state='open'")

    live = await db.fetchval(
        "select coalesce(sum(credits),0) from reservations where step_id=$1"
        "   and consumed_at is null and refunded_at is null", iv["step_id"])
    assert live > 0, "nothing is on the meter; this test would pass vacuously"

    cons = await interventions.consequences(iv["id"], "rerun_step")
    assert cons["credits_released"] == live, \
        "an option whose own text promises a refund reported giving back nothing"
    # Refunded and charged again: the run costs no more than it did, which is
    # what the option text says.
    assert cons["credits_spent_again"] == 0

    before = await db.fetchval(
        "select credits_spent from tokens where id=$1", org_token["id"])
    await interventions.resolve(iv["id"], "rerun_step", token_id=org_token["id"])
    after = await db.fetchval(
        "select credits_spent from tokens where id=$1", org_token["id"])
    assert before - after == cons["credits_released"], \
        "the ledger and the panel disagree about the refund"


async def test_a_re_run_does_not_pull_a_plate_out_of_a_machine(h, org_token):
    """The SLA sweep was corrected for this; the resolution path had it too.
    "Re-run the affected steps" requeues steps that are `done` by construction,
    and a finished step's plate has often moved on into an instrument for a
    later step. Parking it there walks a plate out of a running machine on a
    decision about a number it produced earlier."""
    from labsched import interventions

    run = await simple_run(org_token["id"], name="rerun-plate")

    async def prep_done_and_moved_on():
        return await db.fetchval(
            "select 1 from steps s0"
            "  join steps s1 on s1.run_id = s0.run_id and s1.idx > s0.idx"
            "  join reservations r on r.step_id = s1.id and r.sample_released_at is null"
            "  join samples sm on sm.id = r.sample_id"
            " where s0.run_id=$1 and s0.state='done' and sm.location_kind='device'"
            "   and s1.state in ('scheduled','running')", run["id"])
    assert await h.spin(40, until=prep_done_and_moved_on), \
        "the run never reached one step done and the next holding the plate"

    done_step = await db.fetchrow(
        "select * from steps where run_id=$1 and state='done' order by idx limit 1", run["id"])
    plate = await db.fetchval(
        "select location_device_id from samples where id=$1", done_step["sample_id"])
    assert plate, "the plate is not in an instrument; this test would pass vacuously"

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            requeued = await interventions.requeue_step(
                conn, done_step["id"], reason="re-run after calibration drift",
                actor="tok-engineer", allow_done=True, refund=False)
    assert requeued

    sample = await db.fetchrow("select * from samples where id=$1", done_step["sample_id"])
    assert sample["location_kind"] == "device" and \
        sample["location_device_id"] == plate, \
        "a re-run walked the plate out of the instrument a live step was using"
    await h.assert_plate_in_one_place()
