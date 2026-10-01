-- Accounts that belong to one team and see only that team. `team` is deliberately outside the
-- viewer < operator < admin ladder: it grants nothing on the staff routes.
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('admin', 'operator', 'viewer', 'team'));
ALTER TABLE users ADD COLUMN team_id uuid REFERENCES teams (id) ON DELETE CASCADE;
ALTER TABLE users ADD CONSTRAINT users_team_scope
  CHECK ((role = 'team') = (team_id IS NOT NULL));
CREATE INDEX users_team_id ON users (team_id) WHERE team_id IS NOT NULL;
