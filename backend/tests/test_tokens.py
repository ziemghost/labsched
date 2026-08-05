"""Capability tokens: attenuation, revocation, budget, and clean drains."""
from __future__ import annotations

import pytest
from biscuit_auth import Biscuit, BlockBuilder

from labsched import db
from labsched.auth import tokens
from labsched.runs import AdmissionError, RunRequest, submit
from tests.conftest import read_only_run, simple_run


async def _chain(org):
    proj = await tokens.attenuate(
        org["id"], "project", "project",
        tokens.Caveats(["bli_reader", "incubator"], 4, 5_000, 120, 800,
                       tokens.default_expiry(7)),
        token_id="tok-proj",
    )
    agent = await tokens.attenuate(
        proj["id"], "agent", "agent",
        tokens.Caveats(["bli_reader"], 2, 2_000, 60, 300, tokens.default_expiry(3)),
        token_id="tok-agent",
    )
    return proj, agent


async def test_attenuation_is_strictly_weaker(h, org_token):
    proj, agent = await _chain(org_token)

    assert (await tokens.authorize(org_token["id"], device_kinds=["liquid_handler"],
                                   concurrent=1, wallclock_s=10, credits=5)).allowed
    assert not (await tokens.authorize(agent["id"], device_kinds=["liquid_handler"],
                                       concurrent=1, wallclock_s=10, credits=5)).allowed
    assert (await tokens.authorize(agent["id"], device_kinds=["bli_reader"],
                                   concurrent=1, wallclock_s=10, credits=5)).allowed


async def test_mint_refuses_to_widen(h, org_token):
    proj, agent = await _chain(org_token)
    for caveats, msg in [
        (tokens.Caveats(["liquid_handler"], 1, 10, 10, 10, tokens.default_expiry(1)), "device kinds"),
        (tokens.Caveats(["bli_reader"], 99, 10, 10, 10, tokens.default_expiry(1)), "concurrent"),
        (tokens.Caveats(["bli_reader"], 1, 99_999, 10, 10, tokens.default_expiry(1)), "wall-clock"),
        (tokens.Caveats(["bli_reader"], 1, 10, 9_999, 10, tokens.default_expiry(1)), "credits per run"),
        (tokens.Caveats(["bli_reader"], 1, 10, 10, 9_999, tokens.default_expiry(1)), "budget"),
        (tokens.Caveats(["bli_reader"], 1, 10, 10, 10, tokens.default_expiry(999)), "expiry"),
    ]:
        with pytest.raises(tokens.TokenError, match=msg):
            await tokens.attenuate(agent["id"], "evil", "agent", caveats)


async def test_a_forged_block_cannot_grant_what_the_parent_denied(h, org_token):
    """Bypass our mint-time validation entirely and append a block by hand
    that claims broader rights. Biscuit's semantics make this useless: blocks
    only ever add checks, so the parent's restriction still applies."""
    proj, agent = await _chain(org_token)
    kp = await tokens.keypair()
    parsed = Biscuit.from_base64(agent["biscuit"], kp.public_key)

    forged = parsed.append(BlockBuilder(
        tokens.BLOCK_SOURCE,
        tokens.Caveats(["liquid_handler", "bli_reader", "incubator"], 99, 99_999,
                       9_999, 9_999, tokens.default_expiry(999)).as_params("forged"),
    ))
    await db.execute(
        "insert into tokens(id,parent_id,label,tier,biscuit,revocation_id,allowed_kinds,"
        "max_concurrent,max_wallclock_s,max_run_credits,budget_credits,expires_at)"
        " values ('tok-forged',$1,'forged','agent',$2,$3,$4,99,99999,9999,9999,$5)",
        agent["id"], forged.to_base64(), list(forged.revocation_ids)[-1],
        ["liquid_handler", "bli_reader", "incubator"], tokens.default_expiry(999),
    )

    res = await tokens.authorize("tok-forged", device_kinds=["liquid_handler"],
                                 concurrent=1, wallclock_s=10, credits=5)
    assert not res.allowed, "a forged block escalated privilege"
    assert "bli_reader" in res.reason

    for kw in (dict(concurrent=50, wallclock_s=10, credits=5),
               dict(concurrent=1, wallclock_s=50_000, credits=5),
               dict(concurrent=1, wallclock_s=10, credits=5_000)):
        assert not (await tokens.authorize("tok-forged", device_kinds=["bli_reader"],
                                           **kw)).allowed


async def test_a_tampered_token_does_not_parse(h, org_token):
    kp = await tokens.keypair()
    raw = org_token["biscuit"]
    tampered = raw[:-6] + ("A" if raw[-6] != "A" else "B") + raw[-5:]
    with pytest.raises(Exception):
        Biscuit.from_base64(tampered, kp.public_key)


async def test_revoking_a_parent_kills_every_descendant(h, org_token):
    proj, agent = await _chain(org_token)
    killed = await tokens.revoke(proj["id"], "matti")
    assert set(killed) == {proj["id"], agent["id"]}

    res = await tokens.authorize(agent["id"], device_kinds=["bli_reader"],
                                 concurrent=1, wallclock_s=10, credits=5)
    assert not res.allowed
    assert "ancestor" in res.reason and proj["id"] in res.reason
    # The ancestor is unaffected.
    assert (await tokens.authorize(org_token["id"], device_kinds=["bli_reader"],
                                   concurrent=1, wallclock_s=10, credits=5)).allowed


