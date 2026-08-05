"""The human review queue.

Holding policy is declared per fault kind in `faults.py` and applied here, so
the rules are readable in one place instead of inferred from five handlers.

Resolving is an authenticated act: destroying a plate is not the same
permission as freeing a gripper, and neither is one the scheduler holds. Each
option names the authority it needs and the audit records the token id.

A fault on a shared instrument implicates every plate that was on it, so cohort
faults compute their affected set at open time and one resolution covers all of
it. Clicking through forty identical questions is how the fortieth gets
answered wrong.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import asyncpg

from . import audit, db
from .auth import tokens
from .faults import HUMAN_FAULTS, FaultKind, option as fault_option


class InterventionError(Exception):
    """Bad request: unknown option, unknown intervention. -> 400"""


class AlreadyResolved(Exception):
    """Already answered. -> 409, or 200 for the same answer: an agent
    retrying a timed-out resolve must not land in a retry loop."""

    def __init__(self, intervention: dict):
        self.intervention = intervention
        super().__init__(
            f"intervention {intervention['id']} was already resolved as "
            f"'{intervention['resolution']}' by {intervention['resolved_by']}"
        )


class StaleVersion(Exception):
    """The screen the decision was made against is out of date. -> 409"""

    def __init__(self, expected: int, actual: int):
        self.expected, self.actual = expected, actual
        super().__init__(
            f"intervention has moved on (you saw version {expected}, it is now {actual}); "
            f"re-read it before deciding"
        )


class Forbidden(Exception):
    """The presented token may not take this option. -> 403"""


def _new_id() -> str:
    return f"int-{uuid.uuid4().hex[:8]}"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ------------------------------------------------------------------- open ---

async def open_intervention(
    conn: asyncpg.Connection,
    *,
    kind: str,
    run_id: str,
    step_id: str | None,
    device_id: str | None,
    sample_id: str | None,
    detail: dict[str, Any] | None = None,
    actor: str = "scheduler",
    extra_samples: Sequence[str] = (),
    extra_runs: Sequence[str] = (),
) -> str:
    spec = HUMAN_FAULTS[kind]
    iid = _new_id()
    detail = dict(detail or {})

    # ---- blast radius ----------------------------------------------------
    samples = {sample_id} if sample_id else set()
    runs = {run_id}
    if spec.cohort_scope and device_id:
        cohort = await conn.fetch(
            """
            select distinct r.sample_id, r.run_id from reservations r
             where r.device_id = $1
               and r.sample_released_at is null
               and r.acquired_at > now() - interval '10 minutes'
            """,
            device_id,
        )
        samples |= {c["sample_id"] for c in cohort}
        runs |= {c["run_id"] for c in cohort}
    samples |= set(extra_samples)
    runs |= set(extra_runs)

    detail.setdefault("could_not_observe", spec.could_not_observe)
    if device_id:
        detail.setdefault("corroboration", await _corroborate(conn, device_id, kind))

    group_key = f"{device_id or run_id}:{kind}"
    if kind in (FaultKind.CALIBRATION_DRIFT, FaultKind.RESULTS_SUSPECT) and device_id:
        # The epoch under question, not the one the instrument is on now:
        # grouping by the latter files the question under the one epoch it is
        # not about.
        marked = detail.get("epoch")
        epoch = marked if marked is not None else await current_epoch(conn, device_id)
        group_key = f"{device_id}:{kind}:{epoch}"

    await conn.execute(
        """
        insert into interventions(id, run_id, step_id, device_id, sample_id, kind,
                                  message, detail, options, holds, state,
                                  required_authority, agent_resolvable, expires_at,
                                  escalation_policy, group_key, affected_sample_ids,
                                  affected_run_ids)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open',$11,$12,$13,$14,$15,$16,$17)
        """,
        iid, run_id, step_id, device_id, sample_id, kind, spec.message, detail,
        [{"key": o.key, "label": o.label, "consequence": o.consequence,
          "authority": o.authority, "reversible": o.reversible,
          "requires_reason": o.requires_reason,
          "agent_resolvable": o.agent_resolvable} for o in spec.options],
        {"device": spec.hold_device, "sample": spec.hold_sample,
         "rationale": spec.hold_rationale},
        min((o.authority for o in spec.options), key=lambda a:
            {"operator": 0, "engineer": 1, "sample_owner": 2}.get(a, 9)),
        any(o.agent_resolvable for o in spec.options),
        _now() + timedelta(seconds=spec.sla_seconds),
        spec.escalation_policy, group_key, sorted(samples), sorted(runs),
    )

    if step_id:
        await conn.execute(
            "update steps set state='blocked_on_human', error=$2 where id=$1"
            " and state not in ('done','failed','cancelled')",
            step_id, spec.title,
        )
    for rid in runs:
        await conn.execute(
            "update runs set state='blocked_on_intervention', updated_at=now()"
            " where id=$1 and state in ('pending','running')",
            rid,
        )

    if step_id:
        if not spec.hold_device:
            await release_device(conn, step_id, reason=f"{kind}: instrument not implicated")
        if not spec.hold_sample:
            await release_sample(conn, step_id, reason=f"{kind}: plate not implicated")

    if device_id and spec.hold_device:
        await conn.execute("update devices set state='faulted', note=$2 where id=$1",
                           device_id, spec.title)

    # ---- physical consequences at open time ------------------------------
    if kind == FaultKind.BATCH_DESTROYED:
        for sid in samples:
            await destroy_sample(conn, sid)
        if device_id:
            await conn.execute(
                "update devices set state='idle', note=null where id=$1 and state<>'offline'",
                device_id)
    elif kind in (FaultKind.UNEXPECTED_READING, FaultKind.CALIBRATION_DRIFT) and sample_id:
        await park_sample(conn, sample_id)
        if device_id:
            await conn.execute(
                "update devices set state='idle', note=null where id=$1 and state<>'offline'",
                device_id)
        if kind == FaultKind.CALIBRATION_DRIFT and device_id:
            await conn.execute("update devices set suspect=true where id=$1", device_id)

    # A plate held by an open question is a plate whose clock is running. The
    # clock is a fact software owns; what it means for the assay is not.
    if spec.hold_sample:
        for sid in samples:
            await conn.execute(
                "update samples set hold_deadline = now() + interval '180 seconds'"
                " where id=$1 and state <> 'destroyed'",
                sid,
            )

    await audit.log(
        conn, actor, "intervention.opened",
        run_id=run_id, step_id=step_id, device_id=device_id, sample_id=sample_id,
        intervention_id=iid, kind=kind,
        holds={"device": spec.hold_device, "sample": spec.hold_sample},
        affected_samples=len(samples), affected_runs=len(runs), group_key=group_key,
    )
    return iid


async def _corroborate(conn, device_id: str, kind: str) -> dict:
    """Context that turns a guess into an inference: how often this instrument
    has done this lately, and what its controls have been doing."""
    recent = await conn.fetchval(
        "select count(*) from interventions where device_id=$1 and kind=$2"
        " and created_at > now() - interval '24 hours'",
        device_id, kind,
    )
    controls = await conn.fetch(
        "select control_value from results where device_id=$1 and control_value is not null"
        " order by created_at desc limit 12",
        device_id,
    )
    values = [r["control_value"] for r in controls]
    prior = await conn.fetch(
        "select resolution, count(*) n from interventions"
        " where device_id=$1 and kind=$2 and state='resolved' group by resolution",
        device_id, kind,
    )
    return {
        "same_fault_on_this_device_24h": recent,
        "recent_control_values": values,
        "control_median": _median(values),
        # Five identical drifts resolved the same way makes the sixth
        # automatic, which is worth putting in front of the person.
        "previous_resolutions": {p["resolution"]: p["n"] for p in prior},
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ------------------------------------------------- calibration epochs (E) ---

async def current_epoch(conn, device_id: str) -> int:
    row = await conn.fetchrow(
        "select epoch from device_calibration_epochs where device_id=$1 and ended_at is null"
        " order by epoch desc limit 1",
        device_id,
    )
    if row:
        return row["epoch"]
    await conn.execute(
        "insert into device_calibration_epochs(device_id, epoch) values ($1, 1)"
        " on conflict do nothing",
        device_id,
    )
    return 1


async def roll_epoch(conn, device_id: str, verdict: str, note: str) -> int:
    """Close the current epoch with a verdict and open the next.

    A recorded verdict is never softened: returning a drift-quarantined
    instrument to service also closes an epoch, and used to stamp the same row
    `good`, erasing the only column that says why its results are in question.
    """
    cur = await current_epoch(conn, device_id)
    await conn.execute(
        "update device_calibration_epochs set ended_at=now(),"
        " verdict = case when verdict = 'suspect' then verdict else $3 end,"
        " note = case when verdict = 'suspect' then note else $4 end"
        " where device_id=$1 and epoch=$2",
        device_id, cur, verdict, note,
    )
    await conn.execute(
        "insert into device_calibration_epochs(device_id, epoch) values ($1,$2)"
        " on conflict do nothing",
        device_id, cur + 1,
    )
    return cur


async def other_runs_on_device(device_id: str | None, run_id: str, ex=db) -> int:
    """How many other runs this instrument's fate reaches.

    Counting the runs currently holding the device gave a number that was
    structurally 0 or 1, since a partial unique index allows one holder. Taking
    an instrument out also makes everything queued that it could have run wait
    for the rest of its kind, which is the set `unstick_starved` calls
    `capable`.
    """
    if not device_id:
        return 0
    return await ex.fetchval(
        """
        select count(distinct s.run_id) from steps s
          join runs r on r.id = s.run_id
          join devices d on d.id = $1
         where s.run_id <> $2
           and s.state not in ('done','failed','cancelled')
           and s.capability = any(d.capabilities)
           and d.kind = any(r.allowed_kinds)
        """,
        device_id, run_id) or 0


async def results_held_elsewhere(device_id: str, epoch: int, ex=db) -> int:
    """The complement of what `mark_epoch_suspect` may take, which is how the
    caller tells an epoch that produced nothing from one whose every number is
    already spoken for. Here rather than at the API edge so the predicate has
    one copy."""
    return await ex.fetchval(
        "select count(*) from results where device_id=$1 and calibration_epoch=$2"
        "   and state <> 'invalidated'"
        "   and held_by in (select id from interventions where state='open')",
        device_id, epoch) or 0


async def condemn_epoch(conn, device_id: str, epoch: int, note: str) -> None:
    """Condemn the epoch named, not the one running.

    Rolling is only right when the named epoch is still current. Rolling a past
    epoch condemns whatever is current instead: a verdict on numbers nobody
    questioned, and no mark on the epoch actually under suspicion. Both callers
    reach backwards by construction, so the branch lives here.
    """
    if epoch == await current_epoch(conn, device_id):
        await roll_epoch(conn, device_id, "suspect", note)
    else:
        await conn.execute(
            "update device_calibration_epochs set verdict='suspect', note=$3,"
            " ended_at = coalesce(ended_at, now())"
            " where device_id=$1 and epoch=$2",
            device_id, epoch, note,
        )


async def mark_epoch_suspect(device_id: str, epoch: int, actor: str,
                             note: str) -> tuple[str | None, int]:
    """Call into question every result the instrument produced during an
    epoch, including released ones, as one question rather than one per result.

    Returns the intervention id, None when the epoch produced nothing, and how
    many results were pulled back. Marking the same epoch twice is a no-op:
    the question is already open over the same results.
    """
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Close it, do not merely label it. Only stamping the verdict left
            # `ended_at` null, so the suspect epoch stayed *current*: results
            # produced hours later were stamped with an epoch already declared
            # untrustworthy and delivered clean. The sibling path
            # (`quarantine_device`) has always rolled it.
            await condemn_epoch(conn, device_id, epoch, note)
            already = await conn.fetchrow(
                "select id from interventions where device_id=$1 and kind=$2"
                " and state='open' and (detail->>'epoch')::int = $3",
                device_id, FaultKind.RESULTS_SUSPECT, epoch)
            if already:
                return already["id"], 0

            # Same filter as the `update` below. Listing a row this question
            # will refuse to take meant resolving it disposed of a number
            # another open question was holding. The guard has to cover the
            # list and the rows alike.
            affected = await conn.fetch(
                "select id, run_id, sample_id, step_id from results"
                " where device_id=$1 and calibration_epoch=$2 and state <> 'invalidated'"
                "   and (held_by is null"
                "        or held_by not in (select id from interventions where state='open'))",
                device_id, epoch,
            )
            if not affected:
                await audit.log(conn, actor, "calibration.epoch_suspect",
                                device_id=device_id, epoch=epoch, affected_results=0,
                                note=note)
                return None, 0

            iid = await open_intervention(
                conn, kind=FaultKind.RESULTS_SUSPECT,
                run_id=affected[0]["run_id"], step_id=None, device_id=device_id,
                sample_id=None, actor=actor,
                detail={"epoch": epoch, "note": note,
                        "affected_result_ids": [a["id"] for a in affected]},
                extra_samples=[a["sample_id"] for a in affected],
                extra_runs=[a["run_id"] for a in affected],
            )
            # `held_by` this question, so resolving it is what lets them go.
            # Never results another open question holds: reassigning would
            # leave that one open over rows it no longer has.
            await conn.execute(
                """
                update results set state='held', qc_verdict='warn', qc_note=$3,
                       held_by=$4
                 where device_id=$1 and calibration_epoch=$2
                   and state <> 'invalidated'
                   and (held_by is null
                        or held_by not in (select id from interventions where state='open'))
                """,
                device_id, epoch, f"calibration epoch {epoch} marked suspect: {note}", iid,
            )
            await audit.log(conn, actor, "calibration.epoch_suspect",
                            device_id=device_id, epoch=epoch,
                            affected_results=len(affected), intervention_id=iid, note=note)
            return iid, len(affected)


# ------------------------------------------------------- resource helpers ---

async def refund_step(conn, step_id: str, reason: str) -> int:
    """Give back what a step paid, at most once.

    Inferring this from whether the reservation still held the instrument or
    the plate was a different fact: a fault that holds neither releases both at
    open time, so resolving it refunded nothing and the customer paid twice.

    A step can hold several reservations, since re-running after a suspect
    epoch charges again, so pay every one that is neither consumed nor already
    refunded. Returns the credits given back.
    """
    rows = await conn.fetch(
        "update reservations set refunded_at = now() where step_id=$1"
        " and refunded_at is null and consumed_at is null"
        " returning token_id, credits",
        step_id)
    total = 0
    for row in rows:
        if not row["credits"]:
            continue
        await tokens.refund(conn, row["token_id"], row["credits"])
        total += row["credits"]
    if total:
        await audit.log(conn, "scheduler", "credits.refunded", step_id=step_id,
                        token_id=rows[0]["token_id"], credits=total, reason=reason)
    return total


async def release_device(conn, step_id: str, reason: str) -> str | None:
    row = await conn.fetchrow(
        """
        update reservations set device_released_at = now(),
               release_reason = coalesce(release_reason, $2)
        where step_id = $1 and device_released_at is null
        returning device_id
        """,
        step_id, reason,
    )
    if row is None:
        return None
    await conn.execute(
        "update devices set state = case when quarantined then 'offline' else 'idle' end,"
        " note = null where id = $1 and state not in ('offline','faulted')",
        row["device_id"],
    )
    return row["device_id"]


async def release_sample(conn, step_id: str, reason: str) -> str | None:
    row = await conn.fetchrow(
        """
        update reservations set sample_released_at = now(),
               release_reason = coalesce(release_reason, $2)
        where step_id = $1 and sample_released_at is null
        returning sample_id
        """,
        step_id, reason,
    )
    if row:
        await conn.execute(
            "update samples set hold_deadline=null where id=$1", row["sample_id"])
        return row["sample_id"]
    return None


async def release_all(conn, step_id: str, reason: str) -> None:
    await release_device(conn, step_id, reason)
    await release_sample(conn, step_id, reason)


async def mark_consumed(conn, step_id: str) -> None:
    """Recorded rather than inferred: with re-runs a step can hold several
    reservations, and the custody columns cannot say afterwards which one
    actually ran."""
    await conn.execute(
        """
        update reservations set consumed_at = now()
         where id = (
             select id from reservations
              where step_id = $1 and consumed_at is null and refunded_at is null
              order by acquired_at desc limit 1
         )
        """,
        step_id)


async def park_sample(conn, sample_id: str, transit_s: int = 3) -> bool:
    """Send a plate back to storage unless something still has it.

    Parking is physical: a mover picks the plate up and carries it out. Callers
    that act on the step in front of them release first, so the guard never
    fires for them. Callers that act on a set do hit it, and both such paths
    walked a plate out of a running instrument before it existed. The state was
    coherent afterwards either time, so only a live reservation catches it.

    Returns whether the plate really moved, for callers that report what they
    did.
    """
    cur = await conn.fetchrow("select * from samples where id=$1 for update", sample_id)
    if cur is None or cur["state"] == "destroyed":
        return False
    held = await conn.fetchval(
        "select step_id from reservations where sample_id=$1"
        "   and sample_released_at is null", sample_id)
    if held:
        await audit.log(conn, "scheduler", "sample.park_refused", sample_id=sample_id,
                        step_id=held, reason="a live step still holds this plate")
        return False
    if cur["location_kind"] == "storage":
        await conn.execute(
            "update samples set state = case when state='destroyed' then 'destroyed'"
            " else 'parked' end where id=$1", sample_id)
        return True
    if cur["location_kind"] == "transit":
        # Already on the mover, so "parked" stays a true account of where this
        # plate is going.
        return True
    await conn.execute(
        """
        update samples
           set state='in_transit', location_kind='transit', location_device_id=null,
               transit_from=$2, transit_to=null, transit_started_at=now(),
               transit_eta = now() + make_interval(secs => $3)
         where id=$1
        """,
        sample_id, cur["location_device_id"], float(transit_s),
    )
    return True


async def destroy_sample(conn, sample_id: str) -> None:
    await conn.execute(
        """
        update samples set state='destroyed', location_kind='storage',
               location_device_id=null, transit_from=null, transit_to=null,
               transit_started_at=null, transit_eta=null, hold_deadline=null
         where id=$1
        """,
        sample_id,
    )


async def quarantine_device(conn, device_id: str, actor: str, note: str) -> None:
    await conn.execute(
        "update devices set quarantined=true, state='offline', note=$2 where id=$1",
        device_id, note,
    )
    rows = await conn.fetch(
        """
        select r.step_id, s.state as step_state from reservations r
          join steps s on s.id = r.step_id
         where r.device_id = $1 and r.device_released_at is null
        """,
        device_id,
    )
    drained = []
    for r in rows:
        if r["step_state"] in ("done", "failed", "cancelled"):
            continue
        await requeue_step(conn, r["step_id"], reason="device quarantined",
                           exclude_device=device_id, actor=actor)
        drained.append(r["step_id"])
    # What was requeued, not everything the query returned: a finished step is
    # not a step this drained.
    await audit.log(conn, actor, "device.quarantined", device_id=device_id,
                    note=note, drained_steps=drained)


async def requeue_step(conn, step_id: str, *, reason: str, exclude_device: str | None = None,
                       actor: str = "scheduler", transit_s: int = 3,
                       allow_done: bool = False, refund: bool = True) -> bool:
    """Put a step back on the queue and give back what it paid.

    The refund belongs here because it follows from the same fact the requeue
    does: the step did not consume the time it was charged for and will be
    charged again. `_take_offline` refunded while seven human-ordered requeues
    did not, so quarantining an instrument billed the customer twice for work
    that never happened, against a real enforced cap.

    `allow_done` is for re-running after an epoch is marked suspect, where the
    step is `done` by construction and re-running it is the point. Everything
    else must not silently put a plate back into a machine. `refund=False` goes
    with it: that instrument time really was spent, the number is just no
    longer trusted.

    Returns whether the step was actually requeued.
    """
    step = await conn.fetchrow("select * from steps where id=$1 for update", step_id)
    if step is None or step["state"] == "cancelled":
        return False
    if step["state"] == "done" and not allow_done:
        return False

    if refund:
        await refund_step(conn, step_id, f"requeued: {reason}")
    await release_all(conn, step_id, reason)
    sample = await conn.fetchrow("select * from samples where id=$1", step["sample_id"])
    if sample and sample["state"] != "destroyed":
        await park_sample(conn, step["sample_id"], transit_s)

    tried = list(step["tried_devices"])
    if exclude_device and exclude_device not in tried:
        tried.append(exclude_device)

    await conn.execute(
        """
        update steps set state='ready', device_id=null, job_handle=null,
               started_at=null, finished_at=null, result=null,
               deadline=null, attempt=attempt+1,
               comms_fail=0, tried_devices=$2, error=$3,
               retry_after = now() + make_interval(secs => $4)
         where id=$1
        """,
        step_id, tried, reason, float(min(8, 1.5 ** step["attempt"])),
    )
    await audit.log(conn, actor, "step.requeued", step_id=step_id, run_id=step["run_id"],
                    device_id=exclude_device, sample_id=step["sample_id"],
                    reason=reason, attempt=step["attempt"] + 1)
    return True


async def fail_run(conn, run_id: str, reason: str, actor: str = "scheduler") -> None:
    steps = await conn.fetch(
        "select id from steps where run_id=$1 and state not in ('done','failed','cancelled')",
        run_id)
    for s in steps:
        await refund_step(conn, s["id"], f"run failed: {reason}")
        await release_all(conn, s["id"], f"run failed: {reason}")
    await conn.execute(
        "update steps set state='cancelled', error=$2 where run_id=$1"
        " and state not in ('done','failed','cancelled')",
        run_id, reason)
    await conn.execute(
        "update runs set state='failed', updated_at=now(), note=$2 where id=$1", run_id, reason)
    await audit.log(conn, actor, "run.failed", run_id=run_id, reason=reason)


# ----------------------------------------------------------- consequences ---

async def consequences(intervention_id: str, option_key: str, *, conn=None) -> dict:
    """What this option would do, as counts rather than prose.

    Three buttons that read as equally reversible when one destroys a plate is
    how a wrong decision gets made at 2am, so the numbers come from current
    state rather than from the option text.

    `conn` is for callers mid-transaction: a batch's current state includes
    what it has done so far, which a pooled read cannot see.
    """
    ex = conn if conn is not None else db
    iv = await ex.fetchrow("select * from interventions where id=$1", intervention_id)
    if iv is None:
        raise InterventionError(f"no such intervention {intervention_id}")
    opt = fault_option(iv["kind"], option_key)
    if opt is None:
        raise InterventionError(
            f"'{option_key}' is not an option for {iv['kind']}; expected one of "
            f"{[o['key'] for o in iv['options']]}")

    runs = list(iv["affected_run_ids"]) or [iv["run_id"]]

    aborts = option_key in ("discard_abort", "abort_run", "plate_lost")
    destroyed = await _destroyed_sample_ids(iv, option_key, ex)
    # Charges neither consumed by a finished attempt nor already refunded;
    # anything else is money the lab really spent. Only the scope varies by
    # option, and measuring it with `refund_step`'s own predicate is what stops
    # a list of option names reporting a refund that never happens.
    credits_held = await ex.fetchval(
        "select coalesce(sum(credits),0) from reservations"
        " where run_id = any($1::text[])"
        "   and refunded_at is null and consumed_at is null",
        runs) or 0
    requeued, gross, step_refund = await _requeue_cost(iv, option_key, ex)
    if option_key in _REFUNDS_WHOLE_RUN:
        released = credits_held
    else:
        released = step_refund
    # Paid back and charged again nets to nothing. A re-run without a refund,
    # or one whose charge was already consumed, is paid for twice.
    recharged = max(gross - step_refund, 0)

    # Only quarantine reaches other people's work. Releasing a number with a
    # caveat reaches none of it, and the panel printed the same red chip under
    # both.
    other_runs = (await other_runs_on_device(iv["device_id"], iv["run_id"], ex)
                  if option_key == "quarantine_device" else 0)

    return {
        "option": option_key,
        "reversible": opt.reversible,
        "requires_reason": opt.requires_reason,
        "authority": opt.authority,
        "agent_resolvable": opt.agent_resolvable,
        "runs_aborted": len(runs) if aborts else 0,
        "plates_destroyed": len(destroyed),
        "steps_requeued": requeued,
        "credits_released": released,
        "credits_spent_again": recharged,
        "instruments_quarantined": 1 if option_key == "quarantine_device" else 0,
        # A count, never ids: an agent may learn that quarantining reaches
        # other work, not whose. The operator's named set is
        # `affected_run_ids`, which is the narrower "actually on the machine".
        "other_runs_affected": other_runs or 0,
    }


# The one requeue that does not pay back, because the instrument time behind
# the number really was spent and the option text says so.
_REQUEUE_WITHOUT_REFUND = frozenset({"requeue"})

# The options whose refund runs over the whole run rather than over the steps
# they put back: the aborts cancel every unfinished step through `fail_run`,
# and `reprep_restart` pays every step of the run before restarting it. Every
# other refunding option pays exactly what it requeues, through
# `requeue_step`'s default.
_REFUNDS_WHOLE_RUN = frozenset({"discard_abort", "abort_run", "plate_lost",
                                "reprep_restart"})


async def _requeue_cost(iv, option_key: str, ex=db) -> tuple[int, int, int]:
    """Steps this option puts back, what re-running them costs at list price,
    and how much comes back first.

    The count used to be `len(affected_run_ids)`, runs labelled as steps, and
    the cost the fault's own step price, which is zero for a question about an
    instrument rather than a step. The results-suspect re-run therefore
    advertised "0 credits again" while recharging the full amount.

    Scoped from `_apply`, option by option: a number read before an
    irreversible button has to come from what the code does.
    """
    ids = await _requeued_step_ids(iv, option_key, ex)
    if not ids:
        return 0, 0, 0
    gross = await ex.fetchval(
        "select coalesce(sum(credit_cost),0) from steps where id = any($1::text[])",
        ids) or 0
    if option_key in _REQUEUE_WITHOUT_REFUND:
        return len(ids), gross, 0
    # The predicate `refund_step` pays on, so estimate and ledger agree.
    refundable = await ex.fetchval(
        "select coalesce(sum(credits),0) from reservations"
        " where step_id = any($1::text[])"
        "   and refunded_at is null and consumed_at is null",
        ids) or 0
    return len(ids), gross, refundable


async def _requeued_step_ids(iv, option_key: str, ex=db) -> list[str]:
    if option_key in ("redo_step", "rerun_step", "freed_resume"):
        return [iv["step_id"]] if iv["step_id"] else []

    if option_key == "quarantine_device":
        # Drains the instrument: the fault's own step if it has one, plus
        # everything else the device still holds.
        rows = await ex.fetch(
            """
            select distinct s.id from steps s
              left join reservations r
                     on r.step_id = s.id and r.device_id = $1
                    and r.device_released_at is null
             where (r.step_id is not null or s.id = $2)
               and s.state not in ('done','failed','cancelled')
            """,
            iv["device_id"], iv["step_id"])
        return [r["id"] for r in rows]

    if option_key == "reprep_restart":
        # The whole run starts over on fresh plates; cancelled steps stay dead.
        rows = await ex.fetch(
            "select id from steps where run_id = any($1::text[]) and state <> 'cancelled'",
            list(iv["affected_run_ids"]) or [iv["run_id"]])
        return [r["id"] for r in rows]

    if option_key == "requeue":
        # Scoped to `held_by` exactly as `_apply` is: the id list is a
        # snapshot, and a number that has since left this question's custody is
        # not one this option re-runs.
        ids = (iv["detail"] or {}).get("affected_result_ids") or []
        rows = await ex.fetch(
            "select distinct step_id from results where id = any($1::text[])"
            "   and held_by = $2", list(ids), iv["id"])
        return [r["step_id"] for r in rows]

    return []


async def _destroyed_sample_ids(iv, option_key: str, ex=db) -> list[str]:
    """Plates this option would destroy that are not destroyed already.

    `len(affected_sample_ids)` was wrong in both directions on the one question
    where scope and cohort differ. `batch_destroyed` destroys its cohort when
    the question opens, leaving `abort_run` nothing to destroy, while
    `reprep_restart` ignores the cohort and re-preps every plate of the run.

    So the scope comes from `_apply` and the count from current state.
    """
    if option_key in ("discard_abort", "plate_lost"):
        scope = [iv["sample_id"]] if iv["sample_id"] else []
    elif option_key == "abort_run":
        scope = list(iv["affected_sample_ids"]) or (
            [iv["sample_id"]] if iv["sample_id"] else [])
    elif option_key == "reprep_restart":
        # `_reprep_and_restart` works off the run's steps, not the cohort.
        rows = await ex.fetch(
            "select distinct sample_id from steps"
            " where run_id = any($1::text[]) and sample_id is not null",
            list(iv["affected_run_ids"]) or [iv["run_id"]])
        scope = [r["sample_id"] for r in rows]
    else:
        return []

    rows = await ex.fetch(
        "select id from samples where id = any($1::text[]) and state <> 'destroyed'",
        scope)
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- resolve ---

async def check_authority(iv, option_key: str, token_id: str) -> str:
    """Two ways to pass: the token carries the authority the option demands,
    or the question is about budget or policy rather than physical judgement
    and an agent may answer it for its own run. The human gate is a capability
    boundary, not a wall."""
    opt = fault_option(iv["kind"], option_key)
    if opt is None:
        raise InterventionError(
            f"'{option_key}' is not an option for {iv['kind']}; expected one of "
            f"{[o['key'] for o in iv['options']]}")

    # `check all` needs a device-kind fact, so probe with this instrument's.
    kind = await db.fetchval("select kind from devices where id=$1", iv["device_id"]) \
        if iv["device_id"] else None
    kinds = [kind] if kind else await tokens_any_kind(token_id)

    res = await tokens.authorize(
        token_id, device_kinds=kinds, concurrent=1, wallclock_s=0, credits=0,
        authority=opt.authority,
    )
    if res.allowed:
        return f"authority:{opt.authority}"

    if opt.agent_resolvable:
        owner = await db.fetchval("select token_id from runs where id=$1", iv["run_id"])
        if owner:
            chain = {t["id"] for t in await tokens.lineage(owner)}
            if token_id in chain:
                agent_ok = await tokens.authorize(
                    token_id, device_kinds=kinds, concurrent=1, wallclock_s=0, credits=0)
                if agent_ok.allowed:
                    return "agent_self_service"
    raise Forbidden(res.reason)


async def tokens_any_kind(token_id: str) -> list[str]:
    row = await db.fetchrow("select allowed_kinds from tokens where id=$1", token_id)
    return [row["allowed_kinds"][0]] if row and row["allowed_kinds"] else []


async def resolve(
    intervention_id: str,
    option: str,
    *,
    token_id: str,
    reason: str | None = None,
    expected_version: int | None = None,
    transit_s: int = 3,
    batch_id: str | None = None,
) -> dict:
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _resolve_on(
                conn, intervention_id, option, token_id=token_id, reason=reason,
                expected_version=expected_version, transit_s=transit_s,
                batch_id=batch_id,
            )


async def _resolve_on(
    conn,
    intervention_id: str,
    option: str,
    *,
    token_id: str,
    reason: str | None = None,
    expected_version: int | None = None,
    transit_s: int = 3,
    batch_id: str | None = None,
) -> dict:
    """Split out so a batch runs every member in one transaction. Its reads go
    through `conn` too: a cohort resolution changes the state the next member's
    consequences are computed from, and a pooled read would miss it."""
    iv = await conn.fetchrow(
        "select * from interventions where id=$1 for update", intervention_id)
    if iv is None:
        raise InterventionError(f"no such intervention {intervention_id}")
    if iv["state"] == "resolved":
        raise AlreadyResolved(dict(iv))
    if expected_version is not None and expected_version != iv["version"]:
        raise StaleVersion(expected_version, iv["version"])

    opt = fault_option(iv["kind"], option)
    if opt is None:
        raise InterventionError(
            f"'{option}' is not an option for {iv['kind']}; expected one of "
            f"{[o['key'] for o in iv['options']]}")
    if opt.requires_reason and not (reason or "").strip():
        raise InterventionError(
            f"'{option}' requires a written reason")

    basis = await check_authority(iv, option, token_id)
    actor = f"token:{token_id}"
    counts = await consequences(intervention_id, option, conn=conn)

    await _apply(conn, iv, option, actor, transit_s)

    await conn.execute(
        "update interventions set state='resolved', resolution=$2,"
        " resolved_by=$3, resolved_by_token=$4, resolved_at=now(),"
        " resolution_reason=$5, batch_id=$6, version=version+1 where id=$1",
        intervention_id, option, basis, token_id, reason, batch_id,
    )
    await audit.log(
        conn, actor, "intervention.resolved",
        run_id=iv["run_id"], step_id=iv["step_id"], device_id=iv["device_id"],
        sample_id=iv["sample_id"], intervention_id=intervention_id,
        token_id=token_id, kind=iv["kind"], resolution=option, basis=basis,
        reason=reason, batch_id=batch_id, consequences=counts,
    )
    for rid in (list(iv["affected_run_ids"]) or [iv["run_id"]]):
        await _resume_run_if_clear(conn, rid, actor)
    return {
        "intervention_id": intervention_id, "resolution": option,
        "resolved_by_token": token_id, "basis": basis, "applied": counts,
    }


async def resolve_batch(group_key: str, option: str, *, token_id: str,
                        reason: str | None = None, transit_s: int = 3) -> dict:
    """One decision over a whole cohort, in one transaction.

    Looping `resolve()` opened a transaction per member, so a failure halfway
    left half the cohort resolved, from a UI whose pitch is that one answer
    covers all forty plates.

    An already-resolved member is skipped rather than fatal, and the branch is
    Python-level so the transaction stays usable.
    """
    batch_id = f"batch-{uuid.uuid4().hex[:8]}"
    applied, skipped = [], []

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = [r["id"] for r in await conn.fetch(
                "select id from interventions where group_key=$1 and state='open'"
                " order by created_at", group_key)]
            if not ids:
                raise InterventionError(f"no open interventions in group '{group_key}'")

            for iid in ids:
                try:
                    applied.append(await _resolve_on(
                        conn, iid, option, token_id=token_id, reason=reason,
                        transit_s=transit_s, batch_id=batch_id))
                except AlreadyResolved:
                    skipped.append({"intervention_id": iid, "why": "already resolved"})

    return {"batch_id": batch_id, "group_key": group_key, "option": option,
            "resolved": applied, "skipped": skipped}


async def preview_batch(group_key: str, option: str) -> dict:
    ids = [r["id"] for r in await db.fetch(
        "select id from interventions where group_key=$1 and state='open'", group_key)]
    return {
        "group_key": group_key, "option": option, "intervention_ids": ids,
        "per_intervention": [await consequences(i, option) for i in ids],
    }


async def acknowledge(intervention_id: str, who: str) -> dict:
    row = await db.fetchrow(
        "update interventions set acknowledged_by=$2, acknowledged_at=now(),"
        " version=version+1 where id=$1 and state='open' returning *",
        intervention_id, who)
    if row is None:
        raise InterventionError(f"no open intervention {intervention_id}")
    return dict(row)


async def _apply(conn, iv, option: str, actor: str, transit_s: int) -> None:
    kind, step_id = iv["kind"], iv["step_id"]
    run_id, device_id, sample_id = iv["run_id"], iv["device_id"], iv["sample_id"]
    all_runs = list(iv["affected_run_ids"]) or [run_id]
    all_samples = list(iv["affected_sample_ids"]) or ([sample_id] if sample_id else [])

    if kind == FaultKind.SAMPLE_INTEGRITY_UNKNOWN:
        if option == "redo_step":
            await _reset_device(conn, device_id, actor)
            await requeue_step(conn, step_id, reason="operator ordered re-run after "
                               "unknown transfer state", actor=actor, transit_s=transit_s)
        elif option == "accept_continue":
            await _reset_device(conn, device_id, actor)
            await _complete_step(
                conn, step_id, result={"accepted_by_operator": True},
                actor=actor, transit_s=transit_s,
                verdict_note=f"transfer accepted as-is by {actor}; the instrument "
                             "could not confirm the volume landed")
        elif option == "discard_abort":
            await _reset_device(conn, device_id, actor)
            await release_all(conn, step_id, "sample discarded")
            await destroy_sample(conn, sample_id)
            await fail_run(conn, run_id, "operator discarded sample", actor)

    elif kind == FaultKind.BATCH_DESTROYED:
        if option == "abort_run":
            for sid in all_samples:
                await destroy_sample(conn, sid)
            for rid in all_runs:
                await fail_run(conn, rid, "batch destroyed in incubator", actor)
        elif option == "reprep_restart":
            for rid in all_runs:
                await _reprep_and_restart(conn, rid, actor)

    elif kind == FaultKind.UNEXPECTED_READING:
        if option == "accept_reading":
            await _complete_step(
                conn, step_id, result=(iv["detail"] or {}).get("result"),
                actor=actor, transit_s=transit_s,
                extra={"accepted_out_of_range": True},
                verdict_note=f"out-of-range reading accepted as real by {actor}")
        elif option == "rerun_step":
            await requeue_step(conn, step_id, reason="re-read ordered",
                               exclude_device=device_id, actor=actor, transit_s=transit_s)
        elif option == "quarantine_device":
            await requeue_step(conn, step_id, reason="instrument quarantined after "
                               "out-of-range reading", exclude_device=device_id,
                               actor=actor, transit_s=transit_s)
            await quarantine_device(conn, device_id, actor, "out-of-range reading")

    elif kind == FaultKind.PLATE_STUCK:
        if option == "freed_resume":
            await _reset_device(conn, device_id, actor)
            await requeue_step(conn, step_id, reason="operator freed the plate",
                               actor=actor, transit_s=transit_s)
        elif option == "plate_lost":
            await _reset_device(conn, device_id, actor)
            await release_all(conn, step_id, "plate lost in gripper")
            await destroy_sample(conn, sample_id)
            await fail_run(conn, run_id, "plate lost in gripper", actor)

    elif kind == FaultKind.CALIBRATION_DRIFT:
        # Both options must dispose of the numbers this question holds.
        # Guessing at them by device and epoch left results `held` with nothing
        # open about them, three times in three different subsets. `held_by`
        # removes the guess.
        epoch = (iv["detail"] or {}).get("epoch")
        if epoch is None and device_id:
            epoch = await current_epoch(conn, device_id)

        if option == "quarantine_device":
            if step_id:
                await requeue_step(conn, step_id, reason="instrument quarantined for "
                                   "calibration drift", exclude_device=device_id,
                                   actor=actor, transit_s=transit_s)
            await quarantine_device(conn, device_id, actor, "calibration drift")
            # Raise the question here rather than trusting that something
            # else will: nothing did. And condemn the epoch computed above, not
            # the current one, or an instrument that rolled since the drift was
            # detected gets a clean epoch marked and the suspect one missed.
            await condemn_epoch(conn, device_id, epoch, "drift confirmed by operator")
            await _hand_over_to_results_suspect(
                conn, iv, device_id, epoch, actor,
                note="calibration epoch marked suspect after confirmed drift")
        elif option == "ignore_continue":
            await conn.execute("update devices set suspect=true where id=$1", device_id)
            if step_id:
                await _complete_step(
                    conn, step_id, result=(iv["detail"] or {}).get("result"),
                    actor=actor, transit_s=transit_s,
                    extra={"accepted_with_drift": True},
                    verdict_note=f"released with a caveat by {actor}: instrument "
                                 "kept in service while flagged suspect")
            released = await conn.fetch(
                """
                update results set state='released', released_at=now(),
                       qc_verdict='warn', held_by=null,
                       qc_note = coalesce(qc_note,'') ||
                                 ' [drift accepted by ' || $2 || '; instrument kept in ' ||
                                 'service flagged suspect]'
                 where held_by = $1 and state='held'
             returning id
                """,
                iv["id"], actor)
            if released:
                await audit.log(conn, actor, "results.released_with_caveat",
                                device_id=device_id, epoch=epoch,
                                result_ids=[r["id"] for r in released],
                                intervention_id=iv["id"])

    elif kind == FaultKind.RESULTS_SUSPECT:
        # The id list is this question's record of what it took, but a list is
        # a snapshot and `held_by` is the fact. Scoping to it stops any branch
        # reaching a row that has since become another question's.
        ids = (iv["detail"] or {}).get("affected_result_ids") or []
        if option == "accept_with_caveat":
            await conn.execute(
                "update results set state='released', released_at=now(), held_by=null,"
                " qc_note=coalesce(qc_note,'') || ' [released with suspect-calibration caveat]'"
                " where id = any($1::text[]) and held_by = $2", ids, iv["id"])
        elif option == "invalidate":
            await conn.execute(
                "update results set state='invalidated', held_by=null,"
                " invalidated_reason='calibration epoch marked suspect'"
                " where id = any($1::text[]) and held_by = $2", ids, iv["id"])
        elif option == "requeue":
            # `returning` rather than a second select, so the steps put back
            # are exactly the ones whose numbers this withdrew.
            steps = await conn.fetch(
                "update results set state='invalidated', held_by=null,"
                " invalidated_reason='re-run ordered after calibration drift'"
                " where id = any($1::text[]) and held_by = $2"
                " returning step_id, device_id", ids, iv["id"])
            for s in steps:
                await conn.execute(
                    "update runs set state='running', updated_at=now() where id ="
                    " (select run_id from steps where id=$1) and state='done'", s["step_id"])
                # `done` is what produced the numbers being withdrawn, hence
                # `allow_done`. No refund: that instrument time was really
                # spent, and the option text says it costs credits again.
                await requeue_step(conn, s["step_id"], reason="re-run after calibration drift",
                                   exclude_device=s["device_id"], actor=actor,
                                   transit_s=transit_s, allow_done=True, refund=False)
    else:
        raise InterventionError(f"no resolution handler for kind {kind}")


async def _hand_over_to_results_suspect(conn, iv, device_id: str | None, epoch,
                                        actor: str, note: str) -> str | None:
    """Move this question's held results onto a fresh results-suspect one.

    Quarantining answers "is it drifting" and not "what about the numbers it
    already produced", which is a different decision for a different person.
    Somebody has to raise it: closing a question and leaving its results held
    with nothing open about them is the failure this code keeps rediscovering.
    """
    if not device_id:
        return None
    held = await conn.fetch(
        "select id, run_id, sample_id from results where held_by=$1 and state='held'",
        iv["id"])
    also = await conn.fetch(
        "select id, run_id, sample_id from results where device_id=$1"
        " and calibration_epoch=$2 and state='pending_qc'",
        device_id, epoch)
    affected = list(held) + list(also)
    if not affected:
        return None

    iid = await open_intervention(
        conn, kind=FaultKind.RESULTS_SUSPECT,
        run_id=affected[0]["run_id"], step_id=None, device_id=device_id,
        sample_id=None, actor=actor,
        detail={"epoch": epoch, "note": note,
                "affected_result_ids": [a["id"] for a in affected]},
        extra_samples=[a["sample_id"] for a in affected],
        extra_runs=[a["run_id"] for a in affected],
    )
    await conn.execute(
        "update results set state='held', qc_verdict='warn', held_by=$2, qc_note=$3"
        " where id = any($1::text[])",
        [a["id"] for a in affected], iid, note)
    await audit.log(conn, actor, "results.handed_over", device_id=device_id,
                    epoch=epoch, intervention_id=iid, from_intervention=iv["id"],
                    result_ids=[a["id"] for a in affected])
    return iid


async def _reset_device(conn, device_id: str | None, actor: str) -> None:
    if not device_id:
        return
    await conn.execute(
        "update devices set state = case when quarantined then 'offline' else 'idle' end,"
        " note=null where id=$1 and state='faulted'",
        device_id)
    await audit.log(conn, actor, "device.fault_cleared", device_id=device_id)


async def _complete_step(conn, step_id: str, result, actor: str, transit_s: int,
                         extra: dict | None = None, verdict_note: str | None = None) -> None:
    """Finish a step because a person said to.

    This number has already had the only review it will get, so it is released
    here instead of waiting in `pending_qc` for a machine check that will never
    run. `warn`, not `pass`: it was accepted despite an open question, and the
    note records the option and the credential.
    """
    step = await conn.fetchrow("select * from steps where id=$1 for update", step_id)
    if step is None or step["state"] in ("done", "cancelled"):
        return
    payload = dict(result or {})
    if extra:
        payload.update(extra)
    await conn.execute(
        "update steps set state='done', finished_at=now(), error=null, result=$2 where id=$1",
        step_id, payload)
    rid = await record_result(conn, step, payload, actor)
    if rid:
        await conn.execute(
            "update results set state='released', released_at=now(), qc_verdict='warn',"
            " qc_note=$2 where id=$1 and state='pending_qc'",
            rid, verdict_note or f"accepted by {actor} while resolving an intervention")
    await mark_consumed(conn, step_id)
    await release_all(conn, step_id, "step completed")
    sample = await conn.fetchrow("select * from samples where id=$1", step["sample_id"])
    if sample and sample["state"] != "destroyed":
        await park_sample(conn, step["sample_id"], transit_s)
    await audit.log(conn, actor, "step.done", step_id=step_id, run_id=step["run_id"],
                    device_id=step["device_id"], sample_id=step["sample_id"])


async def record_result(conn, step, payload: dict, actor: str) -> str | None:
    """`steps.state = 'done'` means the robot finished and says nothing about
    whether the number is trustworthy. That is what this row tracks, and unlike
    execution state it can move backwards."""
    if step["device_id"] is None:
        return None
    # An invalidated result does not block a new one: the step was re-run
    # because its previous number was withdrawn, and keeping both rows is what
    # makes the history readable.
    existing = await conn.fetchval(
        "select id from results where step_id=$1 and state <> 'invalidated'", step["id"])
    if existing:
        return existing
    rid = f"res-{uuid.uuid4().hex[:8]}"
    control = payload.get("control_value")
    await conn.execute(
        """
        insert into results(id, run_id, step_id, sample_id, device_id,
                            calibration_epoch, payload, control_value, state)
        values ($1,$2,$3,$4,$5,$6,$7,$8,'pending_qc')
        """,
        rid, step["run_id"], step["id"], step["sample_id"], step["device_id"],
        step["calibration_epoch"], payload,
        float(control) if isinstance(control, (int, float)) else None,
    )
    await audit.log(conn, actor, "result.recorded", run_id=step["run_id"],
                    step_id=step["id"], device_id=step["device_id"],
                    sample_id=step["sample_id"], result_id=rid)
    return rid


async def _reprep_and_restart(conn, run_id: str, actor: str) -> None:
    olds = await conn.fetch(
        "select distinct sample_id from steps where run_id=$1", run_id)
    mapping = {}
    for o in olds:
        await destroy_sample(conn, o["sample_id"])
        old = await conn.fetchrow("select * from samples where id=$1", o["sample_id"])
        new_id = f"plate-{uuid.uuid4().hex[:6]}"
        await conn.execute(
            "insert into samples(id, label, state, location_kind)"
            " values ($1,$2,'parked','storage')",
            new_id, (old["label"] if old else "plate") + " (re-prep)")
        mapping[o["sample_id"]] = new_id

    steps = await conn.fetch("select id, sample_id from steps where run_id=$1", run_id)
    for s in steps:
        # Restarting charges every step again on re-dispatch, so skipping the
        # refund billed the run twice, next to a sibling option that refunded
        # in full.
        await refund_step(conn, s["id"], "run restarted after re-prep")
        await release_all(conn, s["id"], "run restarted after re-prep")
        await conn.execute(
            """
            update steps set sample_id=$2, state='pending', device_id=null, job_handle=null,
                   attempt=0, comms_fail=0, tried_devices='{}', retry_after=null,
                   deadline=null, scheduled_at=null, started_at=null, finished_at=null,
                   error=null, result=null, calibration_epoch=null
             where id=$1 and state <> 'cancelled'
            """,
            s["id"], mapping.get(s["sample_id"], s["sample_id"]))
    # Invalidated, not deleted: a withdrawn number is history worth keeping,
    # which is `record_result`'s argument too.
    await conn.execute(
        "update results set state='invalidated', held_by=null,"
        " invalidated_reason='run restarted after re-prep' where run_id=$1"
        " and state <> 'invalidated'", run_id)
    await conn.execute(
        "update runs set state='pending', updated_at=now(), note='restarted after re-prep'"
        " where id=$1", run_id)
    await audit.log(conn, actor, "run.restarted", run_id=run_id, new_plates=list(mapping.values()))


async def _resume_run_if_clear(conn, run_id: str, actor: str) -> None:
    still_open = await conn.fetchval(
        "select count(*) from interventions where state='open'"
        " and ($1 = any(affected_run_ids) or run_id = $1)",
        run_id)
    if still_open:
        return
    run = await conn.fetchrow("select * from runs where id=$1", run_id)
    if run is None or run["state"] in ("failed", "cancelled", "done"):
        return
    await conn.execute(
        "update steps set state='ready' where run_id=$1 and state='blocked_on_human'", run_id)
    await conn.execute(
        "update runs set state='running', updated_at=now() where id=$1"
        " and state='blocked_on_intervention'", run_id)
    await audit.log(conn, actor, "run.resumed", run_id=run_id)
