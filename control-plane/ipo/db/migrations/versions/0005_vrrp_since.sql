-- When each gateway last changed VRRP state. Two rows saying MASTER are a split brain only if
-- both gateways have reported since the later of the two promotions; a gateway that died as
-- MASTER never reports again, so it cannot be mistaken for a second one.
ALTER TABLE gateway_status ADD COLUMN vrrp_since timestamptz;
UPDATE gateway_status SET vrrp_since = coalesce(last_heartbeat, now());

CREATE FUNCTION gateway_status_vrrp_since() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR OLD.vrrp_state IS DISTINCT FROM NEW.vrrp_state THEN
    NEW.vrrp_since := now();
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER gateway_status_vrrp_since BEFORE INSERT OR UPDATE ON gateway_status
  FOR EACH ROW EXECUTE FUNCTION gateway_status_vrrp_since();
