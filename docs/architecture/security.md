# Security architecture

What is protected from whom, by which mechanism, and what proves it. The network policy has one
source of truth (`security-groups.yaml`) rendered into two enforcers; everything else is layered
the same way: a second control behind each first one, and a test that breaks if either is removed.

- [Assets and trust boundaries](#assets-and-trust-boundaries)
- [Sandbox isolation](#sandbox-isolation) · [Defence in depth (network)](#defence-in-depth-network)
- [STRIDE by component](#stride-by-component)
- [Lifecycle security: what teardown guarantees](#lifecycle-security-what-teardown-guarantees)
- [Authentication and authorisation](#authentication-and-authorisation)
- [Secrets and keys](#secrets-and-keys) · [Data handling](#data-handling)
- [Abuse and resource limits](#abuse-and-resource-limits) · [Known gaps](#known-gaps)

## Assets and trust boundaries

| Asset | Why it matters |
| --- | --- |
| The control plane and its database | decides who gets an address, a VM, a route; holds the audit trail |
| The config signing key | whoever holds it can make the gateways serve any route |
| OpenStack credentials | create and delete VMs in the project |
| The gateways' management interface | accepts config; a compromised gateway sees all teams' traffic |
| Each team's sandbox | runs the team's own code: **untrusted by definition** |
| The management network | reaches everything above |

| Zone | Who | Reaches |
| --- | --- | --- |
| Internet | anyone | the floating IP (VIP) on 80/443; the bastion's floating IP on UDP 51820 |
| sandbox-net | team VMs, untrusted | the relay's telemetry ports; the gateways' public ports, like anyone |
| edge-net | the gateway pair and the VIP | - |
| mgmt-net | control plane, Postgres, CA, observability, agents, SSH | reached only through the bastion's WireGuard |

Trust assumptions: OpenStack's own isolation (hypervisor, Neutron) holds; the operators who hold
the Ansible secrets are trusted; sandbox VMs are hostile. Everything below is built so that a
fully compromised sandbox gains nothing beyond its own address.

## Sandbox isolation

The requirement: sandboxes cannot talk to each other, and cannot touch the gateway or anything on
the management network. Three independent mechanisms enforce it, each sufficient for part of the
threat and together for all of it:

| Mechanism | Where | What it stops |
| --- | --- | --- |
| **Neutron security groups** | the cloud fabric, per port | `sg-sandbox.sandbox` admits only `sg-gateway` on TCP 80, nothing from other sandboxes (a remote-group rule, not an address range). Management ports admit only the named groups |
| **Neutron port security** | the cloud fabric, per port | a sandbox sending with another's IP or MAC, an unused pool address or a gateway's address: dropped. This is the only thing between sandboxes at layer 2 and must stay on; the Pulumi policy pack refuses a port or network without it |
| **nftables on every host** | each platform host, rendered from the same spec by the `hardening` role | destination-pinned rules close the one leak the fabric alone has (a sandbox routing mgmt-net through the relay's sandbox address, delivering one-way UDP); per-source rate limits on shared telemetry; forged VRRP adverts dropped |

nftables is the iptables successor and is what the hosts run; eBPF was not used. The fabric rules
and the host rules are generated from the same file, so they cannot drift apart, and the suite
checks each layer alone. A sandbox's own firewall is never counted on: teams own their images.

**Evidence.** `tests/isolation` probes 1,683 address-and-port pairs across every machine in the
topology, of which the policy opens 105 (the positive controls), and checks verdicts from both
the probing and the listening end ([results](../reports/isolation.md)): sandbox to sandbox on any
port, sandbox to a gateway's management address or the agent port, sandbox to anything on mgmt-net
(API, Postgres, Grafana, SSH), forged source IP/MAC, rerouting, forged VRRP and flooding. The same
probes run against the real cloud ([real FelCloud run](../reports/isolation.md#real-felcloud-run)).

## Defence in depth (network)

| Threat | Cloud (Neutron) | Host | Verified by |
| --- | --- | --- | --- |
| Sandbox reaches another sandbox | security groups (`sg-sandbox.sandbox` admits only the gateways on 80) | none: teams own their images | isolation suite, real-cloud probes |
| Sandbox reaches the platform | security groups per port | nftables from the same spec, per destination address | isolation suite, each layer alone |
| Sandbox spoofs another's IP or MAC | port security | rp_filter only stops off-subnet sources | isolation suite; Pulumi policy keeps port security on |
| Sandbox routes around (weak host) | not stopped | destination-pinned rules | isolation suite (fabric alone leaks one-way UDP) |
| Forged VRRP takes the VIP | security groups | nftables | isolation suite |
| Sandbox floods shared telemetry | - | per-source nftables limits, then Alloy's per-stream limit | isolation suite (flood test) |
| Internet reaches anything but 80/443 | security groups | nftables | isolation suite; Pulumi policy pack |
| A rule mistake in one layer | the other layer | the other layer | isolation suite (injected rule) |

## STRIDE by component

S spoofing · T tampering · R repudiation · I information disclosure · D denial of service ·
E elevation of privilege. Each row is a threat, the control, and the test that would fail without it.

### Public edge: Traefik behind the VIP

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S | A client claims another team's `Host` | routing is by exact `Host(...)` match from the signed config; unknown hosts get 404 | `tests/load` (every host served by its own sandbox), chaos suite |
| T | A request rewrites the routing table | the table is a file only the agent writes; Traefik's API listens on loopback | deploy tests (unit hardening) |
| R | Who requested what | JSON access log per request (no headers; the agent forwards only host, method, path without query, status, duration, never the client address) | `test_traffic`, chaos traffic tests |
| I | Team A reads team B's traffic | team logins see only their own host (`/v1/me*`) | `test_team_access` walks the OpenAPI document, so a route added later is covered |
| D | One team floods the gateway | single shared capacity today; per-source limits protect only the shared telemetry ports | throughput measured in the [benchmarks](../reports/benchmarks.md); per-team limits are a [known gap](#known-gaps) |
| E | Traefik compromise | rootless Quadlet, no capabilities, read-only root, `NoNewPrivileges` | deploy tests (`systemd-analyze security` ≤ 2.0) |

### Gateway agent (`ipo-agent`)

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S | Anyone pushes a config | mutual TLS; and the body must carry a valid **Ed25519 signature** from the control plane for a version newer than the live one | Go agent tests, `test_agent_contract` |
| T | A modified or replayed config | signature covers version, hash and body; stale versions refused; the agent validates the body against its own policy before staging: one `Host(...)` rule per router, no duplicate hosts, only allowed middlewares, and **every backend inside the sandbox CIDR** (so even a compromised control plane cannot point a gateway elsewhere); atomic rename; **probe, then commit or roll back to last-known-good** | pipeline tests including crash at each stage |
| R | Which config served what | every version, its hash, signature and the acks are kept in `config_versions` and `gateway_status`; spans per pipeline stage | `test_compiler` |
| I | Metrics or status leak | `/metrics` and `/healthz` carry no team data; `/status` needs a client certificate | agent server tests |
| D | A bad config takes the gateway down | the canary route and changed-route probes gate the commit; a failed check restores the last-known-good config automatically | chaos suite, [rollback benchmark](../reports/benchmarks.md) |
| E | A sandbox reaches `:8443` | security group admits only `sg-control`; `POST /fault` needs the admin certificate's CN | isolation suite, agent tests |

### Control plane API

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S | Forged or stolen token | HS256 JWT signed with a ≥ 32-byte secret, 15-minute life, role in the claim; a **team token is checked against the database on every call**, so deleting the team or its login ends it at once | `test_api`, `test_lifecycle` |
| T | Tampering with state through the API | strict models (extra fields rejected), role ladder per route, idempotency keys; state transitions enforced by a **database trigger**, not application code | `test_lease_state_machine`, hypothesis tests |
| R | Denying an action | every mutation is written to a **hash-chained audit log** (each row hashes the previous); `GET /v1/audit` verifies the chain; settings changes name the actor | `test_schema`, `test_lifecycle` (chain intact after deletes) |
| I | Account enumeration; data of other teams | one error and the same timing for unknown accounts and wrong passwords (argon2id with a dummy verify); team routes scoped by `team_id` | `test_bad_credentials_are_indistinguishable`, `test_team_access` |
| D | Registration flood exhausts addresses or the quota | `reserve_free_ips` guard answers 429 before the pool is empty; idempotency stops retry storms creating duplicates; the instance quota backs the warm pool off exponentially | `test_ipam_concurrency`, `test_concurrent_registration` |
| E | Team or viewer escalates | `team` is outside the role ladder and refused on every staff route; deny-by-default test walks every path | `test_every_route_rejects_requests_without_a_valid_token`, `test_team_access` |

### Database (PostgreSQL)

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S/E | Another host talks to Postgres | the security group admits only `sg-control` on mgmt-net, and `pg_hba.conf` lists the two control-plane addresses by `/32` with `scram-sha-256` | deploy tests render and load the real `pg_hba.conf` |
| T | Silent edits to history | audit chain detects changes; the lease trigger refuses illegal transitions and deletes | `test_schema` |
| I | Credential exposure | passwords are SCRAM-hashed and reach `psql` on stdin, never argv; nightly dumps go to an rclone remote that needs its own credentials | `restore-backup` runbook |
| D | Loss of the primary | streaming replica, promotion runbook; the **gateways keep serving from the last verified config with the control plane down** | [measured: control plane killed under load, 0 failed requests](../reports/benchmarks.md) |

### Sandbox VMs and the cloud adapter

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S | A sandbox impersonates another or a gateway | port security (MAC/IP), `sg-sandbox` has no ingress from other sandboxes | isolation suite |
| T | A sandbox modifies the platform | nothing platform-side is reachable from it; the cloud adapter acts only on resources tagged `ipo` | `test_openstack_sdk`, reconciler tests |
| I | A new tenant sees an old one's data | a team's VM and port are **deleted**, never reused; the address is quarantined; leases keep no link to the old team | [lifecycle](#lifecycle-security-what-teardown-guarantees) |
| D | A sandbox burns CPU or floods the network | flavor-sized VM; telemetry rate limits; lease expiry (default 2 h) reclaims it | isolation suite (flood), reaper tests |
| E | A sandbox reaches the OpenStack metadata service to learn credentials | the sandbox image carries no cloud credentials; the control plane's credentials live only on its own hosts | credential handling below |

### Management plane: bastion, CA, observability

| | Threat | Control | Evidence |
| --- | --- | --- | --- |
| S | An outsider reaches mgmt-net | only WireGuard (UDP 51820) is open; peers are masqueraded onto exactly the ports `sg-bastion` may reach | isolation suite (`--as internet`) |
| T | A stolen host certificate | 24-hour certificates from the platform CA, renewed by timer | deploy tests, `rotate-certs` runbook |
| I | Logs or metrics reveal secrets | the Traefik access log keeps no headers (`defaultMode: drop`) and no client address reaches the control plane | `traefik.yml.j2`, `test_traffic` |
| D | An outage goes unnoticed | alert rules and five dashboards, each alert with a runbook entry | `runbooks/alerts.md` |

## Lifecycle security: what teardown guarantees

A team's time on the platform ends by expiry (the reaper, every second), by `DELETE`, or by a failed
registration being undone. All three run the same idempotent steps in a fixed order, and each is
checked:

| Step | What is purged | Checked by |
| --- | --- | --- |
| `drain` | the route is deleted and the compiler pushes a new config | gateways ack a version without the host, or teardown carries on after the timeout and the reconciler re-pushes |
| `await_route_removal` | waits until both gateways' *live* version lacks the host | lab: host answers 404 within ~2 s of expiry |
| `unpublish_dns` | the team's DNS record (`rfc2136` mode; the wildcard default has none) | unit tests, lab with a signed-update DNS server |
| `release` | the VM, then its port; the lease goes `quarantined`, with team, server and port links cleared. **Then verified**: no server or port named for that address remains, and the lease carries nothing of the old team, or the step fails and retries | `test_an_address_with_anything_left_on_it_is_not_released` |
| `finalize` | the team's logins, its traffic history and recent requests are deleted; the team row stays so its name is never reused; the audit log records what was purged | `test_deleting_a_team_removes_its_logins_…` |
| quarantine | the address cannot be handed out again for `quarantine_seconds` (default 60), so stale DNS caches, connections and ARP entries age out | lifecycle benchmark |

What if something goes wrong halfway? A worker that dies mid-step is detected by its stale lock
and the step re-run; a teardown job that dies for good is finished by the **reconciler** within a
cycle (60 s, 30 s grace), which also deletes cloud resources the database does not know about,
marks a team `failed` when its VM disappears, withdraws DNS records of dead teams and restores
those of live ones, and re-pushes config to a gateway that is behind. Tests kill the worker at
every boundary of every step (`test_worker_killed_mid_teardown_is_finished_by_the_reconciler`) and
run 100 create/delete cycles asserting zero leftover routes, VMs, ports, DNS records or linked
leases.

## Authentication and authorisation

| Principal | Credential | Authorised for |
| --- | --- | --- |
| Operators, viewers, admins | email and password (argon2id) → 15-minute JWT | the role ladder in the [API guide](../api/integration.md#authentication) |
| Team accounts | the same, issued by an admin per team | their team's traffic and events only |
| Gateway agents | an operator account each, plus their host certificate toward the control plane | heartbeats, traffic reports, config pull |
| Control plane → agents | mutual TLS (client certificate CN `ipo-control-plane`) and the config signature | config push; `POST /fault` needs the admin CN |
| Operators → platform hosts | WireGuard to the bastion, then SSH | everything, audited by SSH logs |

## Secrets and keys

| Secret | Where it lives | Rotation |
| --- | --- | --- |
| Config signing key (Ed25519) | Podman secret on cp-1/cp-2; public half in the agents' env | new key, push to agents first |
| JWT secret, admin and agent passwords | Podman secrets; agents' in a root-owned 0640 env file | re-run `site.yml` with new values |
| Host certificates (24 h) | `/etc/ipo/pki`, key 0640 to `ipo-pki` | `ipo-cert-renew.timer`, or `rotate_certs.yml` |
| CA keys and password | step-ca's volume on cp-1; password as a Podman secret | - |
| Postgres passwords | Podman secrets; sent to psql on stdin, never argv | re-run `control_plane.yml` |
| OpenStack application credential | `~ipo/.config/openstack/clouds.yaml`, 0600 | revoke in Keystone, re-run |
| DNS TSIG key (`rfc2136` mode only) | the control plane's 0600 environment file | new key at the server, re-run `control_plane.yml` |
| All of the above at rest | `ansible/secrets.enc.yaml` (SOPS); `secrets.yaml` is git-ignored | - |

## Data handling

| Data | Kept | Not kept |
| --- | --- | --- |
| Per-request telemetry | host, method, path (no query), status, duration; one-minute counters; last few hundred requests (unlogged tables, fine to lose) | client addresses, query strings, headers, bodies |
| Accounts | email, argon2id hash, role | plaintext passwords |
| Audit | actor, action, target, detail, hash chain | secrets |
| Deleted teams | the team row and audit trail | logins, traffic, routes, DNS record, VM, port |

## Abuse and resource limits

Registration needs an authenticated operator; a name is 1–63 characters of `[a-z0-9-]`; the
free-address reserve (`reserve_free_ips`) refuses registrations before the pool is exhausted; the
warm pool is capped (`pool_target`, 0–200; it grows with recent demand only up to `pool.max_size`) and a lowered target discards idle VMs; leases expire
(`lease_ttl_seconds`); traffic reports are bounded (500 hosts, 200 recent requests); the pool
manager backs off on quota errors. All settings change at runtime and every change is audited.

## Known gaps

- **Agents use an `operator` account.** A compromised gateway can therefore call every operator
  route. A narrower `gateway` role limited to heartbeat, traffic and config pull would shrink that.
- **No per-team rate limit at the gateway.** One team can use the gateway's shared capacity.
  Traefik's rate-limit middleware per router is the natural next step.
- **TLS to teams is optional.** The gateway supports ACME (`traefik_acme_email`), but the default
  deployment serves plain HTTP on 80 and the team subdomains need a wildcard certificate to
  serve 443 without per-host issuance.
- **`/metrics` is unauthenticated** on the control plane; it is reachable only on mgmt-net.
- **Not yet run on real hosts:** the host firewall rules (verified in the lab against the real
  rendered files) and the Ansible roles. The Neutron half is verified on the real cloud.
- **eBPF** is not used; nftables covers the same enforcement points with far less operational
  weight.

Isolation results: [`docs/reports/isolation.md`](../reports/isolation.md). Decisions:
[ADR 3](../adr/0003-one-network-policy-two-enforcers.md),
[ADR 4](../adr/0004-rootless-quadlets-on-the-host-network.md).
