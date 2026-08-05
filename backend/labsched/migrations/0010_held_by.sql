-- Every bug seven rounds of review have found in this project is the same
-- shape: a row enters a state and no code path takes it out. Results held by
-- QC were the worst instance: three separate options were meant to dispose
-- of them and each disposed of a different subset, so numbers sat `held` with
-- nothing open about them and a badge counting up beside "no decisions
-- pending".
--
-- The cause is that "held" recorded a state without recording *who is holding
-- it*. So a resolution had to guess which results were its to release, by
-- device and epoch and timing, and each guess was wrong in a different way.
--
-- A held result now names the question it is waiting on. Releasing is then
-- "dispose of what this question holds", which cannot reach another
-- question's results and cannot leave its own behind. The invariant is
-- checkable in one query, and a test checks it: no result is held by a
-- question that is not open.
alter table results add column if not exists held_by text references interventions(id);

-- Attach existing held results to the question that is actually about them:
-- same instrument, same calibration epoch, and one of the two kinds that hold
-- results at all. Matching on device alone would hand a held number to an open
-- `plate_stuck` question, whose resolution never touches results, stranding
-- it again the moment that question is answered.
update results r set held_by = (
    select i.id from interventions i
     where i.state = 'open'
       and i.kind in ('results_suspect', 'calibration_drift')
       and i.device_id = r.device_id
       and coalesce((i.detail->>'epoch')::int, r.calibration_epoch) = r.calibration_epoch
     order by i.created_at desc limit 1
) where r.state = 'held' and r.held_by is null;

-- Anything still unclaimed was stranded before this column existed: held with
-- no open question, which is precisely the state this migration exists to make
-- impossible. Put it back in the queue rather than inventing a question for
-- it: the scheduler's QC sweep assesses it again and either releases it or
-- hold it against a question that really exists.
update results
   set state = 'pending_qc', qc_verdict = null, held_by = null,
       qc_note = coalesce(qc_note || ' ', '') || '[requeued for QC: was held with no open question]'
 where state = 'held' and held_by is null;
