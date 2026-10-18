# Jury presentation: Resilient IP Optimizer

A 12-minute talk (the slides are [`IPO-Jury-Deck.pdf`](IPO-Jury-Deck.pdf), built from the measurements by `build_deck.py`) for the FelCloud technical representatives and the jury at CSTAM 3.0: fifteen
slides, what to say on each, what to show, and the questions to expect. Figures come from
[the benchmark report](../reports/benchmarks.md) and [the first real deployment](../reports/real-deployment.md);
quote them as measured on the lab (real keepalived, Traefik and gateway agents on one laptop) or
on FelCloud, as marked.

**Structure of the talk:** the problem (1 min) → the design (2) → provisioning, on the lab and on
FelCloud, and the real deployment (3) → the three scored areas with proof (4) → API, security,
evidence and limits (1) → live demo (1 min of the video, questions).

---

## 1. Title

*Resilient IP Optimizer: sandboxes with their own subdomain, a highly available front door, and a
platform that cleans up after itself.* Team name, CSTAM 3.0, FelCloud.

> Say: "A hackathon platform gives every team a sandbox VM reachable at `team.domain`. We built
> the part that has to keep working while teams come and go: provisioning, routing, failover,
> isolation and cleanup. And we deployed all of it on FelCloud."

## 2. The problem

Twenty teams arrive at once. Each needs a VM, an address, a route, a name; later each must vanish
without leaving a route, a DNS record or an address behind. And the front door cannot go down.

**Numbers to remember:** pool of 100 addresses, 50-instance quota, VM boot 17 s alone and up to a
minute when many boot at once on FelCloud.

## 3. Architecture

**Show:** [`system-architecture.svg`](../diagrams/system-architecture.svg).

- Control plane (FastAPI + PostgreSQL): API, saga workers, controllers; two replicas.
- Two gateways, each keepalived + Traefik + `ipo-agent`, sharing one VIP; a floating IP maps to it.
- Sandbox VMs on their own network; management network reachable only through a WireGuard bastion,
  which is also the platform's only, allow-listed, way out to the internet.
- Everything defined by one `platform.yaml` and one `security-groups.yaml`, consumed by Pulumi,
  Ansible, the control plane and the tests.

## 4. Gateway data flow

**Show:** [`gateway-data-flow.svg`](../diagrams/gateway-data-flow.svg). Walk one request
(Host header → Traefik router → per-team rate limit → sandbox), then the config path (database →
compile → sign (Ed25519) → both agents → verify → validate → stage → swap → converge → probe →
commit or roll back). "A bad config never takes the gateway down, and a compromised control plane
cannot point a gateway outside the sandbox range."

## 5. Up to 90 teams at the same instant: no failures (concurrent provisioning, 15 points)

**Claim:** 10+ simultaneous registrations are fast and error-free.

- Registration is a **saga** with idempotency keys, a database-enforced lease state machine, and a
  **warm pool**: a team is serving about **0.4 s** after its request.
- Burst test: **10 / 20 / 30 / 50 / 90 teams at the same instant**, five rounds each (1,000 in
  all): every one `active`, on a distinct address, **served by its own sandbox**, zero failures.
  Whole burst served in **≈0.5 / 0.8 / 1.1 / 1.5 / 2.5 s** (lab).
- Absorbs trouble: transient Nova/Neutron errors during a burst, a retry storm on one key (one
  team), ten claims on one name (one winner).

**What the benchmark found:** the first measurement of 30 teams took **15 s**. Traefik applies at
most one provider change per 2 s by default, so every config version cost two seconds on each
gateway. Fixing that, batching config versions properly and waking 48 workers by `NOTIFY` made it
**1.1 s**. A 90-team test then found that the agent could not read more than 100 routers from
Traefik (a wrapping pagination header): fixed. Say this: it shows the tests find real things.

## 6. On the real FelCloud

- **Simultaneous registrations against real Nova and Neutron:** 10 warm all `active` in **1.5 s**,
  20 in **2.4 s**; cold **108–140 s** (ten concurrent boots are far slower than one, 17 s), so the
  pool **grows to the last 15 minutes' registrations** and shrinks after.
- **20 teams torn down in 14 s**, addresses free about 7 s later (5 s quarantine).
- **What the real cloud caught:** warm claims on still-booting VMs, the reconciler deleting VMs
  mid-boot, and teardown waiting on Nova's lingering DELETED entries (85 s → 6 s). All fixed.

