-- Initial schema. The invariants live here, not in application code.

CREATE TYPE lease_state AS ENUM ('free', 'pooled', 'leased', 'draining', 'quarantined');

-- ---------------------------------------------------------------- users
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  role          text NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
  password_hash text NOT NULL
);

-- ---------------------------------------------------------------- teams
CREATE TABLE teams (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'),
  subdomain  text NOT NULL UNIQUE,
  state      text NOT NULL DEFAULT 'pending'
             CHECK (state IN ('pending', 'active', 'draining', 'deleted', 'failed')),
  owner      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);

-- --------------------------------------------------------------- leases
CREATE TABLE leases (
  ip                inet PRIMARY KEY,
  state             lease_state NOT NULL DEFAULT 'free',
  team_id           uuid REFERENCES teams (id),
  port_id           text,
  server_id         text,
  expires_at        timestamptz,
  quarantined_until timestamptz,
  row_version       bigint NOT NULL DEFAULT 1,
  CONSTRAINT lease_shape CHECK (
    CASE state
      WHEN 'free' THEN team_id IS NULL AND port_id IS NULL AND server_id IS NULL
                       AND expires_at IS NULL AND quarantined_until IS NULL
      WHEN 'pooled' THEN team_id IS NULL AND expires_at IS NULL AND quarantined_until IS NULL
      WHEN 'leased' THEN team_id IS NOT NULL AND expires_at IS NOT NULL
                         AND quarantined_until IS NULL
      WHEN 'draining' THEN quarantined_until IS NULL
      WHEN 'quarantined' THEN team_id IS NULL AND port_id IS NULL AND server_id IS NULL
                              AND expires_at IS NULL AND quarantined_until IS NOT NULL
    END)
);
CREATE UNIQUE INDEX leases_one_leased_per_team ON leases (team_id) WHERE state = 'leased';
CREATE INDEX leases_state ON leases (state);

CREATE FUNCTION lease_transition_allowed(from_state lease_state, to_state lease_state)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT (from_state, to_state) IN (
    ('free', 'pooled'),          -- warm-pool manager reserves an address
    ('free', 'leased'),          -- cold path: reserve, then boot
    ('pooled', 'leased'),        -- claim from the warm pool
    ('pooled', 'draining'),      -- discard a warm resource
    ('leased', 'pooled'),        -- saga undo of a claim
    ('leased', 'draining'),      -- expiry or manual teardown
    ('draining', 'quarantined'), -- VM and port are gone
    ('quarantined', 'free')      -- quarantine elapsed
  )
$$;

CREATE FUNCTION leases_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'free' THEN
      RAISE EXCEPTION 'lease % must be inserted as free', NEW.ip USING ERRCODE = 'IPO01';
    END IF;
    RETURN NEW;
  ELSIF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'leases cannot be deleted' USING ERRCODE = 'IPO01';
  END IF;
  IF NEW.ip <> OLD.ip THEN
    RAISE EXCEPTION 'lease ip is immutable' USING ERRCODE = 'IPO01';
  END IF;
  IF NEW.state <> OLD.state AND NOT lease_transition_allowed(OLD.state, NEW.state) THEN
    RAISE EXCEPTION 'illegal lease transition % -> % for %', OLD.state, NEW.state, OLD.ip
      USING ERRCODE = 'IPO01';
  END IF;
  NEW.row_version := OLD.row_version + 1;
  RETURN NEW;
END $$;

CREATE TRIGGER leases_guard BEFORE INSERT OR UPDATE OR DELETE ON leases
  FOR EACH ROW EXECUTE FUNCTION leases_guard();

-- --------------------------------------------------------------- routes
CREATE TABLE routes (
  host         text PRIMARY KEY,
  team_id      uuid NOT NULL REFERENCES teams (id),
  backend_ip   inet NOT NULL,
  backend_port integer NOT NULL CHECK (backend_port BETWEEN 1 AND 65535),
  middlewares  jsonb NOT NULL DEFAULT '[]',
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX routes_team ON routes (team_id);

-- ----------------------------------------------------------------- jobs
CREATE TABLE jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind            text NOT NULL,
  payload         jsonb NOT NULL DEFAULT '{}',
  state           text NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'compensated')),
  attempts        integer NOT NULL DEFAULT 0,
  run_after       timestamptz NOT NULL DEFAULT now(),
  locked_by       text,
  trace_id        text,
  idempotency_key text NOT NULL UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready ON jobs (run_after) WHERE state = 'queued';

