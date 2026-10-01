# Testing

The aim is that every claim in these documents is backed by something that can be re-run. Tests
are layered by what they need: unit and integration tests run anywhere; the labs need Linux user
namespaces and real binaries but no root; the deploy checks need the pinned config readers; and
a few runs touched the real cloud.

## Suites

| Suite | Where | Proves | Count |
| --- | --- | --- | --- |
| Control plane | `control-plane/tests` | state machine, sagas and their crash recovery, reconciler rules, config compiler and signing, roles, team isolation of the API, traffic store, metrics, OpenAPI export; runs against a real PostgreSQL | 309 (+1 skipped) |
| Gateway agent | `gateway-agent/internal/*` | signature, policy, atomic swap, rollback, VRRP file handling, traffic parsing, with `-race` | 8 packages |
| Dashboard | `dashboard/src` | pages, role-based navigation, session handling, Grafana embedding | 72 |
| Pulumi | `infra/pulumi/tests` | the rules module against the spec, every CrossGuard policy, the whole program under Pulumi's mocks | 28 |
| Deploy | `tests/deploy` | every Ansible template rendered per host and read by its real program (nft, keepalived, sshd, systemd, Quadlet generator, Caddy, promtool, amtool, Loki, Tempo, Alloy, PostgreSQL, Grafana) | 67 |
| Chaos lab | `tests/chaos` | failover, hot reload, traffic, split brain on real keepalived, Traefik and agents | 46 |
| Isolation lab | `tests/isolation` | every source, destination, address and port against `security-groups.yaml`, both layers and each alone | 35 |
| System, lab-free | `tests/` | spec cross-checks, oracle, event parsing, dashboard rendering | 99 with `deploy` (49 lab tests skip outside the lab) |

Static checks everywhere: `ruff`, `mypy --strict`, `go vet`, `tsc`, `ansible-lint` (production
profile), `promtool`, JSON Schema for `platform.yaml` and `security-groups.yaml`.

## The labs

Both run inside `unshare --user --map-root-user --net --mount`, so they need no root and leave
nothing behind.

**Chaos lab** (`uv run python -m chaos.run`, about 4 minutes): two gateway namespaces each running
real keepalived 2.4, Traefik 3.7 and the real `ipo-agent`, a sandbox backend answering on every
pool address, and the real control plane with a fake cloud and real PostgreSQL. A load generator
opens 50 new connections per second through the VIP; a "gap" is the longest stretch with no
success. Scenarios: VM death (10 runs), Traefik SIGKILL and SIGTERM (10 runs), keepalived stop,
old MASTER returning, manual failover through the agent, a partition that creates two MASTERs,
steady state with no failures, and per-team traffic from Traefik's access log to the API.

| Scenario | Worst request gap |
| --- | --- |
| VM death (11 runs) | 0.52 to 0.66 s |
| Traefik crash (12 runs) | 1.2 to 1.7 s |
| keepalived stopped | 0.12 s |
| Manual failover | 0.12 s |
| Old MASTER returns | 0 s |

**Isolation lab** (`uv run python -m isolation.run`, about 90 s): namespaces for every machine
and for the internet, router namespaces that forward and map the floating IPs, and two
enforcement layers built from the same spec by different code: the nftables the `hardening`
role renders on each host, and a bridge-level emulation of Neutron port security and security
groups. A probe dials every other machine on every address it has and 17 ports (1,683 probes per
pass); the policy opens 105 of them, which act as positive controls. It also forges source MAC
and IP, forges VRRP adverts, reroutes through every neighbour, floods the relay, and turns each
layer off in turn. Results: [isolation report](../reports/isolation.md).

**Deploy checks** (`uv run pytest tests/deploy`): the role templates are rendered with Ansible's
variable precedence and handed to the program that reads them in production, at the pinned
version (`sh lab/fetch-tools.sh validators` fetches them). Grafana is started behind the real
Caddy front and the embedding is exercised through it.

## Real-cloud runs

`pulumi preview`, `up` and `destroy` were run against the team's FelCloud project several times
(from an empty project: 79 resources planned, VMs ACTIVE in about 45 s, destroy in about 30 s),
and the demo drives the same. The Ansible roles, VRRP between real OpenStack VMs, and replication
have **not** been run on real hosts.

## What running the real software found

Each of these passed its unit tests and failed against the real thing, and now has a regression
test:

| Found by | Problem | Fix |
| --- | --- | --- |
| chaos lab | Traefik's file provider refuses a `.json` filename and gives up if the file is missing or empty at start | `live.yml`, a non-empty seed, and the agent starts first |
| chaos lab | `/status` reported `faulted: true` on every gateway: keepalived creates its track file holding `0` and the agent treated existence as a fault | only a non-zero value is a fault |
| isolation lab | sandboxes lost the gateways' web ports: replies to edge-sourced traffic left by the sandbox port and port security dropped them | `gateway_network` role (policy route) |
| isolation lab | with security groups alone, a sandbox could push one-way UDP to the relay's management address through a neighbour's port | destination-pinned host firewall rules |
| deploy checks | Quadlet re-quoted an inline `HealthCmd` into one that always failed, which would restart a healthy container forever | `python -m ipo.healthcheck` |
| deploy checks | keepalived ignores a fault file that does not exist at start | tmpfiles entry creates it |
| real cloud | hard anti-affinity put gateway and control-plane VMs in ERROR | soft anti-affinity by default |
| real cloud | the external network had no free floating IP | `floatingIps` switch; the demo checks first |
| real cloud | the project had no key pair | the program uploads one |

## Not tested

Load and throughput at scale (the plan's benchmark matrix), concurrent registration against the
real cloud, the 100-cycle create-and-delete soak, and the plan's Molecule scenarios. Those are
listed in [SUBMISSION.md](../SUBMISSION.md#gaps).
