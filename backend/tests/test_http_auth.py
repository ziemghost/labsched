"""The HTTP surface, tested at the wire.

Every other test in this suite calls `submit()` directly, which is the right
level for scheduling behaviour and the wrong level for exactly one question:
*can someone who holds no credential start a physical experiment?* That answer
lives in the route signature, not in the domain layer, so it has to be asked
over HTTP or it is not being asked at all.
"""
from __future__ import annotations

import httpx
import pytest

from labsched import db
from labsched.api import app

STEPS = [{"name": "prep", "op": "liquid_transfer",
          "with": {"volume_ul": 40, "source": "buffer_a"}}]


def client() -> httpx.AsyncClient:
    # No lifespan: the `h` fixture owns the schema and we do not want a second
    # scheduler ticking underneath the one the test is driving.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_run_submission_requires_a_credential(h, org_token):
    async with client() as c:
        r = await c.post("/api/runs", json={"name": "no auth", "steps": STEPS})

    assert r.status_code == 401
    assert "requires a capability token" in r.json()["detail"]["error"]
    assert await db.fetchval("select count(*) from runs") == 0


async def test_planning_requires_a_credential(h, org_token):
    """Planning answers "would my token allow this", which is not a question
    the caller may ask about someone else's token."""
    async with client() as c:
        r = await c.post("/api/runs/plan", json={"name": "no auth", "steps": STEPS})
    assert r.status_code == 401


async def test_the_body_may_not_name_the_token(h, org_token):
    """The old shape took `token_id` as a field. Charging a run to a token you
    merely named is the whole failure mode; refuse the field outright rather
    than ignoring it, so an old client is told instead of silently recharged."""
    async with client() as c:
        r = await c.post(
            "/api/runs",
            headers={"Authorization": "Bearer tok-org"},
            json={"name": "named token", "token_id": "tok-org", "steps": STEPS},
        )
    assert r.status_code == 422


@pytest.mark.parametrize("header", ["Bearer tok-org", "tok-org"])
async def test_a_valid_credential_admits_and_is_what_gets_charged(h, org_token, header):
    async with client() as c:
        r = await c.post("/api/runs", headers={"Authorization": header},
                         json={"name": "with auth", "steps": STEPS})

    assert r.status_code == 201, r.text
    assert r.json()["token_id"] == "tok-org"
    assert await db.fetchval(
        "select token_id from runs where id=$1", r.json()["id"]) == "tok-org"


# The fix for the missing credential on `POST /api/runs` was scoped to that
# route, and `POST /api/tokens/{id}/attenuate` turned out to have the same hole
# a token factory, which is worse. So this asserts the class: every route
# that changes state resolves a credential. New endpoints fail here by default.
READ_ONLY_POSTS = {
    # A preview computes counts and writes nothing. It is a POST only because
    # it takes a body.
    "/api/interventions/resolve-batch/preview",
}


def _mutating_routes():
    import inspect

    for r in app.routes:
        methods = {m for m in (getattr(r, "methods", None) or set())
                   if m in ("POST", "PUT", "PATCH", "DELETE")}
        if not methods or r.path in READ_ONLY_POSTS:
            continue
        yield r.path, inspect.signature(r.endpoint).parameters


def test_every_mutating_route_takes_a_credential():
    missing = [p for p, params in _mutating_routes() if "authorization" not in params]
    assert missing == [], (
        f"these change state without resolving a credential: {missing}. "
        "Add `authorization: Annotated[str | None, Header()] = None` and call "
        "`bearer()`, or add the path to READ_ONLY_POSTS with a reason.")


async def test_attenuating_a_token_you_do_not_hold_is_refused(h, org_token):
    """Attenuation hands out a narrower credential. It must not be a way to
    acquire one: minting an engineer-authority child of the org token from an
    unauthenticated shell would make every other check decorative."""
    body = {
        "label": "forged", "tier": "agent", "allowed_kinds": ["bli_reader"],
        "max_concurrent": 1, "max_wallclock_s": 100, "max_run_credits": 50,
        "budget_credits": 100, "authorities": ["engineer"],
        # Shorter than the parent, so a grandchild does not trip the
        # expiry-widening check while this test is about who may mint.
        "expires_in_days": 3,
    }
    async with client() as c:
        anon = await c.post("/api/tokens/tok-org/attenuate", json=body)

        child = await c.post("/api/tokens/tok-org/attenuate",
                             headers={"Authorization": "Bearer tok-org"}, json=body)
        assert child.status_code == 200, child.text
        kid = child.json()["id"]

        # A sibling cannot mint off the parent it does not hold...
        sibling = await c.post("/api/tokens/tok-org/attenuate",
                               headers={"Authorization": f"Bearer {kid}"}, json=body)
        # ...but a token may attenuate itself, and its ancestor may too.
        # A grandchild expires sooner again: a child may not outlive its
        # parent, and the same window measured a moment later would.
        narrower = body | {"expires_in_days": 1}
        self_mint = await c.post(f"/api/tokens/{kid}/attenuate",
                                 headers={"Authorization": f"Bearer {kid}"}, json=narrower)
        ancestor = await c.post(f"/api/tokens/{kid}/attenuate",
                                headers={"Authorization": "Bearer tok-org"}, json=narrower)

    assert anon.status_code == 401
    assert sibling.status_code == 403
    assert sibling.json()["detail"]["remedy"] == "present_the_parent_token"
    assert self_mint.status_code == 200, self_mint.text
    assert ancestor.status_code == 200, ancestor.text


async def test_an_unknown_token_is_refused(h, org_token):
    async with client() as c:
        r = await c.post("/api/runs", headers={"Authorization": "Bearer tok-nope"},
                         json={"name": "forged", "steps": STEPS})
    assert r.status_code == 401
    assert await db.fetchval("select count(*) from runs") == 0
