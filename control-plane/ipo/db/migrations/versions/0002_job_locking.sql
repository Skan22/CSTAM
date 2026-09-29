-- A worker that dies leaves its job 'running'. locked_at lets another worker tell a live lock
-- from a dead one; last_error records why a job ended in 'failed' or 'compensated'.
ALTER TABLE jobs ADD COLUMN locked_at timestamptz;
ALTER TABLE jobs ADD COLUMN last_error text;
ALTER TABLE jobs ADD COLUMN team_id uuid REFERENCES teams (id);
CREATE INDEX jobs_running ON jobs (locked_at) WHERE state = 'running';
GRANT SELECT, INSERT, UPDATE ON jobs TO ipo_app;
