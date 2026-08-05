"""Admission: pure planning, complete error reports, lab-owned pricing."""
from __future__ import annotations

import pytest

from labsched import catalog, db, protocols
from labsched.auth import tokens
from labsched.runs import AdmissionError, RunRequest, commit, plan, submit
from tests.conftest import read_only_run


def _read(**kw):
    return {"op": "bli_read", "with": {"target": "TREM2"}, **kw}


async def test_plan_touches_nothing(h, org_token):
    before = await db.fetchval("select count(*) from runs")
    p, problems = await plan(RunRequest(
        name="dry", token_id=org_token["id"], steps=[_read(name="a")]))

    assert problems == []
    assert p is not None
    assert await db.fetchval("select count(*) from runs") == before
    assert await db.fetchval("select count(*) from samples") == 0


async def test_plan_projects_cost_from_the_catalog_not_the_request(h, org_token):
    """A request cannot price itself. This is what makes the budget caveat
    mean anything at all."""
    op = await catalog.get("bli_read")
    p, _ = await plan(RunRequest(
        name="priced", token_id=org_token["id"],
        # Both of these are ignored: they are not part of the wire format.
        steps=[{"op": "bli_read", "with": {"target": "T"},
                "duration_s": 9999, "credit_cost": 0}]))

    assert p.total_credits == op.credit_cost
    assert p.steps[0].duration_s == op.nominal_duration_s
    assert p.steps[0].credit_cost == op.credit_cost


async def test_every_problem_is_reported_at_once(h, org_token):
    """An agent with four mistakes should need one round trip, not four."""
    p, problems = await plan(RunRequest(
        name="messy", token_id=org_token["id"],
        steps=[
            {"op": "incubaet", "with": {}},                        # typo
            {"op": "bli_read", "with": {}},                        # missing target
            {"op": "incubate", "with": {"minutes": 99999}},        # out of range
            {"op": "bli_read", "with": {"target": "T", "nope": 1}},  # unknown param
        ]))

    assert p is None
    codes = {x.code for x in problems}
    assert {"unknown_operation", "missing_param", "param_out_of_range",
            "unknown_param"} <= codes, codes
    # Every problem carries a path an agent can act on.
    assert all(x.path for x in problems)
    typo = next(x for x in problems if x.code == "unknown_operation")
    assert typo.path == "steps[0].op"
    assert "incubate" in (typo.hint or ""), "no did-you-mean for a one-letter typo"


async def test_status_classes_are_distinct(h, org_token):
    """Malformed, forbidden and unsatisfiable are three different answers with
    three different correct agent responses."""
    from labsched.auth import tokens

    # 422: the request itself is wrong.
    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(name="bad", token_id=org_token["id"],
                                steps=[{"op": "nonexistent", "with": {}}]))
    assert exc.value.status == 422
    assert exc.value.body()["remedy"] == "edit_request"

    # 403: fine request, wrong token.
    narrow = await tokens.attenuate(
        org_token["id"], "readers only", "agent",
        tokens.Caveats(["bli_reader"], 2, 5_000, 100, 500, tokens.default_expiry(3)),
        token_id="tok-narrow")
    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(
            name="forbidden", token_id=narrow["id"],
            steps=[{"op": "liquid_transfer",
                    "with": {"volume_ul": 40, "source": "buffer_a"}}]))
    assert exc.value.status == 403
    assert exc.value.body()["remedy"] == "widen_token_or_escalate"

    # 409: nothing wrong with the request or the token; the fleet cannot do it.
    await db.execute("update devices set quarantined=true, state='offline'"
                     " where 'bli_read' = any(capabilities)")
    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(name="nofleet", token_id=org_token["id"],
                                steps=[_read()]))
    assert exc.value.status == 409
    assert exc.value.body()["retryable"] is True


async def test_warnings_do_not_block_admission(h, org_token):
    """Two steps on one plate with no declared order will serialise. The
    reservation index already prevents the bug; the plan says so at authoring
    time instead."""
    p, problems = await plan(RunRequest(
        name="unordered", token_id=org_token["id"],
        steps=[_read(name="a"), _read(name="b")]))

    assert problems == []
    assert any(w.code == "unordered_shared_plate" for w in p.warnings), \
        [w.code for w in p.warnings]
    assert all(w.severity == "warning" for w in p.warnings)


async def test_a_cycle_is_caught_before_anything_is_written(h, org_token):
    before = await db.fetchval("select count(*) from samples")
    p, problems = await plan(RunRequest(
        name="cyclic", token_id=org_token["id"],
        steps=[_read(name="a", after=[1]), _read(name="b", after=[0])]))
    assert p is None
    assert any(x.code == "cycle" for x in problems)
    assert await db.fetchval("select count(*) from samples") == before


# ------------------------------------------------------------- protocols ---

async def test_protocol_expands_over_plates(h, org_token):
    p, problems = await plan(RunRequest(
        name="panel", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plate_count=3))

    assert problems == []
    assert len(p.steps) == 9, "3 protocol steps x 3 plates"
    assert p.protocol_name == "binding_screen" and p.protocol_digest
    # Each plate's read depends on that plate's own incubation, not another's.
    reads = [s for s in p.steps if s.op == "bli_read"]
    assert len(reads) == 3
    assert all(len(s.after) == 1 for s in reads)
    assert len({s.sample for s in p.steps}) == 3