async def test_revocation_drains_a_live_run_without_orphaning_anything(h, org_token):
    """The property that matters operationally: pulling a token mid-run must
    not strand a plate inside a machine or leave a reservation behind."""
    proj, agent = await _chain(org_token)
    run = await read_only_run(agent["id"], "victim")

    assert await h.spin(8, until=lambda: _step_running(run["id"])), "never started"
    device = await db.fetchval(
        "select device_id from steps where run_id=$1 and state='running'", run["id"]
    )
    assert device

    await tokens.revoke(proj["id"], "matti")
    await h.spin(20, until=lambda: _run_settled(run["id"]))

    assert await h.run_state(run["id"]) == "cancelled"
    assert await h.held_reservations() == [], "revocation left a reservation behind"
    await h.assert_no_orphan_locks()
    await h.assert_plate_in_one_place()

    sample = await db.fetchrow(
        "select * from samples where id=(select sample_id from steps where run_id=$1 limit 1)",
        run["id"],
    )
    assert sample["state"] in ("parked", "ok"), f"plate left as {sample['state']}"
    assert sample["location_kind"] == "storage", "plate stranded in a machine"

    dev = await db.fetchrow("select * from devices where id=$1", device)
    assert dev["state"] == "idle", f"instrument left as {dev['state']}"


async def test_revocation_does_not_kill_an_in_flight_step_mid_operation(h, org_token):
    """Drain means finish or park cleanly, not abort in place."""
    proj, agent = await _chain(org_token)
    run = await read_only_run(agent["id"], "drainme")
    assert await h.spin(8, until=lambda: _step_running(run["id"]))

    await tokens.revoke(agent["id"], "matti")
    await h.sched.tick()

    run_row = await db.fetchrow("select * from runs where id=$1", run["id"])
    assert run_row["drain_requested"] is True
    step = await db.fetchrow("select * from steps where run_id=$1", run["id"])
    assert step["state"] == "running", "step was killed mid-operation instead of drained"

    await h.spin(20, until=lambda: _run_settled(run["id"]))
    assert await h.run_state(run["id"]) in ("cancelled", "done")
    assert await h.held_reservations() == []


async def test_new_runs_are_refused_once_the_token_is_revoked(h, org_token):
    proj, agent = await _chain(org_token)
    await tokens.revoke(proj["id"], "matti")
    with pytest.raises(AdmissionError, match="revoked"):
        await read_only_run(agent["id"], "nope")


async def test_admission_rejects_with_a_precise_reason(h, org_token):
    proj, agent = await _chain(org_token)
    with pytest.raises(AdmissionError) as exc:
        await submit(RunRequest(
            name="needs a handler", token_id=agent["id"],
            steps=[{"name": "prep", "op": "liquid_transfer",
                    "with": {"volume_ul": 40, "source": "buffer_a"}}]))

    err = exc.value
    assert err.status == 403, "a forbidden run is not a malformed one"
    assert err.body()["remedy"] == "widen_token_or_escalate"
    problem = next(p for p in err.problems if p.code == "token_forbids_kind")
    assert problem.path == "steps[0].op"
    assert "bli_reader" in problem.message          # what the token does allow
    assert "liquid_transfer" in problem.message     # what the step needs
    assert "liquid_handler" in problem.message      # what could provide it


async def test_budget_is_shared_down_the_whole_lineage(h, org_token):
    """A child cannot spend more than its parent has, and two children cannot
    each spend the parent's full budget."""
    parent = await tokens.attenuate(
        org_token["id"], "small-project", "project",
        tokens.Caveats(["bli_reader"], 4, 5_000, 100, 30, tokens.default_expiry(7)),
        token_id="tok-small",
    )
    child_a = await tokens.attenuate(
        parent["id"], "a", "agent",
        tokens.Caveats(["bli_reader"], 2, 2_000, 100, 30, tokens.default_expiry(3)),
        token_id="tok-a",
    )
    child_b = await tokens.attenuate(
        parent["id"], "b", "agent",
        tokens.Caveats(["bli_reader"], 2, 2_000, 100, 30, tokens.default_expiry(3)),
        token_id="tok-b",
    )

    # A bli_read costs 25 credits from the lab's catalog; the request cannot
    # declare a cheaper price, which is the whole point of moving price out of
    # the request. The shared parent budget only covers one of them.
    await read_only_run(child_a["id"], "a1")
    await h.spin(3)   # let it reserve, which is when credits are actually spent

    with pytest.raises(AdmissionError) as exc:
        await read_only_run(child_b["id"], "b1")
    assert any("credits" in p.message for p in exc.value.problems), exc.value.problems
    assert exc.value.status == 403

    spent = {r["id"]: r["credits_spent"] for r in
             await db.fetch("select id, credits_spent from tokens")}
    assert spent["tok-small"] <= 30, f"lineage budget overspent: {spent}"
    assert spent["tok-a"] + spent["tok-b"] == spent["tok-small"], (
        f"child spend does not roll up to the parent: {spent}")


async def test_every_reservation_is_traceable_to_its_token(h, org_token):
    proj, agent = await _chain(org_token)
    run = await read_only_run(agent["id"], "traced")
    assert await h.spin(8, until=lambda: _step_running(run["id"]))

    res = await db.fetchrow("select * from reservations where run_id=$1", run["id"])
    assert res["token_id"] == agent["id"]

    chain = await tokens.lineage(res["token_id"])
    assert [t["id"] for t in chain] == [org_token["id"], proj["id"], agent["id"]]

    entry = await db.fetchrow(
        "select * from audit where action='reservation.acquired' and run_id=$1", run["id"]
    )
    assert entry["token_id"] == agent["id"]


async def _step_running(run_id):
    return await db.fetchval(
        "select count(*) from steps where run_id=$1 and state='running'", run_id) > 0


async def _run_settled(run_id):
    return await db.fetchval("select state from runs where id=$1", run_id) in (
        "done", "failed", "cancelled")
