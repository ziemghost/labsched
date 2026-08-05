-- `default_option` was the wrong name for what this column holds, and the name
-- caused a real bug. Its value is `park_and_hold`, which is not a key on any
-- fault's option list and can never be passed to `resolve()`. It is a policy
-- for what an expiry does: free what can be freed, keep the question open.
-- Not a decision the system takes on the operator's behalf.
--
-- Calling it the default option invited exactly the reading that an SLA answers
-- the question, and the escalation path had drifted into half-doing that:
-- parking plates the intervention was explicitly holding.
alter table interventions rename column default_option to escalation_policy;
