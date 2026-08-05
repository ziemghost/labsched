"""Capability tokens (biscuit).

A token is a chain of blocks: the root is minted by the lab operator, every
delegation appends one. Appending can only add checks, so a child is weaker
than its parent by construction rather than by our validation being careful.
Mint-time validation exists only for the error message.

Facts the authorizer supplies (the request), six of them, no more:

    op_device_kind("bli_reader")   one per device kind the operation needs
    op_authority("operator")       role this action demands, or "none"
    op_concurrent(3)               reservations this run may hold at once
    op_wallclock(1200)             total instrument-seconds the run will burn
    op_credits(48)                 credits this run will cost
    time(<now>)                    for expiry checks

Checks mirror those facts. Credits appear twice on purpose: the token caps
per-run cost, while the cumulative budget is a ledger in Postgres (see
`charge`), because a token cannot know what it has already spent.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from biscuit_auth import (
    Algorithm,
    Authorizer,
    AuthorizerBuilder,
    Biscuit,
    BiscuitBuilder,
    BlockBuilder,
    KeyPair,
    PrivateKey,
    PublicKey,
)

from .. import db

TIERS = ("org", "project", "agent")

# Who a token may act as when a decision needs a person behind it. These are
# distinct roles with distinct powers, not job titles:
#   operator      on site; the only one who can open a door or free a gripper
#   engineer      remote; may quarantine, drain, requeue; may not touch a plate
#   sample_owner  the customer; the only one who may authorise destroying
#                   their plate, accepting a suspect number, or releasing a result
AUTHORITIES = ("operator", "engineer", "sample_owner")

# "none" means "this request needs no elevated authority". Every token carries
# it implicitly, so ordinary scheduling passes the check while a resolve that
# demands `operator` does not.
NO_AUTHORITY = "none"

BLOCK_SOURCE = """
  token({tid});
  check all op_device_kind($k), {kinds}.contains($k);
  check all op_authority($a), {auth}.contains($a);
  check if op_concurrent($n), $n <= {mc};
  check if op_wallclock($s), $s <= {ws};
  check if op_credits($c), $c <= {cr};
  check if time($t), $t <= {exp};
