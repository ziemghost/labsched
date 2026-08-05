from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from labsched import catalog, db, protocols
from labsched.auth import tokens
from labsched.bootstrap import make_registry
from labsched.config import settings as base_settings
from labsched.drivers.sim import SimDriver
from labsched.runs import RunRequest, submit
from labsched.scheduler import Scheduler

# Fast clock: short steps, instant plate moves, tight timeouts. The scheduler
# reads all of these from config, so the tests exercise the same code paths the
# demo does, just compressed.
TEST_SETTINGS = replace(
    base_settings,
    transit_s=0,
    step_timeout_grace_s=2,
    heartbeat_timeout_s=4,
    tick_interval_s=0.05,
    qc_sweep_grace_s=1,
)

def _database_name(dsn: str) -> str:
    # Not a naive rsplit: the socket form puts a path in the query string
    # (`postgresql:///labsched?host=/var/run/postgresql`) and the last slash
    # belongs to the socket directory, not the database.
    return urlsplit(dsn).path.lstrip("/")


def pytest_configure(config):
    """Refuse to run against anything that is not obviously a test database.

    Every fixture here starts with `drop schema public cascade`. `DATABASE_URL`
    in `.env` points at the demo database, so a bare `pytest` used to delete
    the running lab out from under the API, which is a bad way to discover
    that the scheduler recovers from its tables disappearing. The suite is
    destructive by design, so the safety belongs at the door.
    """
    name = _database_name(base_settings.dsn)
    if not name.endswith(("_test", "_eval")):
        raise pytest.UsageError(
            f"refusing to run: DATABASE_URL points at '{name}', which is not a test "
            "database (expected a name ending in _test or _eval).\n"
            "This suite drops and recreates the schema.\n"
            "  createdb labsched_test\n"
            "  DATABASE_URL='postgresql:///labsched_test?host=/var/run/postgresql' pytest"
        )


FLEET = [
    ("lh-1", "liquid_handler", ["liquid_transfer"], 1, 0),
    ("lh-2", "liquid_handler", ["liquid_transfer"], 1, 2),
    ("inc-1", "incubator", ["incubate"], 3, 0),
    ("bli-1", "bli_reader", ["bli_read"], 5, 0),
    ("bli-2", "bli_reader", ["bli_read"], 5, 2),
]


async def install_fleet(fleet=FLEET, step_seconds: int = 1):
    for did, kind, caps, x, y in fleet:
        await db.execute(
            "insert into devices(id, kind, capabilities, state, last_heartbeat,"
            " layout_x, layout_y) values ($1,$2,$3,'idle',now(),$4,$5)",
            did, kind, caps, x, y,
        )
        await db.execute(
            "insert into device_calibration_epochs(device_id, epoch) values ($1,1)"
            " on conflict do nothing", did)

    # The catalog is lab-owned in tests too: durations are not something a
    # test request may declare, any more than a customer may. We just make the
    # lab's own numbers small.
    await catalog.install_defaults()
    await db.execute("update operations set nominal_duration_s = $1", step_seconds)
    await protocols.load_directory()


