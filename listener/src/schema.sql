CREATE TABLE IF NOT EXISTS pr_waits(
    item TEXT PRIMARY KEY REFERENCES items(id),
    issue TEXT NOT NULL, pr TEXT NOT NULL, revision TEXT NOT NULL,
    host TEXT NOT NULL CHECK(host IN ('codex','claude')), profile TEXT NOT NULL,
    thread TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
    condition TEXT NOT NULL DEFAULT 'merged',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','rebind','decided')),
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS pr_decisions(
    id INTEGER PRIMARY KEY, item TEXT NOT NULL REFERENCES pr_waits(item),
    pr TEXT NOT NULL, revision TEXT NOT NULL, merge_oid TEXT,
    host TEXT NOT NULL, profile TEXT NOT NULL, thread TEXT NOT NULL,
    generation INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('closeout','review')),
    reason TEXT NOT NULL, delivery TEXT NOT NULL, UNIQUE(item,pr,revision,generation,kind)
);
CREATE UNIQUE INDEX IF NOT EXISTS pr_closeout_once
    ON pr_decisions(item,pr,revision) WHERE kind='closeout';
CREATE TRIGGER IF NOT EXISTS pr_owner_changed AFTER UPDATE OF thread,host ON items
WHEN NEW.thread != OLD.thread OR NEW.host != OLD.host
BEGIN
    UPDATE pr_waits SET generation=generation+1,state='rebind'
        WHERE item=NEW.id AND state!='decided';
END;

CREATE VIEW IF NOT EXISTS pr_deliverable AS
SELECT d.id,i.name AS item,d.pr,d.revision,d.reason,d.profile,d.thread,d.host,d.delivery
FROM pr_decisions d
JOIN pr_waits w ON w.item=d.item AND w.generation=d.generation
    AND w.revision=d.revision AND w.state='pending'
JOIN items i ON i.id=w.item AND i.thread=d.thread AND i.host=d.host
WHERE d.kind='review' AND i.hold IS NULL AND i.phase IN ('assigned','ready')
AND NOT EXISTS (
    SELECT 1 FROM transfers t LEFT JOIN deliveries a ON a.id=t.action_delivery
    WHERE t.item=i.id AND (t.state='offered' OR (t.state='accepted' AND a.state!='done'))
);
