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
| **Concurrent provisioning stress test (15)**: rapid, error-free provisioning, IP allocation and routing updates for 10+ simultaneous registrations | 48 saga workers woken by `NOTIFY`, config batching that no longer waits, a warm pool that follows demand (12 minimum, up to 40), Traefik's throttle cut from 2 s to 25 ms, idempotent registration, database-enforced lease states; a burst tool (`tests/load/burst.py`) | **10, 20, 30, 50 and 90 teams at the same instant, five rounds each (1,000 teams) on the real gateway stack: all `active`, every one on its own address and served by its own sandbox, zero failures.** Whole burst served in 0.5 / 0.8 / 1.1 / 1.5 / 2.5 s. **On FelCloud, 10 at once: 1.5 s warm; 20: 2.4 s; 10 cold: about two minutes** (Nova's boot). [Benchmarks](reports/benchmarks.md); `uv run python -m load.run`; `control-plane/tests/test_concurrent_registration.py` |
| **Automated teardown and lifecycle security (10)**: purge expired sandboxes, proxy/DNS rules, sanitise IP allocations | Teardown saga with a DNS step (wildcard default, RFC 2136 optional), route removal acknowledged by both gateways, VM and port deletion verified, the address quarantined and its lease unlinked, logins and traffic purged, tokens revoked; reaper every second; reconciler repairs any leftover | Lab: route and DNS record gone 1.5 s after expiry, address reusable one quarantine plus about 2 s later. **FelCloud: 20 teams torn down in 14 s, addresses free about 7 s after.** Worker killed at every boundary of every step is finished by the reconciler; 100 create/delete cycles leave nothing. [Security: lifecycle](architecture/security.md#lifecycle-security-what-teardown-guarantees); `control-plane/tests/test_lifecycle.py`, `test_teardown.py`; `tests/load/test_lifecycle.py` |
| **Network isolation and access safeguards (5)**: Security Groups/iptables/eBPF blocking inter-sandbox traffic and protecting the gateway management layer | One policy file rendered into Neutron security groups and per-host nftables, port security kept on by a policy pack | 1,782-probe lab suite (both layers, each alone, forged frames, rerouting, floods) and **a run against the real FelCloud security groups: 270 probes, no leak, sandbox-to-sandbox blocked on every port, gateway management addresses blocked, positive controls pass, and 32 forged source-IP and source-MAC packets from a sandbox: none delivered.** [Isolation report](reports/isolation.md); `uv run python -m isolation.run`, `isolation.cloud_probe` |
| **Short video demonstration**: subdomain routing, hot reload, simulated primary failover | `python -m chaos.demo` (interactive and `--auto`, with a burst of twelve and expiry added); `python -m chaos.record` renders the scripted run to video | **[`demo.mp4`](presentation/demo.mp4)** (104 s, recorded from the real lab: routing, hot reload, a burst of 12, expiry, failover, VM death, split brain; no narration) and the [script and runbook](presentation/demo-script.md) for a take with a voice |
| **Presentation to FelCloud and the jury** | A 14-slide deck and the speaker notes with a question bank | **[`IPO-Jury-Deck.pdf`](presentation/IPO-Jury-Deck.pdf)** (numbers read from the measurements by `build_deck.py`) and [`jury-deck.md`](presentation/jury-deck.md) |
| **Benchmark report**: provisioning latency, gateway failover time, IP recycling speed, load test metrics | `tests/load` (bench, report), `bench.real_cloud`, the chaos suite, charts | [Benchmark report](reports/benchmarks.md); `uv run python -m load.bench` |
| **API integration and security architecture documentation** | Integration guide, STRIDE per component, lifecycle guarantees | [API integration guide](api/integration.md), [security architecture](architecture/security.md) |

### Deployed for real

Beyond the list above, the whole platform was deployed on FelCloud by its own Ansible roles and
used from the internet ([report](reports/real-deployment.md)):

- **Failover between two real gateway VMs**, under 50 new connections per second through the
  floating IP: a power loss of the primary costs **1.0 s**; Traefik down, **1.36 s**; a planned
  failover, **0.24 s**. The old primary rejoins as BACKUP.
- **A team registered through the real gateways is active in 0.6 s** and served from the internet
  by its own sandbox. A team whose VM is still booting becomes active once the VM serves (94 s),
  on the first attempt.
- **21 defects that no lab could show**, all fixed: FelCloud's routers do not SNAT (the bastion now
  runs an allow-listed egress proxy), config pushes silently sent no client certificate, the
  control plane had no path to the VIP for its probe, a slow VM could block every later
  registration, and 17 more.
