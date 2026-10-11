CREATE TABLE opportunity_status (
 source TEXT NOT NULL, external_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('new','interested','applied','interviewing','offer',
 'rejected','dismissed')),
 note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(source,external_id)
)
