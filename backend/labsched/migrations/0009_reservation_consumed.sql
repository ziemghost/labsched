-- A step can now hold more than one reservation over its life: re-running a
-- step after its calibration epoch was marked suspect charges again, on
-- purpose, because the instrument time really is spent twice.
--
-- That broke the refund, which asked "is this reservation unrefunded?" and got
-- back rows belonging to attempts that had already run to completion. Aborting
-- mid-re-run promised the sum of both and paid one, then stamped both as
-- refunded.
--
-- "Refunded" and "consumed" are different facts about a reservation and both
-- have to be recorded. A reservation is consumed when its step actually
-- finishes; only an unconsumed, unrefunded one is owed back.
alter table reservations add column if not exists consumed_at timestamptz;

-- Existing rows: a released reservation whose step is done was consumed.
update reservations r set consumed_at = coalesce(r.device_released_at, now())
  from steps s
 where s.id = r.step_id and s.state = 'done' and r.consumed_at is null;