class Harness:
    """Scheduler under test plus the small helpers every test wants."""

    def __init__(self, scheduler: Scheduler, driver: SimDriver):
        self.sched = scheduler
        self.driver = driver

    async def spin(self, seconds: float = 5.0, step: float = 0.05, until=None) -> bool:
        """Tick until `until()` is true or the budget runs out."""
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            await self.sched.tick()
            if until is not None and await until():
                return True
            await asyncio.sleep(step)
        return until is None

    async def restart(self) -> "Harness":
        """Throw the scheduler away and build a new one. Anything that
        survives lives in Postgres."""
        self.sched.stop()
        driver = SimDriver(random.Random(7))
        self.sched = Scheduler(make_registry(driver), TEST_SETTINGS)
        self.driver = driver
        return self

    async def force_fault(self, kind: str, device_id=None, step_id=None):
        await db.execute(
            "insert into pending_faults(kind, device_id, step_id) values ($1,$2,$3)",
            kind, device_id, step_id,
        )

    @staticmethod
    async def run_state(run_id: str) -> str:
        return await db.fetchval("select state from runs where id=$1", run_id)

    @staticmethod
    async def step_states(run_id: str) -> dict[str, str]:
        rows = await db.fetch("select name, state from steps where run_id=$1 order by idx", run_id)
        return {r["name"]: r["state"] for r in rows}

    @staticmethod
    async def open_interventions() -> list:
        return await db.fetch("select * from interventions where state='open' order by created_at")

    @staticmethod
    async def held_reservations() -> list:
        return await db.fetch(
            "select * from reservations"
            " where device_released_at is null or sample_released_at is null"
        )

    @staticmethod
    async def assert_no_orphan_locks():
        """Nothing may be held by a step that has stopped."""
        rows = await db.fetch(
            """
            select r.id, r.step_id, s.state from reservations r
              join steps s on s.id = r.step_id
             where (r.device_released_at is null or r.sample_released_at is null)
               and s.state in ('done','failed','cancelled')
            """
        )
        assert rows == [], f"orphaned reservations: {[dict(r) for r in rows]}"

    @staticmethod
    async def assert_plate_in_one_place():
        """The invariant, checked three ways: the location columns cannot
        describe two places, no plate is held by two reservations, and no plate
        sits on a device while a different device holds it."""
        bad = await db.fetch(
            """
            select id from samples
             where (location_kind='device' and location_device_id is null)
                or (location_kind='storage' and location_device_id is not null)
                or (location_kind='transit' and (transit_eta is null or state <> 'in_transit'))
            """
        )
        assert bad == [], f"samples with incoherent location: {[r['id'] for r in bad]}"

        dupes = await db.fetch(
            "select sample_id, count(*) n from reservations"
            " where sample_released_at is null group by sample_id having count(*) > 1"
        )
        assert dupes == [], f"plate held by two reservations: {[dict(d) for d in dupes]}"

        mismatched = await db.fetch(
            """
            select s.id, s.location_device_id, r.device_id
              from samples s join reservations r on r.sample_id = s.id
             where r.sample_released_at is null and r.device_released_at is null
               and s.location_kind = 'device'
               and s.location_device_id <> r.device_id
            """
        )
        assert mismatched == [], f"plate on a device other than the one holding it: {mismatched}"

        # The direction the two queries above cannot see: a plate that is on
        # the shelf while a live reservation says a step still has it. Parking
        # is a physical act, and the only rows that may be parked are rows
        # nothing is holding. An overdue question walked a plate out of a
        # running incubator and neither location coherence nor the mismatch
        # check noticed, because `storage` with a null device is coherent.
        abandoned = await db.fetch(
            """
            select s.id, s.location_kind, r.device_id, st.state as step_state
              from samples s
              join reservations r on r.sample_id = s.id and r.sample_released_at is null
              join steps st on st.id = r.step_id
             where s.location_kind = 'storage'
               and st.state in ('scheduled','running')
            """
        )
        assert abandoned == [], (
            "plate parked in storage while a live step still holds it: "
            f"{[dict(r) for r in abandoned]}")

    @staticmethod
    async def assert_no_stranded_results():
        """Every held result names an OPEN question, and every open question's
        held results are still its own.

        This is the invariant behind the bug this project kept rediscovering:
        a number moved to `held` by one code path and left there because the
        path that was supposed to dispose of it looked for a different set.
        Six review rounds found three separate instances. It is one query.
        """
        stranded = await db.fetch(
            """
            select r.id, r.held_by, i.state as question_state
              from results r left join interventions i on i.id = r.held_by
             where r.state = 'held'
               and (r.held_by is null or i.state <> 'open')
            """
        )
        assert stranded == [], (
            "results held with no open question about them: "
            f"{[dict(r) for r in stranded]}")

        # The other direction, which the query above cannot see and the
        # docstring claimed anyway: an open question whose id list contains a
        # result another open question is holding. Resolving it would dispose
        # of a number it does not hold and empty a question that is still
        # open. held -> open stays true the whole way through, so only this
        # catches it.
        poached = await db.fetch(
            """
            select q.id as question, r.id as result, r.held_by
              from interventions q
              cross join lateral jsonb_array_elements_text(
                     coalesce(q.detail->'affected_result_ids', '[]'::jsonb)) as x(rid)
              join results r on r.id = x.rid
              join interventions holder on holder.id = r.held_by
             where q.state = 'open' and holder.state = 'open' and holder.id <> q.id
            """
        )
        assert poached == [], (
            "an open question lists results another open question holds: "
            f"{[dict(r) for r in poached]}")

    @staticmethod
    async def assert_no_double_booking():
        dupes = await db.fetch(
            "select device_id, count(*) n from reservations"
            " where device_released_at is null group by device_id having count(*) > 1"
        )
        assert dupes == [], f"device double-booked: {[dict(d) for d in dupes]}"


@pytest_asyncio.fixture
async def h():
    await db.reset_schema()
    tokens.reset_keypair_cache()
    await install_fleet()
    driver = SimDriver(random.Random(7))
    sched = Scheduler(make_registry(driver), TEST_SETTINGS)
    yield Harness(sched, driver)
    await db.close_pool()


@pytest_asyncio.fixture
async def org_token():
    return await tokens.mint_root(
        "org",
        tokens.Caveats(
            allowed_kinds=["liquid_handler", "bli_reader", "incubator", "plate_reader"],
            max_concurrent=8, max_wallclock_s=100_000, max_run_credits=500,
            budget_credits=10_000, expires_at=tokens.default_expiry(30),
            authorities=["operator", "engineer", "sample_owner"],
        ),
        token_id="tok-org",
    )


async def simple_run(token_id: str, name: str = "r", priority: int = 0, **kw) -> dict:
    """prep -> incubate -> read, the canonical three-step workflow."""
    run, _ = await submit(RunRequest(
        name=name, token_id=token_id, priority=priority,
        steps=[
            {"name": "prep", "op": "liquid_transfer",
             "with": {"volume_ul": 40, "source": "buffer_a"}},
            {"name": "incubate", "op": "incubate", "with": {"minutes": 30}, "after": [0]},
            {"name": "read", "op": "bli_read", "with": {"target": "TREM2"}, "after": [1]},
        ],
        **kw,
    ))
    return run


async def read_only_run(token_id: str, name: str = "ro", **kw) -> dict:
    run, _ = await submit(RunRequest(
        name=name, token_id=token_id,
        steps=[{"name": "read", "op": "bli_read", "with": {"target": "TREM2"}}],
        **kw,
    ))
    return run


async def one_op_run(token_id: str, op: str, name: str = "x", **kw) -> dict:
    params = {
        "liquid_transfer": {"volume_ul": 40, "source": "buffer_a"},
        "incubate": {"minutes": 30},
        "bli_read": {"target": "TREM2"},
        "absorbance_read": {"wavelength_nm": 450},
    }[op]
    run, _ = await submit(RunRequest(
        name=name, token_id=token_id,
        steps=[{"name": name, "op": op, "with": params}], **kw))
    return run
