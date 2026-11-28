INSERT INTO pr_waits(item,issue,pr,revision,host,profile,thread)
VALUES (?1,?2,?3,?4,?5,?6,?7)
ON CONFLICT(item) DO UPDATE SET
  host=excluded.host, profile=excluded.profile, thread=excluded.thread,
  state=CASE WHEN pr_waits.state='rebind' THEN 'pending' ELSE pr_waits.state END,
  last_error=NULL
WHERE pr_waits.condition='merged' AND pr_waits.issue=excluded.issue
  AND (pr_waits.state='rebind' OR (pr_waits.host=excluded.host
       AND pr_waits.profile=excluded.profile AND pr_waits.thread=excluded.thread))