"""

_keypair: KeyPair | None = None


@dataclass(frozen=True)
class Caveats:
    allowed_kinds: list[str]
    max_concurrent: int
    max_wallclock_s: int
    max_run_credits: int
    budget_credits: int
    expires_at: datetime
    authorities: list[str] = field(default_factory=list)

    def as_params(self, tid: str) -> dict[str, Any]:
        return {
            "tid": tid,
            "kinds": list(self.allowed_kinds),
            "auth": [NO_AUTHORITY, *self.authorities],
            "mc": self.max_concurrent,
            "ws": self.max_wallclock_s,
            "cr": self.max_run_credits,
            "exp": self.expires_at,
        }


@dataclass(frozen=True)
class AuthzResult:
    allowed: bool
    reason: str
    denied_by_token_id: str | None = None
    denied_by_label: str | None = None


class TokenError(Exception):
    pass


# --------------------------------------------------------------- root key ---

async def keypair() -> KeyPair:
    """Root signing key, persisted so tokens outlive a restart."""
    global _keypair
    if _keypair is not None:
        return _keypair
    row = await db.fetchval("select value from sim_config where key = 'root_key'")
    if row and row.get("private"):
        _keypair = KeyPair.from_private_key(PrivateKey.from_bytes(bytes.fromhex(row["private"]), Algorithm.Ed25519))
        return _keypair
    kp = KeyPair()
    await db.execute(
        "insert into sim_config(key, value) values ('root_key', $1)"
        " on conflict (key) do nothing",
        {"private": bytes(kp.private_key.to_bytes()).hex()},
    )
    # Re-read: another process may have won the race.
    row = await db.fetchval("select value from sim_config where key = 'root_key'")
    _keypair = KeyPair.from_private_key(PrivateKey.from_bytes(bytes.fromhex(row["private"]), Algorithm.Ed25519))
    return _keypair


def reset_keypair_cache() -> None:
    global _keypair
    _keypair = None


async def public_key() -> PublicKey:
    return (await keypair()).public_key


# ------------------------------------------------------------------- mint ---

async def mint_root(label: str, caveats: Caveats, token_id: str | None = None) -> dict:
    kp = await keypair()
    tid = token_id or f"tok-{uuid.uuid4().hex[:8]}"
    biscuit = BiscuitBuilder(BLOCK_SOURCE, caveats.as_params(tid)).build(kp.private_key)
    return await _store(tid, None, label, "org", biscuit, caveats)


async def attenuate(
    parent_id: str, label: str, tier: str, caveats: Caveats, token_id: str | None = None
) -> dict:
    parent = await db.fetchrow("select * from tokens where id = $1", parent_id)
    if parent is None:
        raise TokenError(f"no such token {parent_id}")
    if tier not in TIERS:
        raise TokenError(f"tier must be one of {TIERS}")
    _reject_widening(parent, caveats)

    kp = await keypair()
    tid = token_id or f"tok-{uuid.uuid4().hex[:8]}"
    parent_biscuit = Biscuit.from_base64(parent["biscuit"], kp.public_key)
    child = parent_biscuit.append(BlockBuilder(BLOCK_SOURCE, caveats.as_params(tid)))
    return await _store(tid, parent_id, label, tier, child, caveats)


def _reject_widening(parent, c: Caveats) -> None:
    """Mint-time ergonomics only. Biscuit enforces this regardless; we just
    prefer a clear 400 over minting a token that can never authorize."""
    extra = sorted(set(c.allowed_kinds) - set(parent["allowed_kinds"]))
    if extra:
        raise TokenError(
            f"cannot grant device kinds the parent lacks: {extra} "
            f"(parent '{parent['label']}' allows {sorted(parent['allowed_kinds'])})"
        )
    for field, pretty in (
        ("max_concurrent", "max concurrent reservations"),
        ("max_wallclock_s", "max wall-clock seconds"),
        ("max_run_credits", "max credits per run"),
        ("budget_credits", "budget"),
    ):
        want = getattr(c, field)
        if want > parent[field]:
            raise TokenError(
                f"cannot raise {pretty} above parent: {want} > {parent[field]}"
            )
    extra_auth = sorted(set(c.authorities) - set(parent["authorities"]))
    if extra_auth:
        raise TokenError(
            f"cannot grant authority the parent lacks: {extra_auth} "
            f"(parent '{parent['label']}' may act as {sorted(parent['authorities']) or 'nothing'})"
        )
    if c.expires_at > parent["expires_at"]:
        raise TokenError(
            f"cannot extend expiry beyond parent: {c.expires_at.isoformat()} > "
            f"{parent['expires_at'].isoformat()}"
        )


async def _store(tid, parent_id, label, tier, biscuit: Biscuit, c: Caveats) -> dict:
    rev_id = list(biscuit.revocation_ids)[-1]
    row = await db.fetchrow(
        """
        insert into tokens(id, parent_id, label, tier, biscuit, revocation_id,
                           allowed_kinds, max_concurrent, max_wallclock_s,
                           max_run_credits, budget_credits, expires_at, authorities)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        returning *
        """,
        tid, parent_id, label, tier, biscuit.to_base64(), rev_id,
        c.allowed_kinds, c.max_concurrent, c.max_wallclock_s,
        c.max_run_credits, c.budget_credits, c.expires_at, list(c.authorities),
    )
    return dict(row)


# ---------------------------------------------------------------- revoke ----

async def revoke(token_id: str, by: str) -> list[str]:
    """Revoke a token and return every id killed: the token plus its subtree.

    The subtree part enforces itself, since a child biscuit carries its
    parent's revocation id. The recursive update below is bookkeeping for the
    UI, not the mechanism."""
    rows = await db.fetch(
        """
        with recursive lineage as (
            select id from tokens where id = $1
            union all
            select t.id from tokens t join lineage l on t.parent_id = l.id
        )
        update tokens set revoked = true, revoked_at = now(), revoked_by = $2
        where id in (select id from lineage) and revoked = false
        returning id
        """,
        token_id, by,
    )
    return [r["id"] for r in rows]


async def revoked_ids() -> set[str]:
    rows = await db.fetch("select revocation_id from tokens where revoked")
    return {r["revocation_id"] for r in rows}


async def is_revoked(biscuit: Biscuit) -> str | None:
    """Returns the revocation id that killed this token, if any. Checks every
    id in the chain, so revoking a parent kills every child ever issued from
    it, including ones we have never seen."""
    ids = set(biscuit.revocation_ids)
    return await db.fetchval(
        "select revocation_id from tokens where revoked and revocation_id = any($1::text[])",
        list(ids),
    )


# ------------------------------------------------------------- authorize ----

_CHECK_RE = re.compile(r"Check n°(\d+) in block n°(\d+): (.*?)(?:,|$)")


async def load(token_id: str) -> tuple[dict, Biscuit]:
    row = await db.fetchrow("select * from tokens where id = $1", token_id)
    if row is None:
        raise TokenError(f"no such token {token_id}")
    kp = await keypair()
    return dict(row), Biscuit.from_base64(row["biscuit"], kp.public_key)


async def lineage(token_id: str) -> list[dict]:
    """Root-first list of tokens, index-aligned with the biscuit's blocks."""
    rows = await db.fetch(
        """
        with recursive up as (
            select * , 0 as depth from tokens where id = $1
            union all
            select t.*, up.depth + 1 from tokens t join up on up.parent_id = t.id
        )
        select * from up order by depth desc
        """,
        token_id,
    )
    return [dict(r) for r in rows]


