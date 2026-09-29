# ipo-agent

The gateway-side agent of the Resilient IP Optimizer. It sits next to Traefik on each gateway VM,
accepts signed config from the control plane, and only lets Traefik see a config that has been
verified, validated and proven to route. Go, standard library only.

## Pipeline

`PUT /config` (or the pull loop) runs, in order, each stage traced with the request's
`X-Request-ID`:

1. **verify**: Ed25519 signature over `ipo-config\n{version}\n{sha256}\n{body}` (403 on failure).
2. **validate**: strict JSON, one `Host(...)` rule per router, unique hosts, backends inside the
   sandbox CIDR (or the canary), only allowed middlewares (422). A version at or below the live
   one is a replay (409); the same version with the same hash is acked as a no-op.
3. **stage**: write and fsync `staging.json`.
4. **swap**: atomically rename onto `live.yml`, which Traefik watches (Traefik's file provider refuses a `.json` name; JSON is valid YAML, so the body stays JSON).
5. **converge**: wait until Traefik's API lists the new routers.
6. **probe**: request every route changed since the live config, plus the canary.
7. **commit**: hard-link to `lkg-<version>.json` and drop older ones, or **rollback** to the last
   known good file (422). Rollback and commit need no free disk space.

On start `live.yml` is reset to the newest `lkg-*` file (or `{"http":{"routers":{},"services":{}}}`), so a process killed
mid-swap resumes from the last verified config.

## Other endpoints

`GET /status`, `GET /metrics` (Prometheus text), `GET /healthz`, and `POST`/`DELETE /fault`
(admin client-cert CN only) which write the keepalived track file so a gateway can be failed on
purpose. VRRP state comes from the keepalived notify script through `IPO_VRRP_STATE_FILE`.

## Running

Configuration is by environment, see `cmd/ipo-agent/main.go`. `cmd/dev-gateway` runs the agent in
front of an in-process fake Traefik, for tests and demos:

    go test -race ./...
    go run ./cmd/dev-gateway -pubkey <base64 ed25519 public key>

`deploy/` holds example Traefik, keepalived and systemd files. They have **not** been run against
real Traefik or keepalived.

## Known limits

- The agent logs in to the control plane with an operator account; a dedicated `gateway` role
  would be tighter.
- An agent-side rollback is a permanent 422, so the control plane marks that version `rejected`
  until the next route change.
- Probing the changed routes means one slow-booting new backend can roll back a whole config and
  hold up other route changes.
- Converge compares router names, so a router whose backend changed under the same name is
  only proven by the probe, which cannot tell the old backend from the new one.
- The probe treats a 404 from `GET /` as "no route", so an application that answers 404 at its
  root fails the probe and rolls the config back.
- A version the agent refused is not pulled again until the control plane serves a different
  version or hash; a transient local failure therefore waits for the next push or route change.
- Middleware definitions are limited to `headers`, `rateLimit` and `compress`.
- Not verified here: real Traefik file-provider reload timing, keepalived failover timing, real
  mTLS certificate provisioning.
