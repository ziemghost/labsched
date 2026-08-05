"""Greedy priority-ordered scheduler, no solver.

    pending -> ready -> scheduled -> running -> done
                 ^          |           |
                 +----------+-----------+  (requeue on auto-recoverable faults)
                            |
                            v
                     blocked_on_human

Two properties everything else rests on:

1.  A tick holds no memory. Every transition reads its inputs from Postgres
    and writes its outputs back before returning, so a scheduler starting cold
    finds the same work and recovery needs no special path. Hence the
    pull-based `probe` in drivers/base.py.

2.  Exclusivity belongs to the two partial unique indexes on `reservations`,
    not to this loop. Racing coroutines or racing processes both lose the same
    way, so the loop only has to handle losing.

A step is marked `running` before the instrument is told to go, so a crash in
between leaves evidence: the step has no job handle on restart, and we ask the
driver whether it recognises the step rather than guessing.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import audit, catalog, db, interventions
from .auth import tokens
from .config import Settings, settings as default_settings
from .drivers.base import (
    DeviceDriver,
    DriverRegistry,
    JobSpec,
    JobState,
    TransientDriverError,
)
from .faults import FaultKind, is_ambiguous, is_human

# Consecutive transient driver errors tolerated before we stop retrying and
# treat the instrument as gone.
COMMS_FAIL_LIMIT = 3


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Scheduler:
    def __init__(self, registry: DriverRegistry, cfg: Settings | None = None) -> None:
        self.registry = registry
        self.cfg = cfg or default_settings
        self._stop = asyncio.Event()
        self.ticks = 0
        #: Not sticky: a reseed drops the schema under a live loop, and pinning
        #: that error into /api/health forever taught people to ignore it.
        self.last_error: str | None = None
        #: Cumulative, so recovering does not erase the evidence.
        self.error_count = 0

    # ------------------------------------------------------------- driving ---
    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:                      # never let the loop die
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.error_count += 1
                try:
                    await self._audit("scheduler", "tick.error", error=self.last_error)
                except Exception:
                    # Postgres going away fails the tick and this write alike,
                    # and an exception in the handler leaves the loop for good.
                    # It died that way once. `last_error` still reports it.
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.tick_interval_s)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        """Lets a supervisor tell a shutdown from a loop that ended by itself."""
        return self._stop.is_set()

    async def tick(self) -> None:
        """One pass. Safe to call standalone; the tests do."""
        await self.sweep_heartbeats()
        await self.enforce_revocations()
        await self.poll_running()
        await self.advance_transits()
        await self.start_arrived()
        await self.promote_ready()
        await self.unstick_starved()
        await self.dispatch()
        await self.finalize_runs()
        await self.sweep_slas()
        await self.sweep_pending_qc()
        self.ticks += 1

    # ---------------------------------------------------------------- SLAs ---
    async def sweep_slas(self) -> None:
        """An overdue intervention is escalated, never decided.

        Park-and-hold: park what is not physically held, leave what is held,
        keep the question open. A hold exists because software cannot know the
        plate's state, and a timer does not teach it. Idling an instrument
        costs a bounded amount of money; shipping a wrong number does not.

        A plate's own hold deadline is the one thing the system may act on
        alone, because how long a plate has sat out is a fact software owns.
        """
        # No `escalated = false` filter: escalating once and re-arming gave the
        # UI a countdown that hit zero and then did nothing forever.
        overdue = await db.fetch(
            "select * from interventions where state='open' and expires_at is not null"
            " and expires_at <= now()"
        )
        p = await db.pool()
        for iv in overdue:
            async with p.acquire() as conn:
                async with conn.transaction():
                    # `holds.sample` is a property of the fault kind, but a
                    # backwards-reaching question's affected set can include
                    # plates a live step is running right now. Parking those
                    # walked a plate out of a machine mid-step, so a live
                    # reservation counts as held too.
                    parked, in_use = [], []
                    if not iv["holds"]["sample"]:
                        for sid in (list(iv["affected_sample_ids"]) or []):
                            if await interventions.park_sample(
                                    conn, sid, self.cfg.transit_s):
                                parked.append(sid)
                            else:
                                in_use.append(sid)
                    # 120s, 240s, 480s, capped at an hour. A fixed re-arm made
                    # one unanswered question out-log every step the lab ran.
                    n = await conn.fetchval(
                        """
                        update interventions
                           set detail = jsonb_set(
                                   detail || '{"escalated": true}'::jsonb,
                                   '{escalations}',
                                   to_jsonb(coalesce((detail->>'escalations')::int, 0) + 1)),
                               expires_at = now() + make_interval(secs =>
                                   least(3600,
                                         120 * power(2, coalesce((detail->>'escalations')::int, 0))
                                   )),
                               version = version + 1
                         where id=$1
                     returning (detail->>'escalations')::int
                        """,
                        iv["id"])
                    await audit.log(
                        conn, "scheduler", "intervention.escalated",
                        intervention_id=iv["id"], run_id=iv["run_id"],
                        device_id=iv["device_id"], kind=iv["kind"],
                        policy=iv["escalation_policy"],
                        escalation=n,
                        parked_samples=parked,
                        # "We left these where they were" is the interesting
                        # half of a park-and-hold entry.
                        samples_in_use=in_use,
                        still_held={"device": iv["holds"]["device"],
                                    "sample": iv["holds"]["sample"]},
                        note="SLA expired; escalated, question left open")

        stale_plates = await db.fetch(
            "select * from samples where hold_deadline is not null and hold_deadline <= now()"
            " and state <> 'destroyed' and suspect_reason is null"
        )
        for s in stale_plates:
            await db.execute(
                "update samples set suspect_reason=$2, hold_deadline=null where id=$1",
                s["id"],
                "held past its stability window while an intervention was open",
            )
            await self._audit("scheduler", "sample.suspect_by_time", sample_id=s["id"],
                              note="hold deadline passed; plate flagged, not discarded")

    async def _audit(self, actor: str, action: str, **kw: Any) -> None:
        p = await db.pool()
        async with p.acquire() as conn:
            await audit.log(conn, actor, action, **kw)

    def _driver(self, device: asyncpg.Record | dict) -> DeviceDriver:
        return self.registry.for_device(device["id"], device["kind"])

    # ---------------------------------------------------------- heartbeats ---
    async def sweep_heartbeats(self) -> None:
        """A failed heartbeat only fails to refresh `last_heartbeat`; the age
        of that timestamp is what takes a device offline, so a driver that
        hangs, throws or stays silent all end up in the same place."""
        devices = await db.fetch("select * from devices")
        for d in devices:
            try:
                hb = await self._driver(d).heartbeat(d["id"])
            except LookupError:
                raise                                 # misconfiguration, not a fault
            except Exception:
                continue                              # no refresh; ageing decides
            await db.execute(
                "update devices set last_heartbeat=now(), suspect = case when $2 then true"
                " else suspect end where id=$1",
                d["id"], hb.health.value == "degraded",
            )
            if d["state"] == "offline" and not d["quarantined"]:
                await self._bring_online(d["id"])

        stale = await db.fetch(
            """
            select * from devices
             where state <> 'offline'
               and (last_heartbeat is null or last_heartbeat < now() - make_interval(secs => $1))
            """,
            float(self.cfg.heartbeat_timeout_s),
        )
        for d in stale:
            await self._take_offline(d, FaultKind.HEARTBEAT_LOST,
                                     "no heartbeat within timeout")

    async def _bring_online(self, device_id: str) -> None:
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                held = await conn.fetchval(
                    "select count(*) from reservations where device_id=$1"
                    " and device_released_at is null", device_id,
                )
                await conn.execute(
                    "update devices set state = case when $2 > 0 then 'reserved' else 'idle' end,"
                    " note=null where id=$1 and state='offline' and quarantined=false",
                    device_id, held,
                )
                await audit.log(conn, "scheduler", "device.online", device_id=device_id)

    async def _take_offline(self, device: asyncpg.Record, kind: str, reason: str) -> None:
        """The auto-recoverable path: retrieve the plate, requeue the step
        elsewhere, refund it. No human, because nothing is ambiguous; we know
        the step did not finish."""
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "update devices set state='offline', note=$2 where id=$1", device["id"], reason
                )
                await audit.log(conn, "scheduler", "device.offline",
                                device_id=device["id"], kind=kind, reason=reason)
                rows = await conn.fetch(
                    """
                    select r.step_id, r.token_id, r.credits, s.state as step_state, s.run_id
                      from reservations r join steps s on s.id = r.step_id
                     where r.device_id=$1 and r.device_released_at is null
                    """,
                    device["id"],
                )
                for r in rows:
                    if r["step_state"] in ("done", "failed", "cancelled", "blocked_on_human"):
                        continue
                    # `requeue_step` refunds; doing it here too would credit
                    # the customer twice for one drained step.
                    await interventions.requeue_step(
                        conn, r["step_id"], reason=f"{kind}: {reason}",
                        exclude_device=device["id"], transit_s=self.cfg.transit_s,
                    )

    # --------------------------------------------------------- revocations ---
    async def enforce_revocations(self) -> None:
        """A revoked token stops new work at once but never abandons a plate:
        the run drains, and is cancelled only once it holds nothing."""
        rows = await db.fetch(
            """
            select r.id, r.token_id, t.label from runs r join tokens t on t.id = r.token_id
             where t.revoked and r.drain_requested = false
               and r.state in ('pending','running','blocked_on_intervention')
            """
        )
        for r in rows:
            await self.request_drain(r["id"], f"token '{r['label']}' revoked")

    async def request_drain(self, run_id: str, reason: str, actor: str = "scheduler") -> None:
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "update runs set drain_requested=true, drain_reason=$2, updated_at=now()"
                    " where id=$1 and drain_requested=false",
                    run_id, reason,
                )
                # Steps that have not yet taken a resource can stop right now.
                await conn.execute(
                    "update steps set state='cancelled', error=$2 where run_id=$1"
                    " and state in ('pending','ready')",
                    run_id, reason,
                )
                await audit.log(conn, actor, "run.drain_requested",
                                run_id=run_id, reason=reason)

    # ------------------------------------------------------------- polling ---
    async def poll_running(self) -> None:
        rows = await db.fetch(
            """
            select s.*, d.kind as device_kind, d.state as device_state
              from steps s join devices d on d.id = s.device_id
             where s.state = 'running'
            """
        )
        for step in rows:
            try:
                await self._poll_one(step)
            except LookupError:
                raise
            except Exception as exc:
                await self._audit("scheduler", "step.poll_error", step_id=step["id"],
                                  run_id=step["run_id"], error=f"{type(exc).__name__}: {exc}")

    async def _poll_one(self, step: asyncpg.Record) -> None:
        driver = self.registry.for_device(step["device_id"], step["device_kind"])
        handle = step["job_handle"]

        if handle is None:
            # Crashed between marking the step running and recording the
            # handle.
            handle = await driver.find_handle_for_step(step["device_id"], step["id"])
            if handle:
                await db.execute("update steps set job_handle=$2 where id=$1", step["id"], handle)
                await self._audit("scheduler", "step.handle_recovered", step_id=step["id"],
                                  run_id=step["run_id"], device_id=step["device_id"],
                                  handle=handle)
            else:
                await self._ambiguous_outcome(
                    step, "scheduler restarted with no record of whether the "
                          "operation was ever submitted")
                return

        try:
            status = await driver.probe(step["device_id"], handle)
        except TransientDriverError as exc:
            await self._comms_failure(step, str(exc))
            return

        if step["comms_fail"]:
            await db.execute("update steps set comms_fail=0 where id=$1", step["id"])

        if status.state is JobState.RUNNING:
            if step["deadline"] and _now() > step["deadline"]:
                await self._timeout(step, driver, handle)
            return

        if status.state is JobState.DONE:
            await self._complete(step, status.result or {})
            return

        if status.state is JobState.UNKNOWN:
            await self._ambiguous_outcome(step, status.message or "instrument lost the job")
            return

        # FAILED
        kind = status.fault_kind or FaultKind.DEVICE_TIMEOUT
        if is_human(kind):
            await self._escalate(step, kind, status.result)
        elif is_ambiguous(kind):
            await self._ambiguous_outcome(step, status.message or kind, kind=kind)
        else:
            await self._auto_recover(step, kind, status.message or kind)

    async def _comms_failure(self, step: asyncpg.Record, msg: str) -> None:
        """Transient transport error: retry with backoff, then fail over."""
        n = step["comms_fail"] + 1
        if n < COMMS_FAIL_LIMIT:
            await db.execute("update steps set comms_fail=$2 where id=$1", step["id"], n)
            await self._audit("scheduler", "step.comms_retry", step_id=step["id"],
                              run_id=step["run_id"], device_id=step["device_id"],
                              attempt=n, limit=COMMS_FAIL_LIMIT, error=msg)
            await asyncio.sleep(min(0.2, self.cfg.retry_backoff_base_s ** n / 100))
            return
        device = await db.fetchrow("select * from devices where id=$1", step["device_id"])
        await self._take_offline(device, FaultKind.COMMS_ERROR,
                                 f"unreachable after {n} attempts: {msg}")

    async def _timeout(self, step, driver: DeviceDriver, handle: str) -> None:
        """Ambiguous, not auto-recoverable: the instrument accepted the job
        and stopped reporting, so whether repeating it is safe belongs to the
        operation. Same door as a lost handle."""
        await driver.cancel(step["device_id"], handle)
        await self._ambiguous_outcome(
            step,
            f"no result {int((_now() - step['started_at']).total_seconds())}s in, "
            f"declared duration {step['duration_s']}s",
            kind=FaultKind.DEVICE_TIMEOUT,
        )

    # ------------------------------------------------------------ outcomes ---
    async def _complete(self, step: asyncpg.Record, result: dict) -> None:
        """`done` means the robot finished, nothing more, so completing a
        step also opens a row in the result plane. A step can be `done` while
        its result is `held`."""
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchval("select state from steps where id=$1 for update",
                                          step["id"])
                if cur != "running":
                    return
                await conn.execute(
                    "update steps set state='done', finished_at=now(), result=$2, error=null"
                    " where id=$1", step["id"], result,
                )
                await interventions.record_result(
                    conn, step, result, f"driver:{step['device_id']}")
                await interventions.mark_consumed(conn, step["id"])
                await interventions.release_all(conn, step["id"], "step completed")
                await interventions.park_sample(conn, step["sample_id"], self.cfg.transit_s)
                await audit.log(conn, f"driver:{step['device_id']}", "step.done",
                                step_id=step["id"], run_id=step["run_id"],
                                device_id=step["device_id"], sample_id=step["sample_id"],
                                result=result)
        await self.run_qc(step, result)

    async def sweep_pending_qc(self) -> None:
        """QC runs outside the transaction that commits the result, so a QC
        failure cannot roll back a step the robot really finished. The cost is
        a result that gets committed and never assessed, so the tick looks
        again instead of trusting that an earlier path succeeded."""
        stale = await db.fetch(
            """
            select s.*, r.payload as qc_payload
              from results r join steps s on s.id = r.step_id
             where r.state = 'pending_qc'
               and r.created_at < now() - make_interval(secs => $1)
             limit 50
            """,
            float(self.cfg.qc_sweep_grace_s),
        )
        for step in stale:
            try:
                await self.run_qc(step, dict(step["qc_payload"] or {}))
            except Exception as exc:
                await self._audit("scheduler", "qc.sweep_error", step_id=step["id"],
                                  run_id=step["run_id"],
                                  error=f"{type(exc).__name__}: {exc}")

    async def run_qc(self, step: asyncpg.Record, result: dict) -> None:
        """Derive a fault from the data, since a drifting instrument reports
        no fault code: green run, `done` step, plausible numbers.

        Two checks, because either alone has a blind spot. The declared control
        target catches an instrument that was already drifting when we first
        used it, which a self-referential check cannot: its own bad readings
        become its baseline. The rolling median catches a slow walk that has
        not yet left the absolute band.
        """
        op = await catalog.get(step["op"]) if step["op"] else None
        control = result.get("control_value")

        # "Nothing to check" is a verdict too. These returns used to leave the
        # result in `pending_qc` forever, behind a check that could never run.
        if op is None or op.control_target is None:
            await self._release_result(step, note="operation has no control well")
            return
        if not isinstance(control, (int, float)):
            await self._release_result(step, note="instrument reported no control value")
            return

        # The protocol may tighten the operation's default tolerance.
        tolerance = float((step["qc"] or {}).get("control_within", op.control_tolerance))
        target = float(op.control_target)

        absolute_dev = abs(control - target) / abs(target) if target else 0.0
        reason = None
        if absolute_dev > tolerance:
            reason = (
                f"control {control:.3f} is {absolute_dev:.0%} away from the expected "
                f"{target:.3f} for '{op.name}' (tolerance {tolerance:.0%})"
            )
            detail_extra = {"check": "absolute", "control_target": target}
            deviation = absolute_dev
        else:
            history = await db.fetch(
                "select control_value from results where device_id=$1"
                " and control_value is not null and step_id <> $2"
                " and calibration_epoch = $3 order by created_at desc limit 12",
                step["device_id"], step["id"], step["calibration_epoch"],
            )
            values = [h["control_value"] for h in history]
            if len(values) < 4:
                await self._release_result(step)
                return
            srt = sorted(values)
            n = len(srt)
            median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
            if not median:
                await self._release_result(step)
                return
            deviation = abs(control - median) / abs(median)
            if deviation <= tolerance:
                await self._release_result(step)
                return
            reason = (
                f"control {control:.3f} deviates {deviation:.0%} from this instrument's "
                f"rolling median {median:.3f} (tolerance {tolerance:.0%})"
            )
            detail_extra = {"check": "rolling_median", "rolling_median": median}

        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                # `for update` on the same row `_resolve_on` locks: without
                # it, QC could attach a number to a question someone was
                # resolving in the next transaction over, stranding it against
                # an answered question. The loser re-checks and opens a fresh
                # one.
                already = await conn.fetchval(
                    "select id from interventions where device_id=$1 and kind=$2"
                    " and state='open'"
                    " and coalesce((detail->>'epoch')::int, $3) = $3"
                    " for update",
                    step["device_id"], FaultKind.CALIBRATION_DRIFT,
                    step["calibration_epoch"])
                iid = already or await interventions.open_intervention(
                    conn, kind=FaultKind.CALIBRATION_DRIFT, run_id=step["run_id"],
                    step_id=None, device_id=step["device_id"], sample_id=None,
                    actor="qc",
                    detail={"detected_by": "control chart", "control_value": control,
                            "deviation": round(deviation, 4), "tolerance": tolerance,
                            "note": reason,
                            # Resolving must not use whatever epoch the
                            # instrument has rolled to since.
                            "epoch": step["calibration_epoch"], **detail_extra},
                )
                # `held_by` is what makes resolving that question release the
                # number. `state='pending_qc'` so a concurrent release is not
                # dragged back to held.
                await conn.execute(
                    "update results set qc_verdict='fail', state='held', qc_note=$2,"
                    " held_by=$3 where step_id=$1 and state='pending_qc'",
                    step["id"], reason, iid,
                )
                # Logged even when the question is already open: repeated
                # out-of-tolerance readings are the corroboration this fault is
                # judged on.
                await audit.log(conn, "qc", "fault.derived",
                                device_id=step["device_id"], step_id=step["id"],
                                run_id=step["run_id"], kind=FaultKind.CALIBRATION_DRIFT,
                                control_value=control, deviation=round(deviation, 4),
                                check=detail_extra["check"])

    async def _release_result(self, step: asyncpg.Record, note: str | None = None) -> None:
        """Everything that enters the results plane has to leave it, or the
        queue is not a queue."""
        await db.execute(
            "update results set qc_verdict='pass', state='released', released_at=now(),"
            " qc_note=coalesce(qc_note, $2) where step_id=$1 and state='pending_qc'",
            step["id"], note)

    async def _auto_recover(self, step: asyncpg.Record, kind: str, message: str) -> None:
        """Requeue away from this instrument while attempts remain, then
        fail loudly rather than retry forever."""
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                await audit.log(conn, "scheduler", "fault.auto",
                                step_id=step["id"], run_id=step["run_id"],
                                device_id=step["device_id"], sample_id=step["sample_id"],
                                kind=kind, message=message)

                if step["attempt"] + 1 >= step["max_attempts"]:
                    # `requeue_step` refunds; this branch tears the
                    # reservation down itself, so it has to.
                    await interventions.refund_step(
                        conn, step["id"], f"{kind}: attempts exhausted")
                    await interventions.release_all(conn, step["id"], f"{kind}: attempts exhausted")
                    await conn.execute(
                        "update steps set state='failed', error=$2, finished_at=now() where id=$1",
                        step["id"], f"{kind}: {message} (no attempts left)",
                    )
                    await interventions.park_sample(conn, step["sample_id"], self.cfg.transit_s)
                    await interventions.fail_run(
                        conn, step["run_id"],
                        f"step '{step['name']}' exhausted {step['max_attempts']} attempts: {kind}",
                    )
                    return

                await interventions.requeue_step(
                    conn, step["id"], reason=f"{kind}: {message}",
                    exclude_device=step["device_id"], transit_s=self.cfg.transit_s,
                )

    async def _escalate(self, step: asyncpg.Record, kind: str, result: dict | None,
                        detail: dict | None = None) -> None:
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchval("select state from steps where id=$1 for update",
                                          step["id"])
                if cur not in ("running", "scheduled"):
                    return
                await interventions.open_intervention(
                    conn, kind=kind, run_id=step["run_id"], step_id=step["id"],
                    device_id=step["device_id"], sample_id=step["sample_id"],
                    detail={"result": result, "capability": step["capability"],
                            "op": step["op"], "attempt": step["attempt"],
                            **(detail or {})},
                )

    async def _ambiguous_outcome(self, step: asyncpg.Record, why: str,
                                 kind: str = FaultKind.COMMS_ERROR) -> None:
        """The instrument cannot say what happened, and what to do about
        that belongs to the operation rather than the fault. A lost read is
        safe to repeat; a dispense is not, and a second incubation changes the
        chemistry even though nothing broke. Ask the catalog."""
        policy = await catalog.on_unknown_for(step["op"], step["capability"])

        if policy == "retry":
            # Log the originating fault, not the policy: an operator needs to
            # see which instrument hung, not a generic comms error.
            await self._auto_recover(
                step, kind,
                f"{why} (operation '{step['op'] or step['capability']}' is physically "
                f"repeatable, so retrying is safe)",
            )
        elif policy == "fail":
            await self._fail_step(step, f"{why} (operation may not be repeated or assumed)")
        else:  # "ask", the conservative default
            await self._escalate(step, FaultKind.SAMPLE_INTEGRITY_UNKNOWN, None,
                                 detail={"why_unknown": why})
            await self._audit("scheduler", "fault.ambiguous_escalated",
                              step_id=step["id"], run_id=step["run_id"],
                              device_id=step["device_id"], capability=step["capability"],
                              op=step["op"], on_unknown=policy, reason=why)

    async def _fail_step(self, step: asyncpg.Record, message: str) -> None:
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                # A failed step did not consume what it paid for.
                await interventions.refund_step(conn, step["id"], message)
                await interventions.release_all(conn, step["id"], message)
                await conn.execute(
                    "update steps set state='failed', error=$2, finished_at=now() where id=$1",
                    step["id"], message)
                await interventions.park_sample(conn, step["sample_id"], self.cfg.transit_s)
                await interventions.fail_run(conn, step["run_id"], message)

    # ------------------------------------------------------------- transit ---
    async def advance_transits(self) -> None:
        """Nothing may be reserved onto a plate still on the mover."""
        arrived = await db.fetch(
            "select * from samples where location_kind='transit' and transit_eta <= now()"
        )
        for s in arrived:
            if s["transit_to"] is None:
                await db.execute(
                    """
                    update samples set location_kind='storage', location_device_id=null,
                           state='parked', transit_from=null, transit_to=null,
                           transit_started_at=null, transit_eta=null
                     where id=$1
                    """,
                    s["id"],
                )
            else:
                await db.execute(
                    """
                    update samples set location_kind='device', location_device_id=$2,
                           state='ok', transit_from=null, transit_to=null,
                           transit_started_at=null, transit_eta=null
                     where id=$1
                    """,
                    s["id"], s["transit_to"],
                )
            await self._audit("scheduler", "sample.arrived", sample_id=s["id"],
                              device_id=s["transit_to"], at="storage" if not s["transit_to"] else "device")

    # --------------------------------------------------------------- start ---
    async def start_arrived(self) -> None:
        """Steps whose plate has landed on the reserved instrument."""
        rows = await db.fetch(
            """
            select s.*, d.kind as device_kind
              from steps s
              join devices d on d.id = s.device_id
              join samples sm on sm.id = s.sample_id
             where s.state='scheduled'
               and sm.location_kind='device' and sm.location_device_id = s.device_id
               and sm.state='ok'
               and d.state in ('reserved','busy')
            """
        )
        for step in rows:
            try:
                await self._start_one(step)
            except LookupError:
                raise
            except Exception as exc:
                await self._audit("scheduler", "step.start_error", step_id=step["id"],
                                  run_id=step["run_id"], error=f"{type(exc).__name__}: {exc}")

    async def _start_one(self, step: asyncpg.Record) -> None:
        driver = self.registry.for_device(step["device_id"], step["device_kind"])

        # Intent first: dying before the handle is stored leaves a running
        # step with no handle, which recovery asks about instead of
        # resubmitting.
        deadline = _now() + timedelta(
            seconds=step["duration_s"] + self.cfg.step_timeout_grace_s
        )
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                ok = await conn.fetchval(
                    "update steps set state='running', started_at=now(), deadline=$2,"
                    " job_handle=null where id=$1 and state='scheduled' returning id",
                    step["id"], deadline,
                )
                if ok is None:
                    return
                await conn.execute("update devices set state='busy' where id=$1", step["device_id"])
                await audit.log(conn, "scheduler", "step.starting", step_id=step["id"],
                                run_id=step["run_id"], device_id=step["device_id"],
                                sample_id=step["sample_id"], capability=step["capability"])

        spec = JobSpec(
            step_id=step["id"], capability=step["capability"],
            duration_s=step["duration_s"], sample_id=step["sample_id"],
        )
        try:
            handle = await driver.start(step["device_id"], spec)
        except TransientDriverError as exc:
            # The plate is still on the deck, so the next tick can retry.
            await db.execute(
                "update steps set state='scheduled', started_at=null, deadline=null,"
                " comms_fail=comms_fail+1 where id=$1 and state='running'",
                step["id"],
            )
            await db.execute(
                "update devices set state='reserved' where id=$1 and state='busy'",
                step["device_id"],
            )
            fresh = await db.fetchrow("select * from steps where id=$1", step["id"])
            await self._audit("scheduler", "step.start_retry", step_id=step["id"],
                              run_id=step["run_id"], device_id=step["device_id"],
                              attempt=fresh["comms_fail"], error=str(exc))
            if fresh["comms_fail"] >= COMMS_FAIL_LIMIT:
                device = await db.fetchrow("select * from devices where id=$1", step["device_id"])
                await self._take_offline(device, FaultKind.COMMS_ERROR,
                                         f"submission failed {fresh['comms_fail']}x: {exc}")
            return

        await db.execute("update steps set job_handle=$2 where id=$1", step["id"], handle)

    # ------------------------------------------------------------ promote ---
    async def promote_ready(self) -> None:
        """pending -> ready once every dependency is done. A dependency that
        ends any other way blocks the step permanently, and the run fails."""
        await db.execute(
            """
            update steps s set state='ready'
             where s.state='pending'
               and not exists (
                   select 1 from step_deps dep
                     join steps p on p.id = dep.depends_on
                    where dep.step_id = s.id and p.state <> 'done')
               and exists (select 1 from runs r where r.id = s.run_id
                            and r.state in ('pending','running')
                            and r.drain_requested = false)
            """
        )

    # ------------------------------------------------------------ starved ---
    async def unstick_starved(self) -> None:
        """Failover excludes the instrument that misbehaved, so on a small
        fleet a step can exclude everything capable and wait forever, ready and
        unschedulable. Clearing the list keeps termination bounded by
        `max_attempts`, which counts requeues whatever their cause.

        A step with no capable instrument at all is annotated, not failed: the
        instrument may come back, and a stalled run beats a dead one.
        """
        rows = await db.fetch(
            """
            select s.id, s.tried_devices, s.run_id,
                   (select count(*) from devices d
                     where d.quarantined = false
                       and s.capability = any(d.capabilities)
                       and d.kind = any(r.allowed_kinds)) as capable,
                   (select count(*) from devices d
                     where d.quarantined = false
                       and s.capability = any(d.capabilities)
                       and d.kind = any(r.allowed_kinds)
                       and d.id <> all(s.tried_devices)) as untried
              from steps s join runs r on r.id = s.run_id
             where s.state = 'ready' and r.drain_requested = false
            """
        )
        for r in rows:
            if r["capable"] == 0:
                await db.execute(
                    "update steps set error='waiting: no instrument in service can run"
                    " this step' where id=$1 and state='ready'", r["id"],
                )
            elif r["untried"] == 0:
                await db.execute(
                    "update steps set tried_devices='{}', error=null where id=$1"
                    " and state='ready'", r["id"],
                )
                await self._audit("scheduler", "step.exclusions_cleared",
                                  step_id=r["id"], run_id=r["run_id"],
                                  tried=list(r["tried_devices"]),
                                  reason="every capable instrument had been excluded")

    # ----------------------------------------------------------- dispatch ---
    async def dispatch(self) -> None:
        """Highest priority first, FIFO within a priority. Each candidate
        gets its own transaction, and losing a race just means trying again
        next tick."""
        candidates = await db.fetch(
            """
            select s.id, s.run_id, r.priority, r.created_at
              from steps s join runs r on r.id = s.run_id
             where s.state='ready'
               and r.state in ('pending','running')
               and r.drain_requested = false
               and (s.retry_after is null or s.retry_after <= now())
             order by r.priority desc, r.created_at asc, s.idx asc
            """
        )
        for c in candidates:
            try:
                await self._try_reserve(c["id"])
            except asyncpg.UniqueViolationError:
                # Someone took the instrument or the plate between our select
                # and our insert. The index did its job.
                await self._audit("scheduler", "dispatch.lost_race", step_id=c["id"],
                                  run_id=c["run_id"])
            except _Rollback as exc:
                # Concurrency ceiling: the transaction rolled back, so the
                # credit charge went with it.
                await self._audit("scheduler", "reservation.denied", step_id=c["id"],
                                  run_id=c["run_id"], reason=str(exc))

    async def _try_reserve(self, step_id: str) -> bool:
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                step = await conn.fetchrow(
                    "select * from steps where id=$1 and state='ready' for update", step_id
                )
                if step is None:
                    return False
                run = await conn.fetchrow(
                    "select * from runs where id=$1 for update", step["run_id"]
                )
                if run is None or run["drain_requested"] or run["state"] not in ("pending", "running"):
                    return False

                # The plate must be somewhere we can pick it up from, and free.
                sample = await conn.fetchrow(
                    "select * from samples where id=$1 for update", step["sample_id"]
                )
                if sample is None or sample["state"] not in ("ok", "parked"):
                    return False
                if sample["location_kind"] == "transit":
                    return False
                held = await conn.fetchval(
                    "select 1 from reservations where sample_id=$1 and sample_released_at is null",
                    step["sample_id"],
                )
                if held:
                    return False

                device = await conn.fetchrow(
                    """
                    select d.* from devices d
                     where d.state = 'idle'
                       and d.quarantined = false
                       and $1 = any(d.capabilities)
                       and d.kind = any($2::text[])
                       and d.id <> all($3::text[])
                       and not exists (select 1 from reservations res
                                        where res.device_id = d.id
                                          and res.device_released_at is null)
                     order by d.suspect asc, d.last_heartbeat desc nulls last, d.id asc
                     for update skip locked
                     limit 1
                    """,
                    step["capability"], list(run["allowed_kinds"]), list(step["tried_devices"]),
                )
                if device is None:
                    return False

                # Per reservation, not just per run: the token may have been
                # revoked or expired since admission.
                authz = await tokens.authorize(
                    run["token_id"], device_kinds=[device["kind"]], concurrent=1,
                    wallclock_s=step["duration_s"], credits=step["credit_cost"],
                )
                if not authz.allowed:
                    await audit.log(conn, "scheduler", "reservation.denied",
                                    run_id=run["id"], step_id=step_id,
                                    device_id=device["id"], token_id=run["token_id"],
                                    reason=authz.reason)
                    await conn.execute(
                        "update steps set retry_after = now() + interval '5 seconds',"
                        " error=$2 where id=$1", step_id, authz.reason,
                    )
                    return False

                # Both against the whole lineage. `charge` takes row locks, so
                # schedulers racing on one token serialise rather than
                # double-spend.
                if not await tokens.charge(conn, run["token_id"], step["credit_cost"]):
                    await audit.log(conn, "scheduler", "reservation.denied",
                                    run_id=run["id"], step_id=step_id, token_id=run["token_id"],
                                    reason="budget exhausted along token lineage")
                    await conn.execute(
                        "update steps set retry_after = now() + interval '10 seconds',"
                        " error='budget exhausted' where id=$1", step_id,
                    )
                    return False

                over = await self._concurrency_breach(conn, run["token_id"])
                if over:
                    raise _Rollback(over)

                res_id = f"res-{uuid.uuid4().hex[:10]}"
                await conn.execute(
                    """
                    insert into reservations(id, run_id, step_id, device_id, sample_id,
                                             token_id, credits)
                    values ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    res_id, run["id"], step_id, device["id"], step["sample_id"],
                    run["token_id"], step["credit_cost"],
                )
                await conn.execute("update devices set state='reserved' where id=$1", device["id"])
                # Without the epoch, discovering drift later tells you the
                # instrument is bad but not which results to distrust.
                epoch = await interventions.current_epoch(conn, device["id"])
                await conn.execute(
                    "update steps set state='scheduled', device_id=$2, scheduled_at=now(),"
                    " calibration_epoch=$3, retry_after=null, error=null where id=$1",
                    step_id, device["id"], epoch,
                )
                await conn.execute(
                    "update runs set state='running', updated_at=now()"
                    " where id=$1 and state='pending'", run["id"],
                )
                await self._send_plate(conn, step["sample_id"], sample, device["id"])
                await audit.log(conn, "scheduler", "reservation.acquired",
                                run_id=run["id"], step_id=step_id, device_id=device["id"],
                                sample_id=step["sample_id"], token_id=run["token_id"],
                                reservation_id=res_id, credits=step["credit_cost"],
                                capability=step["capability"])
                return True

    async def _concurrency_breach(self, conn, token_id: str) -> str | None:
        """`max_concurrent` has to bind everything issued beneath a token, or
        a project mints ten agents and gets ten times its allowance. Biscuit
        caps the shape of a run at admission; this caps the ledger at
        dispatch."""
        ids = await tokens._ancestor_ids(conn, token_id)
        for tid in ids:
            row = await conn.fetchrow(
                """
                with recursive sub as (
                    select id, max_concurrent, label from tokens where id = $1
                    union all
                    select t.id, t.max_concurrent, t.label from tokens t
                      join sub on t.parent_id = sub.id
                )
                select (select max_concurrent from tokens where id=$1) as cap,
                       (select label from tokens where id=$1) as label,
                       (select count(*) from reservations
                         where (device_released_at is null or sample_released_at is null)
                           and token_id in (select id from sub)) as held
                """,
                tid,
            )
            # `held` excludes the reservation we are about to insert, so the
            # test is held + 1. `held > cap` let every token hold one more than
            # it was sold.
            if row["held"] + 1 > row["cap"]:
                return (f"token '{row['label']}' allows {row['cap']} concurrent "
                        f"reservations and already holds {row['held']}")
        return None

    async def _send_plate(self, conn, sample_id: str, sample, device_id: str) -> None:
        if sample["location_kind"] == "device" and sample["location_device_id"] == device_id:
            return                                    # already on the deck
        await conn.execute(
            """
            update samples set state='in_transit', location_kind='transit',
                   location_device_id=null, transit_from=$2, transit_to=$3,
                   transit_started_at=now(),
                   transit_eta = now() + make_interval(secs => $4)
             where id=$1
            """,
            sample_id, sample["location_device_id"], device_id, float(self.cfg.transit_s),
        )

    # ------------------------------------------------------------ finalize ---
    async def finalize_runs(self) -> None:
        rows = await db.fetch(
            """
            select r.id, r.drain_requested, r.drain_reason,
                   count(*) filter (where s.state = 'done')      as done,
                   count(*) filter (where s.state = 'failed')    as failed,
                   count(*) filter (where s.state = 'cancelled') as cancelled,
                   count(*) filter (where s.state in ('scheduled','running')) as busy,
                   count(*) as total
              from runs r left join steps s on s.run_id = r.id
             where r.state in ('pending','running')
             group by r.id
            """
        )
        p = await db.pool()
        for r in rows:
            if r["failed"]:
                async with p.acquire() as conn:
                    async with conn.transaction():
                        await interventions.fail_run(conn, r["id"], "a step failed terminally")
                continue
            if r["drain_requested"]:
                if r["busy"] == 0:
                    await self._cancel_drained(r["id"], r["drain_reason"] or "drained")
                continue
            if r["total"] and r["done"] + r["cancelled"] == r["total"] and r["done"]:
                async with p.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "update runs set state='done', updated_at=now() where id=$1", r["id"]
                        )
                        await audit.log(conn, "scheduler", "run.done", run_id=r["id"])

    async def _cancel_drained(self, run_id: str, reason: str) -> None:
        """Finish a drain: nothing of ours is running any more, so release
        every remaining hold and close the run out. This is the path that has
        to leave no orphan reservation behind after a revocation."""
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                steps = await conn.fetch(
                    "select id, sample_id from steps where run_id=$1"
                    " and state not in ('done','failed','cancelled')",
                    run_id,
                )
                for s in steps:
                    await interventions.release_all(conn, s["id"], f"drained: {reason}")
                    await interventions.park_sample(conn, s["sample_id"], self.cfg.transit_s)
                await conn.execute(
                    "update steps set state='cancelled', error=$2 where run_id=$1"
                    " and state not in ('done','failed','cancelled')",
                    run_id, reason,
                )
                # Any leftover hold from a step that already ended.
                await conn.execute(
                    """
                    update reservations set device_released_at = coalesce(device_released_at, now()),
                           sample_released_at = coalesce(sample_released_at, now()),
                           release_reason = coalesce(release_reason, $2)
                     where run_id = $1
                       and (device_released_at is null or sample_released_at is null)
                    """,
                    run_id, f"drained: {reason}",
                )
                await conn.execute(
                    "update devices d set state='idle', note=null"
                    " where d.state in ('reserved','busy')"
                    "   and not exists (select 1 from reservations res where res.device_id=d.id"
                    "                    and res.device_released_at is null)"
                    "   and d.quarantined=false",
                )
                await conn.execute(
                    "update interventions set state='resolved', resolution='run_cancelled',"
                    " resolved_by='scheduler', resolved_at=now()"
                    " where run_id=$1 and state='open'", run_id,
                )
                await conn.execute(
                    "update runs set state='cancelled', updated_at=now(), note=$2 where id=$1",
                    run_id, reason,
                )
                await audit.log(conn, "scheduler", "run.cancelled", run_id=run_id, reason=reason)

    # ------------------------------------------------------------- devices ---
    async def reset_faulted(self, device_id: str) -> None:
        d = await db.fetchrow("select * from devices where id=$1", device_id)
        if d:
            await self._driver(d).reset(device_id)


class _Rollback(Exception):
    """Raised inside a reservation transaction to abort it cleanly."""
