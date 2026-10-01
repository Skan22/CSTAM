# Security architecture

## Trust boundaries

| Zone | Who | Reaches |
| --- | --- | --- |
| Internet | anyone | the floating IP (VIP) on 80/443; the bastion's floating IP on UDP 51820 |
| sandbox-net | team VMs, untrusted | the relay's telemetry ports; the gateways' public ports, like anyone |
| edge-net | the gateway pair and the VIP | - |
| mgmt-net | control plane, Postgres, CA, observability, agents, SSH | reached only through the bastion's WireGuard |

## Defence in depth (network)

| Threat | Cloud (Neutron) | Host | Verified by |
| --- | --- | --- | --- |
| Sandbox reaches another sandbox | security groups (`sg-sandbox.sandbox` admits only the gateways on 80) | none: teams own their images | isolation suite |
| Sandbox reaches the platform | security groups per port | nftables from the same spec, per destination address | isolation suite, each layer alone |
| Sandbox spoofs another's IP or MAC | port security | rp_filter only stops off-subnet sources | isolation suite; Pulumi policy keeps port security on |
| Sandbox routes around (weak host) | not stopped | destination-pinned rules | isolation suite (fabric alone leaks one-way UDP) |
| Forged VRRP takes the VIP | security groups | nftables | isolation suite |
| Sandbox floods shared telemetry | - | per-source nftables limits, then Alloy's per-stream limit | isolation suite (flood test) |
| Internet reaches anything but 80/443 | security groups | nftables | isolation suite; Pulumi policy pack |
| A rule mistake in one layer | the other layer | the other layer | isolation suite (injected rule) |

## Components

- **Gateways.** Traefik rootless on the host network (it binds 80/443 through
  `ip_unprivileged_port_start`); ipo-agent as a system service with no capabilities, a read-only
  system and a syscall filter (`systemd-analyze security` rates the unit 1.2, "OK"; `tests/deploy` keeps it at or under 2.0). The agent
  accepts only configs signed by the control plane (Ed25519) and only admin calls from the
  control plane's client certificate. Known gap: the agent logs in to the API with an operator
  account; a narrower `gateway` role would limit what a compromised gateway could do.
- **Control plane.** Rootless Quadlets on the host network behind one HTTPS front (Caddy, the
  platform CA's certificate). Secrets are Podman secrets, never env files or images. Team
  accounts see only their own team (`/v1/me*`), and never Grafana.
- **Bastion.** WireGuard only; peers are masqueraded onto exactly the mgmt ports `sg-bastion`
  may reach, so the cloud's rules and port security still apply to operator traffic.

## Secrets and keys

| Secret | Where it lives | Rotation |
| --- | --- | --- |
| Config signing key (Ed25519) | Podman secret on cp-1/cp-2; public half in the agents' env | new key, push to agents first |
| JWT secret, admin and agent passwords | Podman secrets; agents' in a root-owned 0640 env file | re-run `site.yml` with new values |
| Host certificates (24 h) | `/etc/ipo/pki`, key 0640 to `ipo-pki` | `ipo-cert-renew.timer`, or `rotate_certs.yml` |
| CA keys and password | step-ca's volume on cp-1; password as a Podman secret | - |
| Postgres passwords | Podman secrets; sent to psql on stdin, never argv | re-run `control_plane.yml` |
| OpenStack application credential | `~ipo/.config/openstack/clouds.yaml`, 0600 | revoke in Keystone, re-run |
| All of the above at rest | `ansible/secrets.enc.yaml` (SOPS); `secrets.yaml` is git-ignored | - |

Isolation results: `docs/reports/isolation.md`. Decisions: `docs/adr/0003`, `docs/adr/0004`.
