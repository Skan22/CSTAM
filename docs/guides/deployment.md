# Deployment

Three layers, each from a file in the repository and never by hand: **Pulumi** builds the cloud
resources (day 0), **Ansible** configures the hosts (day 1), and **Podman Quadlets** run the
services (day 2). `platform.yaml` and `security-groups.yaml` feed all three.

| Layer | Directory | Does | Tested by |
| --- | --- | --- | --- |
| Pulumi | `infra/pulumi` | networks, routers, security groups, VIP port, floating IPs, 6 VMs, key pair, CrossGuard policies | mocked tests; run against the real cloud |
| Ansible | `ansible` | 14 roles and 6 playbooks (below) | `tests/deploy`: every template read by its real program; `ansible-lint` production profile |
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

Settings for what the cloud may not give you: `ipo:floatingIps=false` (no free floating IPs) and
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

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml     # only the openstack.cloud inventory plugin
ansible-playbook playbooks/site.yml -e @secrets.yaml      # run it twice: the second run changes nothing
```

`inventory/openstack.yml` builds the inventory from the VMs' `role` tag (use `-i
inventory/static.yml` if the API is down); operators reach the hosts through the bastion's
WireGuard tunnel (`bastion_peers` in `inventory/group_vars/bastions.yml`).

`site.yml` runs, in order:

| Order | Hosts | Roles |
| --- | --- | --- |
| 1 | all | `common` (users, time, journald, operators), `hardening` (nftables from `security-groups.yaml`, sysctls, SSH, auditd), `alloy` (metrics and logs) |
| 2 | cp-1, then cp-2 | `podman`, `step_ca`, `postgres`, `control_plane`; cp-1 first because it holds the CA and the primary |
| 3 | cp-2 | `observability` (Prometheus, Alertmanager, Loki, Tempo, Grafana) |
| 4 | gw-a, then gw-b, one at a time so the other keeps the VIP | `podman`, `step_ca`, `gateway_network`, `keepalived`, `gw_agent`, `traefik` |
| 5 | relay, bastion | `relay`; `bastion` (WireGuard) |

Other playbooks: `bake_image.yml` (boot a builder, harden it, strip machine id and SSH host keys,
snapshot as `ipo-sandbox-vN`, delete it), `promote_replica.yml`, `rotate_certs.yml`.

Notes for a first run:

- The base image must be Debian 13 (Podman 5.0+); the `podman` role refuses older versions.
- The `alloy` role installs from Grafana's apt repository and supports Debian only.
- Roles use only `ansible.builtin`, so no collection beyond the inventory plugin is needed.
- The playbooks have been linted and syntax-checked and every template has been read by the
  program that will use it, **but they have not been run on real VMs yet**. Expect to find
  environment issues on the first run; each role is small and idempotent.

## Day 2: Quadlets

The `podman` role installs the units from `quadlets/` into the rootless `ipo` user's
`~/.config/containers/systemd/`; the service roles start them. Ports, hosts and the rules every
unit follows are in [`quadlets/README.md`](../../quadlets/README.md). Images are pinned to a tag;
`podman-auto-update.timer` follows it, and `Notify=healthy` makes a unit active only once its
health check passes, so an image that never becomes healthy is rolled back.

## Verify

1. `GET https://<cp-1 mgmt IP>:8000/readyz` returns 200 on both control-plane hosts.
2. The dashboard's Gateways page shows exactly one MASTER and both gateways on the same config version.
3. Register a test team and request its subdomain through the VIP.
4. `.ci/isolation-dev.sh` runs the isolation probes from a sandbox and from the internet and
   exits non-zero on any leak.
5. Fail over (`POST /v1/gateways/failover`) and check the other gateway becomes MASTER.

## CI

`.ci/pipeline.sh` runs, in order: control-plane lint, types and tests; Pulumi lint, types and
mocked tests; system-test lint and types; Ansible lint, playbook syntax and every rendered config
through its real program; the isolation lab; and (with `IPO_CI_CHAOS=1`) the chaos lab.
`.ci/pulumi.sh preview|up` is the pull-request and merge job; `.ci/isolation-dev.sh` is the
nightly isolation check against a deployment.

## Tear down

`pulumi destroy` from `infra/pulumi` removes everything Pulumi created. Back up first if the
database matters ([restore runbook](../runbooks/restore-backup.md)).