async def test_protocol_params_are_substituted_and_bounded(h, org_token):
    p, _ = await plan(RunRequest(
        name="param", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "PD-L1", "incubation_min": 45}, plate_count=1))
    inc = next(s for s in p.steps if s.op == "incubate")
    read = next(s for s in p.steps if s.op == "bli_read")
    assert inc.params["minutes"] == 45, "substitution must preserve the int type"
    assert read.params["target"] == "PD-L1"

    _, problems = await plan(RunRequest(
        name="param", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "X", "incubation_min": 9000}, plate_count=1))
    assert any(x.code == "param_out_of_range" for x in problems)


async def test_protocol_qc_policy_reaches_the_step(h, org_token):
    """Failure policy is declared by the protocol, not hardcoded in the
    scheduler."""
    run, _ = await submit(RunRequest(
        name="qc", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "T"}, plate_count=1))
    read = await db.fetchrow(
        "select * from steps where run_id=$1 and op='bli_read'", run["id"])
    assert read["qc"] == {"control_within": 0.15}


async def test_a_pinned_protocol_version_cannot_be_edited(h, org_token):
    """'What exactly ran' has to stay answerable, so a version in use is
    immutable."""
    await submit(RunRequest(
        name="pin", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "T"}, plate_count=1))

    current = await protocols.get("binding_screen")
    edited = current.source.replace("default: 30", "default: 31")
    with pytest.raises(ValueError, match="already pinned"):
        await protocols.register_source(edited)

    # Publishing a new version is the supported path. Derived from whatever
    # version the file is on, so editing the protocol does not break the test
    # that guards editing the protocol.
    bumped = edited.replace(f"version: {current.version}",
                            f"version: {current.version + 1}")
    p = await protocols.register_source(bumped)
    assert p.version == current.version + 1


async def test_runs_record_the_digest_they_ran_under(h, org_token):
    run, _ = await submit(RunRequest(
        name="digest", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "T"}, plate_count=1))
    proto = await protocols.get("binding_screen", run["protocol_version"])
    assert run["protocol_digest"] == proto.digest
    assert run["params"]["incubation_min"] == 30, "defaults are recorded, not just supplied"


async def test_plate_labels_cannot_smuggle_past_the_protocol_bound(h, org_token):
    """The bound was checked against `plate_count` while the run expanded from
    `len(plates)`, so sending both ran as many plates as you liked: eight
    plates through a protocol that declares a maximum of four."""
    _, problems = await plan(RunRequest(
        name="smuggle", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plate_count=1,
        plates=[f"p{i}" for i in range(12)],
    ))
    codes = {p.code for p in problems}
    assert "bad_plate_count" in codes, f"twelve plates admitted: {codes}"
    assert "conflicting_plate_count" in codes, "the contradiction itself is worth saying"


async def test_the_bound_is_reported_against_what_was_asked_for(h, org_token):
    _, problems = await plan(RunRequest(
        name="huge", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plate_count=500))
    bad = [p for p in problems if p.code == "bad_plate_count"]
    assert bad and "got 500" in bad[0].message, bad


async def test_plates_alone_still_admit_within_the_bound(h, org_token):
    p, problems = await plan(RunRequest(
        name="fine", token_id=org_token["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plates=["a", "b"]))
    assert problems == [] and p is not None
    assert p.plates == ["a", "b"]


async def test_a_run_wider_than_the_token_admits_and_warns(h, org_token):
    """`max_concurrent` binds live reservations, enforced at dispatch. A wide
    run is not illegal, it serialises, and the warning that says so was
    unreachable because admission refused the run first."""
    narrow = await tokens.attenuate(
        org_token["id"], "narrow", "agent",
        tokens.Caveats(
            allowed_kinds=list(org_token["allowed_kinds"]), max_concurrent=1,
            max_wallclock_s=10_000, max_run_credits=500, budget_credits=5_000,
            expires_at=tokens.default_expiry(3), authorities=["operator"],
        ),
    )
    p, problems = await plan(RunRequest(
        name="wide", token_id=narrow["id"], protocol="binding_screen",
        params={"target": "TREM2"}, plate_count=4))

    assert problems == [], f"a serialisable run was refused: {[x.message for x in problems]}"
    assert p is not None and p.concurrency > 1
    warned = [w for w in p.warnings if w.code == "will_not_parallelise"]
    assert warned, "no warning about the run that will serialise"
    assert "serialise" in warned[0].message


# ----------------------------------------------------------- idempotency ---

async def test_resubmitting_with_the_same_key_returns_the_original_run(h, org_token):
    """An agent that times out mid-POST and retries must not start a second
    physical experiment."""
    req = RunRequest(name="once", token_id=org_token["id"], steps=[_read()],
                     client_run_id="agent-key-1")

    p1, _ = await plan(req)
    run1, replayed1 = await commit(p1)
    p2, _ = await plan(req)
    run2, replayed2 = await commit(p2)

    assert replayed1 is False and replayed2 is True
    assert run1["id"] == run2["id"]
    assert await db.fetchval("select count(*) from runs") == 1
    assert await db.fetchval("select count(*) from samples") == 1, "a second plate was issued"


async def test_the_same_key_under_a_different_token_is_a_different_run(h, org_token):
    from labsched.auth import tokens
    other = await tokens.attenuate(
        org_token["id"], "other", "agent",
        tokens.Caveats(["bli_reader"], 2, 5_000, 100, 500, tokens.default_expiry(3)),
        token_id="tok-other")

    a, _ = await submit(RunRequest(name="a", token_id=org_token["id"],
                                   steps=[_read()], client_run_id="k"))
    b, _ = await submit(RunRequest(name="b", token_id=other["id"],
                                   steps=[_read()], client_run_id="k"))
    assert a["id"] != b["id"]
