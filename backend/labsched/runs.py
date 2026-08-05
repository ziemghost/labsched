"""Run admission: a pure planner and a separate committer.

`plan()` runs every check and touches no state, so `POST /api/runs/plan` is a
real submission minus the writes. Where the unit of waste is a plate and three
weeks, asking what something would cost before committing beats any scheduling
cleverness.

Every problem is reported at once, with a machine-readable `code` and `path`,
because the consumer is a program repairing itself rather than a human reading
prose. Raising on the first bad step cost an agent five round trips to find
five mistakes.

A problem's class picks the status: 422 the caller fixes the request, 403 needs
a wider token, 409 needs a different fleet. Collapsing all three into
403-with-a-string leaves an agent nothing to branch on but English.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import audit, catalog, db, protocols
from .auth import tokens
from .config import settings

# Which remedy a problem implies. The response reports the most fundamental
# class present: a caller cannot act on "your token is too narrow" while the
# request itself is still malformed.
#
# Only the two non-default classes are listed; `klass` falls through to
# "protocol", so anything unnamed here is the caller's request to fix. A third
# set nothing read sat here drifting out of date.
TOKEN_CODES = {"token_forbids_kind", "token_limit", "token_revoked", "token_unknown"}
FLEET_CODES = {"no_such_capability", "no_instrument_in_service"}

#: Ceiling on plates in one run, for the ad-hoc form that has no protocol to
#: declare its own bound. Matches a standard carrier.
MAX_PLATES = 96

REMEDY = {
    "protocol": "edit_request",
    "token": "widen_token_or_escalate",
    "fleet": "wait_for_capacity_or_give_up",
}
STATUS = {"protocol": 422, "token": 403, "fleet": 409}


@dataclass
class Problem:
    code: str
    path: str
    message: str
    hint: str | None = None
    severity: str = "error"

    @property
    def klass(self) -> str:
        if self.code in TOKEN_CODES:
            return "token"
        if self.code in FLEET_CODES:
            return "fleet"
        return "protocol"

    def as_dict(self) -> dict:
        return {
            "code": self.code, "path": self.path, "message": self.message,
            "hint": self.hint, "severity": self.severity,
        }


@dataclass
class PlannedStep:
    name: str
    op: str
    capability: str
    params: dict[str, Any]
    duration_s: int
    credit_cost: int
    max_attempts: int
    after: list[int]
    sample: int
    qc: dict[str, Any]
    candidate_kinds: list[str]
    candidate_devices: list[str]


@dataclass
class Plan:
    name: str
    token_id: str
    priority: int
    plates: list[str]
    steps: list[PlannedStep]
    allowed_kinds: list[str]
    total_credits: int
    total_wallclock_s: int
    concurrency: int
    critical_path_s: int
    warnings: list[Problem] = field(default_factory=list)
    protocol_name: str | None = None
    protocol_version: int | None = None
    protocol_digest: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    client_run_id: str | None = None
    project_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "token_id": self.token_id,
            "priority": self.priority,
            "protocol": (
                {"name": self.protocol_name, "version": self.protocol_version,
                 "digest": self.protocol_digest, "params": self.params}
                if self.protocol_name else None
            ),
            "plates": self.plates,
            "allowed_kinds": self.allowed_kinds,
            # Every duration in here is a *simulated* second, the same clock the
            # scheduler runs on. The screen shows lab time, which is the same
            # numbers multiplied by `time_scale`; carrying the factor in the
            # payload is what stops the two unit systems from being confused
            # for one, which they were.
            "projected": {
                "credits": self.total_credits,
                "instrument_seconds": self.total_wallclock_s,
                "critical_path_s": self.critical_path_s,
                "max_concurrent_steps": self.concurrency,
                "lab_seconds_per_second": settings.time_scale,
            },
            "steps": [
                {
                    "idx": i, "name": s.name, "op": s.op, "capability": s.capability,
                    "params": s.params, "duration_s": s.duration_s,
                    "credit_cost": s.credit_cost, "after": s.after, "plate": s.sample,
                    "candidate_kinds": s.candidate_kinds,
                    "candidate_devices": s.candidate_devices,
                }
                for i, s in enumerate(self.steps)
            ],
            "warnings": [w.as_dict() for w in self.warnings],
        }


@dataclass
class RunRequest:
    name: str
    token_id: str
    priority: int = 0
    protocol: str | None = None
    version: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    plates: list[str] | None = None
    plate_count: int | None = None
    steps: list[dict] | None = None      # wire DAG form, when no protocol
    client_run_id: str | None = None
    project_id: str | None = None


class AdmissionError(Exception):
    """Carries the whole problem list, not just the first thing that broke."""

    def __init__(self, problems: Sequence[Problem]):
        self.problems = list(problems)
        super().__init__("; ".join(p.message for p in self.problems) or "admission failed")

    @property
    def klass(self) -> str:
        for k in ("protocol", "token", "fleet"):
            if any(p.klass == k for p in self.problems):
                return k
        return "protocol"

    @property
    def status(self) -> int:
        return STATUS[self.klass]

    def body(self) -> dict:
        return {
            "errors": [p.as_dict() for p in self.problems],
            "remedy": REMEDY[self.klass],
            "retryable": self.klass == "fleet",
        }


# ------------------------------------------------------------------ graph ---

def _topo_partial(after: list[list[int]]) -> list[int]:
    """The steps that can be ordered. Everything else is in a cycle or behind
    one, which is what the error message has to name."""
    n = len(after)
    indeg = [len(a) for a in after]
    out: list[list[int]] = [[] for _ in range(n)]
    for i, deps in enumerate(after):
        for d in deps:
            out[d].append(i)
    queue = [i for i in range(n) if indeg[i] == 0]
    order: list[int] = []
    while queue:
        i = queue.pop(0)
        order.append(i)
        for j in out[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                queue.append(j)
    return order


def _topo(after: list[list[int]]) -> list[int] | None:
    """A full ordering, or None when one does not exist."""
    order = _topo_partial(after)
    return order if len(order) == len(after) else None


def _layer_depths(after: list[list[int]], order: list[int]) -> list[int]:
    depth = [0] * len(after)
    for i in order:
        for d in after[i]:
            depth[i] = max(depth[i], depth[d] + 1)
    return depth


def _critical_path(after: list[list[int]], order: list[int], dur: list[int]) -> int:
    best = [0] * len(after)
    for i in order:
        best[i] = dur[i] + max((best[d] for d in after[i]), default=0)
    return max(best, default=0)


# ------------------------------------------------------------------- plan ---

async def plan(req: RunRequest) -> tuple[Plan | None, list[Problem]]:
    """Validate everything and compute the projection. Touches no state."""
    problems: list[Problem] = []
    warnings: list[Problem] = []

    ops = await catalog.all_operations()
    if not ops:
        problems.append(Problem("no_instrument_in_service", "",
                                "the operation catalog is empty; the lab is not configured"))
        return None, problems

    # ---- resolve the step list, from a protocol or from the wire DAG ------
    proto = None
    resolved_params: dict[str, Any] = {}
    plate_labels: list[str] = []
    raw_steps: list[dict] = []

    if req.protocol:
        proto = await protocols.get(req.protocol, req.version)
        if proto is None:
            known = [p.name for p in await protocols.list_all()]
            problems.append(Problem(
                "unknown_protocol", "protocol",
                f"no protocol '{req.protocol}'"
                + (f" at version {req.version}" if req.version else ""),
                hint=(f"did you mean '{catalog.suggest(req.protocol, known)}'?"
                      if catalog.suggest(req.protocol, known) else f"known: {sorted(set(known))}"),
            ))
            return None, problems

        resolved_params, pp = protocols.validate_params(proto, req.params or {})
        problems.extend(Problem(**p) for p in pp)
        problems.extend(Problem(**p) for p in protocols.unknown_step_refs(proto))

        # Derive the labels FIRST, then validate what the run will actually
        # expand from. Validating `plate_count` while expanding from
        # `len(plates)` meant a request carrying both got as many plates as it
        # asked for: `plate_count: 1` alongside eight labels passed a bound of
        # four and ran eight plates.
        lo, hi = proto.plate_bounds
        # `or lo` treated an explicit 0 as "unset" and quietly ran one plate;
        # asking for zero plates is a mistake worth being told about.
        asked = req.plate_count if req.plate_count is not None else lo
        requested = len(req.plates) if req.plates else max(0, asked)
        if req.plates:
            plate_labels = list(req.plates)
        else:
            # Capped at hi + 1 so a request for a million plates still fails
            # the bound below without building a million labels first.
            plate_labels = [f"{req.name} plate {i + 1}"
                            for i in range(min(requested, hi + 1))]

        if not lo <= requested <= hi:
            problems.append(Problem(
                "bad_plate_count", "plates",
                f"protocol '{proto.name}' accepts {lo}..{hi} plates, got {requested}",
                hint=f"submit between {lo} and {hi} plates",
            ))
        if req.plates and req.plate_count is not None and req.plate_count != len(req.plates):
            problems.append(Problem(
                "conflicting_plate_count", "plate_count",
                f"'plate_count' is {req.plate_count} but {len(req.plates)} plate "
                f"labels were supplied",
                hint="send one or the other, not both",
            ))
        if problems:
            return None, problems

        expanded = protocols.expand(proto, resolved_params, len(plate_labels))
        raw_steps = [
            {"name": e.name, "op": e.op, "with": e.params, "after": e.after,
             "sample": e.sample, "qc": e.qc}
            for e in expanded
        ]
    else:
        raw_steps = list(req.steps or [])
        # `plate_count` was accepted and silently ignored here: ask for four
        # plates in the ad-hoc form and you got one, with `ok: true`. A
        # parameter that is read on one path and dropped on the other is worse
        # than one that does not exist.
        if req.plates:
            plate_labels = list(req.plates)
        else:
            n = max(1, req.plate_count or 1)
            plate_labels = ([f"{req.name} plate {i + 1}" for i in range(n)]
                            if n > 1 else [f"{req.name} plate"])
        if not raw_steps:
            problems.append(Problem("no_steps", "steps", "a run needs at least one step"))
            return None, problems
        # A plate is a physical object the lab has to find room for, and the
        # ad-hoc form has no protocol to bound it. Five hundred labels against
        # one step created five hundred plates for one step's credits.
        if len(plate_labels) > MAX_PLATES:
            problems.append(Problem(
                "bad_plate_count", "plates",
                f"{len(plate_labels)} plates requested; the lab accepts at most "
                f"{MAX_PLATES} in one run",
                hint=f"split this across runs of at most {MAX_PLATES} plates",
            ))
            return None, problems

    dupes = sorted({p for p in plate_labels if plate_labels.count(p) > 1})
    if dupes:
        problems.append(Problem(
            "duplicate_plate_labels", "plates",
            f"plate labels must be distinct; repeated: {dupes}",
            hint="two plates with the same label cannot be told apart on the floor",
        ))

    # ---- per-step validation --------------------------------------------
    planned: list[PlannedStep] = []
    after_lists: list[list[int]] = []

    for i, s in enumerate(raw_steps):
        path = f"steps[{i}]"
        op_name = s.get("op")
        op = ops.get(op_name) if op_name else None
        if op is None:
            hint = catalog.suggest(str(op_name), list(ops)) if op_name else None
            problems.append(Problem(
                "unknown_operation", f"{path}.op",
                f"no operation '{op_name}' in the lab catalog",
                hint=(f"did you mean '{hint}'?" if hint else f"known operations: {sorted(ops)}"),
            ))
            after_lists.append([])
            continue

        problems.extend(
            Problem(**p) for p in catalog.validate_params(op, dict(s.get("with") or {}), path)
        )

        deps = []
        for d in s.get("after") or []:
            if not isinstance(d, int) or not 0 <= d < len(raw_steps):
                problems.append(Problem(
                    "unknown_step_ref", f"{path}.after",
                    f"step {i} depends on step {d}, which does not exist",
                    hint=f"valid indexes are 0..{len(raw_steps) - 1}",
                ))
            elif d == i:
                problems.append(Problem(
                    "self_dependency", f"{path}.after", f"step {i} depends on itself"))
            else:
                deps.append(d)
        after_lists.append(deps)

        sample = s.get("sample", 0)
        if not isinstance(sample, int) or not 0 <= sample < len(plate_labels):
            problems.append(Problem(
                "bad_sample_ref", f"{path}.sample",
                f"step {i} references plate {sample} but the run declares {len(plate_labels)}",
                hint=f"valid plate indexes are 0..{len(plate_labels) - 1}",
            ))
            sample = 0

        planned.append(PlannedStep(
            name=s.get("name") or op.name, op=op.name, capability=op.capability,
            params=dict(s.get("with") or {}),
            duration_s=op.nominal_duration_s,       # lab-owned, never client-supplied
            credit_cost=op.credit_cost,             # ditto: this is what makes
            max_attempts=op.max_attempts,           # the budget caveat enforceable
            after=deps, sample=sample, qc=dict(s.get("qc") or {}),
            candidate_kinds=[], candidate_devices=[],
        ))

    if problems:
        return None, problems

    used_plates = {p.sample for p in planned}
    unused = [i for i in range(len(plate_labels)) if i not in used_plates]
    if unused:
        problems.append(Problem(
            "unused_plates", "plates",
            f"plates {unused} are declared but no step uses them",
            hint="every plate in a run is a physical plate someone has to fetch",
        ))

    order = _topo(after_lists)
    if order is None:
        # `_topo` returns the steps it *could* order, so the cycle is exactly
        # what is missing from it. Naming every step instead sent the caller
        # looking at steps that are fine.
        ordered = set(_topo_partial(after_lists))
        stuck = [planned[i].name for i in range(len(planned)) if i not in ordered]
        problems.append(Problem(
            "cycle", "steps", f"the step graph has a cycle involving {stuck}",
            hint="every step must be reachable from a step with no dependencies",
        ))
        return None, problems
    for i, deps in enumerate(after_lists):
        planned[i].after = deps

    # ---- what the fleet can do -------------------------------------------
    fleet = await db.fetch(
        "select id, kind, capabilities, quarantined, state from devices order by id"
    )
    kinds_for_cap: dict[str, list[str]] = {}
    devices_for_cap: dict[str, list[str]] = {}
    in_service_for_cap: dict[str, list[str]] = {}
    for d in fleet:
        for cap in d["capabilities"]:
            kinds_for_cap.setdefault(cap, [])
            if d["kind"] not in kinds_for_cap[cap]:
                kinds_for_cap[cap].append(d["kind"])
            devices_for_cap.setdefault(cap, []).append(d["id"])
            if not d["quarantined"]:
                in_service_for_cap.setdefault(cap, []).append(d["id"])

    for i, s in enumerate(planned):
        if s.capability not in kinds_for_cap:
            problems.append(Problem(
                "no_such_capability", f"steps[{i}].op",
                f"no instrument in this lab provides capability '{s.capability}' "
                f"(needed by operation '{s.op}')",
                hint="this fleet cannot run this protocol at all",
            ))
        elif s.capability not in in_service_for_cap:
            problems.append(Problem(
                "no_instrument_in_service", f"steps[{i}].op",
                f"every instrument providing '{s.capability}' is quarantined",
                hint="wait for capacity, or return an instrument to service",
            ))

    # ---- authorization ----------------------------------------------------
    blocked = await tokens.blocked_reason(req.token_id)
    if blocked:
        problems.append(Problem(
            "token_revoked" if "revok" in blocked else "token_limit", "token_id", blocked,
            hint="ask the token's parent to issue a new one",
        ))
        return None, problems

    fleet_kinds = sorted({d["kind"] for d in fleet})
    permitted, refusals = await tokens.allowed_kinds_for(req.token_id, fleet_kinds)
    tok = await db.fetchrow("select * from tokens where id=$1", req.token_id)

    used_kinds: set[str] = set()
    for i, s in enumerate(planned):
        candidates = kinds_for_cap.get(s.capability, [])
        feasible = [k for k in candidates if k in permitted]
        s.candidate_kinds = feasible
        s.candidate_devices = [
            d["id"] for d in fleet
            if d["kind"] in feasible and s.capability in d["capabilities"] and not d["quarantined"]
        ]
        if candidates and not feasible:
            # Which refusal this actually was. A spent budget and a full
            # concurrency cap deny the same probe, and reporting both as "your
            # token forbids that instrument" sent the caller after a wider
            # token that would not have helped.
            on_token = [k for k in candidates if k in (tok["allowed_kinds"] or [])]
            if on_token:
                why = "; ".join(sorted({refusals[k] for k in on_token if k in refusals}))
                problems.append(Problem(
                    "token_limit", f"steps[{i}].op",
                    f"token '{tok['label']}' allows {sorted(candidates)} for "
                    f"operation '{s.op}', but cannot use it right now: {why}",
                    hint="wait for the token's running work to finish, or ask its "
                         "parent for more budget",
                ))
            else:
                problems.append(Problem(
                    "token_forbids_kind", f"steps[{i}].op",
                    f"token '{tok['label']}' allows {sorted(tok['allowed_kinds'])}; "
                    f"operation '{s.op}' needs capability '{s.capability}', provided only by "
                    f"{sorted(candidates)}",
                    hint="ask for a token that allows " + ", ".join(sorted(candidates)),
                ))
        used_kinds.update(feasible)

    if problems:
        return None, problems

    total_credits = sum(s.credit_cost for s in planned)
    total_wall = sum(s.duration_s for s in planned)
    depths = _layer_depths(after_lists, order)
    layer_count: dict[int, int] = {}
    for d in depths:
        layer_count[d] = layer_count.get(d, 0) + 1
    width = max(layer_count.values()) if layer_count else 1

    # A run wider than the concurrency cap is legal: the scheduler serialises
    # it and the ledger enforces the cap at dispatch. Asking the caveat about
    # the full `width` refused those runs outright, which made the warning
    # below unreachable and false.
    cap = tok["max_concurrent"] if tok else width
    authz = await tokens.authorize(
        req.token_id, device_kinds=sorted(used_kinds), concurrent=min(width, cap),
        wallclock_s=total_wall, credits=total_credits,
    )
    if not authz.allowed:
        problems.append(Problem("token_limit", "token_id", authz.reason,
                                hint="ask the token's parent for a wider one"))
        return None, problems

    # Budget is a ledger, so the static caveat above cannot see it.
    headroom = await _lineage_headroom(req.token_id)
    if headroom is not None and total_credits > headroom:
        problems.append(Problem(
            "token_limit", "token_id",
            f"run costs {total_credits} credits but only {headroom} remain in this "
            f"token's lineage budget",
            hint="wait for credits to be released, or ask for a larger budget",
        ))
        return None, problems

    # ---- warnings (never fatal) ------------------------------------------
    warnings.extend(_ordering_warnings(planned, after_lists, order))
    if width > cap:
        warnings.append(Problem(
            "will_not_parallelise", "steps", severity="warning",
            message=f"this run has {width} steps that could run at once but the token "
                    f"allows {cap} concurrent reservations; it will admit and then "
                    f"serialise",
            hint="not an error; expect a longer wall-clock time",
        ))
    for i, s in enumerate(planned):
        if len(s.candidate_devices) == 1:
            warnings.append(Problem(
                "single_point_of_failure", f"steps[{i}].op", severity="warning",
                message=f"only one instrument ({s.candidate_devices[0]}) can run "
                        f"'{s.op}'; if it faults there is nowhere to fail over to",
                hint=None,
            ))

    # No `estimated_finish_s` here. It used to exist, computed as the critical
    # path plus a queue-depth fudge with no queueing model behind it, and it
    # was surfaced to agents as though it were a projection. A number an agent
    # might plan against has to be derived from something; this one was not.
    crit = _critical_path(after_lists, order, [s.duration_s for s in planned])

    return Plan(
        name=req.name, token_id=req.token_id, priority=req.priority,
        plates=plate_labels, steps=planned, allowed_kinds=sorted(used_kinds),
        total_credits=total_credits, total_wallclock_s=total_wall,
        concurrency=width, critical_path_s=crit,
        warnings=warnings,
        protocol_name=proto.name if proto else None,
        protocol_version=proto.version if proto else None,
        protocol_digest=proto.digest if proto else None,
        params=resolved_params, client_run_id=req.client_run_id,
        project_id=req.project_id or (tok["project_id"] if tok else None),
    ), []


def _ordering_warnings(planned, after_lists, order) -> list[Problem]:
    """Two steps on the same plate with no path between them will serialise on
    the reservation index rather than failing. The invariant already stops the
    bug; this tells the author at authoring time instead of at runtime."""
    reach: list[set[int]] = [set() for _ in planned]
    for i in order:
        for d in after_lists[i]:
            reach[i] |= reach[d] | {d}
    out = []
    for i in range(len(planned)):
        for j in range(i + 1, len(planned)):
            if planned[i].sample != planned[j].sample:
                continue
            if j in reach[i] or i in reach[j]:
                continue
            out.append(Problem(
                "unordered_shared_plate", f"steps[{j}]", severity="warning",
                message=f"'{planned[i].name}' and '{planned[j].name}' both use plate "
                        f"{planned[i].sample + 1} with no declared ordering; they will "
                        f"serialise in an arbitrary order",
                hint=f"add `after: [{i}]` if the order matters",
            ))
    return out


async def _lineage_headroom(token_id: str) -> int | None:
    rows = await db.fetch(
        """
        with recursive up as (
            select id, parent_id, budget_credits, credits_spent from tokens where id = $1
            union all
            select t.id, t.parent_id, t.budget_credits, t.credits_spent
              from tokens t join up on up.parent_id = t.id
        )
        select budget_credits - credits_spent as headroom from up
        """,
        token_id,
    )
    return min((r["headroom"] for r in rows), default=None)


# ----------------------------------------------------------------- commit ---

async def commit(p: Plan) -> tuple[dict, bool]:
    """Persist a plan. Returns (run, replayed).

    An agent that times out mid-POST and retries must not start a second
    physical experiment, so `(token_id, client_run_id)` is unique and the retry
    returns the original run.
    """
    if p.client_run_id:
        existing = await db.fetchval(
            "select id from runs where token_id=$1 and client_run_id=$2",
            p.token_id, p.client_run_id,
        )
        if existing:
            return await get_run(existing), True

    rid = f"run-{uuid.uuid4().hex[:8]}"
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            sample_ids = []
            for label in p.plates:
                sid = f"plate-{uuid.uuid4().hex[:6]}"
                await conn.execute(
                    "insert into samples(id, label, state, location_kind)"
                    " values ($1,$2,'parked','storage')",
                    sid, label,
                )
                sample_ids.append(sid)

            await conn.execute(
                """
                insert into runs(id, name, priority, state, token_id, allowed_kinds,
                                 protocol_name, protocol_version, protocol_digest,
                                 params, client_run_id, project_id)
                values ($1,$2,$3,'pending',$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                rid, p.name, p.priority, p.token_id, p.allowed_kinds,
                p.protocol_name, p.protocol_version, p.protocol_digest,
                p.params, p.client_run_id, p.project_id,
            )

            step_ids = [f"{rid}-s{i}" for i in range(len(p.steps))]
            for i, s in enumerate(p.steps):
                await conn.execute(
                    """
                    insert into steps(id, run_id, idx, name, op, capability, params,
                                      duration_s, credit_cost, max_attempts, qc,
                                      sample_id, state)
                    values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    """,
                    step_ids[i], rid, i, s.name, s.op, s.capability, s.params,
                    s.duration_s, s.credit_cost, s.max_attempts, s.qc,
                    sample_ids[s.sample], "ready" if not s.after else "pending",
                )
            for i, s in enumerate(p.steps):
                for dep in s.after:
                    await conn.execute(
                        "insert into step_deps(step_id, depends_on) values ($1,$2)",
                        step_ids[i], step_ids[dep],
                    )

            await audit.log(
                conn, "api", "run.admitted", run_id=rid, token_id=p.token_id,
                steps=len(p.steps), priority=p.priority, credits=p.total_credits,
                wallclock_s=p.total_wallclock_s, concurrency=p.concurrency,
                protocol=f"{p.protocol_name}@{p.protocol_version}" if p.protocol_name else None,
                protocol_digest=p.protocol_digest,
                warnings=[w.code for w in p.warnings],
            )
    return await get_run(rid), False


