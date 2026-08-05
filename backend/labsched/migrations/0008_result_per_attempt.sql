-- `unique (step_id)` said a step has at most one result, ever. That is true
-- right up until a calibration epoch is marked suspect and the operator picks
-- "re-run the affected steps": the old number is invalidated and the step goes
-- back on the floor, and the number it produces the second time is a different
-- number. Under the old constraint the re-run silently produced nothing,
-- because `record_result` found the withdrawn row and returned it.
--
-- Withdrawn results are kept rather than overwritten: "we delivered this and
-- then took it back" is exactly the history this plane exists to record. So
-- the constraint becomes what it always meant: a step has at most one result
-- that still counts.
alter table results drop constraint if exists results_step_id_key;
create unique index if not exists results_one_live_per_step
    on results(step_id) where state <> 'invalidated';
