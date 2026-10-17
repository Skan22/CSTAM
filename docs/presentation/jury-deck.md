# Jury presentation: Resilient IP Optimizer

A 12-minute talk for the FelCloud technical representatives and the jury at CSTAM 3.0: fifteen
slides, what to say on each, what to show, and the questions to expect. Figures come from
[the benchmark report](../reports/benchmarks.md); quote them as measured on the lab (real
keepalived, Traefik and gateway agents on one laptop) or on FelCloud, as marked.

**Structure of the talk:** the problem (1 min) → the design (3) → the three scored areas, each
with proof (5) → the real cloud, honesty and what is next (2) → live demo (1 min of the video,
questions).

---

## 1. Title

*Resilient IP Optimizer: sandboxes with their own subdomain, a highly available front door, and a
platform that cleans up after itself.* Team name, CSTAM 3.0, FelCloud.

> Say: "A hackathon platform gives every team a sandbox VM reachable at `team.domain`. We built
> the part that has to keep working while teams come and go: provisioning, routing, failover,
> isolation and cleanup."

## 2. The problem in one picture

Twenty teams arrive at once. Each needs a VM, an address, a route, a name; later each must vanish
without leaving a route, a DNS record or an address behind. And the front door cannot go down.

**Show:** the request-path figure. **Numbers to remember:** pool of 100 addresses, 50-instance
quota, VM boot 17 s alone and up to a minute when many boot at once on FelCloud.

## 3. Architecture at a glance

**Show:** [`system-architecture.png`](../diagrams/system-architecture.png).

- Control plane (FastAPI + PostgreSQL): API, saga workers, controllers; two replicas.
- Two gateways, each keepalived + Traefik + `ipo-agent`, sharing one VIP; a floating IP maps to it.
- Sandbox VMs on their own network; management network reachable only through a WireGuard bastion.
- Everything defined by one `platform.yaml` and one `security-groups.yaml`, consumed by Pulumi,
  Ansible, the control plane and the tests.

## 4. Data flow: request, config and telemetry paths

**Show:** [`gateway-data-flow.png`](../diagrams/gateway-data-flow.png). Walk one request
(Host header → Traefik router → sandbox), then the config path (database → compile → sign
(Ed25519) → both agents → verify → validate → stage → swap → converge → probe → commit or roll
back). "A bad config never takes the gateway down, and a compromised control plane cannot point a
gateway outside the sandbox range."

## 5. Concurrent provisioning (15 points)

**Claim:** 10+ simultaneous registrations are fast and error-free.

- Registration is a **saga** with idempotency keys, a database-enforced lease state machine, and a
  **warm pool** (12 VMs booted ahead): a team is serving about **0.6 s** after its request.
- Burst test: **10 / 20 / 30 / 50 teams at the same instant**, five rounds each: every one
  `active`, on a distinct address, **served by its own sandbox**, zero failures. Whole burst
  served in **≈1.2 s / 2.4 s / 3.3 s / 5.7 s** for 10 / 20 / 30 / 50 (lab).
- Cold path (no warm VM): same platform overhead; the rest is Nova's boot. **FelCloud, 10 at once:
  warm 1.5 s, cold about two minutes** (ten concurrent boots are far slower than one, 17 s): the
  warm pool is the lever.
- Absorbs trouble: transient Nova/Neutron errors during a burst, a retry storm on one key (one
  team), ten claims on one name (one winner).

**What the benchmark found:** the first measurement of 30 teams took **15 s**. Traefik applies at
most one provider change per 2 s by default, so every config version cost two seconds on each
gateway. One line (`providersThrottleDuration: 100ms`) made it **3 s**. Say this: it shows the
tests find real things.

## 6. Dynamic hot reload

Registering or deleting a team changes the routing table without touching Traefik's process.
Show the demo's process ids staying the same. Rollback of a bad config: **0.3 s** median to both
gateways (operator pin), and automatic on a failed probe.

## 7. High availability

**Show:** [`openstack-ha.png`](../diagrams/openstack-ha.png). VRRP with unicast peers and a
health-check script that fails keepalived when Traefik is down.

Measured under a stream of 50 new connections per second (lab, real keepalived/Traefik):

| Failure | Longest gap |
| --- | --- |
| Primary VM dies | **0.56–0.70 s** |
| Traefik crashes | **1.3–1.5 s** |
| Planned failover | **0.12 s** |
| Control plane killed 8 s | **0 failed requests** |

Split brain: detected by the control plane from both heartbeats, alarmed, and healed to one master.

## 8. Automated teardown and lifecycle security (10 points)

**Claim:** nothing of a team outlives it.

- The reaper (every second) starts teardown at lease expiry; `DELETE` does the same.
- Order: stop traffic (route out, **both gateways ack**) → DNS record out → VM and port deleted →
  address **quarantined and then verified clean** → logins and traffic purged.
