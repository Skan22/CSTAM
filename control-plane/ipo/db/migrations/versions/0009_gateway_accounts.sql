-- Accounts for the gateway agents, narrower than an operator: each is bound to one gateway and
-- may only heartbeat, report traffic and pull the config for that gateway. Like `team`, the role
-- is outside the viewer < operator < admin ladder and no staff route admits it.
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('admin', 'operator', 'viewer', 'team', 'gateway'));
ALTER TABLE users ADD COLUMN gateway text;
ALTER TABLE users ADD CONSTRAINT users_gateway_scope CHECK ((role = 'gateway') = (gateway IS NOT NULL));
