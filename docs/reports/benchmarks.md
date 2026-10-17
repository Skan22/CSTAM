# Benchmark report

Provisioning latency, simultaneous registrations, gateway failover, address recycling and load,
measured on two environments that this report keeps apart, plus what the measuring changed.

- [Summary](#summary) · [Environments and method](#environments-and-method)
- [Lab results](#lab-results): [provisioning](#provisioning-latency) ·
  [bursts](#simultaneous-registrations) · [failover](#gateway-failover) ·
  [expiry and recycling](#expiry-and-ip-recycling) · [rollback](#config-rollback) ·
  [throughput](#gateway-throughput) · [control-plane crash](#control-plane-killed-under-load)
- [Results on FelCloud](#results-on-felcloud)
- [What the benchmarks changed](#what-the-benchmarks-changed) · [Caveats](#caveats) ·
  [Reproduce](#reproduce)

## Summary

| Question | Answer | Where |
| --- | --- | --- |
| How fast is one registration? | **0.6 s** from request to serving traffic through both gateways (lab, warm path) | [provisioning](#provisioning-latency) |
| Do 10+ simultaneous registrations work? | **Yes: 10, 20, 30 and 50 at the same instant, 5 rounds each, 550 teams in all: 550 `active`, 0 failures, 0 duplicate addresses, every host served by its own sandbox** | [bursts](#simultaneous-registrations) |
| How long do they take? | whole burst served in **1.2 s (10), 2.4 s (20), 3.3 s (30), 5.7 s (50)** (lab); **ten at once on FelCloud: 1.5 s warm** | [bursts](#simultaneous-registrations), [FelCloud](#results-on-felcloud) |
| And with no warm VM (cold)? | the platform adds under a second; **on FelCloud the Nova boot dominates: ten concurrent cold registrations took 108–128 s** | [FelCloud](#results-on-felcloud) |
| Gateway failover time? | primary VM dies: **0.56–0.70 s**; Traefik crashes: **1.3–1.5 s**; planned failover: **0.12 s** (longest silence in a 50 requests/s stream; target 2 s / 4 s) | [failover](#gateway-failover) |
| IP recycling speed? | route and DNS gone **2.1 s** after expiry; an address is reusable **1.7 s (median) after its quarantine ends**; **FelCloud: 10 teams torn down in 6 s, addresses free 7.5 s later with a 5 s quarantine** | [recycling](#expiry-and-ip-recycling) |
| Load? | **2,000 requests/s** across 30 teams' hosts, new connection each, 0 failures, p99 6.5 ms (lab, the generator's limit before the gateways') | [throughput](#gateway-throughput) |
| Control plane down? | killed with `-9` for 8 s under 100 requests/s: **0 failed requests**; back and registering in 2.2 s | [crash](#control-plane-killed-under-load) |
| Rollback? | pinning the previous config reaches both gateways in **0.32 s** median | [rollback](#config-rollback) |

## Environments and method

**Lab** (`tests/load`, `tests/chaos`): a 20-thread i7-13650HX laptop with 16 GB, Linux 7.2, PostgreSQL
18, Python 3.14 / 3.12, Traefik 3.7.10, keepalived 2.4.3, in an unprivileged user/network namespace
lab. Everything on the request and configuration path is real: two gateways each running
keepalived, Traefik and the Go `ipo-agent`; the control plane (24 saga workers, all controllers);
a PostgreSQL; a DNS server that takes signed RFC 2136 updates. Two things are not real: the cloud
(a fake that creates servers instantly, so VM boot time is *not* in the lab numbers) and the
sandboxes (one process answering on all 100 pool addresses and echoing the address it was reached
on). Load generators run on the same machine. Treat the lab as a measurement of the platform's
own overhead and of the failover machinery, not of a deployment's absolute speed.

**FelCloud** (`control-plane/bench/real_cloud.py`): the control plane running locally against the
real OpenStack API of the team's project (Nova, Neutron, Glance), real `sandbox-net` and security
groups built by `pulumi up`, sandbox VMs of flavor `G0.basic.1c1g` booted from the stock *Debian
13 - Trixie* image (not the baked one). No gateways stand behind it, so these runs measure the
cloud's contribution and the control plane's use of it, and registration is judged `active` when
the saga finishes, not by a request through a VIP.

**Definitions.** *Accepted*: the HTTP response to `POST /v1/teams`. *Active*: the team's state is
`active` (route acknowledged by both gateways and probed). *Serving*: the first request for the
team's subdomain through the VIP is answered 200 *by that team's sandbox* (the sandbox echoes the
address it was reached on, and the test compares it with the address the API assigned). A burst's
*whole-burst time* runs from the instant the threads are released to the last team serving. A
failover *gap* is the longest stretch with no successful request in a stream of 50 new connections
per second. *Recycling overhead* is the time from an address's quarantine ending to the address
being `free` again.

## Lab results

### Provisioning latency

One team at a time, each deleted before the next, so the platform is otherwise idle.

| Path | Teams | OK | API accepts p50 (s) | Active p50 (s) | Serving p50 (s) | Serving p95 (s) | Serving max (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Warm (VM already booted, one team at a time) | 50 | 50/50 | 0.02 | 0.59 | 0.59 | 0.61 | 0.84 |
| Cold (address reserved and VM created on demand) | 10 | 10/10 | 0.03 | 0.49 | 0.49 | 0.92 | 0.92 |

The API answers in about 20 ms; the remaining ~0.55 s is the saga (route, config compile, push to
both agents, verify, swap, converge, probe, acknowledge). Cold looks no slower than warm here only
because the fake cloud boots instantly: on a real cloud it adds a Nova boot ([below](#results-on-felcloud)).

### Simultaneous registrations

Each round: *N* threads released together, each `POST /v1/teams` with its own idempotency key,
then polls until `active`, then requests its subdomain through the VIP until a sandbox answers.
The warm pool is set to *N* and settled first, and previous teams are gone.

| Simultaneous teams | Rounds | Succeeded | Duplicate addresses | Median team served (s) | Whole burst served, median (s) | Whole burst, worst (s) | Slowest single team (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | 5 | 50/50 | 0 | 0.88 | 1.23 | 1.53 | 1.53 |
| 20 | 5 | 100/100 | 0 | 1.72 | 2.41 | 2.60 | 2.60 |
| 30 | 5 | 150/150 | 0 | 1.96 | 3.30 | 3.41 | 3.41 |
| 50 | 5 | 250/250 | 0 | 3.73 | 5.65 | 6.05 | 6.05 |

A burst of 12 teams against a warm pool of only 3: 3 warm and 9 cold claims, 12/12 succeeded,
whole burst served in 1.33 s. Time grows about linearly, roughly 0.1 s per team: one config
version per debounced batch, each applied and verified on both gateways, and saga steps that wait
on those acknowledgements.

Besides the timing, the same checks run in the continuous-integration suite without the gateways
(`control-plane/tests/test_concurrent_registration.py`): fifteen simultaneous registrations all
succeed on fifteen distinct addresses with both agents holding every route; twelve calls with one
idempotency key create one team; ten claims on one name have exactly one winner; transient
Nova and Neutron errors during a burst are absorbed by step retries.

### Gateway failover

Real keepalived, Traefik and agents under 50 new connections per second.

| Failure | Runs | Requests lost in the gaps | Shortest gap (s) | Median gap (s) | Longest gap (s) |
| --- | --- | --- | --- | --- | --- |
| Primary gateway VM dies (power off) | 10 | 308 | 0.56 | 0.62 | 0.70 |
| Traefik crashes on the primary (kill -9) | 10 | 711 | 1.32 | 1.46 | 1.54 |
| keepalived stopped cleanly | 1 | 5 | 0.12 | 0.12 | 0.12 |
| Manual failover through the agent | 1 | 5 | 0.12 | 0.12 | 0.12 |

Targets were 2 s and 4 s. The Traefik case is slower because the health check must fail twice (one
second apart) before the standby takes over. "Requests lost" counts the requests sent during the
gaps (about 30 per run at this rate): connections to a VIP nobody held for half a second. The
SIGTERM variant, an old master returning, and a partition that makes two masters are also
covered by the suite ([testing](../guides/testing.md)).

### Expiry and IP recycling

Twenty teams whose leases are made to expire at once, with a 5 s quarantine so the run is short;
the route and DNS probes poll about every half second.

| Teams | Quarantine (s) | Route gone (s) | DNS gone (s) | Team deleted (s) | Address free (s) | Recycle overhead p50 / max (s) |
| --- | --- | --- | --- | --- | --- | --- |
| 20 | 5 | 2.10 | 2.10 | 2.10 | 7.58 | 1.72 / 2.58 |

From expiry, every one of the twenty hosts answers 404 and has no DNS record within about 2 s
(reaper tick, teardown, both gateways acknowledging, record withdrawn); a further quarantine plus
under 3 s later (median 1.7 s) the address is `free` again and a new burst of 20 is served from
the same addresses. With the default 60 s quarantine, an address is reusable about 62 s after its
lease ends. The quarantine, not the machinery, is the dominant term and is an admin setting.

### Config rollback

Pinning the previous configuration (an operator's rollback) twenty times: p50 0.32 s, p95 0.52 s,
max 0.62 s until both gateways run it. A failed probe makes the agent roll back by itself without
waiting for an operator.

### Gateway throughput

Thirty teams registered, requests spread over their 30 hosts, a new TCP connection for each
request, 8 s per rate.

| Offered (req/s) | Achieved (req/s) | Failed | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | --- | --- | --- | --- | --- |
| 200 | 200 | 0/1601 | 1.9 | 2.6 | 2.9 |
| 500 | 500 | 0/4001 | 1.9 | 2.5 | 2.9 |
| 1000 | 1000 | 0/8001 | 2.1 | 3.1 | 3.9 |
| 2000 | 2000 | 0/16001 | 3.2 | 5.0 | 6.5 |

No failures up to the highest rate tried; latency starts to rise at 2,000/s. The generator, the
gateways, Traefik and the backend share one machine, so this is a lower bound on what a gateway
VM does, not a capacity figure.

### Control plane killed under load

`kill -9` the control plane under 100 requests/s for one team, wait 8 s, start it again.

| Run | Down for (s) | Requests | Failed | Restart to serving (s) | p99 (ms) |
| --- | --- | --- | --- | --- | --- |
| 1 | 8 | 1477 | 0 | 2.25 | 3.7 |
| 2 | 8 | 1474 | 0 | 2.22 | 3.8 |
| 3 | 8 | 1476 | 0 | 2.24 | 3.7 |

The gateways keep serving the last configuration they verified. After the restart the plane
knows the existing team and registers a new one (checked in each run).

## Results on FelCloud

Ten simultaneous registrations (`uv run python -m bench.real_cloud`, 2026-10-17). The raw lines
are in [`benchmarks-real-cloud.jsonl`](benchmarks-real-cloud.jsonl). FelCloud's boot times varied
from run to run; the numbers below are the last of several runs, and the ranges give the others.

| Measurement | Result |
| --- | --- |
| One VM, created and booted alone | **17 s** to ACTIVE; a delete is accepted in 0.1 s and the VM is gone 5 s later |
| Warm pool filled to 10 (ten boots at once) | **68 s** for the last VM. Nova boot per VM: min 20 s, p50 41 s, p95 64 s. Neutron port creation: p50 1.3 s |
| **10 simultaneous registrations, warm** | **all 10 `active` in 1.5 s** (median team 1.0 s), 0 failures, 10 distinct addresses. Setting the team's metadata on the real VM: p50 0.16 s |
| **10 simultaneous registrations, cold** (no warm VM) | **108–128 s** for the last team across two rounds (83–190 s across all runs); median team 84–88 s, first team 62–66 s. Ten concurrent boots take much longer than a single one |
| **Teardown of 10 teams** | **5.8 s**. Delete server p50 0.13 s, delete port p50 0.8 s |
| **IP recycling** (5 s quarantine) | teardown 3.3 s; addresses `free` after **6.5–8.5 s** (p50 7.5 s), i.e. 2.5 s over the quarantine. With the default 60 s quarantine: about 63 s |

What this says:

- Warm registration on a real cloud is as fast as in the lab (about a second), because it does no
  Nova work: it takes a booted VM from the pool and labels it. That is the reason the pool exists.
- A cold registration costs a minute or two on this cloud when many arrive together. Ten
  concurrent boots are far slower than one (17 s alone), so a pool sized for the expected burst
  (default 12) is the lever, not parallelism. The pool refills at 10 boots in parallel, taking
  about 70 s.
- Teardown and recycling are quick and dominated by the quarantine setting.

Not measured on FelCloud: requests through a real VIP and floating IP (nothing answers behind them
until the Ansible roles run), and failover between two OpenStack VMs.

## What the benchmarks changed

Measuring found six defects, all fixed and covered by regression tests:

| Found by | Defect | Before → after |
| --- | --- | --- |
| first 30-team burst (lab) | Traefik applies at most one provider change per 2 s by default, so each config version cost two seconds on each gateway | 30 teams: **15 s → 3.3 s** (`providersThrottleDuration: 100ms`) |
| burst scaling (lab) | two saga workers, each polling the job table, mostly waiting on gateway acknowledgements | **24 workers woken by `NOTIFY`** (idle workers cost no queries). In an isolated A/B 30 teams went 3.5 s → 2.1 s (8 → 24 workers); in the full run, with the previous bursts' cleanup overlapping, the gain is smaller (3.6 → 3.3 s; 50 teams 6.2 → 5.7 s) |
| lifecycle test (lab) | the admin's runtime quarantine setting was ignored by teardown | now read live |
| lifecycle test (lab) | a teardown retried after a partial release found the lease already quarantined and deleted nothing | the retry deletes what is left |
| first FelCloud run | a "warm" claim could take an address whose VM Nova was still booting, waiting out the boot while reporting the warm path | only pooled leases with a running VM are claimable and counted |
| first FelCloud run | the reconciler deleted VMs that were still building (a boot outlasts its 30 s grace and the VM is not yet matched to its port), then failed their teams | a building VM is not an orphan; its name says which address it is for |
| second FelCloud run | teardown's check for leftovers counted servers Nova had accepted a delete for or keeps listing as DELETED, so every teardown failed over to the reconciler's minute | those are not counted or reused: **10 teams torn down in 85 s → 6 s** |

## Caveats

- **Lab numbers are not deployment numbers.** One machine runs the load generator, both gateways,
  the control plane and the database; the cloud is instant. They measure the platform's overhead
  and prove its behaviour under concurrency.
- **FelCloud is a shared cloud and its boot times varied** (a single VM 17 s; ten at once 20–100 s
  to boot; whole cold bursts of 83–190 s across runs).
- **Failover is measured with the real software in namespaces**, not between two OpenStack VMs.
- The cold FelCloud numbers use the stock Debian image; the baked image (Podman and the
  application preinstalled) would shorten the first-boot work but not Nova's scheduling and
  image-copy time.
- Single runs for keepalived stop and manual failover; ten for the rest; five rounds for bursts;
  three for the crash; two cold FelCloud rounds.

## Reproduce

```bash
cd tests
uv sync --group deploy && sh lab/fetch-tools.sh
uv run python -m load.bench                 # lab: load + chaos suites, then the tables (about 12 min)
uv run python -m load.run -k simultaneous   # just the bursts (IPO_LOAD_SIZES=10,20,30,50, IPO_LOAD_ROUNDS=5)

# FelCloud (needs the platform built: uv run python -m chaos.cloud up --clouds ~/clouds.yaml)
cd ../control-plane
uv run python -m bench.real_cloud --clouds ~/clouds.yaml --count 10 --out real.jsonl
cd ../tests && uv run python -m chaos.cloud destroy --clouds ~/clouds.yaml
```

`load.report RUN_DIR` regenerates the lab tables from a run's `load-report.jsonl` and
`chaos-report.jsonl`.
