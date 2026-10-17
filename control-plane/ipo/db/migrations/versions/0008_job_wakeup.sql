-- Wake an idle worker the moment a job becomes runnable, instead of every worker polling the table.
-- The payload is empty: the notification only says "look at the queue". Retries scheduled for later
-- (run_after in the future) are found by the workers' periodic check.
CREATE FUNCTION notify_job_queued() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('ipo_jobs', '');
  RETURN NULL;
END $$;

CREATE TRIGGER jobs_queued AFTER INSERT ON jobs
  FOR EACH ROW WHEN (NEW.state = 'queued') EXECUTE FUNCTION notify_job_queued();
CREATE TRIGGER jobs_requeued AFTER UPDATE OF state ON jobs
  FOR EACH ROW WHEN (NEW.state = 'queued' AND OLD.state <> 'queued')
  EXECUTE FUNCTION notify_job_queued();