## 7. Deployed for real: the whole platform on FelCloud

`pulumi up` (91 resources), then **the Ansible roles on all six VMs** through the bastion, then the
sandbox image baked by `bake_image.yml`; used from the internet; destroyed.

- **A team registered through the real gateways is active 0.6 s after the request**, its config on
  both gateways in 0.22 s (signed, verified, swapped, probed), and served from a laptop through the
  floating IP by its own VM. A team whose VM is still booting is `active` when the VM serves
  (**94 s**), on the first attempt.
- **21 defects that no lab could show**, all fixed, most with a regression test. The ones to tell:
  - FelCloud's routers do **not SNAT**: no VM without a floating IP can reach the internet. The
    bastion now runs an allow-listed egress proxy; sandboxes still have no way out, by design.
  - **Every config push was silently failing**: httpx 0.28 drops the client certificate when the CA
    is a path. The agents' 5 s config pull hid it. Now an explicit TLS context, and a test against
    a server that demands a client certificate.
  - The management network has **no path to the VIP**, so the control plane now asks the gateways'
    agents to probe a new team (`POST /probe`, mutual TLS).
  - **One slow VM could block every later registration**: the agent rolled back a config whose new
    team was still booting. Now only a missing route or a failed canary rolls back.
- Be honest about one thing: the deployed control plane used its **fake cloud adapter**, because a
  real one needs an application credential on the hosts and that is the team's decision. The VMs at
  the addresses it assigned were booted from the baked image by hand. The real adapter is measured
  separately (slide 6).

**Baked image:** ACTIVE in 20 s, serving in 88 s (stock Debian: 24 s, 102 s, and without Podman,
Alloy or the firewall, which a sandbox with no route out could never install).

## 8. Hot reload and rollback

Registering or deleting a team changes the routing table without touching Traefik's process.
Show the demo's process ids staying the same. Config is Ed25519-signed; each agent re-validates it,
swaps it atomically, probes every changed route, and rolls back on a failed canary or a missing
route. Operator rollback: **0.3 s** median to both gateways. Control plane killed with `-9` for 8 s
under load: **0 failed requests**.

## 9. High availability

**Show:** the failover chart (or [`openstack-ha.png`](../diagrams/openstack-ha.png)). VRRP with
unicast peers and a health-check script that fails keepalived when Traefik is down.

Measured under a stream of 50 new connections per second:

| Failure | Two real FelCloud VMs, through the floating IP | Lab, many runs |
| --- | --- | --- |
| Primary VM loses power | **1.0 s** | 0.52–0.72 s |
| Traefik down | **1.36 s** | 1.4–1.5 s |
| Planned failover | **0.24 s** | 0.12 s |
| Control plane killed 8 s | — | **0 failed requests** |

On FelCloud the old primary came back, reported FAULT until Traefik was up, and rejoined as BACKUP
without taking the VIP back. Split brain: detected by the control plane from both heartbeats,
alarmed, and healed to one master.

## 10. Automated teardown: nothing outlives a team (10 points)

**Claim:** nothing of a team outlives it.

- The reaper (every second) starts teardown at lease expiry; `DELETE` does the same.
- Order: stop traffic (route out, **both gateways ack**) → DNS record out → VM and port deleted →
  address **verified clean, then quarantined** → logins and traffic purged.
- Measured: route and DNS record gone **within a second** of expiry (lab); address reusable one
  quarantine **+≈1.5 s** later.
  FelCloud: 20 teams torn down in 14 s.
- Fails safe: kill a worker at every boundary of every step and the reconciler finishes the job
  within a cycle; 100 create/delete cycles leave zero routes, VMs, ports, DNS records or linked
  leases. A deleted team's token stops working at once.

## 11. Network isolation (5 points)

**Claim:** sandboxes cannot reach each other or the platform.

