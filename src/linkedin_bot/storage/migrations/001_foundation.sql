CREATE TABLE profile (slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL);
CREATE TABLE facts (
 id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
 snapshot TEXT NOT NULL CHECK(json_valid(snapshot)), PRIMARY KEY(id,revision)
);
CREATE TABLE preferences (
 revision INTEGER PRIMARY KEY, snapshot TEXT NOT NULL CHECK(json_valid(snapshot))
);
CREATE TABLE policies (
 revision INTEGER PRIMARY KEY, snapshot TEXT NOT NULL CHECK(json_valid(snapshot))
);
CREATE TABLE runtime_state (
 id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL,
 restore_audit_required INTEGER NOT NULL
);
INSERT INTO runtime_state VALUES (1,0,0);
CREATE TABLE actions (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, logical_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('proposed','awaiting_approval','blocked','ready',
 'executing','succeeded','failed','unknown','cancelled')),
 digest TEXT NOT NULL, reason_code TEXT
);
CREATE UNIQUE INDEX one_logical_effect ON actions(logical_key)
 WHERE state NOT IN ('failed','cancelled');
CREATE TABLE action_revisions (
 action_id TEXT NOT NULL REFERENCES actions(id), revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL CHECK(json_valid(snapshot)), digest TEXT NOT NULL,
 PRIMARY KEY(action_id,revision)
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, revision INTEGER NOT NULL,
 started_at TEXT NOT NULL, dispatch_at TEXT, outcome TEXT,
 reason_code TEXT, evidence_reference TEXT,
 FOREIGN KEY(action_id,revision) REFERENCES action_revisions(action_id,revision)
);
CREATE TABLE approvals (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL, revision INTEGER NOT NULL,
 digest TEXT NOT NULL, expires_at TEXT NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0,
 consumed_at TEXT, FOREIGN KEY(action_id,revision) REFERENCES action_revisions(action_id,revision)
);
CREATE TABLE reservations (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), local_date TEXT NOT NULL,
 category TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE work_items (
 id TEXT PRIMARY KEY, workflow TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 interval_seconds INTEGER NOT NULL CHECK(interval_seconds>0),
 due_at TEXT NOT NULL, deadline TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','running','blocked','completed')),
 checkpoint TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 retry_count INTEGER NOT NULL DEFAULT 0, recurring INTEGER NOT NULL DEFAULT 1,
 coverage TEXT, reason_code TEXT
);
CREATE TABLE observations (
 source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 snapshot TEXT NOT NULL CHECK(json_valid(snapshot)), first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL, PRIMARY KEY(source,external_id,content_hash)
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
 snapshot TEXT NOT NULL CHECK(json_valid(snapshot))
);
