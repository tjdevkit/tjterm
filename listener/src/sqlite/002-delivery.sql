ALTER TABLE pr_decisions ADD COLUMN dispatch_token TEXT;
-- A v1 dispatching row does not tell us whether the old adapter has submitted it.
UPDATE pr_decisions SET delivery='uncertain' WHERE delivery='dispatching';
-- Preserve unsupported preview conditions for inspection without executing them as merge waits.
UPDATE pr_waits SET last_error='unsupported wait condition: ' || condition
WHERE state='pending' AND condition!='merged';

CREATE VIEW pr_wait_snapshot AS
SELECT w.item, w.issue, w.pr, w.revision, w.generation, w.state, w.condition,
       review.id IS NOT NULL AS review_requested, closeout.merge_oid
FROM pr_waits w
LEFT JOIN pr_decisions review
  ON review.item=w.item AND review.pr=w.pr AND review.revision=w.revision
 AND review.generation=w.generation AND review.kind='review'
 AND review.host=w.host AND review.profile=w.profile AND review.thread=w.thread
LEFT JOIN pr_decisions closeout
  ON closeout.item=w.item AND closeout.pr=w.pr AND closeout.revision=w.revision
 AND closeout.kind='closeout';

CREATE VIEW pr_eligible AS
SELECT w.item, i.name, w.issue, w.pr, w.revision, w.generation, w.host, w.profile, w.thread
FROM pr_waits w
JOIN items i ON i.id=w.item AND i.host=w.host AND i.thread=w.thread
WHERE w.state='pending' AND w.condition='merged' AND i.hold IS NULL AND i.phase IN ('assigned','ready')
AND (i.phase!='ready' OR EXISTS (
    SELECT 1 FROM checks c
    WHERE c.item=i.id AND c.revision=w.revision AND c.exit_code=0
      AND c.attempt=(SELECT MAX(attempt) FROM checks WHERE item=i.id)
))
AND NOT EXISTS (
    SELECT 1 FROM transfers t LEFT JOIN deliveries a ON a.id=t.action_delivery
    WHERE t.item=i.id AND (t.state='offered' OR
          (t.state='accepted' AND t.action_delivery IS NOT NULL AND a.state IS NOT 'done'))
);

DROP VIEW pr_deliverable;
CREATE VIEW pr_deliverable AS
SELECT d.id, w.item AS item_key, w.name AS item, d.pr, d.revision, d.reason,
       d.host, d.profile, d.thread, d.delivery
FROM pr_eligible w JOIN pr_decisions d
  ON d.item=w.item AND d.generation=w.generation AND d.pr=w.pr AND d.revision=w.revision
 AND d.host=w.host AND d.profile=w.profile AND d.thread=w.thread
WHERE d.kind='review';
