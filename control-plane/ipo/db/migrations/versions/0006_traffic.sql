-- Traffic counters reported by the gateway agents from Traefik's access log. UNLOGGED: this is
-- telemetry, cheap to write and fine to lose in a crash (the dashboard just starts empty).
-- One row per minute, gateway and host; a report adds to its minute's row.
CREATE UNLOGGED TABLE traffic_buckets (
  bucket      timestamptz NOT NULL,
  gateway     text        NOT NULL,
  host        text        NOT NULL,
  requests    bigint      NOT NULL DEFAULT 0,
  s2xx        bigint      NOT NULL DEFAULT 0,
  s3xx        bigint      NOT NULL DEFAULT 0,
  s4xx        bigint      NOT NULL DEFAULT 0,
  s5xx        bigint      NOT NULL DEFAULT 0,
  bytes       bigint      NOT NULL DEFAULT 0,
  duration_ms bigint      NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, gateway, host)
);
CREATE INDEX traffic_buckets_host ON traffic_buckets (host, bucket);

-- The last few requests, for the "what just happened" list. No client address, query string or
-- header is ever stored.
CREATE UNLOGGED TABLE traffic_recent (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL,
  gateway     text        NOT NULL,
  host        text        NOT NULL,
  method      text        NOT NULL,
  path        text        NOT NULL,
  status      smallint    NOT NULL,
  duration_ms integer     NOT NULL
);
CREATE INDEX traffic_recent_host ON traffic_recent (host, id DESC);