async def submit(req: RunRequest) -> tuple[dict, bool]:
    p, problems = await plan(req)
    if problems or p is None:
        raise AdmissionError(problems)
    return await commit(p)


# ------------------------------------------------------------------- read ---

async def get_run(run_id: str) -> dict[str, Any]:
    run = await db.fetchrow("select * from runs where id=$1", run_id)
    if run is None:
        raise LookupError(f"no such run {run_id}")
    steps = await db.fetch("select * from steps where run_id=$1 order by idx", run_id)
    deps = await db.fetch(
        "select d.* from step_deps d join steps s on s.id=d.step_id where s.run_id=$1", run_id
    )
    dep_map: dict[str, list[str]] = {}
    for d in deps:
        dep_map.setdefault(d["step_id"], []).append(d["depends_on"])

    samples = await db.fetch(
        "select distinct sm.* from samples sm join steps st on st.sample_id = sm.id"
        " where st.run_id=$1", run_id,
    )
    results = await db.fetch("select * from results where run_id=$1", run_id)

    return {
        **dict(run),
        "steps": [{**dict(s), "depends_on": dep_map.get(s["id"], [])} for s in steps],
        # Never let a client infer where a plate is from step status.
        "physical_state": [
            {"sample_id": s["id"], "label": s["label"], "state": s["state"],
             "location_kind": s["location_kind"], "location_device_id": s["location_device_id"],
             "transit_to": s["transit_to"], "hold_deadline": s["hold_deadline"]}
            for s in samples
        ],
        "results": [dict(r) for r in results],
    }
