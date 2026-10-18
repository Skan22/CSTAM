# Deployment

Three layers, each from a file in the repository and never by hand: **Pulumi** builds the cloud
resources (day 0), **Ansible** configures the hosts (day 1), and **Podman Quadlets** run the
services (day 2). `platform.yaml` and `security-groups.yaml` feed all three.

| Layer | Directory | Does | Tested by |
| --- | --- | --- | --- |
| Pulumi | `infra/pulumi` | networks, routers, security groups, VIP port, floating IPs, 6 VMs, key pair, CrossGuard policies | mocked tests; run against the real cloud |
| Ansible | `ansible` | 14 roles and 6 playbooks (below) | `tests/deploy`: every template read by its real program; `ansible-lint` production profile; run on real FelCloud VMs |
| Quadlets | `quadlets` | 13 container units with health checks and auto-update | Podman 5.4.2's own Quadlet generator |

## Before you start

1. **Credentials.** A `clouds.yaml` for the OpenStack project, kept outside the repository
   (`OS_CLIENT_CONFIG_FILE`, `OS_CLOUD`). For the control plane use an *application credential*.
2. **Tools** on the machine that runs the deployment: `pulumi`, `ansible-core` 2.17+ with
   `ansible-lint`, `sops`, `openstack` CLI (for the image bake), `ssh`.
3. **Secrets.** Copy `ansible/secrets.example.yaml`, fill every key (it lists all 14 the roles
   read), encrypt it with SOPS as `secrets.enc.yaml`, and decrypt next to it as `secrets.yaml`
   (git-ignored) for a run. Generate the signing key with
   `python -c "from ipo.domain.signing import Signer; s=Signer.generate(); print(s.private_b64, s.public_b64)"`
   in `control-plane/`.
4. **Artifacts** the roles copy from the repository:
   - `gateway-agent/dist/ipo-agent`: `cd gateway-agent && CGO_ENABLED=0 go build -o dist/ipo-agent ./cmd/ipo-agent`
   - `dashboard/dist/`: `cd dashboard && npm ci && npm run build`
   - the control-plane image: `podman build -f control-plane/Containerfile -t <registry>/control-plane:dev .`
     from the repository root, pushed to the registry named by `ipo_registry` in
     `ansible/inventory/group_vars/all.yml` (a placeholder, `registry.felcloud.tn/ipo`: set it).

## Day 0: Pulumi

```bash
cd infra/pulumi
uv sync
pulumi login <backend>                         # a Swift bucket, or file://~/.ipo-state
pulumi stack init dev --secrets-provider passphrase
pulumi config set ipo:publicKey "$(cat ~/.ssh/ipo_ed25519.pub)"
pulumi preview --policy-pack policy            # what CI runs on every pull request
pulumi up --policy-pack policy                 # about 90 s from an empty project
mkdir -p outputs && pulumi stack output --json > outputs/dev.json   # Ansible reads this
```

