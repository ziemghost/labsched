-- labsched core schema.
-- Every piece of durable state lives here; the scheduler process holds nothing
-- in memory that it cannot rebuild from these tables.

create table schema_migrations (
    version     text primary key,
    applied_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- tokens ---
-- Capability tokens. `biscuit` is the serialised token; the denormalised
-- caveat columns are for display and the ledger only; authorization decisions
-- always taken by evaluating the biscuit itself, never these columns.
create table tokens (
    id              text primary key,
    parent_id       text references tokens(id),
    label           text not null,
    tier            text not null check (tier in ('org','project','agent')),
    biscuit         text not null,
    revocation_id   text not null unique,   -- this token's own last-block id
    allowed_kinds   text[] not null,
    max_concurrent  int  not null,
    max_wallclock_s int  not null,
    max_run_credits int  not null,
    budget_credits  int  not null,
    credits_spent   int  not null default 0,
    expires_at      timestamptz not null,
    revoked         boolean not null default false,
    revoked_at      timestamptz,
    revoked_by      text,
    created_at      timestamptz not null default now(),
    check (credits_spent >= 0),
    check (credits_spent <= budget_credits)
);
create index tokens_parent_idx on tokens(parent_id);

-- --------------------------------------------------------------- devices ---
create table devices (
    id             text primary key,
    kind           text not null,
    capabilities   text[] not null,
    state          text not null check (state in ('idle','reserved','busy','faulted','offline')),
    last_heartbeat timestamptz,
    quarantined    boolean not null default false,
    suspect        boolean not null default false,  -- calibration drift, still schedulable
    note           text,
    layout_x       int not null default 0,          -- floor-plan grid cell, for the factory view
    layout_y       int not null default 0,
    created_at     timestamptz not null default now()
);
create index devices_kind_idx on devices(kind);

-- --------------------------------------------------------------- samples ---
-- A plate. location is a single pair of columns, so "in two places at once"
-- is not representable, not merely discouraged.
-- `location_*` is the single source of truth for where a plate physically is.
-- 'transit' means it is on the mover between two tiles: still exactly one
-- place, just a place with a direction and an ETA. The transit_* columns are
-- what the factory view interpolates along; they are never inferred client-side.
create table samples (
    id                 text primary key,
    label              text not null,
    state              text not null check (state in ('ok','in_transit','parked','destroyed')),
    location_kind      text not null check (location_kind in ('storage','device','transit')),
    location_device_id text references devices(id),
    transit_from       text references devices(id),   -- null => from storage
    transit_to         text references devices(id),   -- null => to storage
    transit_started_at timestamptz,
    transit_eta        timestamptz,
    created_at         timestamptz not null default now(),
    check ((location_kind = 'device') = (location_device_id is not null)),
    check ((location_kind = 'transit') = (transit_eta is not null)),
    check ((location_kind = 'transit') = (state = 'in_transit'))
);

-- ------------------------------------------------------------------ runs ---
create table runs (
    id              text primary key,
    name            text not null,
    priority        int  not null default 0,
    state           text not null check (state in ('pending','running','awaiting_review','done','failed','cancelled')),
    token_id        text not null references tokens(id),
    allowed_kinds   text[] not null,   -- kinds this run may use, fixed at admission
    drain_requested boolean not null default false,
    drain_reason    text,
    note            text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index runs_state_idx on runs(state);

create table steps (
    id           text primary key,
    run_id       text not null references runs(id) on delete cascade,
    idx          int  not null,
    name         text not null,
    capability   text not null,
    duration_s   int  not null,
    credit_cost  int  not null,
    sample_id    text not null references samples(id),
    state        text not null check (state in
                   ('pending','ready','scheduled','running','done','failed','cancelled','blocked_on_human')),
    device_id    text references devices(id),
    job_handle   text,               -- opaque handle from the device driver
    attempt      int  not null default 0,
    max_attempts int  not null default 3,
    comms_fail   int  not null default 0,   -- consecutive transient driver errors
    tried_devices text[] not null default '{}',
    retry_after  timestamptz,               -- backoff gate before re-dispatch
    deadline     timestamptz,
    scheduled_at timestamptz,
    started_at   timestamptz,
    finished_at  timestamptz,
    error        text,
    result       jsonb,
    unique (run_id, idx)
);
create index steps_state_idx on steps(state);
create index steps_run_idx on steps(run_id);

create table step_deps (
    step_id    text not null references steps(id) on delete cascade,
    depends_on text not null references steps(id) on delete cascade,
    primary key (step_id, depends_on)
);

-- ---------------------------------------------------------- reservations ---
-- The lock table, and the only place exclusivity is decided.
--
-- A reservation holds two resources, an instrument and a plate, which are
-- released *independently*, because the two come free at different moments.
-- When a read finishes with a suspect value the instrument is immediately
-- reusable while the plate is still pinned by an open question; when an
-- incubator cooks a batch the plate is scrap and the instrument just needs
-- cleaning. One row with two release timestamps models that; one row with a
-- single 'active' flag does not.
--
-- The two partial unique indexes are the actual no-double-booking guarantee.
-- They hold even if the scheduler is wrong: a second reservation on a device
-- or plate that is still held cannot be inserted at all.
create table reservations (
    id          text primary key,
    run_id      text not null references runs(id) on delete cascade,
    step_id     text not null references steps(id) on delete cascade,
    device_id   text not null references devices(id),
    sample_id   text not null references samples(id),
    token_id    text not null references tokens(id),
    credits     int  not null default 0,
    acquired_at timestamptz not null default now(),
    device_released_at timestamptz,
    sample_released_at timestamptz,
    release_reason text
);
create unique index reservations_one_holder_per_device on reservations(device_id)
    where device_released_at is null;
create unique index reservations_one_holder_per_sample on reservations(sample_id)
    where sample_released_at is null;
create unique index reservations_one_open_per_step on reservations(step_id)
    where device_released_at is null or sample_released_at is null;
create index reservations_run_idx on reservations(run_id);

-- --------------------------------------------------------- interventions ---
create table interventions (
    id          text primary key,
    run_id      text not null references runs(id) on delete cascade,
    step_id     text references steps(id) on delete cascade,
    device_id   text references devices(id),
    sample_id   text references samples(id),
    kind        text not null,
    message     text not null,
    detail      jsonb not null default '{}'::jsonb,
    options     jsonb not null,          -- [{key,label,consequence}]
    holds       jsonb not null,          -- {device: bool, sample: bool} + why
    state       text not null check (state in ('open','resolved')),
    resolution  text,
    resolved_by text,
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);
create index interventions_state_idx on interventions(state);

-- ----------------------------------------------------------------- audit ---
create table audit (
    seq             bigserial primary key,
    at              timestamptz not null default now(),
    actor           text not null,       -- 'scheduler' | 'driver:<id>' | 'human:<name>' | 'api'
    action          text not null,
    run_id          text,
    step_id         text,
    device_id       text,
    sample_id       text,
    token_id        text,
    intervention_id text,
    detail          jsonb not null default '{}'::jsonb
);
create index audit_run_idx on audit(run_id);
create index audit_device_idx on audit(device_id);
create index audit_token_idx on audit(token_id);
create index audit_at_idx on audit(at desc);

-- ----------------------------------------------------- simulated hardware ---
-- Everything below this line models state that belongs to the *instrument*,
-- not to the scheduler. Only the sim driver may read or write it. It is in
-- Postgres so that a scheduler restart can still probe a job that the
-- "machine" kept running while we were dead.
create table device_jobs (
    handle      text primary key,
    device_id   text not null references devices(id),
    step_id     text not null,
    started_at  timestamptz not null default now(),
    finish_at   timestamptz not null,
    outcome     text not null,          -- pre-rolled: 'ok' or a fault kind
    status      text not null check (status in ('running','done','failed')),
    result      jsonb,
    forgotten   boolean not null default false  -- comms lost: handle unknown
);
create index device_jobs_device_idx on device_jobs(device_id);

-- The instrument's own opinion of its health, which the scheduler learns only
-- by calling heartbeat(). Kept separate from devices.state on purpose:
-- devices.state is what the scheduler currently *believes*, and the gap
-- between the two is exactly what the heartbeat sweep is for.
create table sim_device_health (
    device_id  text primary key references devices(id),
    health     text not null default 'ok' check (health in ('ok','degraded','unreachable')),
    since      timestamptz not null default now(),
    recover_at timestamptz,          -- null => stays until explicitly reset
    reason     text
);

create table pending_faults (
    id         bigserial primary key,
    kind       text not null,
    device_id  text references devices(id),
    run_id     text,
    step_id    text,
    consumed   boolean not null default false,
    created_at timestamptz not null default now()
);
create index pending_faults_open_idx on pending_faults(consumed) where consumed = false;

create table sim_config (
    key   text primary key,
    value jsonb not null
);
