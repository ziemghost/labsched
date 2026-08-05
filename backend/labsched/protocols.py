"""Protocols: declarative YAML, content-addressed, expanded at admission.

Why YAML and not code: a protocol that is a Python function cannot be diffed,
hashed, or shown to a customer three weeks later as "this is exactly what ran".
A version becomes immutable the moment a run references it, and every run
records the digest it ran under, so that question stays answerable forever.

Substitution is `{{ params.x }}` and nothing else. There is deliberately no
expression evaluator: an arithmetic language in a protocol file is a language
you have to secure, version and debug, and the moment it exists someone
computes an incubation time in it.

`for_each: plate` expands into exactly the `steps` rows the scheduler already
consumes. The engine does not know protocols exist.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import db

PROTOCOL_DIR = Path(__file__).resolve().parents[2] / "protocols"

_SUBST = re.compile(r"^\s*\{\{\s*params\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}\s*$")
_INLINE = re.compile(r"\{\{\s*params\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class ProtocolStep:
    id: str
    op: str
    params: dict[str, Any] = field(default_factory=dict)
    after: list[str] = field(default_factory=list)
    for_each: str | None = None
    qc: dict[str, Any] = field(default_factory=dict)


@dataclass
class Protocol:
    name: str
    version: int
    source: str
    digest: str
    spec: dict[str, Any]

    @property
    def steps(self) -> list[ProtocolStep]:
        return [
            ProtocolStep(
                id=s["id"], op=s["op"], params=dict(s.get("with") or {}),
                after=list(s.get("after") or []), for_each=s.get("for_each"),
                qc=dict(s.get("qc") or {}),
            )
            for s in self.spec.get("steps", [])
        ]

    @property
    def param_schema(self) -> dict[str, Any]:
        return self.spec.get("params") or {}

    @property
    def plate_bounds(self) -> tuple[int, int]:
        p = (self.spec.get("inputs") or {}).get("plates") or {}
        return int(p.get("min", 1)), int(p.get("max", 8))


def digest_of(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


# ------------------------------------------------------------- registry ---

async def register_source(source: str) -> Protocol:
    """Register a protocol version. Re-registering one with different content
    is refused once a run has referenced it, or "what exactly ran" becomes a
    lie."""
    spec = yaml.safe_load(source)
    name, version = spec["protocol"], int(spec["version"])
    dg = digest_of(source)

    existing = await db.fetchrow(
        "select * from protocols where name=$1 and version=$2", name, version
    )
    if existing:
        if existing["digest"] == dg:
            return Protocol(name, version, existing["source"], dg, existing["spec"])
        used = await db.fetchval(
            "select count(*) from runs where protocol_name=$1 and protocol_version=$2",
            name, version,
        )
        if used:
            raise ValueError(
                f"protocol {name} v{version} is already pinned by {used} run(s) at digest "
                f"{existing['digest']}; publish a new version instead of editing this one "
                f"(incoming digest {dg})"
            )
        await db.execute(
            "update protocols set source=$3, digest=$4, spec=$5 where name=$1 and version=$2",
            name, version, source, dg, spec,
        )
        return Protocol(name, version, source, dg, spec)

    await db.execute(
        "insert into protocols(name, version, source, digest, spec) values ($1,$2,$3,$4,$5)",
        name, version, source, dg, spec,
    )
    return Protocol(name, version, source, dg, spec)


async def load_directory(path: Path | None = None) -> list[Protocol]:
    """Register every protocol file. One bad file does not stop the others.

    Editing a file without bumping `version` is correctly refused, but that
    refusal used to abort the rest of the loop, so the lab kept running the old
    definition and the only symptom was a form that ignored the edit.
    Rejections come back to the caller to log.
    """
    out: list[Protocol] = []
    rejected: list[tuple[str, str]] = []
    for f in sorted((path or PROTOCOL_DIR).glob("*.yaml")):
        try:
            out.append(await register_source(f.read_text()))
        except Exception as exc:
            rejected.append((f.name, f"{type(exc).__name__}: {exc}"))
    load_directory.rejected = rejected            # type: ignore[attr-defined]
    return out


async def get(name: str, version: int | None = None) -> Protocol | None:
    if version is None:
        row = await db.fetchrow(
            "select * from protocols where name=$1 order by version desc limit 1", name
        )
    else:
        row = await db.fetchrow(
            "select * from protocols where name=$1 and version=$2", name, version
        )
    if row is None:
        return None
    return Protocol(row["name"], row["version"], row["source"], row["digest"], row["spec"])


async def list_all() -> list[Protocol]:
    rows = await db.fetch("select * from protocols order by name, version")
    return [Protocol(r["name"], r["version"], r["source"], r["digest"], r["spec"]) for r in rows]


# ----------------------------------------------------------- validation ---

def validate_params(proto: Protocol, params: dict[str, Any]) -> tuple[dict, list[dict]]:
    """Apply defaults and check bounds. Returns (resolved, problems)."""
    problems: list[dict] = []
    resolved: dict[str, Any] = {}

    for key, rule in proto.param_schema.items():
        if key in params and params[key] is not None:
            resolved[key] = params[key]
        elif "default" in rule:
            resolved[key] = rule["default"]
        elif rule.get("required"):
            problems.append({
                "code": "missing_param",
                "path": f"params.{key}",
                "message": f"protocol '{proto.name}' requires param '{key}'",
                "hint": f"expected {rule.get('type', 'value')}",
            })
            continue
        else:
            continue

        v = resolved.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "min" in rule and v < rule["min"]:
                problems.append({
                    "code": "param_out_of_range", "path": f"params.{key}",
                    "message": f"'{key}' is {v}, minimum is {rule['min']}",
                    "hint": f"allowed range {rule['min']}..{rule.get('max', '∞')}",
                })
            if "max" in rule and v > rule["max"]:
                problems.append({
                    "code": "param_out_of_range", "path": f"params.{key}",
                    "message": f"'{key}' is {v}, maximum is {rule['max']}",
                    "hint": f"allowed range {rule.get('min', '-∞')}..{rule['max']}",
                })

    for key in params:
        if key not in proto.param_schema:
            problems.append({
                "code": "unknown_param", "path": f"params.{key}",
                "message": f"protocol '{proto.name}' takes no param '{key}'",
                "hint": f"known params: {sorted(proto.param_schema)}",
            })
    return resolved, problems


def substitute(value: Any, params: dict[str, Any]) -> Any:
    """`{{ params.x }}` alone keeps x's type; inside a longer string it
    interpolates as text. No arithmetic, by design."""
    if isinstance(value, str):
        whole = _SUBST.match(value)
        if whole:
            return params.get(whole.group(1))
        return _INLINE.sub(lambda m: str(params.get(m.group(1), "")), value)
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    return value


@dataclass
class ExpandedStep:
    name: str
    op: str
    params: dict[str, Any]
    after: list[int]
    sample: int
    qc: dict[str, Any]
    source_step_id: str


def expand(proto: Protocol, params: dict[str, Any], plate_count: int) -> list[ExpandedStep]:
    """Fan a protocol out over plates into the flat step list the scheduler
    already understands. Steps without `for_each` stay single and are depended
    on by every plate's branch."""
    out: list[ExpandedStep] = []
    index_of: dict[tuple[str, int | None], int] = {}

    for pstep in proto.steps:
        resolved = substitute(pstep.params, params)
        if pstep.for_each == "plate":
            for plate in range(plate_count):
                idx = len(out)
                index_of[(pstep.id, plate)] = idx
                out.append(ExpandedStep(
                    name=f"{pstep.id} · plate {plate + 1}" if plate_count > 1 else pstep.id,
                    op=pstep.op, params=resolved, after=[], sample=plate,
                    qc=pstep.qc, source_step_id=pstep.id,
                ))
        else:
            idx = len(out)
            index_of[(pstep.id, None)] = idx
            out.append(ExpandedStep(
                name=pstep.id, op=pstep.op, params=resolved, after=[], sample=0,
                qc=pstep.qc, source_step_id=pstep.id,
            ))

    for pstep in proto.steps:
        for plate in range(plate_count) if pstep.for_each == "plate" else [None]:
            me = index_of.get((pstep.id, plate))
            if me is None:
                continue
            for dep_id in pstep.after:
                # Depend on the same plate's branch when there is one, else on
                # the shared step.
                dep = index_of.get((dep_id, plate))
                if dep is None:
                    dep = index_of.get((dep_id, None))
                if dep is None:
                    # A fan-in from a per-plate step to a shared one: depend on
                    # every plate's copy.
                    for p2 in range(plate_count):
                        d2 = index_of.get((dep_id, p2))
                        if d2 is not None:
                            out[me].after.append(d2)
                    continue
                out[me].after.append(dep)
    return out


def unknown_step_refs(proto: Protocol) -> list[dict]:
    ids = {s.id for s in proto.steps}
    problems = []
    for i, s in enumerate(proto.steps):
        for dep in s.after:
            if dep not in ids:
                problems.append({
                    "code": "unknown_step_ref", "path": f"steps[{i}].after",
                    "message": f"step '{s.id}' depends on unknown step '{dep}'",
                    "hint": f"known steps: {sorted(ids)}",
                })
    return problems
