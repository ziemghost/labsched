-- A result in `pending_qc` had `qc_verdict = 'pass'`, because 'pass' was the
-- column default. The Results tab therefore showed "PENDING QC" and "pass" on
-- the same row: a verdict for a check that had not run yet.
--
-- A verdict is now absent until there is one. `state` says whether the result
-- has been through QC; `qc_verdict` says what QC concluded, and null means
-- "nothing has concluded anything".
alter table results alter column qc_verdict drop not null;
alter table results alter column qc_verdict drop default;
update results set qc_verdict = null where state = 'pending_qc';
