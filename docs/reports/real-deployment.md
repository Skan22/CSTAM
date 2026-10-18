# The first real deployment

On 2026-10-18 the whole platform was deployed on FelCloud for the first time: `pulumi up`, then
the Ansible roles on the six real VMs, then the baked sandbox image. It was exercised from the
internet and taken down again. This report covers what ran, what was measured, and the
**21 defects it found** that no lab, mock or unit test had caught. All 21 are fixed, and most now
have a regression test.

- [What ran](#what-ran) · [Results](#results) · [Defects found](#defects-found-and-fixed) ·
  [What this deployment did not cover](#what-this-deployment-did-not-cover) · [Reproduce](#reproduce)

## What ran

| Layer | How | Outcome |
| --- | --- | --- |
| Cloud | `pulumi up` (91 resources) with `ipo:bootstrapSshFrom` for the first SSH to the bastion | 6 VMs, 3 networks, 2 routers, 9 groups, 2 floating IPs |
| Hosts | `site.yml` on all six, through the bastion (`ipo_jump`), observability tag skipped | every role converged on every host (whether a second run changes nothing was not checked) |
| Control plane | two replicas on cp-1 and cp-2, Postgres primary and replica, step-ca, the Caddy front | built on the hosts (`control_plane_build_on_host`), as no registry was available; cloud adapter `fake` (see below) |
| Gateways | keepalived, Traefik and `ipo-agent` on gw-a and gw-b, with mutual TLS and signed configs | gw-a MASTER, gw-b BACKUP, the agents heartbeat with their narrow `gateway` accounts |
| Sandbox image | `bake_image.yml`: builder VM, hardening, Podman, Alloy, the placeholder page, snapshot | `ipo-sandbox-v1` in 15 min (end to end, upload included) |
| Sandboxes | booted from `ipo-sandbox-v1` on `sandbox-net`, at the addresses the control plane assigned | served through the gateways and the floating IP |

**Why the control plane used the fake cloud adapter.** A real one needs an OpenStack application
credential on the control-plane hosts, and creating one from this session was (rightly) refused:
a credential that outlives the session is the team's call to make. Everything else on the
registration path ran for real: API, sagas, compiler, signing, pushes to both agents, the
gateways, Traefik, VRRP and the probe. The VMs at the assigned addresses were booted from the
baked image by hand. To run fully real, put an application credential in
`ipo_secrets.clouds_yaml` and set `control_plane_cloud: openstack` (the default).

## Results

### Failover between two real gateway VMs

Load came from the bastion: 50 new connections per second to the **floating IP** (internet →
FIP → VIP → the MASTER's Traefik). The gap is the longest stretch without a successful request.

| Failure | Requests | Failed | Longest gap | Lab (same software in namespaces) |
| --- | --- | --- | --- | --- |
| MASTER VM loses power (Nova hard reboot) | 2,251 | 49 | **1.00 s** | 0.52–0.72 s |
| Traefik killed on the MASTER (systemd restarts it within a second, so no failover is needed) | 1,251 | 65 | **1.32 s** | — |
| Traefik stopped on the MASTER and left down (health check fails twice → FAULT) | 1,251 | 67 | **1.36 s** | 1.38–1.54 s |
| Planned failover: `POST /v1/gateways/failover` → agent `/fault` over mutual TLS | 1,001 | 11 | **0.24 s** | 0.12 s |

After its power loss gw-a booted, reported FAULT until Traefik was up, then rejoined as BACKUP and
**did not take the VIP back** (`nopreempt`). The control plane reported each state change and no
split brain. Targets were 2 s for a VM death and 4 s for a Traefik failure. Raw lines:
[`failover-real-cloud.jsonl`](failover-real-cloud.jsonl).

### The edge path and registration, end to end

- From a laptop on the internet: `curl -H 'Host: delta.cstam.felcloud.tn' http://<floating IP>/`
  → `sandbox ok host=delta.cstam.felcloud.tn node=ipo-vm-10-20-0-10`, served by the team's own VM.
- **A registration onto a booted sandbox: active 0.6 s after the request**, on the first attempt.
  The config reached both gateways in 0.22 s (signed, verified, swapped, probed) and the
  gateways' probe of the team succeeded in 0.04 s.
- **A registration whose sandbox is still booting** (`golf`: VM created at the same moment): the
  gateways loaded the route in 2.4 s and served 504 until the VM was up. Registration waited
  through the gateways and the team was **active 94 s after the request**, on one attempt, serving
  from the internet. Before the fixes below, this sequence rolled the config back and would have
  blocked every later registration.

### Isolation, against the real security groups and the configured hosts

`isolation.cloud_probe --pool-end` ([raw](isolation-real-cloud.json)): **270 probes, 0 leaks, every
expected-open TCP path reached**. That includes the sandbox → gateway 80/443 paths an earlier run
could not prove before the hosts were configured. The one unverified path is UDP syslog to the
relay, which never replies by nature. Port security: **0 of 32 forged-source packets delivered**
(neighbour's IP, a gateway's IP, an unused IP, a forged MAC), while the honest datagrams arrived.

### The baked sandbox image

| Image | Nova ACTIVE | Serving its page on port 80 | Has Podman, Alloy, the firewall |
| --- | --- | --- | --- |
| `ipo-sandbox-v1` (baked) | 20 s | 88 s | yes |
| stock Debian 13 + the page via cloud-init | 24 s | 102 s | no, and it **cannot get them**: sandboxes have no route out |

So baking saves about 15 s of boot. Its real value is that a sandbox is complete without network
access, which the isolation design requires. A guest on FelCloud needs about 90 s to serve
whichever image it boots, so registration now waits up to 180 s for the probe (`IPO_PROBE_TIMEOUT`),
and the warm pool is what makes registration fast.

## Defects found and fixed

| # | Found | Defect | Fix | Guarded by |
| --- | --- | --- | --- | --- |
| 1 | first package install | **FelCloud's routers do not SNAT**: a VM without a floating IP has no route out (a floating IP on cp-1 fixed its egress at once, proving it) | the bastion runs an **allow-listed egress proxy** (tinyproxy, `FilterDefaultDeny`); a rule in `security-groups.yaml` opens 3128 from the platform groups; apt and Podman use it, containers do not | isolation lab (3128 probed), deploy tests |
| 2 | proxy rule for the image builder | an ephemeral group (no fixed address) rendered an empty source set: invalid nftables that would have broken the bastion's firewall | the host rule names the group's subnet; the security group stays exact | isolation lab, deploy tests |
| 3 | first SSH | nothing could reach the bastion before WireGuard, which the same run configures | `ipo:bootstrapSshFrom` opens SSH to one address range for the first run; hosts are reached through the bastion (`ipo_jump`) | Pulumi tests (policy pack still passes) |
| 4 | first package install | an image can ship an apt stamp with empty lists, so `cache_valid_time` skipped the refresh and no package existed | refresh every run | — |
| 5 | step-ca, then Caddy | both images give their binary a **file capability**; with `DropCapability=all` the kernel refuses to exec it (EPERM) | `AddCapability=NET_BIND_SERVICE` on those two units | Quadlet generator test |
| 6 | Postgres bootstrap | `podman exec` ran psql as the container's root; `pg_hba.conf` admits the superuser on the socket by peer only | exec as `postgres` | — |
| 7 | first migration | the app login **cannot create roles** (rightly), but the first migration creates `ipo_app` | the bootstrap SQL, run as superuser, creates it | deploy test now runs every migration as the app login (fails without the fix) |
| 8 | control plane start | `ipo` was added to `ipo-pki` after lingering had started its systemd manager, so its containers could not open the key; restarting the manager to fix it wedged them | the group and membership are created with the user, before lingering | — |
| 9 | control plane start | step wrote `root_ca.crt` 0600 root, so the control plane could not verify the agents | 0644 (it is public) | — |
| 10 | control plane start | `UserNS=keep-id` makes rootless Podman re-own the whole image on first run: minutes on a cloud disk, holding the storage lock, so every unit timed out and looped | Ansible does it once before the start, without a timeout; units get 600 s | — |
| 11 | relay | Alloy pinned as `1.20.1`; the package is `1.20.1-1` | match the Debian revision | — |
| 12 | VRRP state `UNKNOWN` | keepalived runs scripts with the user's **primary group only**, so the notify hook could not write the state through a supplementary group (the lab cannot drop groups, so it never showed) | `script_user keepalived_script ipo-vrrp` | deploy test asserts the rendered line |
| 13 | first registration | the deployed fake cloud kept servers in memory; each restart forgot the warm pool the leases named | it adopts what the leases record at start | unit test |
| 14 | redeploy | the control-plane image was built only when missing, so code changes never reached the hosts | rebuild when the source changes | — |
| 15 | registration stuck in `probe` | **the control plane cannot reach the VIP**: the management network has no path to the edge network (by design, and there is no SNAT) | the probe asks the gateways' agents (`POST /probe`, control-plane certificate only) to request the host through their own Traefik | Go and Python tests; real registration |
| 16 | booting sandbox | the agent rolled back a config whose new team's VM did not answer yet; the next change carries that route again, so **one slow VM blocked every later registration** | a config is rolled back only if a route is missing (404) or the canary fails; the control plane waits for the backend | Go tests (inverted), real registration |
| 17 | booting sandbox | an address with nothing behind it does not refuse, it hangs (no ARP reply); Traefik waited 30 s, the agent's probe 1 s | Traefik `dialTimeout: 2s` for team backends (a dead backend gives 502/504 in 2 s); the probe waits 4 s | real Traefik checked |
| 18 | logs | **every config push failed**: httpx 0.28 silently drops the client certificate when the CA is a path. The agents' 5 s config pull had hidden it | an explicit TLS context | test against a server that requires a client certificate (fails without the fix) |
| 19 | job on attempt 5 | a worker refreshed its lock only between steps; a step longer than 60 s (a probe, a cold boot) looked abandoned and was run twice | the lock is kept fresh during a step | test with two workers (fails without the fix) |
| 20 | reviewing cold boots | sandboxes were created **without a config drive**; sandbox-net has no DHCP, so they would have had no address | `config_drive=True` | unit test |
| 21 | image bake | facts gathered before the builder answered SSH; snapshot requested while the VM was still powering off (409); no `inventory_dir` for the builder | wait first; wait for SHUTOFF; fall back to the playbook directory | the bake ran end to end |

Two gaps from the security review were closed in the same pass:
- **Gateway agents now log in with a `gateway` role** bound to their own gateway. It is admitted
  only to that gateway's heartbeat and traffic report and to the config pull, and refused on
  every other route (a test walks the OpenAPI document).
- **Every team's route carries a rate limit keyed by its Host** (200 requests/s, burst 100). In
  the lab, a team flooding at 1,200/s gets 429s while its neighbour is untouched.

## What this deployment did not cover

- **The control plane's real cloud adapter** on the hosts. It is exercised separately by
  `bench.real_cloud` from a workstation; here it was `fake`, for the credential reason above.
- **The observability stack** (Prometheus, Loki, Tempo, Grafana, Alertmanager on cp-2, and Alloy
  on the platform hosts): skipped with `--skip-tags observability` to keep the run short. Alloy
  did install on the relay and in the image.
- **WireGuard with a real peer**, `podman auto-update` rollback, and promoting the Postgres replica.
- The gateway pair may share a compute host (FelCloud honours only soft anti-affinity), so a
  host failure was not tested, only a VM failure.

## Reproduce

```bash
cd tests
IPO_BOOTSTRAP_SSH_FROM=<your address>/32 uv run python -m chaos.cloud up --clouds ~/clouds.yaml
# the inventory reads the stack's outputs (with the demo's PULUMI_BACKEND_URL and passphrase file):
cd ../infra/pulumi && pulumi stack output --json --stack live > outputs/live.json
cd ../../ansible
cat > run.yml <<EOF
ipo_stack: live
ipo_jump: debian@<bastion floating IP>
bootstrap_ssh_from: <your address>/32
ansible_ssh_private_key_file: ~/.local/share/ipo-demo/ssh_ed25519
control_plane_build_on_host: true
ipo_registry: localhost/ipo
EOF
ansible-playbook -i inventory/static.yml -i bastion-public.yml playbooks/site.yml \
  -e @secrets.yaml -e @run.yml --skip-tags observability
ansible-playbook ... playbooks/bake_image.yml -e image_version=1 -e builder_key=ipo-ops
cd ../tests && uv run python -m isolation.cloud_probe --clouds ~/clouds.yaml --pool-end
uv run python -m chaos.cloud destroy --clouds ~/clouds.yaml
```

`bastion-public.yml` names the bastion by its floating IP for the first run:
`all: {children: {bastions: {hosts: {bastion: {ansible_host: <floating IP>}}}}}`.
