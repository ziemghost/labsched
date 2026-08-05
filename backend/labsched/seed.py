"""Seed a demo lab: fleet, operation catalog, protocols, token tree, runs.

    python -m labsched.seed --reset          # wipe and rebuild
    python -m labsched.seed --reset --chaos 0.25
"""
from __future__ import annotations

import argparse
import asyncio

from . import catalog, db, protocols
from .auth import tokens
from .runs import RunRequest, submit

# id, kind, capabilities, floor-plan cell
FLEET = [
    ("lh-1",   "liquid_handler", ["liquid_transfer"],  1, 0),
    ("lh-2",   "liquid_handler", ["liquid_transfer"],  1, 2),
    ("inc-1",  "incubator",      ["incubate"],         3, 0),
    ("inc-2",  "incubator",      ["incubate"],         3, 2),
    ("bli-1",  "bli_reader",     ["bli_read"],         5, 0),
    ("pr-1",   "plate_reader",   ["absorbance_read"],  5, 2),
]

STORAGE_TILE = {"x": -1, "y": 1}


async def seed(reset: bool = False, chaos: float = 0.0) -> dict:
    if reset:
        await db.reset_schema()
        tokens.reset_keypair_cache()
    else:
        await db.migrate()

    for did, kind, caps, x, y in FLEET:
        await db.execute(
            """
            insert into devices(id, kind, capabilities, state, last_heartbeat, layout_x, layout_y)
            values ($1,$2,$3,'idle',now(),$4,$5)
            on conflict (id) do update set kind=excluded.kind,
                capabilities=excluded.capabilities, layout_x=excluded.layout_x,
                layout_y=excluded.layout_y
            """,
            did, kind, caps, x, y,
        )
        await db.execute(
            "insert into device_calibration_epochs(device_id, epoch) values ($1, 1)"
            " on conflict do nothing",
            did,
        )

    await catalog.install_defaults()
    await protocols.load_directory()

    await db.execute(
        "insert into sim_config(key, value) values ('chaos', $1)"
        " on conflict (key) do update set value = excluded.value",
        {"rate": chaos, "kinds": None},
    )
    await db.execute(
        "insert into sim_config(key, value) values ('storage_tile', $1)"
        " on conflict (key) do update set value = excluded.value",
        STORAGE_TILE,
    )

    # --- token tree -------------------------------------------------------
    # Two identities you can act as, because the demo only needs two: the
    # operator standing next to the machines, and the customer whose plates
    # they are. The root is the lab's own key and is not an identity anyone
    # picks, so the header does not offer it.
    org = await tokens.mint_root(
        "Lab",
        tokens.Caveats(
            allowed_kinds=["liquid_handler", "bli_reader", "incubator", "plate_reader"],
            max_concurrent=8, max_wallclock_s=100_000, max_run_credits=500,
            budget_credits=40_000, expires_at=tokens.default_expiry(90),
            authorities=["operator", "engineer", "sample_owner"],
        ),
        token_id="tok-org",
    )
    operator = await tokens.attenuate(
        org["id"], "lab operator (on site)", "agent",
        tokens.Caveats(
            allowed_kinds=["liquid_handler", "bli_reader", "incubator", "plate_reader"],
            max_concurrent=4, max_wallclock_s=20_000, max_run_credits=400,
            budget_credits=8_000, expires_at=tokens.default_expiry(89),
            # Engineer as well as operator: on a two-identity demo the person
            # on site is also the one taking an instrument out of service.
            # They still cannot speak for a customer's plate.
            authorities=["operator", "engineer"],
        ),
        token_id="tok-operator",
    )
    client = await tokens.attenuate(
        org["id"], "client (sample owner)", "project",
        tokens.Caveats(
            allowed_kinds=["liquid_handler", "bli_reader", "incubator", "plate_reader"],
            max_concurrent=4, max_wallclock_s=20_000, max_run_credits=400,
            budget_credits=8_000, expires_at=tokens.default_expiry(29),
            # Their own plates, and nothing else: no quarantine, no door.
            authorities=["sample_owner"],
        ),
        token_id="tok-client",
    )

    # --- runs -------------------------------------------------------------
    made = []
    for name, token, priority, plates, target in [
        ("TREM2 panel A", client["id"], 5, 1, "TREM2"),
        ("TREM2 panel B", client["id"], 1, 1, "TREM2"),
        ("Control duplicate", client["id"], 3, 2, "PD-L1"),
    ]:
        run, _ = await submit(RunRequest(
            name=name, token_id=token, priority=priority,
            protocol="binding_screen", params={"target": target, "incubation_min": 30},
            plate_count=plates,
        ))
        made.append(run)

    run, _ = await submit(RunRequest(
        name="Readout sweep", token_id=client["id"], priority=2,
        protocol="readout_only", params={}, plate_count=1,
    ))
    made.append(run)

    return {
        "devices": len(FLEET),
        "operations": len(await catalog.all_operations()),
        "protocols": [f"{p.name}@{p.version}" for p in await protocols.list_all()],
        "tokens": [org["id"], operator["id"], client["id"]],
        "runs": [r["id"] for r in made],
        "chaos_rate": chaos,
    }


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop and recreate the schema")
    ap.add_argument("--chaos", type=float, default=0.0, help="random fault rate, 0..1")
    args = ap.parse_args()
    out = await seed(reset=args.reset, chaos=args.chaos)
    print("seeded:", out)
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