Settings for what the cloud may not give you: `ipo:floatingIps=false` (if floating IPs cannot be
allocated) and
`ipo:antiAffinity=anti-affinity` (hard separation, only if the cloud has several hosts). See
[configuration](configuration.md#ansible-and-pulumi-settings).

`pulumi up` creates about 80 cloud resources (83 with the floating IPs), plus a few grouping components: 3 networks and subnets, 2
routers, 9 security groups with 38 rules, 12 ports, 6 VMs, 2 server groups, the key pair and
(when available) 2 floating IPs. **They are billed per second.** `pulumi destroy` removes them
in about 30 s.

The CrossGuard pack refuses: anything open to `0.0.0.0/0` except 80 and 443 on the gateways and
WireGuard on the bastion; port security off; a VM without a `role` tag; DHCP on sandbox-net. In
a *preview* it cannot evaluate resources whose inputs are not yet known, so the full pre-merge
gate is the mocked test in `infra/pulumi/tests`; at `up` every resource is checked.

## Day 1: Ansible

The roles have been run on real FelCloud VMs ([the first real deployment](../reports/real-deployment.md)).
A first run differs from later ones in two ways: nothing can reach the hosts yet except through
the bastion's floating IP, and the bastion's WireGuard does not exist until this run creates it.

```bash
# day 0, with SSH to the bastion opened for your address for this first run only
pulumi config set ipo:bootstrapSshFrom "$(curl -s https://api.ipify.org)/32" && pulumi up

cd ansible
ansible-galaxy collection install -r requirements.yml     # only the openstack.cloud inventory plugin
# run.yml:
#   ipo_jump: debian@<the bastion's floating IP>   # every other host is reached through it
#   bootstrap_ssh_from: <your address>/32          # the bastion's own firewall keeps SSH open for it
#   control_plane_build_on_host: true              # no registry: build the image on cp-1 and cp-2
#   ipo_registry: localhost/ipo
ansible-playbook playbooks/site.yml -e @secrets.yaml -e @run.yml
```

Then set up a WireGuard peer (the play prints the bastion's public key), remove
`ipo:bootstrapSshFrom` and `bootstrap_ssh_from`, and run both again: from then on operators come
in through the tunnel only. `inventory/openstack.yml` builds the inventory from the VMs' `role` tag
(use `-i inventory/static.yml` if the API is down).

`site.yml` runs, in order:

| Order | Hosts | Roles |
| --- | --- | --- |
| 1 | bastion | `common`, `hardening`, `bastion` (WireGuard, and the egress proxy the others need next) |
| 2 | all | `common` (users, time, journald, operators, apt through the egress proxy), `hardening` (nftables from `security-groups.yaml`, sysctls, SSH, auditd), `alloy` (metrics and logs; tag `observability`) |
| 3 | cp-1, then cp-2 | `podman`, `step_ca`, `postgres`, `control_plane`; cp-1 first because it holds the CA and the primary |
| 4 | cp-2 | `observability` (Prometheus, Alertmanager, Loki, Tempo, Grafana; tag `observability`) |
| 5 | gw-a, then gw-b, one at a time so the other keeps the VIP | `podman`, `step_ca`, `gateway_network`, `keepalived`, `gw_agent`, `traefik` |
| 6 | relay | `relay` |

Other playbooks: `bake_image.yml` (boot a builder, harden it, install Podman, Alloy and the
placeholder page, strip machine id and SSH host keys, snapshot as `ipo-sandbox-vN`, delete it;
about 15 minutes on FelCloud), `promote_replica.yml`, `rotate_certs.yml`.

### What a FelCloud deployment needs to know

- **No SNAT.** FelCloud's routers give no outbound route to a VM without a floating IP. The bastion
  (which has one) runs an allow-listed HTTP(S) proxy on its management address, and apt and Podman
  on the other hosts use it (`ipo_egress_proxy`, set automatically; empty it on a cloud whose
  routers do SNAT). The allow-list is `bastion_proxy_allow`: Debian, Grafana's apt repository,
  Docker Hub and its CDNs, GHCR, Quay, GitHub releases and PyPI. Anything else is refused, so add a
  host there if a role needs a new source. Sandboxes have no route out at all, by design: what they
  need is baked into their image.
- **The control plane's cloud credential.** `control_plane_cloud: openstack` (the default) needs an
  application credential in `ipo_secrets.clouds_yaml`. `fake` runs everything else for real but
  boots no sandboxes, for trying the platform out.
- **Registration probes through the gateways.** The management network has no path to the VIP, so
  the control plane asks the agents to request a new team's host (`POST /probe`). A sandbox on
  FelCloud serves about 90 s after it is created, so the probe waits up to `IPO_PROBE_TIMEOUT`
  (180 s).
- The base image must be Debian 13 (Podman 5.0+); the `podman` role refuses older versions.
- The first start of the control plane re-owns its image for its user namespace (several minutes
  on a cloud disk); the role does that before the unit starts.

## Day 2: Quadlets

The `podman` role installs the units from `quadlets/` into the rootless `ipo` user's
`~/.config/containers/systemd/`; the service roles start them. Ports, hosts and the rules every
unit follows are in [`quadlets/README.md`](../../quadlets/README.md). Images are pinned to a tag;
`podman-auto-update.timer` follows it, and `Notify=healthy` makes a unit active only once its
health check passes, so an image that never becomes healthy is rolled back.

## DNS

Teams are reached by `<slug>.<domain>` (`domain` in `platform.yaml`). FelCloud has no DNS service,
so the default needs one record, made once by whoever runs the zone: **`*.<domain> A <floating IP
of the VIP>`**. The platform then publishes nothing per team (`control_plane_dns_mode: wildcard`).
If the zone is yours and takes dynamic updates (BIND, Knot, PowerDNS, CoreDNS), set
`control_plane_dns_mode: rfc2136` with the server, the floating IP as `control_plane_dns_target`,
a TSIG key name and its secret (`ipo_secrets.dns_tsig_secret`): each registration then adds the
team's A record and each teardown removes it, and the reconciler repairs any drift. Without any
DNS at all, `curl -H 'Host: <slug>.<domain>' http://<floating IP>/` reaches the same sandbox.

## Verify

1. `GET https://<cp-1 mgmt IP>:8000/readyz` returns 200 on both control-plane hosts.
2. The dashboard's Gateways page shows exactly one MASTER and both gateways on the same config version.
3. Register a test team and request its subdomain through the floating IP, from outside.
4. `uv run python -m isolation.cloud_probe --clouds … --pool-end` (in `tests/`) boots probe VMs
   with the real security groups beside the deployment and exits non-zero on any leak.
5. Fail over (`POST /v1/gateways/failover`) and check the other gateway becomes MASTER.

## CI

`.ci/pipeline.sh` runs, in order: control-plane lint, types and tests; Pulumi lint, types and
mocked tests; system-test lint and types; Ansible lint, playbook syntax and every rendered config
through its real program; the isolation lab; and (with `IPO_CI_CHAOS=1` and `IPO_CI_LOAD=1`) the chaos and load labs.
`.ci/pulumi.sh preview|up` is the pull-request and merge job; `.ci/isolation-dev.sh` is the
nightly isolation check against a deployment.

## Tear down

`pulumi destroy` from `infra/pulumi` removes everything Pulumi created. Back up first if the
database matters ([restore runbook](../runbooks/restore-backup.md)).
