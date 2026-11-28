CREATE TABLE IF NOT EXISTS items(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, thread TEXT NOT NULL,
    directory TEXT NOT NULL,
    worktree TEXT NOT NULL, git_dir TEXT NOT NULL, command TEXT NOT NULL,
    phase TEXT NOT NULL, hold TEXT, last_error TEXT,
    host TEXT NOT NULL DEFAULT 'codex'
);
CREATE TABLE IF NOT EXISTS deliveries(
    id TEXT PRIMARY KEY, thread TEXT NOT NULL, directory TEXT NOT NULL,
    prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', reply TEXT
);
CREATE TABLE IF NOT EXISTS checks(
    item TEXT NOT NULL REFERENCES items(id), attempt INTEGER NOT NULL,
    revision TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER NOT NULL,
    output TEXT NOT NULL, delivery TEXT NOT NULL REFERENCES deliveries(id),
    PRIMARY KEY(item, attempt)
);
CREATE TABLE IF NOT EXISTS worktree_ids(
    item TEXT PRIMARY KEY REFERENCES items(id),
    device TEXT NOT NULL, inode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers(
    id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES items(id),
    sender TEXT NOT NULL, requested_thread TEXT, receiver TEXT,
    next_action TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'offered', detail TEXT,
    action_delivery TEXT REFERENCES deliveries(id), action_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_offer ON transfers(item) WHERE state='offered';
CREATE TABLE IF NOT EXISTS terminal_offers(
    id TEXT PRIMARY KEY REFERENCES transfers(id)
);
CREATE TABLE IF NOT EXISTS recipient_launches(
    transfer TEXT PRIMARY KEY REFERENCES transfers(id),
    host TEXT NOT NULL, prompt TEXT NOT NULL, directory TEXT NOT NULL,
    history TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lane_decisions(
    id TEXT PRIMARY KEY, lane TEXT NOT NULL, model TEXT NOT NULL,
    effort TEXT NOT NULL, reason TEXT NOT NULL, input_hash TEXT NOT NULL, thread TEXT
);
CREATE TABLE IF NOT EXISTS intakes(
    name TEXT PRIMARY KEY REFERENCES items(id),
    task TEXT NOT NULL, delivery TEXT NOT NULL REFERENCES deliveries(id)
);
