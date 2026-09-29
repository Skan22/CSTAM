-- The dashboard's IPAM grid must show every lease state change within a second, including those
-- no API call causes (warming the pool, quarantine expiring). The trigger is the one place all
-- of them pass through. NOTIFY is delivered on commit, so a rolled-back change is never announced.
CREATE FUNCTION leases_announce() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('ipo_events', json_build_object(
    'kind', 'lease.changed', 'ip', host(NEW.ip), 'from', OLD.state, 'to', NEW.state)::text);
  RETURN NULL;
END $$;

CREATE TRIGGER leases_announce AFTER UPDATE ON leases
  FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
  EXECUTE FUNCTION leases_announce();

-- The same for a gateway's config version, which changes through pushes and heartbeats alike.
CREATE FUNCTION gateway_status_announce() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('ipo_events', json_build_object(
    'kind', 'gateway.config', 'gateway', NEW.gateway,
    'from', CASE WHEN TG_OP = 'UPDATE' THEN OLD.live_version END,
    'to', NEW.live_version)::text);
  RETURN NULL;
END $$;

CREATE TRIGGER gateway_status_announce AFTER UPDATE ON gateway_status
  FOR EACH ROW WHEN (OLD.live_version IS DISTINCT FROM NEW.live_version)
  EXECUTE FUNCTION gateway_status_announce();

-- A gateway's first row already carries the version it runs.
CREATE TRIGGER gateway_status_announce_insert AFTER INSERT ON gateway_status
  FOR EACH ROW WHEN (NEW.live_version IS NOT NULL)
  EXECUTE FUNCTION gateway_status_announce();
