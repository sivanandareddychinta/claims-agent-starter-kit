-- Athena table over the action history the ActionHistory Lambda writes (Task 6).
-- Replace <HistoryBucket> with the HistoryBucket stack output.

CREATE EXTERNAL TABLE IF NOT EXISTS claims_action_history (
  caseid       string,
  eventname    string,
  eventid      string,
  `at`         string,
  actor        string,
  action       string,
  detail       string,
  statusbefore string,
  statusafter  string
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://<HistoryBucket>/history/';

-- Everything that happened to one case, in order
SELECT `at`, actor, action, statusbefore, statusafter, detail
FROM claims_action_history
WHERE caseid = 'CLM-20261016-AB12'
ORDER BY `at`;

-- Who made human decisions, and did any agent ever record one? (should return no agent rows)
SELECT actor, action, count(*) AS events
FROM claims_action_history
WHERE action IN ('decision', 'finalize')
GROUP BY actor, action
ORDER BY events DESC;

-- Cases that breached an SLA
SELECT DISTINCT caseid FROM claims_action_history WHERE action = 'slaBreached';
