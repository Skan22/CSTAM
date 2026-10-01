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

The three diagrams are generated from scripts in `docs/diagrams/` (`python <name>.py`), so they
can be edited and re-rendered; the PNG and SVG files are committed.

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
cd control-plane && uv run pytest -q          # 309 tests against a real PostgreSQL (about 2 min)
cd tests && uv sync --group deploy && sh lab/fetch-tools.sh
uv run python -m chaos.run                    # real keepalived + Traefik + agents, failover under load (about 4 min)
uv run python -m isolation.run                # 1,683 network probes against the security policy (about 90 s)
uv run python -m chaos.demo                   # the interactive demo
```

The headline results (failover gaps, isolation findings) are in
[OpenStack HA](architecture/openstack-ha.md#targets) and the [isolation report](reports/isolation.md).

## Coverage of the implementation plan

| Milestone | Status |
| --- | --- |
| M0 Foundations and risk spikes | `platform.yaml` and its schema; the FelCloud project surveyed ([report](reports/felcloud-survey.md)); the first real `pulumi up` exposed the quota, anti-affinity and floating-IP limits |
| M1 Pulumi | done and run on the real cloud; CrossGuard pack; mocked tests |
| M2 Ansible and Podman | 14 roles, 6 playbooks, 13 Quadlets, linted and checked against the real config readers; **not run on real hosts**; Molecule scenarios not written |
| M3 Data model and IPAM | done (7 migrations, trigger-enforced state machine, hash-chained audit) |
| M4 API, saga, warm pool | done |
| M5 Gateway pair and agent | done; failover measured in the lab |
| M6 Compiler, signing, rollback | done |
| M7 Teardown, reconciler, isolation | done; isolation suite in the lab; CI job for a deployment written, not run |
| M8 Observability | alert rules, five dashboards, scrape and push configs; Grafana embedding verified against a real Grafana; no live data yet |
| M9 Dashboard | done, plus team accounts, a traffic page and a metrics page |
| M10 Benchmarks and written deliverables | architecture, security, API, runbooks and ADRs written; **benchmarks not run** |
| M11 Demo | an interactive and a scripted demo that builds and tears down the real cloud; **video not recorded** |

## Gaps

Stated plainly, so nothing here surprises a reviewer:

- **No public entry point yet.** FelCloud's `INTERNET` pool had no free floating IP when tested,
  so the built cloud has no public address; the platform builds without them on request.
- **Not run on real hosts:** the Ansible roles, VRRP between two real OpenStack VMs, `podman
  auto-update` rollback, step-ca enrollment, Postgres replication, WireGuard. They are checked
  against the real programs that read their config, and failover is measured on the real
  software in namespaces, not on OpenStack.
- **Possibly one compute host,** so the gateway pair may not be on separate hardware
  (soft anti-affinity). See [OpenStack HA](architecture/openstack-ha.md#findings-from-the-real-felcloud-cloud).
- **Benchmarks from the plan** (warm and cold provisioning times on the real cloud, concurrent
  registration at 10/20/30, gateway throughput, the 100-cycle soak) have not been run.
- **No recorded demo video.**
- **Security model:** a threat and defence table with isolation evidence, not a full per-component
  STRIDE model; agents use an operator account rather than a narrower role.
- **Bonus features** are not implemented.
- **No licence file** has been added.