- **Security gaps closed in the same pass:** gateway agents log in with an account bound to their
  own gateway, every team's route has its own rate limit, and the baked sandbox image is built and
  used.

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
cd control-plane && uv run pytest -q          # 354 tests against a real PostgreSQL (about 3 min)
cd tests && uv sync --group deploy && sh lab/fetch-tools.sh
uv run python -m chaos.run                    # real keepalived + Traefik + agents, failover under load (about 4 min)
uv run python -m isolation.run                # 1,782 network probes against the security policy (about 90 s)
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
| M2 Ansible and Podman | 14 roles, 6 playbooks, 13 Quadlets, linted and checked against the real config readers, and **run on six real FelCloud VMs** (21 defects found and fixed, [report](reports/real-deployment.md)); Molecule scenarios not written |
| M3 Data model and IPAM | done (9 migrations, trigger-enforced state machine, hash-chained audit) |
| M4 API, saga, warm pool | done |
| M5 Gateway pair and agent | done; failover measured in the lab and **between two real FelCloud VMs** (1.0 s for a power loss) |
| M6 Compiler, signing, rollback | done |
| M7 Teardown, reconciler, isolation | done; isolation suite in the lab; CI job for a deployment written, not run |
| M8 Observability | alert rules, five dashboards, scrape and push configs; Grafana embedding verified against a real Grafana; no live data yet |
| M9 Dashboard | done, plus team accounts, a traffic page and a metrics page |
| M10 Benchmarks and written deliverables | architecture, security (STRIDE), API integration guide, runbooks and ADRs written; **benchmarks run** on the lab and on FelCloud ([report](reports/benchmarks.md)) |
| M11 Demo | an interactive and a scripted demo that builds and tears down the real cloud, with a burst and an expiry scene; script, presentation deck and a recorded video written |

## Gaps

Stated plainly, so nothing here surprises a reviewer:

- **The deployed control plane ran with the fake cloud adapter.** The whole platform was deployed
  on FelCloud by the Ansible roles and exercised from the internet
  ([the first real deployment](reports/real-deployment.md)), but the hosts were given no OpenStack
  credential: creating one was left to the team. The sandboxes at the addresses the control plane
  assigned were booted from the baked image by hand. The real cloud adapter is measured
  separately, from a workstation (`bench.real_cloud`).
- **Not run on real hosts:** the observability stack (skipped in that run), WireGuard with a real
  peer, `podman auto-update` rollback, promoting the Postgres replica.
- **Possibly one compute host,** so the gateway pair may not be on separate hardware
  (soft anti-affinity); a VM failure was tested, a host failure was not. See
  [OpenStack HA](architecture/openstack-ha.md#findings-from-the-real-felcloud-cloud).
- **The benchmarks mix environments, and the report keeps them apart.** Bursts, throughput and
  lifecycle timings are lab numbers (real keepalived, Traefik and agents, a fake cloud, one
  laptop). Failover between real VMs, registration through the real gateways, VM boot,
  teardown and recycling are FelCloud numbers. Boot times on FelCloud varied between runs.
- **A cold registration on FelCloud takes a minute or two**: a guest serves about 90 s after it is
  created, whatever the image. The warm pool, which grows with demand, is what makes
  registration fast; its size is a cost trade-off.
- **The demo video has no narration** (it is a rendering of the real dashboard, with a caption
  bar); a spoken take needs a person.
- **Security model:** one rate limit for every team; egress allow-listed by hostname only; eBPF is
  not used (nftables is). See [known gaps](architecture/security.md#known-gaps).
- **Bonus features** are not implemented.
- **No licence file** has been added.
