-- The reconciler repairs only what has been stuck longer than a grace period, so it needs to
-- know when a lease last changed. The clock is the database's, like every other comparison.
ALTER TABLE leases ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION leases_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END $$;
