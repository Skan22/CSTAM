# Ansible (Day 1)

Turns the VMs Pulumi created into the running platform. Every role uses `ansible.builtin` only;
the one collection (`openstack.cloud`, `requirements.yml`) is for the dynamic inventory.

```
ansible-galaxy collection install -r requirements.yml
sops -d secrets.enc.yaml > secrets.yaml          # shape: secrets.example.yaml (git-ignored)
ansible-playbook playbooks/site.yml -e @secrets.yaml
ansible-playbook -i inventory/static.yml ...     # same hosts, from platform.yaml, if the API is down
```

`site.yml` runs, in order: `common`, `hardening` and `alloy` everywhere; `control_plane.yml`
(cp-1 first: it holds the CA and the Postgres primary that cp-2 copies; then observability on
cp-2); `gateways.yml` (one gateway at a time, so the other keeps the VIP); the relay; the bastion.
Other playbooks: `bake_image.yml` (sandbox image), `promote_replica.yml`, `rotate_certs.yml`.

| Role | Hosts | What it does |
| --- | --- | --- |
| `common` | all | service user `ipo`, operators and their keys, chrony, journald limits, apt pins |
| `hardening` | all | nftables from `security-groups.yaml` (per-host addresses, per-source rate limits), sysctls, SSH, auditd, SELinux where the OS has it |
| `podman` | gateways, control plane | Podman ≥ 5, lingering `ipo` user, auto-update timer; `tasks/quadlet.yml` and `tasks/secret.yml` for the service roles |
| `gateway_network` | gateways | answers from edge addresses leave by the edge port (port security drops them otherwise) |
| `keepalived` | gateways | VRRP pair from `platform.yaml`, health check, notify hook, `/run/ipo` shared with the agent |
| `gw_agent` | gateways | agent binary and a systemd unit `systemd-analyze security` rates 1.2 "OK" (no capabilities, read-only system) |
| `traefik` | gateways | static config and the Traefik Quadlet |
| `step_ca` | cp-1 (CA), gateways and control plane (certificates) | step-ca Quadlet, enrollment with one-time tokens, a renewal timer gated on `needs-renewal` |
| `postgres` | cp-1 primary, cp-2 replica | Quadlet, pg_basebackup seeding, roles and database, nightly dumps to object storage |
| `control_plane` | cp-1, cp-2 | API Quadlet, its secrets, the agents' logins, the dashboard and the Caddy front on :8000 |
| `observability` | cp-2 | Prometheus, Alertmanager, Loki, Tempo and Grafana (served under `/grafana/` by the front) |
| `alloy` | all, and the sandbox image | host metrics and journal, pushed to cp-2 (sandboxes: to the relay over OTLP) |
| `relay` | relay | OTLP and syslog from sandboxes on the sandbox address only, forwarded to cp-2 |
| `bastion` | bastion | WireGuard, masquerading peers onto the mgmt ports `sg-bastion` may reach |
| `image_bake` | builder VM | config-drive cloud-init, Podman; cleanup before the snapshot |

## What is checked here, and what is not

`tests/deploy` renders every template for every host with Ansible's variable precedence and has
the real program read it, at the pinned version: nft (load and reload), sysctl keys, sshd,
keepalived, systemd (`verify`, and `security`, which rates the agent unit), Podman 5.4.2's
Quadlet generator, Caddy, promtool, amtool, Loki, Tempo, Alloy (each host's configuration is
run), a real Postgres (config, and the bootstrap SQL twice, with an awkward password), and a real
Grafana behind the real Caddy front (embedding, cookies, provisioned panels). The isolation lab
(`tests/isolation`) loads the `hardening` and `gateway_network` output into its namespaces.
`ansible-lint` passes on the production profile and every playbook passes `--syntax-check`.

Not run here: the playbooks against real hosts (no OpenStack, Podman or Molecule were available),
`podman auto-update` rollbacks, step-ca enrollment end to end, Postgres replication between two
hosts, WireGuard. The plan's Molecule scenarios are not written for the same reason.
