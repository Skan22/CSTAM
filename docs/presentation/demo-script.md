# Demo video: script and runbook

The required video is short and shows three things: **active subdomain routing**, **dynamic
hot-reloading** and a **simulated primary gateway failover**. This script covers those in about
four minutes and adds the two things the jury scores in Phase 2, a **burst of simultaneous
registrations** and **automated teardown**. Everything runs on one laptop, with real keepalived,
Traefik and `ipo-agent`, and needs no root.

## Before recording

```bash
cd tests
uv sync --group deploy && sh lab/fetch-tools.sh        # once: Traefik and keepalived
uv run python -m chaos.demo --auto                     # the scripted version: nothing to type
uv run python -m chaos.demo                            # the interactive one: you press the keys
uv run python -m chaos.demo --clouds ~/clouds.yaml     # with the real cloud built first (pulumi up)
```

- Terminal at least **120 × 36**, dark background, font size large enough to read in a 1080p
  recording. `q` leaves the demo.
- `--auto` follows the script below by itself and narrates in a caption bar, so you can talk over
  it. Use the interactive mode if you prefer to drive: the keys are shown at the bottom.
- With `--clouds` the demo first runs `pulumi up` against FelCloud (about two minutes, the screen
  shows each resource appear in the network map and the quota bars), then continues on the local
  lab. It asks before creating anything billed, and tears the cloud down at the end unless you
  pass `--keep`. Record this opening if you want the "built on the real cloud" shot; cut to the
  lab afterwards, because the VMs the program creates are not configured by Ansible.
- Do a dry run first. The recording should be one take; the timings below are what to expect.

## Storyboard

| Time | Scene | On screen | Say |
| --- | --- | --- | --- |
| 0:00 | **Intro** | the header and two gateway panels, `gw-a` MASTER holding the VIP, `gw-b` BACKUP | "Two gateways share one virtual IP. keepalived decides who holds it; Traefik routes each request by its subdomain. Everything you see is the real software." |
| 0:20 | **1. Subdomain routing** (key `n`, or automatic) | the routes table lists the first team; the stream shows `200`; a request for an unknown name logs `404` | "A registered team's subdomain reaches its sandbox. A name nobody registered gets a 404." |
| 0:45 | **2. Hot reload** (keys `r`, `alpha`; `r`, `bravo`) | two new rows go `pending` → `active`; the log says each answers 200 a second after the request; the Traefik process ids in the gateway panels do not change | "I register two teams while requests keep flowing. The control plane compiled a signed config, both agents verified it, swapped it in, probed the new route and committed. Traefik reloaded without a restart: look, same process ids." |
| 1:20 | | `d`, `bravo` | "Removing a team removes its route the same way; alpha never noticed." |
| 1:35 | **3. Burst** (key `b`) | twelve rows appear at once; the log: "12 teams on 12 different sandboxes, all answering 1.x s after the first request" | "Now twelve teams register in the same instant. Each gets its own VM and address, and all twelve answer on their own subdomain within about a second and a half. No duplicate address, no failure." |
| 2:00 | **Teardown** (key `e`) | the rows go `draining` → disappear; their subdomains answer 404 within seconds | "When a lease ends, the platform removes the route, the DNS record, the VM and the port, checks nothing is left, and only then lets the address be reused." |
| 2:20 | **4. Failover** (key `f`) | the request stream; `gw-b` becomes MASTER in a fraction of a second; `via=` flips to the other gateway; no failed request | "The control plane faults the primary. The VIP moves in about a tenth of a second and the stream does not drop a request." |
| 2:50 | | key `h`, then `k` (kill VM), then `h` | "Harder: power off the primary's VM. The standby takes over in about half a second; only requests in flight at that instant can notice. When the VM returns it rejoins as BACKUP and does not take the VIP back." |
| 3:30 | **Split brain** (keys `x`, `h`) | the banner "SPLIT BRAIN"; healed | "If the heartbeat between healthy gateways is cut, both claim the VIP. The control plane hears from both and raises the alarm; healing leaves one." |
| 3:55 | **Close** | the summary line | "Resilient, hot-reloadable, and isolated per team." |

`--auto` runs exactly these scenes (subdomain routing, hot reload, burst, expiry, failover, VM
death, split brain), holding each caption for the time shown.

## What to say about the numbers (lab, one laptop)

Quote these from [the benchmark report](../reports/benchmarks.md), and say they are lab numbers:

- a team is serving traffic about **0.6 s** after the request (warm path); **30 teams at once** are
  all serving within about **3 s**, with no failures or duplicate addresses;
- primary VM death: the longest gap in a 50 requests/s stream is about **0.55–0.7 s**; Traefik
  crash **1.3–1.5 s**; a deliberate failover **0.12 s**;
- a team's route is gone about **2 s** after its lease ends, and its address is free again one
  quarantine period (default 60 s) plus about **2 s** later;
- the control plane killed for 8 s under load: **0 failed requests**.

## If something goes wrong on camera

| Symptom | Cause | Fix |
| --- | --- | --- |
| "unprivileged user namespaces are not available" | the kernel forbids `unshare` | `sysctl kernel.unprivileged_userns_clone=1` (Debian/Arch) and retry |
| The terminal is garbled | too small | resize to at least 120 × 36 |
| The first request after boot is slow | gateways still electing a master | wait for one MASTER in the header |
| The burst row count is below 12 | the dashboard panel is short | enlarge the window; the log line still reports all 12 |
| `pulumi up` stops at the preview | it is asking before creating billed resources | press Enter |

## After recording

`python -m chaos.demo --clouds … ` tears the cloud down itself; check with
`uv run python -m chaos.cloud status --clouds ~/clouds.yaml` that the project is empty (no
servers, no networks, no floating IPs).
