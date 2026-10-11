CREATE INDEX observations_identity ON observations(source,external_id,last_seen);
CREATE INDEX observations_first_seen ON observations(first_seen)