- Measured: route gone **≈2–3 s** after expiry; address reusable again quarantine (60 s default)
  **+≈2 s**.
- Fails safe: kill a worker at every boundary of every step and the reconciler finishes the job
  within a cycle; 100 create/delete cycles leave zero routes, VMs, ports, DNS records or linked
  leases.
- Found by its own test: an admin's runtime quarantine setting was ignored, and a retry after a
  partial release trusted a quarantined lease without re-deleting. Both fixed.

## 9. Network isolation (5 points)

**Claim:** sandboxes cannot reach each other or the platform.

- Three independent layers from one spec: **Neutron security groups**, **port security**
  (anti-spoofing), **nftables** on every host (iptables' successor).
- **1,683 probes** across every machine and 17 ports (105 are positive controls); rerouting, forged
  source MAC/IP, forged VRRP, floods; each layer tested alone. **Run again on the real FelCloud
  security groups with probe VMs** (next slide).
- Honest finding: Neutron's group rules filter by port, not destination address; the host firewall
  closes the one-way UDP leak that gap allows.

## 10. On the real cloud

- `pulumi up` builds the whole platform on FelCloud (≈80 resources); `destroy` empties it in ≈30 s.
- **10 simultaneous registrations on FelCloud:** warm all `active` in **1.5 s**; cold 108–128 s
  (Nova); **10 teams torn down in 6 s**, addresses free 7.5 s later (5 s quarantine).
- **Isolation probes from real VMs** with the real security groups: 255 probes, **0 leaks**.
- **What the real cloud caught:** warm claims on still-booting VMs, the reconciler deleting VMs
  mid-boot, and teardown waiting on Nova's lingering DELETED entries (85 s → 6 s). All fixed.
- What the real cloud taught us: hard anti-affinity fails (one compute host), floating IPs need the
  external subnet named, associations race the router interface, no DNS service (so wildcard DNS
  is the default and RFC 2136 is optional).

## 11. API and integration

**Show:** `docs/api/integration.md`. Bearer JWT with roles; idempotent asynchronous operations
(`202` + job); RFC 9457 errors; server-sent events; OpenAPI + Postman; a Python client in 15
lines; the gateway agent's own signed-config API.

## 12. Security architecture

STRIDE per component (edge, agent, API, database, sandboxes, management plane), the trust
boundaries, secret handling, and **named known gaps**: agents use an operator account, no per-team
rate limit, TLS to teams optional, eBPF not used.

## 13. Quality evidence

353 control-plane tests against a real PostgreSQL, Go agent tests with `-race`, 72 dashboard
tests, 67 deploy checks that load every rendered config into the real program, 46 chaos, 33 load
and 35 isolation lab tests. `ruff`, `mypy --strict`, `go vet`, `tsc`,
`ansible-lint` clean.

## 14. Demo (video)

Subdomain routing → hot reload → burst of 12 → teardown → failover → VM kill → split brain
([script](demo-script.md)).

## 15. Limits and next steps

Not done: Ansible roles on real hosts, Molecule, per-team gateway rate limits, a narrower agent
role, wildcard TLS. Next: those, plus a second region for the control plane.

---

## Questions to expect

| Question | Answer |
| --- | --- |
| Why not run the control plane on one VM? | Two replicas coordinate through row locks and advisory locks; PostgreSQL streaming replication with a promotion runbook. The gateways keep serving with the control plane down (measured). |
| What if both gateways fail? | The VIP is down; the control plane alarms and the reconciler keeps the database right. Recovery is a Pulumi/Ansible rebuild (documented, [rebuild from zero](../runbooks/rebuild-from-zero.md)). |
| How many teams fit? | 100 addresses minus the reserve, bounded by the 50-instance quota (warm pool counts). Larger pools are a CIDR change in `platform.yaml`. |
| Why a warm pool, and what does it cost? | Boot takes 35–45 s; a claim takes under a second. It costs `pool.target_size` idle 1-vCPU VMs (12), changeable at runtime, and lowering it discards the surplus. |
| What stops one team starving others? | The reserve guard and quota back-off protect capacity; per-team rate limiting at the gateway is a known gap. |
| How do you know teardown removed everything? | It verifies the cloud after deleting (no server or port left on the address, lease unlinked), the reconciler repairs anything else, and a 100-cycle soak asserts zero leftovers. |
| Why nftables and not eBPF? | Same enforcement points with a fraction of the operational weight; both enforcers are generated from one spec and tested against each other. |
| What is wildcard DNS and why? | FelCloud has no DNS service, so `*.domain` points at the floating IP once. Per-team records via RFC 2136 are supported and tested. |
| Is the failover time real? | Real keepalived and Traefik on namespaces, measured under load; VRRP between two OpenStack VMs is the one thing not yet run for real, and said so. |
