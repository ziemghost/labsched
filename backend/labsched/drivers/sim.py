"""Simulated instruments.

This is a stand-in for vendor SDKs, not a stand-in for the scheduler. It obeys
`DeviceDriver` exactly, and it keeps its state in its own tables
(`device_jobs`, `sim_device_health`) which nothing outside this module reads or
writes. Two consequences worth stating:

* The scheduler cannot cheat: there is nowhere to look up what a job "really"
  did, so it calls `probe` as it would against a real Gator.
* A job survives a scheduler crash. The machine kept running while we were
  dead, so `probe` still answers on restart, which makes recovery a real code
  path instead of a fixture.

Outcomes are rolled once, at `start`, and then merely revealed as time passes.
Deterministic given the roll, which keeps the fault tests honest.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .. import db
from ..faults import ALL_KINDS, HUMAN_FAULTS, FaultKind
from .base import (
    DeviceDriver,
    DeviceHealth,
    Heartbeat,
    JobSpec,
    JobState,
    JobStatus,
    TransientDriverError,
)

# Fraction of the declared duration after which an abort-style fault fires.
ABORT_AT = 0.45
# Multiplier applied to a job that is going to hang, so the scheduler's timeout
# sweep is what catches it rather than the job ever completing.
HANG_FACTOR = 20

# Nominal control-well reading for a healthy instrument.
CONTROL_BASELINE = 1.0


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class SimDriver(DeviceDriver):
    kind = "sim"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    # ------------------------------------------------------------- config ---
    async def _chaos_rate(self, conn: asyncpg.Connection) -> float:
        v = await conn.fetchval("select value from sim_config where key = 'chaos'")
        if not v:
            return 0.0
        return float(v.get("rate", 0.0))

    async def _chaos_kinds(self, conn: asyncpg.Connection) -> list[str]:
        v = await conn.fetchval("select value from sim_config where key = 'chaos'")
        if not v or not v.get("kinds"):
            return list(ALL_KINDS)
        return list(v["kinds"])

    # ---------------------------------------------------------- heartbeat ---
    async def heartbeat(self, device_id: str) -> Heartbeat:
        p = await db.pool()
        async with p.acquire() as conn:
            row = await conn.fetchrow(
                "select health, recover_at, reason from sim_device_health where device_id = $1",
                device_id,
            )
            if row is None:
                return Heartbeat(health=DeviceHealth.OK, at=_now())
            if row["recover_at"] is not None and row["recover_at"] <= _now():
                await conn.execute(
                    "update sim_device_health set health='ok', recover_at=null, reason=null,"
                    " since=now() where device_id=$1",
                    device_id,
                )
                return Heartbeat(health=DeviceHealth.OK, at=_now())
            health = DeviceHealth(row["health"])
            if health is DeviceHealth.UNREACHABLE:
                # A real driver would raise/time out here rather than politely
                # report its own death.
                raise TransientDriverError(row["reason"] or "no response from instrument")
            return Heartbeat(health=health, at=_now(), message=row["reason"])

    # -------------------------------------------------------------- start ---
    async def start(self, device_id: str, job: JobSpec) -> str:
        p = await db.pool()
        async with p.acquire() as conn:
            outcome = await self._roll_outcome(conn, device_id, job)

            if outcome == FaultKind.COMMS_ERROR:
                await self._consume_forced(conn, device_id, job.step_id, outcome)
                raise TransientDriverError("connection reset while submitting job")

            handle = f"job-{uuid.uuid4().hex[:12]}"
            now = _now()
            if outcome == FaultKind.DEVICE_TIMEOUT:
                finish_at = now + timedelta(seconds=job.duration_s * HANG_FACTOR)
            elif outcome in (
                FaultKind.SAMPLE_INTEGRITY_UNKNOWN,
                FaultKind.PLATE_STUCK,
                FaultKind.BATCH_DESTROYED,
                FaultKind.DEVICE_OFFLINE,
            ):
                finish_at = now + timedelta(seconds=max(1.0, job.duration_s * ABORT_AT))
            else:
                finish_at = now + timedelta(seconds=job.duration_s)

            await conn.execute(
                """
                insert into device_jobs(handle, device_id, step_id, started_at,
                                        finish_at, outcome, status)
                values ($1,$2,$3,$4,$5,$6,'running')
                """,
                handle, device_id, job.step_id, now, finish_at, outcome,
            )
            await self._consume_forced(conn, device_id, job.step_id, outcome)
            return handle

    async def _roll_outcome(
        self, conn: asyncpg.Connection, device_id: str, job: JobSpec
    ) -> str:
        """Forced faults win over random ones. Returns 'ok' or a FaultKind.

        Forced does not mean physically impossible: a queued fault an
        instrument cannot produce is left queued rather than fired here, so it
        lands on the next operation that could actually have raised it. The
        `/api/sim/fault` endpoint refuses the obviously wrong pairings up
        front; this is the backstop for a row inserted directly.
        """
        forced = await conn.fetch(
            """
            select id, kind from pending_faults
            where consumed = false
              and (device_id is null or device_id = $1)
              and (step_id is null or step_id = $2)
            order by id asc limit 8
            """,
            device_id, job.step_id,
        )
        for row in forced:
            if self._plausible(row["kind"], job.capability):
                return row["kind"]

        rate = await self._chaos_rate(conn)
        if rate > 0 and self._rng.random() < rate:
            kinds = await self._chaos_kinds(conn)
            plausible = [k for k in kinds if self._plausible(k, job.capability)]
            if plausible:
                return self._rng.choice(plausible)
        return "ok"

    @staticmethod
    def _plausible(kind: str, capability: str) -> bool:
        spec = HUMAN_FAULTS.get(kind)
        if spec is None:
            return True   # auto faults can happen to anything
        if spec.capabilities is None:
            return True
        return capability in spec.capabilities

    async def _consume_forced(
        self, conn: asyncpg.Connection, device_id: str, step_id: str, kind: str
    ) -> None:
        await conn.execute(
            """
            update pending_faults set consumed = true
            where id = (
                select id from pending_faults
                where consumed = false and kind = $3
                  and (device_id is null or device_id = $1)
                  and (step_id is null or step_id = $2)
                order by id asc limit 1
            )
            """,
            device_id, step_id, kind,
        )

    # -------------------------------------------------------------- probe ---
    async def probe(self, device_id: str, handle: str) -> JobStatus:
        p = await db.pool()
        async with p.acquire() as conn:
            row = await conn.fetchrow("select * from device_jobs where handle = $1", handle)
            if row is None or row["forgotten"]:
                return JobStatus(
                    state=JobState.UNKNOWN,
                    message="instrument has no record of this job",
                )

            health = await conn.fetchrow(
                "select health from sim_device_health where device_id = $1", device_id
            )
            if health and health["health"] == "unreachable":
                raise TransientDriverError("no response from instrument")

            now = _now()
            if row["status"] == "running" and now < row["finish_at"]:
                span = (row["finish_at"] - row["started_at"]).total_seconds() or 1.0
                return JobStatus(
                    state=JobState.RUNNING,
                    progress=min(1.0, (now - row["started_at"]).total_seconds() / span),
                    finish_at=row["finish_at"],
                )

            return await self._settle(conn, row)

    async def _settle(self, conn: asyncpg.Connection, row: asyncpg.Record) -> JobStatus:
        outcome = row["outcome"]

        if outcome == "ok" or outcome == FaultKind.CALIBRATION_DRIFT:
            result = row["result"] or await self._make_result(conn, row, wild=False)
        elif outcome == FaultKind.UNEXPECTED_READING:
            result = row["result"] or await self._make_result(conn, row, wild=True)
        else:
            result = None

        if outcome == "ok":
            await conn.execute(
                "update device_jobs set status='done', result=$2 where handle=$1",
                row["handle"], result,
            )
            return JobStatus(state=JobState.DONE, result=result, progress=1.0)

        if outcome == FaultKind.DEVICE_OFFLINE:
            # The instrument fell over mid-job: it stops answering entirely.
            await conn.execute(
                """
                insert into sim_device_health(device_id, health, reason, recover_at)
                values ($1,'unreachable','instrument dropped mid-job', now() + interval '25 seconds')
                on conflict (device_id) do update
                  set health='unreachable', since=now(), reason=excluded.reason,
                      recover_at=excluded.recover_at
                """,
                row["device_id"],
            )
            await conn.execute(
                "update device_jobs set status='failed', forgotten=true where handle=$1",
                row["handle"],
            )
            raise TransientDriverError("no response from instrument")

        if outcome == FaultKind.DEVICE_TIMEOUT:
            return JobStatus(state=JobState.RUNNING, progress=0.99,
                             finish_at=row["finish_at"], message="still running")

        await conn.execute(
            "update device_jobs set status='failed', result=$2 where handle=$1",
            row["handle"], result,
        )
        msg = HUMAN_FAULTS[outcome].message if outcome in HUMAN_FAULTS else outcome
        return JobStatus(
            state=JobState.FAILED, fault_kind=outcome, message=msg,
            result=result, progress=1.0,
        )

    async def _make_result(self, conn: asyncpg.Connection, row: asyncpg.Record, *,
                           wild: bool) -> dict[str, Any]:
        """Opaque instrument output. The numbers are deliberately meaningless --
        this project schedules operations, it does not interpret them.

        Except for one: a *read* also reports the value it measured on its
        control well. That number is not a fault code and nothing is wrong with
        it on its own; it only means something compared against this
        instrument's own recent history. It is what makes a drifting instrument
        detectable at all, because a drifting instrument reports no fault and
        produces perfectly plausible-looking results.

        Only reads report one, because only reads have a detector pointed at a
        control well. A liquid handler emitting an absorbance control is the
        kind of detail a lab automation engineer spots in about a second, and
        it used to emit one for every operation.
        """
        if wild:
            value = round(self._rng.uniform(8_000.0, 40_000.0), 3)
        else:
            value = round(self._rng.uniform(0.2, 3.5), 3)

        payload: dict[str, Any] = {
            "channel": "primary",
            "value": value,
            "unit": "au",
            "sane_range": [0.0, 10.0],
            "in_range": 0.0 <= value <= 10.0,
            "failed_wells": [],
        }

        # The lab's catalog decides which operations have a control well; the
        # instrument does not get to invent one.
        has_control = await conn.fetchval(
            "select o.control_target is not null from steps s"
            " join operations o on o.name = s.op where s.id = $1",
            row["step_id"],
        )
        if not has_control:
            return payload

        # A drifting instrument still answers, still says it is fine, and
        # quietly returns controls off its baseline.
        drifting = await conn.fetchval(
            "select silent_drift from sim_device_health where device_id=$1", row["device_id"])
        drift = 1.45 if drifting else 1.0
        payload["control_value"] = round(
            CONTROL_BASELINE * drift * self._rng.uniform(0.995, 1.005), 4)
        return payload

    # ------------------------------------------------------- cancel/reset ---
    async def cancel(self, device_id: str, handle: str) -> None:
        await db.execute(
            "update device_jobs set status='failed', forgotten=true"
            " where handle=$1 and status='running'",
            handle,
        )

    async def reset(self, device_id: str) -> None:
        await db.execute("delete from sim_device_health where device_id = $1", device_id)
        await db.execute(
            "update device_jobs set status='failed', forgotten=true"
            " where device_id=$1 and status='running'",
            device_id,
        )

    async def find_handle_for_step(self, device_id: str, step_id: str) -> str | None:
        return await db.fetchval(
            "select handle from device_jobs where device_id=$1 and step_id=$2"
            " and forgotten=false order by started_at desc limit 1",
            device_id, step_id,
        )

    # --------------------------------------------- sim-only control surface ---
    async def set_degraded(self, device_id: str, reason: str = "calibration drifting") -> None:
        """Make an instrument start quietly returning off-baseline controls.

        It keeps answering heartbeats, keeps reporting `ok`, and keeps
        completing jobs. Nothing it reports is an error. The only way to notice
        is to compare its controls against its own history, which is what QC
        does.

        It deliberately leaves `health` alone. That column is what the
        instrument says about itself, and the scheduler marks a device suspect
        as soon as it hears `degraded`, so using it for silent drift had the
        instrument announcing the thing the control chart should discover.
        """
        await db.execute(
            """
            insert into sim_device_health(device_id, health, reason, silent_drift)
            values ($1,'ok',$2,true)
            on conflict (device_id) do update
              set since=now(), reason=excluded.reason, silent_drift=true
            """,
            device_id, reason,
        )

    async def knock_offline(self, device_id: str, seconds: int | None = 25,
                            reason: str = "heartbeat lost") -> None:
        recover = None if seconds is None else _now() + timedelta(seconds=seconds)
        await db.execute(
            """
            insert into sim_device_health(device_id, health, reason, recover_at)
            values ($1,'unreachable',$2,$3)
            on conflict (device_id) do update
              set health='unreachable', since=now(), reason=excluded.reason,
                  recover_at=excluded.recover_at
            """,
            device_id, reason, recover,
        )
