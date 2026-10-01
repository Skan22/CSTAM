# Getting started

Run the control plane, the dashboard and the tests on a laptop, with no OpenStack account.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Python and [uv](https://docs.astral.sh/uv/) | 3.12+ | control plane, system tests, Pulumi program |
| Go | 1.24+ | gateway agent |
| Node.js and npm | current LTS | dashboard |
| PostgreSQL server binaries (`initdb`, `pg_ctl`) | 16 or newer | database (the tests start a private cluster themselves) |
| `unshare`, `nft`, `ip` (Linux with unprivileged user namespaces) | | the chaos and isolation labs |
| `chromium` | optional | rendering the diagrams and screenshots |

No root is needed for anything below.

## 1. Run the control plane

With `IPO_CLOUD=fake` it uses an in-memory cloud and in-process gateway doubles, so a team
registration works end to end.

```bash
# a throwaway database
initdb -D /tmp/ipo-pg -U postgres --auth=trust -E UTF8
pg_ctl -D /tmp/ipo-pg -o "-p 5544 -k /tmp" -l /tmp/ipo-pg.log -w start
psql -h /tmp -p 5544 -U postgres -c "CREATE DATABASE ipo"

cd control-plane
uv sync
export IPO_DATABASE_URL="postgresql://postgres@/ipo?host=/tmp&port=5544"
export IPO_PLATFORM=../platform.yaml IPO_CLOUD=fake IPO_MIGRATE=1
export IPO_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export IPO_ADMIN_EMAIL=admin@example.com IPO_ADMIN_PASSWORD=change-me-please
uv run python -m ipo.main            # API on http://127.0.0.1:8000
```

Then, in another shell:

```bash
curl localhost:8000/readyz
TOKEN=$(curl -s -X POST localhost:8000/v1/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"change-me-please"}' | jq -r .access_token)
curl -s -X POST localhost:8000/v1/teams -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' -d '{"slug":"alpha"}'
curl -s localhost:8000/v1/teams -H "authorization: Bearer $TOKEN"   # alpha becomes active in seconds
curl -s localhost:8000/v1/pool -H "authorization: Bearer $TOKEN"
```

The interactive API documentation is at `/docs`. All settings are environment variables; the
list is in [configuration](configuration.md).

## 2. Run the dashboard

```bash
cd dashboard
npm ci
npm run dev        # http://127.0.0.1:5173, proxies /v1 to http://127.0.0.1:8000
```

Sign in with the admin account above. `npm run typecheck && npm test` check it;
`npm run gen:api` regenerates the typed API client after the API changes.

## 3. Run a gateway on its own (optional)

`cmd/dev-gateway` is the real agent pipeline in front of a fake Traefik, for trying the config
path without Traefik or keepalived:

```bash
cd gateway-agent
go test -race ./...
go run ./cmd/dev-gateway -pubkey <the control plane's base64 Ed25519 public key>
```

## 4. Run the tests

```bash
# control plane: starts its own PostgreSQL cluster; add IPO_TEST_DATABASE_URL to use one you have
(cd control-plane && uv run ruff check . && uv run mypy && uv run pytest -q)
(cd gateway-agent && go vet ./... && go test -race ./...)
(cd dashboard && npx tsc -b && npx vitest run)
(cd infra/pulumi && uv run pytest -q)

# system tests
cd tests
uv sync --group deploy
sh lab/fetch-tools.sh              # Traefik and keepalived for the failover lab
sh lab/fetch-tools.sh validators   # Loki, Tempo, Alloy, Caddy, amtool, Quadlet for deploy/
uv run pytest                      # deploy checks and lab-free tests
uv run python -m chaos.run         # failover, hot reload, traffic: about 4 minutes
uv run python -m isolation.run     # network isolation: about 90 seconds
```

[Testing](testing.md) explains what each suite proves.

## 5. Watch it work

```bash
cd tests
uv run python -m chaos.demo          # interactive dashboard on the real gateway software
uv run python -m chaos.demo --auto   # the same, narrated and hands-off
```

Press `r` to register a team and watch its subdomain start answering with no restart, `f` to fail
over, `k` to power off the primary's VM, `x` to cut the heartbeat and cause a split brain, `h` to
heal. With `--clouds <clouds.yaml>` the demo first builds the real cloud with `pulumi up`.

## Where to go next

| I want to | Read |
| --- | --- |
| understand the design | [system architecture](../architecture/system-architecture.md) |
| deploy it | [deployment](deployment.md) |
| change a setting | [configuration](configuration.md) |
| run it in production | [operations](operations.md) |
