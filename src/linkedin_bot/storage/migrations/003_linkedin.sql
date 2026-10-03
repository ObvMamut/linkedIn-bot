CREATE TABLE linkedin_state (
 slot INTEGER PRIMARY KEY CHECK(slot=1), state TEXT NOT NULL, last_success TEXT
);
INSERT INTO linkedin_state VALUES (1,'unknown',NULL)
