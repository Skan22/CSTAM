# Submission guide

What is being submitted, where each deliverable is, how to check it, and an honest list of what
is not done.

## Deliverables

| Deliverable | Where |
| --- | --- |
| **Source code repository** | this Git repository (branch `main`); layout in the [README](../README.md#repository-layout) |
| **System architecture and OpenStack HA** | [system architecture](architecture/system-architecture.md) and [OpenStack high availability](architecture/openstack-ha.md), with figures [`system-architecture.png`](diagrams/system-architecture.png) and [`openstack-ha.png`](diagrams/openstack-ha.png) |
| **Gateway data flow diagram** | [`gateway-data-flow.png`](diagrams/gateway-data-flow.png) (SVG: [`gateway-data-flow.svg`](diagrams/gateway-data-flow.svg)), explained step by step in [gateway data flow](architecture/gateway-data-flow.md) |
| **Documentation** | the [index](README.md): architecture, guides, API reference, runbooks, ADRs, reports |

**Phase 2 (integration and testing)** deliverables are in the next section. The three diagrams alone, one per A3 landscape page and nothing else, are in
[`IPO-Diagrams.pdf`](IPO-Diagrams.pdf) (rebuild with `python docs/build_pdf.py`).

The three diagrams are generated from scripts in `docs/diagrams/` (`python <name>.py`), so they
can be edited and re-rendered; the PNG and SVG files are committed.

## Phase 2: integration and testing

| Objective (points) | What was built | Evidence, and how to re-run it |
| --- | --- | --- |
| **Concurrent provisioning stress test (15)**: rapid, error-free provisioning, IP allocation and routing updates for 10+ simultaneous registrations | Saga workers (24, woken by `NOTIFY`), a 12-VM warm pool refilled every 2 s, Traefik's config throttle cut from 2 s to 100 ms, idempotent registration, database-enforced lease states; a burst tool (`tests/load/burst.py`) | **10, 20, 30 and 50 teams at the same instant, five rounds each, on the real gateway stack: all `active`, every one on its own address and served by its own sandbox, zero failures.** Whole burst served in about 1.5 / 2.5 / 3.5 / 6 s. **On FelCloud, ten at once: 1.5 s warm, about two minutes cold** (Nova's boot). [Benchmarks](reports/benchmarks.md); `uv run python -m load.run`; `control-plane/tests/test_concurrent_registration.py` |
| **Automated teardown and lifecycle security (10)**: purge expired sandboxes, proxy/DNS rules, sanitise IP allocations | Teardown saga with a DNS step (wildcard default, RFC 2136 optional), route removal acknowledged by both gateways, VM and port deletion verified, the address quarantined and its lease unlinked, logins and traffic purged, tokens revoked; reaper every second; reconciler repairs any leftover | Lab: route gone about 3 s after expiry, DNS record gone, address reusable one quarantine plus about 2 s later. **FelCloud: ten teams torn down in 6 s, addresses free 7 s after.** Worker killed at every boundary of every step is finished by the reconciler; 100 create/delete cycles leave nothing. [Security: lifecycle](architecture/security.md#lifecycle-security-what-teardown-guarantees); `control-plane/tests/test_lifecycle.py`, `test_teardown.py`; `tests/load/test_lifecycle.py` |
| **Network isolation and access safeguards (5)**: Security Groups/iptables/eBPF blocking inter-sandbox traffic and protecting the gateway management layer | One policy file rendered into Neutron security groups and per-host nftables, port security kept on by a policy pack | 1,683-probe lab suite (both layers, each alone, forged frames, rerouting, floods) and **a run against the real FelCloud security groups: 255 probes, no leak, sandbox-to-sandbox blocked on every port, gateway management addresses blocked, positive controls pass.** [Isolation report](reports/isolation.md); `uv run python -m isolation.run`, `isolation.cloud_probe` |
| **Short video demonstration**: subdomain routing, hot reload, simulated primary failover | `python -m chaos.demo` (interactive and `--auto`, with a burst of twelve and expiry added) | [Script and runbook](presentation/demo-script.md). **The video itself is not recorded** (it needs a person at a terminal) |
| **Presentation to FelCloud and the jury** | Slide-by-slide talk with notes and a question bank | [Jury presentation](presentation/jury-deck.md) |
| **Benchmark report**: provisioning latency, gateway failover time, IP recycling speed, load test metrics | `tests/load` (bench, report), `bench.real_cloud`, the chaos suite | [Benchmark report](reports/benchmarks.md); `uv run python -m load.bench` |
| **API integration and security architecture documentation** | Integration guide, STRIDE per component, lifecycle guarantees | [API integration guide](api/integration.md), [security architecture](architecture/security.md) |

## Getting the repository

The history is a normal Git repository with no remote configured by this work. To publish it:

```bash
git remote add origin <your repository URL>
git push -u origin main
```

or hand over a single file without a remote:

```bash
git bundle create ipo.bundle --all          # restore with: git clone ipo.bundle ipo
git archive --format=zip -o ipo-source.zip HEAD   # source only, no history
```

No credentials, keys or state are committed. `.gitignore` excludes `secrets.yaml`, `*.key`,
`*.pem`, `.env` and local Pulumi stacks; `ansible/secrets.example.yaml` has placeholder values only.

## Verify it in ten minutes

```bash
cd control-plane && uv run pytest -q          # 353 tests against a real PostgreSQL (about 3 min)
cd tests && uv sync --group deploy && sh lab/fetch-tools.sh
uv run python -m chaos.run                    # real keepalived + Traefik + agents, failover under load (about 4 min)
uv run python -m isolation.run                # 1,683 network probes against the security policy (about 90 s)
uv run python -m load.run                     # 10 to 50 simultaneous registrations, lifecycle, benchmarks (about 7 min)
uv run python -m chaos.demo                   # the interactive demo (b = burst of 12, e = expire them)
```

The headline results (failover gaps, isolation findings) are in
[OpenStack HA](architecture/openstack-ha.md#targets) and the [isolation report](reports/isolation.md).

## Coverage of the implementation plan

| Milestone | Status |
| --- | --- |
| M0 Foundations and risk spikes | `platform.yaml` and its schema; the FelCloud project surveyed ([report](reports/felcloud-survey.md)); the first real `pulumi up` exposed the quota, anti-affinity and floating-IP limits |
| M1 Pulumi | done and run on the real cloud; CrossGuard pack; mocked tests |
| M2 Ansible and Podman | 14 roles, 6 playbooks, 13 Quadlets, linted and checked against the real config readers; **not run on real hosts**; Molecule scenarios not written |
| M3 Data model and IPAM | done (8 migrations, trigger-enforced state machine, hash-chained audit) |
| M4 API, saga, warm pool | done |
| M5 Gateway pair and agent | done; failover measured in the lab |
| M6 Compiler, signing, rollback | done |
| M7 Teardown, reconciler, isolation | done; isolation suite in the lab; CI job for a deployment written, not run |
| M8 Observability | alert rules, five dashboards, scrape and push configs; Grafana embedding verified against a real Grafana; no live data yet |
| M9 Dashboard | done, plus team accounts, a traffic page and a metrics page |
| M10 Benchmarks and written deliverables | architecture, security (STRIDE), API integration guide, runbooks and ADRs written; **benchmarks run** on the lab and on FelCloud ([report](reports/benchmarks.md)) |
| M11 Demo | an interactive and a scripted demo that builds and tears down the real cloud, with a burst and an expiry scene; script and presentation written; **video not recorded** |

## Gaps

Stated plainly, so nothing here surprises a reviewer:

- **The public address is allocated but untested end to end.** The floating IPs attach to the VIP
  port and the bastion, but nothing answers behind them until the Ansible roles have run, so no
  request has yet travelled from the internet through the floating IP to a gateway.
- **Not run on real hosts:** the Ansible roles, VRRP between two real OpenStack VMs, `podman
  auto-update` rollback, step-ca enrollment, Postgres replication, WireGuard. They are checked
  against the real programs that read their config, and failover is measured on the real
  software in namespaces, not on OpenStack.
- **Possibly one compute host,** so the gateway pair may not be on separate hardware
  (soft anti-affinity). See [OpenStack HA](architecture/openstack-ha.md#findings-from-the-real-felcloud-cloud).
- **The benchmarks mix two environments, and the report keeps them apart.** Provisioning
  overhead, bursts, failover, throughput and lifecycle timings are lab numbers (real keepalived,
  Traefik and agents, a fake cloud, one laptop). The cloud-dependent numbers (VM boot, port
  creation, deletion, recycling) are from FelCloud with the control plane running locally, stock
  Debian as the sandbox image, no gateways behind it, so a real end-to-end request through the
  VIP was not part of those. Boot times on FelCloud varied between runs.
- **Cold registration is slow on FelCloud** (about two minutes for ten at once, because ten
  concurrent Nova boots are slower than one: a single VM boots in about 17 s). The warm pool is
  what makes registration fast; its size is a cost trade-off.
- **No recorded demo video.**
- **Security model:** agents use an operator account rather than a narrower role; no per-team
  rate limit at the gateway; eBPF is not used (nftables is). See
  [known gaps](architecture/security.md#known-gaps).
- **Sandbox-to-gateway paths through the router** (edge ports 80 and 443) could not be proven open
  on the real cloud because the gateways there have not been configured by Ansible; the lab
  proves them.
- **Bonus features** are not implemented.
- **No licence file** has been added.
