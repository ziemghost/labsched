-- A control well has a *known expected value*. Comparing an instrument only
-- against its own recent history misses two real cases: an instrument that was
-- already drifting the first time we used it, and a drift slow enough that the
-- rolling median follows it. Both look perfectly steady from the inside.
--
-- So QC gets two signals: an absolute check against the operation's declared
-- control target, and the rolling-median check for change the absolute band is
-- still too wide to catch.
alter table operations add column if not exists control_target double precision;
alter table operations add column if not exists control_tolerance double precision not null default 0.15;