- Three independent layers from one spec: **Neutron security groups**, **port security**
  (anti-spoofing), **nftables** on every host (iptables' successor).
- Lab: **1,782 probes** across every machine and 18 ports (112 are positive controls); rerouting,
  forged source MAC/IP, forged VRRP, floods; each layer tested alone.
- **FelCloud, probe VMs behind the real security groups and the configured hosts:** 270 probes,
  **0 leaks**, every expected-open TCP path reached; **0 of 32 forged-source packets delivered**.
- Honest finding: Neutron's group rules filter by port, not destination address; the host firewall
  closes the one-way UDP leak that gap allows.

## 12. API and security architecture

**Show:** `docs/api/integration.md` and `docs/architecture/security.md`. Bearer JWT with roles;
idempotent asynchronous operations (`202` + job); RFC 9457 errors; server-sent events; OpenAPI +
Postman; a Python client in 15 lines; the gateway agent's own signed-config API. STRIDE per
component with the test that would fail without each control.

- **Closed in this phase:** agents log in with a `gateway` account bound to their own gateway;
  every team's route is rate-limited by its Host (200/s, burst 100: a flooding team gets 429s and
  its neighbour does not notice); platform egress goes through an allow-listed proxy.
- **Named gaps:** one rate limit size for all teams, TLS to teams optional, nftables rather than
  eBPF.

## 13. Evidence

371 control-plane tests against a real PostgreSQL, Go agent tests with `-race`, 72 dashboard
tests, 68 deploy checks that load every rendered config into the real program, 46 chaos, 22 load
and 35 isolation lab tests, and the real deployment itself. `ruff`, `mypy --strict`, `go vet`,
`tsc`, `ansible-lint` (production profile) clean.

## 14. Demo (video)

Subdomain routing → hot reload → burst of 12 → expiry → failover → VM kill → split brain
([script](demo-script.md)).

## 15. Limits and next steps

Not done: the control plane's real cloud adapter on the deployed hosts (needs a credential the
team issues), the observability stack on real hosts, WireGuard with a real peer, a compute-host
failure (FelCloud honours only soft anti-affinity), wildcard TLS. Next: those, then a rate limit
sized per team and a second region for the control plane.

---

## Questions to expect

| Question | Answer |
| --- | --- |
| Did the deployed platform boot real sandboxes itself? | Not there: on the hosts the control plane used its fake cloud adapter, because a real one needs an application credential and creating one is the team's call. Everything else on the path was real (API, sagas, signing, both agents, Traefik, VRRP, the probe), and the VMs at the assigned addresses were booted from the baked image. The real adapter is benchmarked against FelCloud from a workstation (slide 6). |
| How do hosts without a floating IP install anything? | FelCloud's routers do not SNAT, so the bastion runs an allow-listed HTTP(S) proxy (Debian, Grafana's repo, the container registries, GitHub releases, PyPI) on the management network; apt and Podman use it. Sandboxes have no route out at all, which is why their image is baked. |
| Why not run the control plane on one VM? | Two replicas coordinate through row locks and advisory locks; PostgreSQL streaming replication with a promotion runbook. The gateways keep serving with the control plane down (measured). |
| What if both gateways fail? | The VIP is down; the control plane alarms and the reconciler keeps the database right. Recovery is a Pulumi/Ansible rebuild (documented, [rebuild from zero](../runbooks/rebuild-from-zero.md)). |
| How many teams fit? | 100 addresses minus the reserve, bounded by the 50-instance quota (warm pool counts). Larger pools are a CIDR change in `platform.yaml`. |
| Why a warm pool, and what does it cost? | A FelCloud VM is ACTIVE in about 20 s but serves only after about 90 s; a claim takes under a second. It costs the idle 1-vCPU VMs of `pool.target_size`, grown to recent demand (up to `pool.max_size`) and shrunk when the window passes. |
| What stops one team starving others? | Each team's route is rate-limited by its Host at the gateway (200 requests/s, burst 100); the reserve guard and quota back-off protect capacity. The limit is the same size for every team today. |
| Can a stolen gateway token change the platform? | No: a gateway account is admitted only to its own gateway's heartbeat, traffic report and config pull, and refused everywhere else (a test walks every route in the OpenAPI document). Configs are signed by the control plane, and the agent rejects any backend outside the sandbox range. |
| How do you know teardown removed everything? | It verifies the cloud after deleting (no server or port left on the address, lease unlinked), the reconciler repairs anything else, and a 100-cycle soak asserts zero leftovers. |
| Why nftables and not eBPF? | Same enforcement points with a fraction of the operational weight; both enforcers are generated from one spec and tested against each other. |
| What is wildcard DNS and why? | FelCloud has no DNS service, so `*.domain` points at the floating IP once. Per-team records via RFC 2136 are supported and tested. |
| Is the failover time real? | Yes: between two real FelCloud gateway VMs, through the floating IP, under load: 1.0 s when the primary loses power, 1.36 s when Traefik stops, 0.24 s planned. The lab gives the spread over many runs. |
