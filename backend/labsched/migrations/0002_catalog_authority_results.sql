-- Three things the first cut was missing, and one security hole it had.
--
-- 1. An OPERATION CATALOG owned by the lab. Previously the client declared
--    both `duration_s` and `credit_cost` on every step, which made the token
--    budget caveat decorative: an agent that declares cost 0 has infinite
--    budget. Duration and price are properties of the lab's instrument and
--    reagents, not of the request, so they move server-side.
--
-- 2. AUTHORITY. Resolving an intervention could destroy a customer's plate
--    over an unauthenticated POST with a free-text `by=` string. Authority is
--    now a token caveat and the audit records a token id.
--
-- 3. A RESULTS PLANE separate from the execution plane. `run.state = 'done'`
--    means the robot finished. It must not mean the number is trustworthy --
--    that is a different queue, a different reviewer, and a state that can go
--    backwards when calibration drift is discovered after delivery.

-- ------------------------------------------------------------ operations ---
create table operations (
    name                text primary key,
    capability          text not null,
    nominal_duration_s  int  not null check (nominal_duration_s > 0),
    credit_cost         int  not null check (credit_cost >= 0),
    params_schema       jsonb not null default '{}'::jsonb,
    -- What to do when the instrument cannot tell us whether this operation
    -- happened. This is a property of the OPERATION, not of the fault that
    -- caused the ambiguity: a read is physically safe to repeat, a dispense is
    -- not, and a second incubation changes the chemistry even though nothing
    -- broke. Previously the scheduler used one blanket rule keyed off the
    -- fault kind, which got timeouts wrong in both directions.
    on_unknown          text not null check (on_unknown in ('retry','ask','fail')),
    -- Reads are cheap to repeat, transfers are not; bounds retry per operation
    -- rather than a uniform max_attempts = 3.
    max_attempts        int  not null default 3 check (max_attempts >= 1),
    reversible_result   boolean not null default true,
    description         text not null default ''
);

-- ------------------------------------------------------------- protocols ---
-- A protocol is content-addressed and immutable once any run references it,
-- so "what exactly ran" stays answerable months later.
create table protocols (
    name          text not null,
    version       int  not null,
    source        text not null,           -- the YAML, verbatim
    digest        text not null,           -- sha256 of source
    spec          jsonb not null,          -- parsed form
    registered_at timestamptz not null default now(),
    primary key (name, version)
);

-- ------------------------------------------------- calibration epochs (E) ---
-- Drift is discovered *now* and calls into question results already produced
-- and possibly already delivered. Stamping each step with the instrument's
-- calibration epoch is what makes that reach-back expressible at all.
create table device_calibration_epochs (
    device_id  text not null references devices(id),
    epoch      int  not null,
    started_at timestamptz not null default now(),
    ended_at   timestamptz,
    verdict    text not null default 'good' check (verdict in ('good','suspect','bad')),
    note       text,
    primary key (device_id, epoch)
);

alter table steps add column op text;
alter table steps add column params jsonb not null default '{}'::jsonb;
alter table steps add column calibration_epoch int;
-- Failure policy declared by the protocol, e.g. {"control_within": 0.15}.
alter table steps add column qc jsonb not null default '{}'::jsonb;

alter table samples add column hold_deadline timestamptz;
alter table samples add column suspect_reason text;

-- --------------------------------------------------------------- results ---
-- The second plane. `state` here is about the trustworthiness of a number,
-- and unlike execution state it can move backwards.
create table results (
    id                text primary key,
    run_id            text not null references runs(id) on delete cascade,
    step_id           text not null references steps(id) on delete cascade,
    sample_id         text not null references samples(id),
    device_id         text not null references devices(id),
    calibration_epoch int,
    payload           jsonb not null,
    control_value     double precision,
    qc_verdict        text not null default 'pass' check (qc_verdict in ('pass','warn','fail')),
    qc_note           text,
    state             text not null default 'pending_qc'
                      check (state in ('pending_qc','released','held','invalidated')),
    invalidated_reason text,
    created_at        timestamptz not null default now(),
    released_at       timestamptz,
    unique (step_id)
);
create index results_state_idx on results(state);
create index results_device_epoch_idx on results(device_id, calibration_epoch);

-- ------------------------------------------------------------ run fields ---
alter table runs add column protocol_name text;
alter table runs add column protocol_version int;
alter table runs add column protocol_digest text;
alter table runs add column params jsonb not null default '{}'::jsonb;
alter table runs add column project_id text;
-- Idempotent submission: an agent that times out mid-POST and retries must not
-- start a second physical experiment.
alter table runs add column client_run_id text;
create unique index runs_idempotency on runs(token_id, client_run_id)
    where client_run_id is not null;

-- `awaiting_review` was doing double duty: "a robot is stuck waiting on a
-- human" and "a scientist is judging a curve". Those are different queues with
-- different people and different urgency. Execution blocking keeps its own
-- name; science review lives in `results`.
alter table runs drop constraint runs_state_check;
update runs set state = 'blocked_on_intervention' where state = 'awaiting_review';
alter table runs add constraint runs_state_check check (state in
    ('pending','running','blocked_on_intervention','done','failed','cancelled'));

-- --------------------------------------------------------- interventions ---
alter table interventions add column required_authority text not null default 'operator';
-- Some gates are policy questions ("a re-read costs 12 more credits, proceed?")
-- rather than physical judgement. A sufficiently authorised token may cross
-- those by itself: the human gate is a capability boundary, not a wall.
alter table interventions add column agent_resolvable boolean not null default false;
alter table interventions add column expires_at timestamptz;
alter table interventions add column default_option text;
alter table interventions add column group_key text;
alter table interventions add column affected_sample_ids text[] not null default '{}';
alter table interventions add column affected_run_ids text[] not null default '{}';
alter table interventions add column acknowledged_by text;
alter table interventions add column acknowledged_at timestamptz;
alter table interventions add column version int not null default 1;
alter table interventions add column resolved_by_token text;
alter table interventions add column resolution_reason text;
alter table interventions add column batch_id text;
create index interventions_group_idx on interventions(group_key) where state = 'open';

-- ----------------------------------------------------------- token scope ---
-- Which of {operator, engineer, sample_owner} this token may act as. Enforced
-- by a biscuit caveat like every other permission; this column is for display
-- and for the mint-time check only.
alter table tokens add column authorities text[] not null default '{}';
alter table tokens add column project_id text;
