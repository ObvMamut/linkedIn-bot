CREATE TABLE telegram_binding (
 slot INTEGER PRIMARY KEY CHECK(slot=1), user_id INTEGER, chat_id INTEGER, paired_at TEXT,
 code_hash TEXT, code_expires_at TEXT, code_attempts INTEGER NOT NULL DEFAULT 0,
 last_update_id INTEGER NOT NULL DEFAULT 0
);
INSERT INTO telegram_binding(slot) VALUES (1);
CREATE TABLE onboarding (
 slot INTEGER PRIMARY KEY CHECK(slot=1), step TEXT NOT NULL,
 answers TEXT NOT NULL CHECK(json_valid(answers))
);
INSERT INTO onboarding VALUES (1,'roles','{}')
