"""The lab's operation catalog.

`duration_s` and `credit_cost` used to arrive in the request, which was wrong
three ways:

  * the customer does not know how long a BLI read takes, the lab does;
  * cost is not proportional to duration, since an overnight incubation is
    cheap per hour and a Carterra run is not. It follows the reagents and the
    instrument class;
  * and the token's budget caveat is unenforceable if the caller sets the
    price. An agent declaring `credit_cost: 0` had an infinite budget.

So the catalog is lab-owned. A request names an operation and supplies its
parameters; duration, price, retry policy and ambiguity policy are looked up
here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import db


@dataclass(frozen=True)
class Operation:
    name: str
    capability: str
    nominal_duration_s: int
    credit_cost: int
    params_schema: dict[str, Any]
    on_unknown: str          # retry | ask | fail
    max_attempts: int
    reversible_result: bool
    description: str
    #: Expected reading on this operation's control well, and how far off it
    #: may be before the result is suspect. None means the operation has no
    #: control (an incubation does not produce a number).
    control_target: float | None = None
    control_tolerance: float = 0.15


# name, capability, duration, credits, on_unknown, max_attempts, reversible,
# schema, description, control_target
DEFAULT_OPERATIONS: list[tuple] = [
    (
        "liquid_transfer", "liquid_transfer", 8, 12, "ask", 2, False,
        {
            "volume_ul": {"type": "number", "required": True, "min": 1, "max": 300},
            "source": {"type": "string", "required": True},
        },
        "Move liquid onto the plate. Not repeatable: a second run risks a double dispense.",
        None,
    ),
    (
        "incubate", "incubate", 10, 4, "ask", 2, False,
        {
            "minutes": {"type": "integer", "required": True, "min": 1, "max": 2880},
            "celsius": {"type": "number", "required": False, "min": 4, "max": 60},
        },
        "Hold the plate at temperature. Not repeatable: a second incubation changes the chemistry.",
        None,
    ),
    (
        "bli_read", "bli_read", 8, 25, "retry", 3, True,
        {
            "target": {"type": "string", "required": True},
            "concentrations_nM": {"type": "array", "required": False},
            "replicates": {"type": "integer", "required": False, "min": 1, "max": 8},
        },
        "Label-free binding read. Physically repeatable, so an ambiguous outcome is safe to retry.",
        1.0,
    ),
    (
        "absorbance_read", "absorbance_read", 6, 6, "retry", 3, True,
        {"wavelength_nm": {"type": "integer", "required": False, "min": 200, "max": 1000}},
        "Plate absorbance read. Repeatable.",
        1.0,
    ),
]


async def install_defaults() -> None:
    for (name, cap, dur, cost, unk, att, rev, schema, desc, target) in DEFAULT_OPERATIONS:
        await db.execute(
            """
            insert into operations(name, capability, nominal_duration_s, credit_cost,
                                   params_schema, on_unknown, max_attempts,
                                   reversible_result, description, control_target)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            on conflict (name) do update set
                capability=excluded.capability,
                nominal_duration_s=excluded.nominal_duration_s,
                credit_cost=excluded.credit_cost,
                params_schema=excluded.params_schema,
                on_unknown=excluded.on_unknown,
                max_attempts=excluded.max_attempts,
                reversible_result=excluded.reversible_result,
                description=excluded.description,
                control_target=excluded.control_target
            """,
            name, cap, dur, cost, schema, unk, att, rev, desc, target,
        )


def _op(row) -> Operation:
    return Operation(
        name=row["name"], capability=row["capability"],
        nominal_duration_s=row["nominal_duration_s"], credit_cost=row["credit_cost"],
        params_schema=row["params_schema"], on_unknown=row["on_unknown"],
        max_attempts=row["max_attempts"], reversible_result=row["reversible_result"],
        description=row["description"], control_target=row["control_target"],
        control_tolerance=row["control_tolerance"],
    )


async def all_operations() -> dict[str, Operation]:
    rows = await db.fetch("select * from operations order by name")
    return {r["name"]: _op(r) for r in rows}


async def get(name: str) -> Operation | None:
    row = await db.fetchrow("select * from operations where name=$1", name)
    return _op(row) if row else None


async def on_unknown_for(op_name: str | None, capability: str) -> str:
    """Ambiguity policy for a step, falling back to the conservative answer
    when the operation is unknown. Never assume repeatability."""
    if op_name:
        op = await get(op_name)
        if op:
            return op.on_unknown
    row = await db.fetchrow(
        "select on_unknown from operations where capability=$1 limit 1", capability
    )
    return row["on_unknown"] if row else "ask"


# ------------------------------------------------------------ validation ---

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_params(op: Operation, params: dict[str, Any], path: str) -> list[dict]:
    """Check a step's parameters against the operation's schema.

    Deliberately a small hand-rolled check rather than a JSON Schema
    dependency: the vocabulary is type/required/min/max/enum and nothing else,
    and every message has to carry a `path` an agent can act on.
    """
    problems: list[dict] = []
    schema = op.params_schema or {}

    for key, rule in schema.items():
        present = key in params and params[key] is not None
        if rule.get("required") and not present:
            problems.append({
                "code": "missing_param",
                "path": f"{path}.with.{key}",
                "message": f"operation '{op.name}' requires parameter '{key}'",
                "hint": f"expected {rule.get('type', 'value')}",
            })
            continue
        if not present:
            continue

        value = params[key]
        want = rule.get("type")
        if want and want in _TYPES:
            # bool is an int subclass in Python; do not let it satisfy numbers.
            if isinstance(value, bool) and want in ("integer", "number"):
                ok = False
            else:
                ok = isinstance(value, _TYPES[want])
            if not ok:
                problems.append({
                    "code": "param_type",
                    "path": f"{path}.with.{key}",
                    "message": f"'{key}' must be {want}, got {type(value).__name__}",
                    "hint": None,
                })
                continue

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "min" in rule and value < rule["min"]:
                problems.append({
                    "code": "param_out_of_range",
                    "path": f"{path}.with.{key}",
                    "message": f"'{key}' is {value}, below the minimum {rule['min']}",
                    "hint": f"allowed range {rule['min']}..{rule.get('max', '∞')}",
                })
            if "max" in rule and value > rule["max"]:
                problems.append({
                    "code": "param_out_of_range",
                    "path": f"{path}.with.{key}",
                    "message": f"'{key}' is {value}, above the maximum {rule['max']}",
                    "hint": f"allowed range {rule.get('min', '-∞')}..{rule['max']}",
                })

        if "enum" in rule and value not in rule["enum"]:
            problems.append({
                "code": "param_not_allowed",
                "path": f"{path}.with.{key}",
                "message": f"'{key}' must be one of {rule['enum']}",
                "hint": None,
            })

    for key in params:
        if schema and key not in schema:
            problems.append({
                "code": "unknown_param",
                "path": f"{path}.with.{key}",
                "message": f"operation '{op.name}' takes no parameter '{key}'",
                "hint": f"known parameters: {sorted(schema)}",
            })
    return problems


def suggest(name: str, known: list[str]) -> str | None:
    """Cheap edit-distance suggestion, so a typo costs one round trip."""
    def dist(a: str, b: str) -> int:
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    best = min(known, key=lambda k: dist(name, k), default=None)
    return best if best and dist(name, best) <= max(2, len(name) // 3) else None