async def authorize(
    token_id: str,
    *,
    device_kinds: Sequence[str],
    concurrent: int,
    wallclock_s: int,
    credits: int,
    authority: str = NO_AUTHORITY,
) -> AuthzResult:
    """The single authorization entry point. Called before a run is admitted
    and again before every individual reservation."""
    try:
        row, biscuit = await load(token_id)
    except TokenError as exc:
        return AuthzResult(False, str(exc))

    killer = await is_revoked(biscuit)
    if killer:
        who = await db.fetchrow(
            "select id, label from tokens where revocation_id = $1", killer
        )
        if who and who["id"] != token_id:
            return AuthzResult(
                False,
                f"token '{row['label']}' is revoked: its ancestor '{who['label']}' "
                f"({who['id']}) was revoked, which kills the whole lineage below it",
                who["id"], who["label"],
            )
        return AuthzResult(False, f"token '{row['label']}' has been revoked",
                           row["id"], row["label"])

    src = "".join(f'op_device_kind("{k}");' for k in sorted(set(device_kinds)))
    src += f'op_authority("{authority}");'
    src += (
        f"op_concurrent({int(concurrent)});"
        f"op_wallclock({int(wallclock_s)});"
        f"op_credits({int(credits)});"
        "allow if true;"
    )
    builder = AuthorizerBuilder(src)
    builder.set_time()
    authorizer: Authorizer = builder.build(biscuit)
    try:
        authorizer.authorize()
    except Exception as exc:  # biscuit raises AuthorizationError
        return await _explain(token_id, str(exc), device_kinds, concurrent,
                              wallclock_s, credits, authority)
    return AuthzResult(True, "authorized")


async def _explain(
    token_id: str, err: str, kinds: Sequence[str], concurrent: int,
    wallclock_s: int, credits: int, authority: str = NO_AUTHORITY,
) -> AuthzResult:
    """Turn biscuit's 'Check n°1 in block n°2 failed' into something an
    operator can act on.

    The *decision* is always biscuit's. This function only produces prose, and
    it deliberately does not simply name the block biscuit happened to report
    first: with a chain of nested limits, block 0 usually fails first while the
    interesting constraint is the tightest one further down. So we read the
    check index (which limit was violated) from biscuit, then walk the lineage
    ourselves to find the token that actually binds. Naming the org token when
    the agent token is the real ceiling would send an operator to the wrong
    place.
    """
    chain = await lineage(token_id)   # root-first, index-aligned with blocks
    m = _CHECK_RE.search(err.replace("\n", " "))
    if not m or not chain:
        return AuthzResult(False, f"token denied the request: {err}")
    check_idx, block_idx = int(m.group(1)), int(m.group(2))

    want = sorted(set(kinds))

    def tightest(violates, key):
        """Deepest-then-strictest token among those that reject the request."""
        bad = [t for t in chain if violates(t)]
        if not bad:
            return chain[block_idx] if block_idx < len(chain) else chain[0]
        return min(bad, key=key)

    if check_idx == 0:
        tok = tightest(lambda t: not set(want) <= set(t["allowed_kinds"]),
                       lambda t: len(t["allowed_kinds"]))
        missing = sorted(set(want) - set(tok["allowed_kinds"]))
        reason = (
            f"token '{tok['label']}' allows device kinds {sorted(tok['allowed_kinds'])}, "
            f"run needs {want} (not permitted: {missing})"
        )
    elif check_idx == 1:
        tok = tightest(lambda t: authority not in ([NO_AUTHORITY] + list(t["authorities"])),
                       lambda t: len(t["authorities"]))
        reason = (
            f"token '{tok['label']}' may act as "
            f"{sorted(tok['authorities']) or 'no elevated authority'}, "
            f"but this action requires authority '{authority}'"
        )
    elif check_idx == 2:
        tok = tightest(lambda t: concurrent > t["max_concurrent"],
                       lambda t: t["max_concurrent"])
        reason = (
            f"token '{tok['label']}' allows at most {tok['max_concurrent']} concurrent "
            f"reservations, request asks for {concurrent}"
        )
    elif check_idx == 3:
        tok = tightest(lambda t: wallclock_s > t["max_wallclock_s"],
                       lambda t: t["max_wallclock_s"])
        reason = (
            f"token '{tok['label']}' allows at most {tok['max_wallclock_s']}s of "
            f"instrument time per run, request needs {wallclock_s}s"
        )
    elif check_idx == 4:
        tok = tightest(lambda t: credits > t["max_run_credits"],
                       lambda t: t["max_run_credits"])
        reason = (
            f"token '{tok['label']}' allows at most {tok['max_run_credits']} credits "
            f"per run, request costs {credits}"
        )
    elif check_idx == 5:
        now = datetime.now(tz=timezone.utc)
        tok = tightest(lambda t: t["expires_at"] <= now, lambda t: t["expires_at"])
        reason = f"token '{tok['label']}' expired at {tok['expires_at'].isoformat()}"
    else:
        tok = chain[block_idx] if block_idx < len(chain) else chain[0]
        reason = f"token '{tok['label']}' denied the request: {m.group(3)}"

    return AuthzResult(False, reason, tok["id"], tok["label"])


