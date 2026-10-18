# API integration guide

How to drive the platform from code: authenticate, register teams, follow a provisioning job, read
traffic and state, and what every error means. The machine-readable contract is
[`openapi.json`](openapi.json) (regenerate with `uv run python -m ipo.api.export ../docs/api` in `control-plane/`) and a
[Postman collection](ipo.postman_collection.json); the control plane also serves `/docs` (Swagger
UI) and `/openapi.json` itself.

- [Concepts](#concepts) · [Authentication](#authentication) · [Conventions](#conventions) ·
  [Errors](#errors)
- [Walkthrough: register, use, extend, delete](#walkthrough)
- [Endpoint reference](#endpoint-reference)
- [Events (server-sent)](#events-server-sent)
- [Gateway agent API](#gateway-agent-api) · [DNS and routing](#dns-and-routing) ·
  [Cloud (OpenStack) calls](#cloud-openstack-calls)
- [Client recipes](#client-recipes) · [Security notes for integrators](#security-notes-for-integrators)

## Concepts

| Term | Meaning |
| --- | --- |
| **Team** | One sandbox: a name (`slug`), an address from the pool, a VM and a subdomain `<slug>.<domain>`. |
| **Lease** | The claim on one address. States: `free → pooled → leased → draining → quarantined → free`. Enforced by a database trigger, so no code path can skip one. |
| **Warm / cold path** | A *warm* registration claims a VM that is already booted in the pool (about a second end to end). A *cold* one reserves an address and boots a VM first (a boot's worth longer). The response says which. |
| **Job** | Every registration and teardown is an asynchronous saga: you get `202 Accepted` and a job to follow. |
| **Config version** | The routing table the gateways serve, compiled from the database, signed, and pushed to both gateways. Adding or removing a team makes a new version. |

```
POST /v1/teams ──202──▶ job: claim → provision → personalize → route → dns → converge → probe → activate
                         └─ the team is `active` once both gateways serve it and the VIP answered its probe
DELETE /v1/teams/{id} ─202─▶ job: drain → await_route_removal → unpublish_dns → release → finalize
```

## Authentication

`POST /v1/auth/login` with an email and password returns a bearer token (HS256 JWT, 15 minutes).
Send it as `Authorization: Bearer <token>`. There are no long-lived API keys; log in again when a
call answers `401`.

```bash
TOKEN=$(curl -s https://ipo.example/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"operator@example.com","password":"…"}' | jq -r .access_token)
```

```json
{"access_token": "eyJ…", "token_type": "bearer", "expires_in": 900, "role": "operator"}
```

| Role | Can |
| --- | --- |
| `viewer` | read: teams, jobs, pool, IPAM, gateways, traffic, events |
| `operator` | viewer + register, delete and extend teams |
| `admin` | operator + settings, config versions and rollback, audit log, manual failover, team logins |
| `team` | a single team's own login: `/v1/me`, `/v1/me/traffic`, `/v1/me/traffic/recent`, `/v1/me/events`. It is refused everywhere else, and never sees another team |
| `gateway` | a gateway agent's login, bound to one gateway: its own `heartbeat` and `traffic` routes and `GET /v1/config/latest`. Refused everywhere else and for the other gateway |

Each role includes the ones before it, except `team`, which is outside the ladder. A team token
stops working as soon as its team is deleted, even before it expires.

Browsers' `EventSource` cannot set headers, so the two event streams also accept
`?access_token=<token>`. That is offered only there, because URLs end up in logs.

Public (no token): `GET /healthz`, `GET /readyz`, `GET /metrics`, `POST /v1/auth/login`,
`GET /openapi.json`. The management network is the only place these are reachable in a deployment.

## Conventions

| | |
| --- | --- |
| Format | JSON in and out (`Content-Type: application/json`). Unknown request fields are rejected with `422`. |
| Times | RFC 3339 with offset, e.g. `2026-10-17T11:09:18.490773+01:00`. |
| Ids | UUIDs for teams and jobs. |
| Request id | Send `X-Request-ID` (printable, up to 128 characters) or one is made; it is echoed on the response and stored as the job's `trace_id`, so a request can be followed into the logs and the saga. |
| **Idempotency** | `POST /v1/teams` **requires** an `Idempotency-Key` header (1–200 characters). Repeating a call with the same key returns the original answer with `Idempotent-Replayed: true` and creates nothing. The same key for a *different* team is `409`. Generate one key per intended team and reuse it on every retry. |
| Asynchrony | `202` means accepted, not done. Poll `GET /v1/jobs/{id}` or `GET /v1/teams/{id}`, or listen to [events](#events-server-sent). |
| Retry hints | `429` and `503` carry `Retry-After: 30`. |
| Pagination | None: the lists are small (hundreds of rows at most). `GET /v1/teams` filters with `?state=` and `?include_deleted=true`. |

## Errors

Every error is `application/problem+json` (RFC 9457):

```json
{"type": "https://ipo.felcloud.tn/problems/slug-taken", "title": "Conflict", "status": 409,
 "detail": "the name 'alpha' is already taken", "instance": "/v1/teams"}
```

Validation errors add `errors: [{"loc": ["body", "slug"], "msg": "…"}]`. Branch on `type`, not on
`detail`.

| `type` suffix | Status | When | What to do |
| --- | --- | --- | --- |
| `idempotency-key` | 400 | `POST /v1/teams` without a usable `Idempotency-Key` | send one |
| `unauthorized` | 401 | no, bad, expired or revoked token; wrong login | log in again |
| `forbidden` | 403 | your role is too low, or a team token on a staff route | use a suitable account |
| `not-found` | 404 | no such team, job, version or gateway | — |
| `slug-taken` | 409 | the name exists (names of deleted teams are never reused) | pick another name |
| `idempotency-conflict` | 409 | the key was used for a different team | use a fresh key |
| `team-busy` | 409 | deleting a team that is still being registered | wait for its job, then delete |
| `team-not-active` | 409 | extending a team that is not `active` | — |
| `email-taken` | 409 | creating a team login for an existing email | — |
| `no-master` | 409 | manual failover while no gateway is MASTER | — |
| `validation` | 422 | malformed body, bad slug (`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`), out-of-range setting | fix the request |
| `quota-guard` | 429 | the registration would leave fewer free addresses than `reserve_free_ips` | retry later, or free teams |
| `pool-exhausted` | 503 | no address is free | retry later |
| `agent-unreachable` | 502 | a gateway agent did not accept a call (failover) | check the gateway |
| `not-implemented` | 501 | failover on a deployment without agent clients | — |

A registration whose *saga* fails after being accepted is not an HTTP error: the job ends
`compensated`, the team `failed`, and everything it created (route, DNS record, VM, port) is undone.
Read `last_error` on the job.

## Walkthrough

```bash
API=https://ipo.example
H="Authorization: Bearer $TOKEN"

# 1. register (idempotent: safe to retry with the same key)
curl -s -X POST $API/v1/teams -H "$H" -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: team-alpha-2026-10-17' -d '{"slug":"alpha"}'
```
```json
{"job_id": "5b0e…", "team_id": "8f3c…", "ip": "10.20.0.14", "path": "warm",
 "subdomain": "alpha.cstam.felcloud.tn", "status_url": "/v1/jobs/5b0e…"}
```

```bash
# 2. follow it
curl -s $API/v1/jobs/5b0e… -H "$H" | jq '{state, steps: [.steps[] | {step, status}]}'
curl -s $API/v1/teams/8f3c… -H "$H" | jq .state          # "pending" → "active"
```
```json
{"state": "succeeded",
 "steps": [{"step":"claim","status":"done"},{"step":"provision","status":"done"},
           {"step":"personalize","status":"done"},{"step":"route","status":"done"},
           {"step":"dns","status":"done"},{"step":"converge","status":"done"},
           {"step":"probe","status":"done"},{"step":"activate","status":"done"}]}
```

```bash
# 3. use it: the gateway VIP routes by Host header (or resolve the name via DNS)
curl -s http://<VIP>/ -H 'Host: alpha.cstam.felcloud.tn'

# 4. give the team a login that sees only its own traffic (admin)
curl -s -X POST $API/v1/teams/8f3c…/users -H "$H_ADMIN" -H 'Content-Type: application/json' \
     -d '{"email":"alpha@team.example","password":"at least 12 characters"}'

# 5. extend the lease by an hour (only while active)
curl -s -X POST $API/v1/teams/8f3c…/extend -H "$H" -H 'Content-Type: application/json' -d '{"seconds":3600}'

# 6. delete (or just let the lease expire; the reaper does the same within seconds)
curl -s -X DELETE $API/v1/teams/8f3c… -H "$H"
```

Deleting twice is harmless: while the teardown runs you get the same job back, and afterwards `200`
with the team in state `deleted`.

## Endpoint reference

Role is the minimum needed. `{id}` is a UUID.

### Auth

| Method and path | Role | Body → response |
| --- | --- | --- |
| `POST /v1/auth/login` | public | `{email, password}` → `{access_token, token_type, expires_in, role}` |
| `GET /v1/me` | any signed-in | → `{email, role, team}`; `team` is set for team accounts |

### Teams and jobs

| Method and path | Role | Body → response |
| --- | --- | --- |
| `POST /v1/teams` | operator | `{slug}` + `Idempotency-Key` → `202 {job_id, team_id, ip, path, subdomain, status_url}` |
| `GET /v1/teams` | viewer | `?state=pending\|active\|draining\|deleted\|failed&include_deleted=` → `[Team]` |
| `GET /v1/teams/{id}` | viewer | → `Team {id, slug, subdomain, state, owner, ip, created_at, expires_at}` |
| `DELETE /v1/teams/{id}` | operator | → `202 {job_id, team_id, status_url}`, or `200 Team` if already deleted |
| `POST /v1/teams/{id}/extend` | operator | `{seconds: 1–86400}` → `Team` with the later `expires_at` |
| `POST /v1/teams/{id}/users` | admin | `{email, password(≥12)}` → `201 {email, team_id}` |
| `GET /v1/jobs/{id}` | viewer | → `Job {id, kind, state, team_id, attempts, last_error, trace_id, steps[]}` |

Team `state`: `pending` (registering) → `active` → `draining` → `deleted`, or `failed`.
Job `state`: `queued`, `running`, `succeeded`, `compensated` (failed and undone), `failed`.
Each step has `status` (`running`, `done`, `failed`, `undone`), timestamps and a `result`.

### Pool and addresses

| Method and path | Role | Response |
| --- | --- | --- |
| `GET /v1/pool` | viewer | `{size, target, free, leased, draining, quarantined}`: warm VMs ready and address counts |
| `GET /v1/ipam` | viewer | `{total, counts, leases: [{ip, state, team, expires_at, quarantined_until}]}` |

### Gateways and config

| Method and path | Role | Notes |
| --- | --- | --- |
| `GET /v1/gateways` | viewer | `{gateways: [{gateway, vrrp_state, live_version, last_heartbeat}], split_brain}` |
| `POST /v1/gateways/failover` | admin | `{}` → `202 {gateway}`: the MASTER gives up the VIP on purpose |
| `POST /v1/gateways/{name}/heartbeat` | gateway (its own), operator | agents: `{vrrp_state, live_version}` → `204` |
| `POST /v1/gateways/{name}/traffic` | gateway (its own), operator | agents: access-log counters → `204` |
| `GET /v1/config/latest` | gateway, operator | the newest signed version `{version, sha256, signature, body}`; agents pull it if a push was missed |
| `GET /v1/config/versions` | admin | `{versions: [{version, sha256, status, created_at}], pinned}` |
| `GET /v1/config/versions/{n}` | admin | the version with its signature and body |
| `POST /v1/config/rollback` | admin | `{version: n}` pins the config to an earlier body (re-issued under a new, higher version); `{version: null}` unpins |

### Traffic

| Method and path | Role | Response |
| --- | --- | --- |
| `GET /v1/traffic?window_minutes=60` | viewer | `{window_minutes, totals, teams[], series[]}` per-team requests, status classes, bytes, mean latency |
| `GET /v1/traffic/recent?limit=50` | viewer | the last requests: time, gateway, host, method, path (never the query string or client address), status, duration |
| `GET /v1/me/traffic`, `GET /v1/me/traffic/recent` | team | the same, for the caller's own team only |

### Administration

| Method and path | Role | Notes |
| --- | --- | --- |
| `GET /v1/settings` | admin | `{values, history}` |
| `PATCH /v1/settings` | admin | any of `pool_target (0–200)`, `reserve_free_ips`, `lease_ttl_seconds (60–604800)`, `quarantine_seconds (0–86400)`, `max_parallel_boots (1–20)`, `demand_window_seconds (0–86400)`; applies at once, no restart, audited |
| `GET /v1/audit` | admin | `{chain: {valid, first_bad_id}, entries[]}`: the hash-chained audit log and its integrity check |
| `GET /v1/ui` | viewer | dashboard configuration (the Grafana URL to embed) |

### Operations

| Path | Notes |
| --- | --- |
| `GET /healthz` | process is up |
| `GET /readyz` | the database answers (`503` otherwise) |
| `GET /metrics` | Prometheus: `ipo_provision_duration_seconds`, `ipo_saga_step_duration_seconds`, `ipo_reconciler_repairs_total`, `ipo_api_request_duration_seconds`, lease and pool gauges |

## Events (server-sent)

`GET /v1/events` (viewer) and `GET /v1/me/events` (team: only its own team's events) stream
`text/event-stream`, built on PostgreSQL `LISTEN/NOTIFY`, so every replica emits every event:

```
: connected

id: 1
event: team.active
data: {"kind":"team.active","team_id":"8f3c…","slug":"alpha","ip":"10.20.0.14","host":"alpha.cstam.felcloud.tn"}
```

Kinds: `team.active`, `team.draining`, `team.deleted`, `team.failed`, `lease.changed` (`ip`, `from`, `to`),
`gateway.vrrp` (`from`, `to`), `gateway.split_brain`, `gateway.split_brain_resolved`,
`alert.vm_gone`, `traffic.batch` (a nudge that hosts served requests). A `: keepalive` comment arrives
every 15 s; reconnect on close. `id` restarts at 1 per connection, so there is no replay: after a
reconnect, list the resource you care about.

## Gateway agent API

Each gateway runs `ipo-agent` on `:8443` (management network, mutual TLS, or plain HTTP in the
lab with `IPO_AGENT_INSECURE=1`). It is called by the control plane, not by users.

| Call | Who | Does |
| --- | --- | --- |
| `POST /probe` | control plane certificate only | `{host}` → `{ok, routed, error}`: request the host once through this gateway's Traefik. Registration uses it to learn that a new team's sandbox serves, since the management network has no path to the VIP |
| `PUT /config` | control plane | deliver `{version, sha256, signature, body}`; verified (Ed25519, version newer than the live one, policy rules), staged, swapped in, and only committed after Traefik serves it and the changed routes plus a canary answer. Otherwise rolled back, and the answer says why. A new team's route whose VM does not answer yet (502/504) is not a reason to roll back: the route is loaded; only a missing route (404) or a failing canary is |
| `GET /status` | anyone with a client certificate | live version, VRRP state, faulted, uptime |
| `POST /fault`, `DELETE /fault` | admin certificate only | give up MASTER on purpose / rejoin |
| `GET /metrics`, `GET /healthz` | scrapers | Prometheus text; liveness |

The agent in turn logs in to the control plane, sends heartbeats every 5 s and traffic counters
every 10 s, and pulls `GET /v1/config/latest` every 5 s as a backstop to pushes.

## DNS and routing

A request reaches a team by its **`Host` header** at the gateways' VIP. Making a name resolve to
that VIP is the integrator's choice, and the platform supports both:

| Mode (`IPO_DNS`) | You provide | The platform does |
| --- | --- | --- |
| `wildcard` (default) | one record `*.<domain> → floating IP`, once | nothing per team: nothing to publish and nothing to leak. FelCloud has no DNS service, so this is the default |
| `rfc2136` | an authoritative server that accepts dynamic updates for the zone, a TSIG key, and the record value (`IPO_DNS_SERVER`, `IPO_DNS_TARGET`, `IPO_DNS_TSIG_NAME`, `IPO_DNS_TSIG_SECRET`) | the `dns` saga step publishes `<slug>.<domain> A <target>`; `unpublish_dns` and the reconciler withdraw it; works with BIND, Knot, PowerDNS, CoreDNS |

## Cloud (OpenStack) calls

The control plane touches only resources it tagged `ipo`, through one interface (`Cloud`):
`ensure_port`, `ensure_server`, `set_server_metadata`, `clear_server_metadata`, `delete_server`,
`delete_port`, `list_ports`, `list_servers`. Every call is idempotent and names resources
deterministically (`ipo-vm-<ip>`, `ipo-port-<ip>`), so a retry or a crash between creating and
recording a resource cannot leave an orphan; the reconciler deletes strays older than 30 s.
Credentials come from `clouds.yaml` (`OS_CLOUD`).

## Client recipes

A minimal Python client that registers a team, waits for it to serve traffic and cleans up:

```python
import httpx, time, uuid

API = "https://ipo.example"
c = httpx.Client(base_url=API, timeout=10)
tok = c.post("/v1/auth/login", json={"email": "operator@example.com", "password": "…"}).json()
c.headers["Authorization"] = f"Bearer {tok['access_token']}"

key = str(uuid.uuid4())                      # one per intended team; reuse on retry
def register(slug):
    for attempt in range(5):
        r = c.post("/v1/teams", json={"slug": slug}, headers={"Idempotency-Key": key})
        if r.status_code in (429, 503):      # no capacity right now
            time.sleep(int(r.headers.get("Retry-After", 30))); continue
        r.raise_for_status()
        return r.json()

reg = register("alpha")
while (t := c.get(f"/v1/teams/{reg['team_id']}").json())["state"] == "pending":
    time.sleep(0.2)
assert t["state"] == "active", c.get(reg["status_url"]).json()["last_error"]
print(httpx.get("http://<VIP>/", headers={"Host": reg["subdomain"]}).text)
c.delete(f"/v1/teams/{reg['team_id']}")
```

Ready-made drivers for load and benchmarking live in `tests/load/`:
`burst.py` registers *N* teams at the same instant and records, per team, the time to the API's
answer, to `active`, and to the first successful request through the VIP, and checks that every
team got a different address and was served by its own sandbox
(`uv run python -m load.run`, see [testing](../guides/testing.md)).

## Security notes for integrators

- Tokens are bearer credentials: keep them out of URLs except the event streams' `access_token`,
  and out of logs. They live 15 minutes.
- Passwords are stored as argon2id hashes; login takes the same time for unknown accounts.
- A team token is useless after its team is deleted, and a deleted team's login, traffic history
  and route are purged; the audit log (a hash chain, verified by `GET /v1/audit`) keeps only that
  the team existed and what was purged.
- Gateway agents authenticate with a `gateway` account bound to their own gateway, so a
  compromised gateway can report only for itself and read only the config it would be sent anyway.
- Every team's route is rate-limited by Host (200 requests/s, burst 100); over it, clients get 429.
- The API validates every body strictly (extra fields are errors) and bounds list sizes; traffic
  reports are capped at 500 hosts and 200 recent requests per report.
