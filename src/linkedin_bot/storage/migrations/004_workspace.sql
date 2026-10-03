CREATE TABLE pins (
 kind TEXT NOT NULL CHECK(kind IN ('firm','person')), url TEXT NOT NULL, name TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags)),
 board_provider TEXT CHECK(board_provider IN ('greenhouse','lever','ashby')), board_slug TEXT,
 pinned_at TEXT NOT NULL, PRIMARY KEY(kind,url)
);
CREATE TABLE saved_searches (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('people','firms','jobs')), query TEXT NOT NULL,
 last_run TEXT, seen TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(seen)), UNIQUE(kind,query)
);
CREATE TABLE digest_settings (
 slot INTEGER PRIMARY KEY CHECK(slot=1), enabled INTEGER NOT NULL DEFAULT 0,
 local_time TEXT NOT NULL DEFAULT '18:00', alerts_boards INTEGER NOT NULL DEFAULT 0,
 alerts_messages INTEGER NOT NULL DEFAULT 0, quiet_start TEXT NOT NULL DEFAULT '22:00',
 quiet_end TEXT NOT NULL DEFAULT '08:00', last_digest_date TEXT, last_alert_at TEXT
);
INSERT INTO digest_settings(slot) VALUES (1);
CREATE TABLE ui_state (
 slot INTEGER PRIMARY KEY CHECK(slot=1), awaiting TEXT,
 context TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context))
);
INSERT INTO ui_state(slot) VALUES (1)