async def blocked_reason(token_id: str) -> str | None:
    """Why this token cannot be used for anything at all, or None.

    This duplicates two of the biscuit checks (revocation, expiry) against the
    database. It exists only so callers can report the real reason: a revoked
    token fails every per-kind probe, which surfaces as "no permitted device
    kind provides this capability": true, and useless. Authorization is still
    decided by the token, not here.
    """
    try:
        row, biscuit = await load(token_id)
    except TokenError as exc:
        return str(exc)

    killer = await is_revoked(biscuit)
    if killer:
        who = await db.fetchrow("select id, label from tokens where revocation_id=$1", killer)
        if who and who["id"] != token_id:
            return (f"token '{row['label']}' is revoked: its ancestor '{who['label']}' "
                    f"({who['id']}) was revoked, which kills the whole lineage below it")
        return f"token '{row['label']}' has been revoked"

    now = datetime.now(tz=timezone.utc)
    for tok in await lineage(token_id):
        if tok["expires_at"] <= now:
            where = "" if tok["id"] == token_id else f" (ancestor of '{row['label']}')"
            return f"token '{tok['label']}'{where} expired at {tok['expires_at'].isoformat()}"
    return None


async def allowed_kinds_for(token_id: str, candidate_kinds: Sequence[str],
                            ) -> tuple[list[str], dict[str, str]]:
    """Which of `candidate_kinds` this token would accept, one probe each, and
    why it refused the rest.

    Used at admission so a run can be pinned to the kinds it is allowed to
    touch, and dispatch never even considers the others. The refusals come back
    too, because `authorize` denies for several reasons: the kind is not on the
    token, the budget is gone, the cap is full. A caller that reports all three
    as "this token forbids that instrument" names the one thing that is not
    true."""
    out, denied = [], {}
    for kind in candidate_kinds:
        res = await authorize(
            token_id, device_kinds=[kind], concurrent=1, wallclock_s=0, credits=0
        )
        if res.allowed:
            out.append(kind)
        else:
            denied[kind] = res.reason or "not permitted"
    return out, denied


# ---------------------------------------------------------------- budget ----

async def charge(conn, token_id: str, credits: int) -> bool:
    """Debit `credits` from this token and every ancestor, atomically.

    Charging the whole chain is what makes a parent's budget a real cap on
    everything below it: a project cannot mint ten agents and get ten times the
    budget. Returns False if any ancestor would go over, having changed
    nothing. Caller supplies the connection so this joins the reservation
    transaction.

    Rows are locked before they are inspected, so two schedulers racing on the
    same token serialise here instead of both reading the same headroom.
    """
    ids = await _ancestor_ids(conn, token_id)
    # Deterministic order: two concurrent charges on overlapping chains take
    # the same locks in the same sequence and cannot deadlock each other.
    locked = await conn.fetch(
        "select id, budget_credits, credits_spent from tokens"
        " where id = any($1::text[]) order by id for update",
        ids,
    )
    if len(locked) != len(ids):
        return False
    if any(r["credits_spent"] + credits > r["budget_credits"] for r in locked):
        return False
    await conn.execute(
        "update tokens set credits_spent = credits_spent + $2 where id = any($1::text[])",
        ids, credits,
    )
    return True


async def _ancestor_ids(conn, token_id: str) -> list[str]:
    rows = await conn.fetch(
        """
        with recursive up as (
            select id, parent_id from tokens where id = $1
            union all
            select t.id, t.parent_id from tokens t join up on up.parent_id = t.id
        )
        select id from up
        """,
        token_id,
    )
    return [r["id"] for r in rows]


async def refund(conn, token_id: str, credits: int) -> None:
    """Give credits back along the same chain. Used when a reservation is torn
    down without the step having consumed the instrument time it paid for."""
    ids = await _ancestor_ids(conn, token_id)
    await conn.execute(
        "update tokens set credits_spent = greatest(0, credits_spent - $2)"
        " where id = any($1::text[])",
        ids, credits,
    )


def default_expiry(days: int = 30) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(days=days)
