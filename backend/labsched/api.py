"""HTTP surface.

Every mutation takes a bearer token and the audit records the token id, not a
name from a query parameter. Resolving an intervention can destroy a customer's
plate, so leaving the destructive endpoints open would undo the rest.

Rejections are typed: 422 malformed, 403 the token forbids it, 409 the fleet
can never satisfy it, each with a `remedy` an agent can branch on, and all
problems reported at once.

`/api/state` is the operator wall display: everything, unauthenticated, at 1 Hz.
Fine for one lab on one screen, hopeless at fifty runs across thirty
instruments. The agent path is `GET /api/runs/{id}` plus the resumable cursor,
which is bounded and does not grow with the lab.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import catalog, db, interventions, protocols
from .auth import tokens
from .bootstrap import make_registry
from .config import settings
from .drivers.sim import SimDriver
from .faults import ALL_KINDS, HUMAN_FAULTS
from .runs import AdmissionError, RunRequest, commit, get_run, plan
from .scheduler import Scheduler

_sim = SimDriver()
_registry = make_registry(_sim)
scheduler = Scheduler(_registry)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.migrate()
    await catalog.install_defaults()
    loaded = await protocols.load_directory()
    for name, why in getattr(protocols.load_directory, "rejected", []):
        # Loud, not fatal. A protocol file that will not register means the lab
        # is running an older definition of it, and silence there is how you
        # end up debugging a form instead of a version number.
        print(f"[protocols] REJECTED {name}: {why}", flush=True)
    print(f"[protocols] loaded {len(loaded)}", flush=True)
    task = asyncio.create_task(scheduler.run_forever())
    # A bare create_task is a loop nobody watches: if `run_forever` returns or
    # raises, the API keeps serving the lab's last state and nothing moves
    # again. This puts that death where /api/health already looks.
    def _loop_ended(t: asyncio.Task) -> None:
        if t.cancelled() or scheduler.stopping:
            return
        exc = t.exception()
        scheduler.last_error = (
            f"scheduler loop exited: {type(exc).__name__}: {exc}" if exc
            else "scheduler loop exited without an error")
        scheduler.error_count += 1
        print(f"[scheduler] {scheduler.last_error}", flush=True)

    task.add_done_callback(_loop_ended)
    try:
        yield
    finally:
        scheduler.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await db.close_pool()


app = FastAPI(title="labsched", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _row(r) -> dict:
    return {k: _iso(v) for k, v in dict(r).items()}


# ------------------------------------------------------------------- auth ---

async def bearer(authorization: str | None, why: str | None = None) -> str:
    """Resolve the credential to a token id.

    A serialised biscuit is the real form, verified against the lab's root
    key, so possession of the bytes is the credential. A bare token id is a
    demo convenience: it is not a secret and a deployment would refuse it.
    """
    if not authorization:
        raise HTTPException(401, detail={
            "error": why or "this endpoint changes physical state and requires "
                            "a capability token",
            "hint": "send `Authorization: Bearer <token id or serialised biscuit>`",
        })
    raw = authorization.removeprefix("Bearer ").strip()

    if raw.startswith("tok-"):
        if not await db.fetchval("select 1 from tokens where id=$1", raw):
            raise HTTPException(401, detail={"error": f"no such token '{raw}'"})
        return raw

    try:
        from biscuit_auth import Biscuit
        kp = await tokens.keypair()
        parsed = Biscuit.from_base64(raw, kp.public_key)
    except Exception as exc:
        raise HTTPException(401, detail={
            "error": "token did not verify against the lab root key",
            "detail": str(exc)[:200],
        })
    rev = list(parsed.revocation_ids)[-1]
    tid = await db.fetchval("select id from tokens where revocation_id=$1", rev)
    if not tid:
        raise HTTPException(401, detail={"error": "token verified but is not known to this lab"})
    return tid


async def require_authority(token_id: str, authority: str, what: str) -> None:
    kinds = await db.fetchval("select allowed_kinds from tokens where id=$1", token_id)
    res = await tokens.authorize(
        token_id, device_kinds=list(kinds or [])[:1], concurrent=1, wallclock_s=0,
        credits=0, authority=authority,
    )
    if not res.allowed:
        raise HTTPException(403, detail={
            "error": f"{what} requires authority '{authority}'",
            "reason": res.reason,
            "remedy": "widen_token_or_escalate",
            "token_id": token_id,
        })


# ------------------------------------------------------------------ state ---

@app.get("/api/state")
async def state() -> dict:
    devices = await db.fetch(
        """
        select d.*,
               extract(epoch from (now() - d.last_heartbeat)) as heartbeat_age_s,
               s.id  as step_id, s.name as step_name, s.state as step_state,
               s.started_at as step_started_at, s.duration_s as step_duration_s,
               s.run_id as step_run_id, r.name as run_name,
               res.sample_id as held_sample_id,
               iv.id as intervention_id, iv.kind as intervention_kind,
               (select epoch from device_calibration_epochs e
                 where e.device_id = d.id and e.ended_at is null
                 order by epoch desc limit 1) as calibration_epoch
          from devices d
          left join reservations res
                 on res.device_id = d.id and res.device_released_at is null
          left join steps s on s.id = res.step_id
          left join runs r on r.id = s.run_id
          left join lateral (
              -- One row per instrument, or the fleet grows: the other joins
              -- are bounded by an index and this one is not. Two open
              -- questions about one machine drew a second tile and counted six
              -- instruments as seven.
              select i.id, i.kind from interventions i
               where i.device_id = d.id and i.state = 'open'
               order by i.created_at, i.id limit 1
          ) iv on true
         order by d.layout_x, d.layout_y, d.id
        """
    )
    samples = await db.fetch(
        """
        select sm.*, r.id as run_id, r.name as run_name, r.state as run_state
          from samples sm
          left join lateral (
              select r.id, r.name, r.state from runs r
                join steps st on st.run_id = r.id
               where st.sample_id = sm.id
               order by r.created_at desc limit 1
          ) r on true
         order by sm.created_at
        """
    )
    runs = await db.fetch(
        """
        select r.*,
               count(s.*) filter (where s.state='done') as steps_done,
               count(s.*) as steps_total
          from runs r left join steps s on s.run_id = r.id
         group by r.id order by r.created_at desc limit 60
        """
    )
    steps = await db.fetch(
        """
        select s.*, r.name as run_name, r.priority
          from steps s join runs r on r.id = s.run_id
         order by r.created_at desc, s.idx limit 400
        """
    )
    ivs = await db.fetch(
        """
        select i.*, s.name as step_name, s.capability, s.op, r.name as run_name,
               sm.label as sample_label, d.kind as device_kind
          from interventions i
          left join steps s on s.id = i.step_id
          left join runs r on r.id = i.run_id
          left join samples sm on sm.id = i.sample_id
          left join devices d on d.id = i.device_id
         order by (i.state='open') desc, i.created_at desc limit 60
        """
    )
    results = await db.fetch(
        """
        select r.*, s.name as step_name, ru.name as run_name
          from results r
          join steps s on s.id = r.step_id
          join runs ru on ru.id = r.run_id
         -- Held first: `held` is the one state that moves backwards in time,
         -- so a plain recency window drops the rows this tab exists to show,
         -- and drops more of them the busier the lab gets.
         order by (r.state='held') desc, r.created_at desc limit 120
        """
    )
    tok = await db.fetch("select * from tokens order by created_at")
    chaos = await db.fetchval("select value from sim_config where key='chaos'") or {}
    storage = await db.fetchval("select value from sim_config where key='storage_tile'") \
        or {"x": -1, "y": 1}

    return {
        "now": datetime.now(tz=timezone.utc).isoformat(),
        "time_scale": settings.time_scale,
        "scheduler": {"ticks": scheduler.ticks, "last_error": scheduler.last_error},
        "chaos": {"rate": float(chaos.get("rate", 0.0))},
        "storage_tile": storage,
        "devices": [_row(d) for d in devices],
        "samples": [_row(s) for s in samples],
        "runs": [_row(r) for r in runs],
        "steps": [_row(s) for s in steps],
        "interventions": [_row(i) for i in ivs],
        "results": [_row(r) for r in results],
        "tokens": [_row(t) for t in tok],
        "open_intervention_count": sum(1 for i in ivs if i["state"] == "open"),
        # Only `held`. A result in `pending_qc` is mid-check, not stopped, and
        # counting it made the badge a number that only ever went up.
        # Counted over the table, not over the page: summing the payload made
        # the badge fall as the lab got busier, because new rows pushed held
        # ones out of the window while nothing was resolved.
        "held_result_count": await db.fetchval(
            "select count(*) from results where state='held'") or 0,
        # Same reason, one component over: the Results tab's filter chips
        # counted the page they were filtering, so they read "released 120"
        # against 155 and offered no chip at all for a state whose rows had
        # all aged out of the window.
        "result_state_counts": {
            r["state"]: r["n"] for r in await db.fetch(
                "select state, count(*) n from results group by state")},
    }


# ------------------------------------------------------------------- runs ---

class StepIn(BaseModel):
    name: str | None = None
    op: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    after: list[int] = []
    sample: int = 0

    model_config = {"populate_by_name": True}


class RunIn(BaseModel):
    """The body of a submission. Note what is *not* here: the token.

    Which capability a run is charged against is not the requester's to
    declare. It is whatever credential they presented, so it comes from the
    `Authorization` header and nowhere else. A `token_id` field in the body
    would be a request to be believed.
    """

    name: str
    priority: int = 0
    protocol: str | None = None
    version: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    plates: list[str] | None = None
    plate_count: int | None = None
    steps: list[StepIn] | None = None
    client_run_id: str | None = None
    project_id: str | None = None

    model_config = {"extra": "forbid"}

    def to_request(self, token_id: str) -> RunRequest:
        return RunRequest(
            name=self.name, token_id=token_id, priority=self.priority,
            protocol=self.protocol, version=self.version, params=self.params,
            plates=self.plates, plate_count=self.plate_count,
            steps=[{"name": s.name, "op": s.op, "with": s.with_, "after": s.after,
                    "sample": s.sample} for s in self.steps] if self.steps else None,
            client_run_id=self.client_run_id, project_id=self.project_id,
        )


@app.post("/api/runs/plan")
async def plan_run(body: RunIn, authorization: Annotated[str | None, Header()] = None):
    """Same body as a submission, no side effects.

    Admission is a pure function; this is it, exposed. In a lab where the unit
    of waste is a plate and three weeks, being able to ask "what would this
    cost, and would it even be allowed" before committing is worth more than
    any scheduling cleverness.

    Same credential as the submission, because half of what it answers is
    "would my token allow this", which means nothing if the caller picks the
    token to be asked about.
    """
    token_id = await bearer(
        authorization,
        why="planning answers what your token would be allowed to do, so it "
            "requires that token")
    p, problems = await plan(body.to_request(token_id))
    if problems or p is None:
        # Same envelope as a real submission. A dry run that reports problems
        # in a different shape from the endpoint it stands in for is useless to
        # write a client against.
        err = AdmissionError(problems)
        raise HTTPException(err.status, detail={"ok": False, **err.body()})
    return {"ok": True, "plan": p.as_dict()}


@app.post("/api/runs", status_code=201)
async def create_run(
    body: RunIn,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    token_id = await bearer(authorization)
    req = body.to_request(token_id)
    if idempotency_key and not req.client_run_id:
        req.client_run_id = idempotency_key

    p, problems = await plan(req)
    if problems or p is None:
        err = AdmissionError(problems)
        raise HTTPException(err.status, detail={**err.body(), "token_id": req.token_id})

    run, replayed = await commit(p)
    # An agent that timed out mid-POST and retried is not making a mistake, and
    # must not start a second physical experiment.
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    if replayed:
        response.status_code = 200
    return _run_body(run) | {"projected": p.as_dict()["projected"],
                             "warnings": [w.as_dict() for w in p.warnings]}


def _run_body(run: dict) -> dict:
    return {
        **{k: _iso(v) for k, v in run.items()
           if k not in ("steps", "physical_state", "results")},
        "steps": [_row(s) for s in run["steps"]],
        "physical_state": [_row(s) for s in run["physical_state"]],
        "results": [_row(r) for r in run["results"]],
    }


@app.get("/api/runs/{run_id}")
async def read_run(run_id: str, authorization: Annotated[str | None, Header()] = None) -> dict:
    try:
        run = await get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, detail={"error": str(exc)})

    body = _run_body(run)
    body["as_of"] = datetime.now(tz=timezone.utc).isoformat()
    body["blocked"] = await _blocked_object(run_id, authorization)
    body["cursor"] = await db.fetchval(
        "select coalesce(max(seq), 0) from audit where run_id=$1", run_id)
    return body


async def _blocked_object(run_id: str, authorization: str | None) -> dict | None:
    """What an agent needs: am I blocked, on whom, until when, can I act.

    Cross-tenant detail is a count, never ids: an agent may learn that three
    other runs are queued on the same incubator, not whose. The operator's
    named set is `affected_run_ids`, which this payload does not carry.
    """
    iv = await db.fetchrow(
        """
        select i.*, s.name as step_name from interventions i
          left join steps s on s.id = i.step_id
         where i.state='open' and ($1 = any(i.affected_run_ids) or i.run_id = $1)
         order by i.created_at limit 1
        """,
        run_id,
    )
    if iv is None:
        return None

    token_id = None
    if authorization:
        try:
            token_id = await bearer(authorization)
        except HTTPException:
            token_id = None

    options = []
    for o in iv["options"]:
        c = await interventions.consequences(iv["id"], o["key"])
        options.append({
            "key": o["key"], "label": o["label"], "authority": o["authority"],
            "reversible": o["reversible"], "agent_resolvable": o["agent_resolvable"],
            "requires_reason": c["requires_reason"],
            "credits_spent_again": c["credits_spent_again"],
            "plates_destroyed": c["plates_destroyed"],
            "runs_aborted": c["runs_aborted"],
        })

    may, why = False, "no credential presented"
    if token_id:
        for o in iv["options"]:
            try:
                await interventions.check_authority(iv, o["key"], token_id)
                may, why = True, f"your token may take '{o['key']}'"
                break
            except Exception as exc:
                why = str(exc)

    others = await interventions.other_runs_on_device(iv["device_id"], run_id)

    return {
        "intervention_id": iv["id"],
        "kind": iv["kind"],
        "question": iv["message"],
        "could_not_observe": (iv["detail"] or {}).get("could_not_observe"),
        "awaiting_authority": iv["required_authority"],
        "opened_at": _iso(iv["created_at"]),
        "expires_at": _iso(iv["expires_at"]),
        "escalation_policy": iv["escalation_policy"],
        "escalated": bool((iv["detail"] or {}).get("escalated")),
        "options": options,
        "agent_may_resolve": may,
        "reason": why,
        "version": iv["version"],
        "other_runs_affected": others or 0,
    }


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, since: int = 0, limit: int = 200) -> dict:
    """Resumable cursor over the audit log.

    An agent reconnects with the last `seq` it saw and gets everything after
    it. Never blocks indefinitely: an agent that times out mid-call and retries
    is how state disagreement starts.

    Gap-free takes care. `seq` is a `bigserial`, allocated at insert and
    visible at commit, which are different moments in different orders: writer
    A takes 5, writer B takes 6 and commits first, and a reader that advances
    to 6 never sees 5. So the cursor stops at the first hole and steps over it
    only once the hole is older than `HOLE_GRACE`, by which point the
    transaction holding it has committed or rolled back for good.
    """
    cap = min(limit, 500)
    watermark = await _audit_watermark(since)
    rows = await db.fetch(
        "select * from audit where run_id=$1 and seq > $2 and seq <= $3"
        " order by seq limit $4",
        run_id, since, watermark, cap,
    )

    # A global position, so it advances even when this run had no events in
    # the span. Otherwise an agent following a quiet run re-scans forever.
    cursor = rows[-1]["seq"] if len(rows) == cap else max(since, watermark)
    return {
        "run_id": run_id,
        "events": [_row(r) for r in rows],
        "cursor": cursor,
        "has_more": len(rows) == cap or cursor < await _max_seq(),
    }


async def _max_seq() -> int:
    return await db.fetchval("select coalesce(max(seq), 0) from audit") or 0


async def _audit_watermark(since: int) -> int:
    """The highest sequence number below which nothing can still appear.

    Holes are global, so this cannot be computed from one run's rows: a run
    legitimately skips every seq belonging to another. Stops just below the
    lowest missing number, unless that hole has aged past `HOLE_GRACE_S` and is
    therefore permanent.
    """
    row = await db.fetchrow(
        """
        with bounds as (
            select greatest($1::bigint, coalesce(max(seq), 0) - 10000) as lo,
                   coalesce(max(seq), 0) as hi
              from audit
        ),
        missing as (
            select s from bounds, generate_series(bounds.lo + 1, bounds.hi) as s
             where not exists (select 1 from audit a where a.seq = s)
        ),
        live as (
            -- A hole stays live until a row above it is old enough that a
            -- transaction holding it would have committed.
            select m.s from missing m
             where not exists (
                 select 1 from audit a
                  where a.seq > m.s and a.at < now() - make_interval(secs => $2)
             )
        )
        select (select hi from bounds) as hi, (select min(s) from live) as first_live_hole
        """,
        since, HOLE_GRACE_S,
    )
    if row is None:
        return since
    hole = row["first_live_hole"]
    return (hole - 1) if hole is not None else row["hi"]


@app.post("/api/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    if not await db.fetchval("select 1 from runs where id=$1", run_id):
        raise HTTPException(404, detail={"error": f"no such run {run_id}"})
    await require_authority(token_id, "engineer", "cancelling a run")

    in_flight = await db.fetchval(
        "select count(*) from steps where run_id=$1 and state in ('scheduled','running')",
        run_id) or 0
    await scheduler.request_drain(run_id, "cancelled by operator", actor=f"token:{token_id}")
    # Cancel is a request, not a command: a plate inside a machine finishes or
    # is parked before the run closes out.
    return {
        "cancelling": True,
        "run_id": run_id,
        "irreversible_steps_in_flight": in_flight,
        "note": "steps already running will finish or park; nothing is aborted mid-operation",
    }


# ---------------------------------------------------------- interventions ---

class ResolveIn(BaseModel):
    option: str
    reason: str | None = None
    expected_version: int | None = None


@app.get("/api/interventions")
async def list_interventions(state: Literal["open", "resolved", "all"] = "open") -> list[dict]:
    sql = """
        select i.*, s.name as step_name, r.name as run_name, sm.label as sample_label
          from interventions i
          left join steps s on s.id = i.step_id
          left join runs r on r.id = i.run_id
          left join samples sm on sm.id = i.sample_id
    """
    if state != "all":
        rows = await db.fetch(sql + " where i.state=$1 order by i.created_at desc", state)
    else:
        rows = await db.fetch(sql + " order by i.created_at desc")
    return [_row(r) for r in rows]


@app.get("/api/interventions/{intervention_id}/consequences")
async def intervention_consequences(intervention_id: str) -> dict:
    iv = await db.fetchrow("select * from interventions where id=$1", intervention_id)
    if iv is None:
        raise HTTPException(404, detail={"error": f"no such intervention {intervention_id}"})
    return {
        "intervention_id": intervention_id,
        "options": [await interventions.consequences(intervention_id, o["key"])
                    for o in iv["options"]],
    }


@app.post("/api/interventions/{intervention_id}/resolve")
async def resolve_intervention(
    intervention_id: str, body: ResolveIn,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    token_id = await bearer(authorization)
    try:
        return await interventions.resolve(
            intervention_id, body.option, token_id=token_id, reason=body.reason,
            expected_version=body.expected_version, transit_s=scheduler.cfg.transit_s,
        )
    except interventions.AlreadyResolved as exc:
        prior = exc.intervention
        # Same answer, already applied: that is success with information, not a
        # failure. Anything else is a genuine conflict.
        if prior["resolution"] == body.option:
            return {"intervention_id": intervention_id, "resolution": prior["resolution"],
                    "already_resolved": True,
                    "resolved_by_token": prior["resolved_by_token"]}
        raise HTTPException(409, detail={
            "error": str(exc), "resolution": prior["resolution"],
            "resolved_by_token": prior["resolved_by_token"],
            "remedy": "read_current_state", "retryable": False,
        })
    except interventions.StaleVersion as exc:
        raise HTTPException(409, detail={
            "error": str(exc), "expected_version": exc.expected,
            "current_version": exc.actual, "remedy": "re_read_then_decide",
        })
    except interventions.Forbidden as exc:
        raise HTTPException(403, detail={
            "error": str(exc), "remedy": "widen_token_or_escalate", "token_id": token_id})
    except interventions.InterventionError as exc:
        raise HTTPException(400, detail={"error": str(exc)})


class BatchIn(BaseModel):
    group_key: str
    option: str
    reason: str | None = None


@app.post("/api/interventions/resolve-batch/preview")
async def preview_batch(body: BatchIn) -> dict:
    return await interventions.preview_batch(body.group_key, body.option)


@app.post("/api/interventions/resolve-batch")
async def resolve_batch(
    body: BatchIn, authorization: Annotated[str | None, Header()] = None
) -> dict:
    token_id = await bearer(authorization)
    try:
        return await interventions.resolve_batch(
            body.group_key, body.option, token_id=token_id, reason=body.reason,
            transit_s=scheduler.cfg.transit_s)
    except interventions.Forbidden as exc:
        raise HTTPException(403, detail={"error": str(exc), "token_id": token_id})
    except interventions.InterventionError as exc:
        raise HTTPException(400, detail={"error": str(exc)})


@app.post("/api/interventions/{intervention_id}/acknowledge")
async def ack(intervention_id: str,
              authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    try:
        row = await interventions.acknowledge(intervention_id, token_id)
    except interventions.InterventionError as exc:
        raise HTTPException(404, detail={"error": str(exc)})
    return _row(row)


@app.get("/api/fault-kinds")
async def fault_kinds() -> dict:
    return {
        "injectable": list(ALL_KINDS),
        "human": {
            k: {
                "title": s.title, "message": s.message,
                "could_not_observe": s.could_not_observe,
                "options": [{"key": o.key, "label": o.label, "consequence": o.consequence,
                             "authority": o.authority, "reversible": o.reversible,
                             "requires_reason": o.requires_reason,
                             "agent_resolvable": o.agent_resolvable} for o in s.options],
                "holds": {"device": s.hold_device, "sample": s.hold_sample,
                          "rationale": s.hold_rationale},
                "sla_seconds": s.sla_seconds, "escalation_policy": s.escalation_policy,
                "cohort_scope": s.cohort_scope,
                # null = any instrument. Otherwise the operations this fault is
                # physically meaningful for, which is what the injector should
                # let you pick from.
                "capabilities": list(s.capabilities) if s.capabilities else None,
            }
            for k, s in HUMAN_FAULTS.items()
        },
    }


# --------------------------------------------------- catalog + protocols ----

@app.get("/api/operations")
async def list_operations() -> list[dict]:
    return [
        {"name": o.name, "capability": o.capability,
         "nominal_duration_s": o.nominal_duration_s, "credit_cost": o.credit_cost,
         "on_unknown": o.on_unknown, "max_attempts": o.max_attempts,
         "params_schema": o.params_schema, "description": o.description}
        for o in (await catalog.all_operations()).values()
    ]


@app.get("/api/protocols")
async def list_protocols() -> list[dict]:
    return [
        {"name": p.name, "version": p.version, "digest": p.digest,
         "params": p.param_schema, "plate_bounds": list(p.plate_bounds),
         "description": (p.spec.get("description") or "").strip(),
         "source": p.source}
        for p in await protocols.list_all()
    ]


@app.get("/api/results")
async def list_results(state: str | None = None) -> list[dict]:
    rows = await db.fetch(
        """
        select r.*, s.name as step_name, ru.name as run_name
          from results r join steps s on s.id=r.step_id join runs ru on ru.id=r.run_id
         where ($1::text is null or r.state = $1)
         order by r.created_at desc limit 200
        """,
        state)
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- devices ---

@app.post("/api/devices/{device_id}/quarantine")
async def quarantine(device_id: str,
                     authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    if not await db.fetchval("select 1 from devices where id=$1", device_id):
        raise HTTPException(404, detail={"error": f"no such device {device_id}"})
    await require_authority(token_id, "engineer", "quarantining an instrument")
    p = await db.pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await interventions.quarantine_device(
                conn, device_id, f"token:{token_id}", "quarantined by operator")
    return {"ok": True, "device_id": device_id, "quarantined": True}


@app.post("/api/devices/{device_id}/unquarantine")
async def unquarantine(device_id: str,
                       authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    if not await db.fetchval("select 1 from devices where id=$1", device_id):
        raise HTTPException(404, detail={"error": f"no such device {device_id}"})
    await require_authority(token_id, "engineer", "returning an instrument to service")
    await scheduler.reset_faulted(device_id)
    p = await db.pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # Note what is NOT set here: last_heartbeat. Inventing a heartbeat
            # the instrument never sent would be the software fabricating a
            # physical fact. The sweep will discover it on the next tick.
            await conn.execute(
                "update devices set quarantined=false, suspect=false, state='offline',"
                " note='returned to service, awaiting heartbeat' where id=$1",
                device_id)
            await interventions.roll_epoch(conn, device_id, "good", "returned to service")
            from . import audit
            await audit.log(conn, f"token:{token_id}", "device.returned_to_service",
                            device_id=device_id, token_id=token_id)
    return {"ok": True, "device_id": device_id, "quarantined": False,
            "note": "instrument stays offline until it sends a heartbeat of its own"}


@app.post("/api/devices/{device_id}/calibration/{epoch}/suspect")
async def suspect_epoch(device_id: str, epoch: int,
                        note: Annotated[str, Body(embed=True)] = "marked suspect by operator",
                        authorization: Annotated[str | None, Header()] = None) -> dict:
    """Reach backwards over results produced, and possibly delivered, under a
    calibration epoch now believed bad."""
    token_id = await bearer(authorization)
    await require_authority(token_id, "engineer", "invalidating a calibration epoch")
    iid, affected = await interventions.mark_epoch_suspect(
        device_id, epoch, f"token:{token_id}", note)
    # Empty has two causes and they are not the same news: an epoch that
    # measured nothing, and one whose every number is already held by an open
    # question. Reporting the second as the first told an engineer looking at a
    # row of results that no results exist.
    elsewhere = (await interventions.results_held_elsewhere(device_id, epoch)
                 if not affected and iid is None else 0)
    return {
        "ok": True, "device_id": device_id, "epoch": epoch, "intervention_id": iid,
        "results_held": affected, "held_elsewhere": elsewhere,
        # The old text claimed "no results were altered" right after moving
        # every one from released/pass to held/warn.
        "note": (
            f"{affected} result(s) moved to held pending one decision about the "
            f"whole epoch; nothing was invalidated"
            if affected else (
                f"a question about that epoch is already open ({iid})" if iid
                else f"epoch closed suspect; its {elsewhere} result(s) are already "
                     f"held by another open question, which decides them"
                if elsewhere
                else "no results were produced under that epoch"
            )
        ),
    }


# ----------------------------------------------------------------- tokens ---

class AttenuateIn(BaseModel):
    label: str
    tier: Literal["org", "project", "agent"] = "agent"
    allowed_kinds: list[str]
    max_concurrent: int
    max_wallclock_s: int
    max_run_credits: int
    budget_credits: int
    expires_in_days: int = 7
    authorities: list[str] = Field(default_factory=list)


@app.get("/api/tokens")
async def list_tokens() -> list[dict]:
    rows = await db.fetch(
        """
        select t.*,
               (select count(*) from runs r where r.token_id = t.id) as runs,
               (select count(*) from reservations res
                 where res.token_id = t.id
                   and (res.device_released_at is null or res.sample_released_at is null))
                 as active_reservations
          from tokens t order by t.created_at
        """
    )
    return [_row(r) for r in rows]


@app.post("/api/tokens/{token_id}/attenuate")
async def attenuate_token(token_id: str, body: AttenuateIn,
                          authorization: Annotated[str | None, Header()] = None) -> dict:
    """Mint a weaker child of a token you hold.

    Weaker than its parent is not enough: the caller also has to hold the
    parent, or this is a token factory. Anyone could mint a child of the org
    token with engineer authority and use it, making attenuation a way to gain
    a credential rather than hand out a narrower one.

    Holding an ancestor counts, since an ancestor can mint anything its
    descendants can.
    """
    actor = await bearer(authorization)
    chain = {t["id"] for t in await tokens.lineage(token_id)}
    if not chain:
        raise HTTPException(404, detail={"error": f"no such token {token_id}"})
    if actor not in chain:
        raise HTTPException(403, detail={
            "error": "you may only attenuate a token you hold",
            "reason": f"'{actor}' is not '{token_id}' nor an ancestor of it",
            "remedy": "present_the_parent_token",
            "token_id": actor,
        })
    try:
        tok = await tokens.attenuate(
            token_id, body.label, body.tier,
            tokens.Caveats(
                allowed_kinds=body.allowed_kinds, max_concurrent=body.max_concurrent,
                max_wallclock_s=body.max_wallclock_s, max_run_credits=body.max_run_credits,
                budget_credits=body.budget_credits,
                expires_at=tokens.default_expiry(body.expires_in_days),
                authorities=body.authorities,
            ),
        )
    except tokens.TokenError as exc:
        raise HTTPException(400, detail={"error": str(exc)})
    return _row(tok)


@app.post("/api/tokens/{token_id}/revoke")
async def revoke_token(token_id: str,
                       authorization: Annotated[str | None, Header()] = None) -> dict:
    actor = await bearer(authorization)
    if not await db.fetchval("select 1 from tokens where id=$1", token_id):
        raise HTTPException(404, detail={"error": f"no such token {token_id}"})
    await require_authority(actor, "engineer", "revoking a token")
    killed = await tokens.revoke(token_id, f"token:{actor}")
    return {"ok": True, "revoked": killed}


@app.get("/api/tokens/{token_id}/lineage")
async def token_lineage(token_id: str) -> list[dict]:
    return [_row(t) for t in await tokens.lineage(token_id)]


# ------------------------------------------------------------------ audit ---

#: How long a hole in the audit sequence is presumed to be an in-flight
#: transaction rather than a rolled-back one. Every transaction in this
#: codebase is short; a hole older than this is never going to be filled.
HOLE_GRACE_S = 5.0

@app.get("/api/audit")
async def audit_log(
    run_id: str | None = None, device_id: str | None = None,
    token_id: str | None = None, since: int = 0, limit: int = 200,
) -> list[dict]:
    rows = await db.fetch(
        """
        select * from audit
         where ($1::text is null or run_id = $1)
           and ($2::text is null or device_id = $2)
           and ($3::text is null or token_id = $3)
           and seq > $4
         order by seq desc limit $5
        """,
        run_id, device_id, token_id, since, min(limit, 1000),
    )
    return [_row(r) for r in rows]


# -------------------------------------------------------------------- sim ---

class FaultIn(BaseModel):
    kind: str
    device_id: str | None = None
    step_id: str | None = None


@app.post("/api/sim/fault")
async def inject_fault(body: FaultIn,
                       authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    await require_authority(token_id, "engineer", "injecting a fault")
    if body.kind not in ALL_KINDS:
        raise HTTPException(400, detail={
            "error": f"unknown fault kind '{body.kind}'",
            "hint": f"expected one of {list(ALL_KINDS)}"})
    device = await db.fetchrow(
        "select * from devices where id=$1", body.device_id) if body.device_id else None
    if body.device_id and device is None:
        raise HTTPException(404, detail={"error": f"no such device {body.device_id}"})

    # A fault has to be one this instrument could physically produce. Opening
    # "liquid handler aborted mid-transfer, this handler has no tip-level
    # liquid sensing" against an incubator makes the whole taxonomy look
    # decorative. Random chaos was already gated on this; hand injection was
    # not, so the UI's "Break it" could bypass it.
    spec = HUMAN_FAULTS.get(body.kind)
    if device is not None and spec is not None and spec.capabilities is not None:
        if not (set(device["capabilities"]) & set(spec.capabilities)):
            able = await db.fetch(
                "select id from devices where capabilities && $1::text[] order by id",
                list(spec.capabilities))
            raise HTTPException(422, detail={
                "error": f"'{body.kind}' cannot happen on {body.device_id}",
                "reason": f"{body.device_id} does '{', '.join(device['capabilities'])}'; "
                          f"this fault is only meaningful for "
                          f"'{', '.join(spec.capabilities)}'",
                "remedy": "pick_a_capable_instrument",
                "instruments": [r["id"] for r in able],
            })

    await db.execute(
        "insert into pending_faults(kind, device_id, step_id) values ($1,$2,$3)",
        body.kind, body.device_id, body.step_id,
    )
    return {"ok": True, "queued": body.kind, "device_id": body.device_id}


@app.post("/api/sim/drift/{device_id}")
async def induce_drift(device_id: str,
                       authorization: Annotated[str | None, Header()] = None) -> dict:
    """Make an instrument start drifting silently.

    Unlike every other injection this raises no fault. The instrument keeps
    answering, completing jobs and reporting success, and only its control
    values move off baseline. If the system notices, it notices by itself.
    """
    token_id = await bearer(authorization)
    await require_authority(token_id, "engineer", "inducing drift")
    if not await db.fetchval("select 1 from devices where id=$1", device_id):
        raise HTTPException(404, detail={"error": f"no such device {device_id}"})
    await _sim.set_degraded(device_id)
    return {"ok": True, "device_id": device_id,
            "note": "no fault raised; controls will drift and QC must catch it"}


class ChaosIn(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)


@app.post("/api/sim/chaos")
async def set_chaos(body: ChaosIn,
                    authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    await require_authority(token_id, "engineer", "changing the fault rate")
    await db.execute(
        "insert into sim_config(key, value) values ('chaos', $1)"
        " on conflict (key) do update set value = excluded.value",
        {"rate": body.rate, "kinds": None},
    )
    return {"ok": True, "rate": body.rate}


@app.post("/api/sim/reseed")
async def reseed(chaos: float = 0.0,
                 authorization: Annotated[str | None, Header()] = None) -> dict:
    token_id = await bearer(authorization)
    await require_authority(token_id, "engineer", "reseeding the lab")
    from .seed import seed
    out = await seed(reset=True, chaos=chaos)
    # A reseed drops the schema, so the root signing key is regenerated and
    # every token minted against the old one stops verifying. This process
    # cached that key at first use; without dropping it here the lab comes
    # back up and then refuses all of its own credentials.
    tokens.reset_keypair_cache()
    return {"ok": True, **out}


@app.get("/api/health")
async def health() -> dict:
    return {
        # `ok` is about the loop right now. `errors` is cumulative, so a
        # process that recovered still admits it had to.
        "ok": scheduler.last_error is None,
        "ticks": scheduler.ticks,
        "last_error": scheduler.last_error,
        "errors": scheduler.error_count,
        "devices": await db.fetchval("select count(*) from devices"),
    }