CREATE TABLE saga_steps (
  job_id      uuid NOT NULL REFERENCES jobs (id),
  step        text NOT NULL,
  status      text NOT NULL CHECK (status IN ('running', 'done', 'failed', 'undone')),
  result      jsonb,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (job_id, step)
);

-- ------------------------------------------------------ config_versions
CREATE TABLE config_versions (
  version    bigint PRIMARY KEY,
  sha256     text NOT NULL,
  signature  text NOT NULL,
  body       text NOT NULL,
  status     text NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'live', 'superseded', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- The version is assigned here, under a lock, so it is strictly increasing and gapless.
CREATE FUNCTION config_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    PERFORM pg_advisory_xact_lock(hashtext('ipo.config_versions'));
    SELECT COALESCE(max(version), 0) + 1 INTO NEW.version FROM config_versions;
    RETURN NEW;
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.version <> OLD.version OR NEW.sha256 <> OLD.sha256
       OR NEW.signature <> OLD.signature OR NEW.body <> OLD.body THEN
      RAISE EXCEPTION 'config versions are immutable; only status may change'
        USING ERRCODE = 'IPO02';
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'config versions cannot be deleted' USING ERRCODE = 'IPO02';
END $$;

CREATE TRIGGER config_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON config_versions
  FOR EACH ROW EXECUTE FUNCTION config_versions_guard();

-- ------------------------------------------------------- gateway_status
CREATE TABLE gateway_status (
  gateway        text PRIMARY KEY,
  vrrp_state     text NOT NULL DEFAULT 'UNKNOWN'
                 CHECK (vrrp_state IN ('MASTER', 'BACKUP', 'FAULT', 'UNKNOWN')),
  live_version   bigint,
  last_heartbeat timestamptz
);

-- ------------------------------------------------------------- settings
CREATE TABLE settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  version    bigint NOT NULL DEFAULT 1,
  changed_by text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION settings_bump() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.version := OLD.version + 1;
  NEW.changed_at := now();
  RETURN NEW;
END $$;

CREATE TRIGGER settings_bump BEFORE UPDATE ON settings
  FOR EACH ROW EXECUTE FUNCTION settings_bump();

-- ------------------------------------------------------------ audit_log
CREATE TABLE audit_log (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor     text NOT NULL,
  action    text NOT NULL,
  target    text,
  detail    jsonb NOT NULL DEFAULT '{}',
  prev_hash text NOT NULL,
  hash      text NOT NULL
);

CREATE FUNCTION audit_chain_hash(prev text, at timestamptz, actor text, action text,
                                 target text, detail jsonb)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(sha256(convert_to(
    concat_ws('|', prev,
              to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
              actor, action, COALESCE(target, ''), detail::text), 'UTF8')), 'hex')
$$;

CREATE FUNCTION audit_log_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  prev text;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('ipo.audit_log'));
  SELECT hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
  NEW.prev_hash := COALESCE(prev, 'genesis');
  NEW.hash := audit_chain_hash(NEW.prev_hash, NEW.at, NEW.actor, NEW.action, NEW.target, NEW.detail);
  RETURN NEW;
END $$;

CREATE TRIGGER audit_log_insert BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_insert();

CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is insert-only' USING ERRCODE = 'IPO03';
END $$;

CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

-- Returns the id of the first row whose hash or link is wrong, or NULL when the chain is intact.
CREATE FUNCTION verify_audit_chain() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE
  r audit_log%ROWTYPE;
  expected_prev text := 'genesis';
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
    IF r.prev_hash <> expected_prev
       OR r.hash <> audit_chain_hash(r.prev_hash, r.at, r.actor, r.action, r.target, r.detail) THEN
      RETURN r.id;
    END IF;
    expected_prev := r.hash;
  END LOOP;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------- roles
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ipo_app') THEN
    CREATE ROLE ipo_app NOLOGIN;
  END IF;
END $$;

GRANT SELECT, INSERT, UPDATE ON users, teams, leases, routes, jobs, saga_steps, gateway_status,
  settings TO ipo_app;
GRANT SELECT, INSERT, UPDATE ON config_versions TO ipo_app;
GRANT DELETE ON routes, teams TO ipo_app;
GRANT SELECT, INSERT ON audit_log TO ipo_app;
GRANT EXECUTE ON FUNCTION verify_audit_chain() TO ipo_app;
