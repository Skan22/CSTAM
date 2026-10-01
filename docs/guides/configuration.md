# Configuration reference

There are two files that configure every layer, and environment variables for each process.

## `platform.yaml`

Validated against `platform.schema.json` in CI and at runtime. Read by Pulumi, Ansible, the
control plane and the tests.

| Key | Meaning | Default |
| --- | --- | --- |
| `domain` | team hosts are `<slug>.<domain>` | `cstam.felcloud.tn` |
| `ip_pool.cidr`, `first`, `last` | the sandbox range (also Neutron's allocation pool) | 10.20.0.0/24, `.10` to `.109` (100 addresses) |
| `pool.target_size` | warm VMs kept ready | 5 |
| `pool.max_parallel_boots` | concurrent boots | 3 |
| `pool.reserve_free_ips` | registration refuses below this many unallocated addresses | 5 |
| `lease.ttl_seconds` | how long a team lives before the reaper tears it down | 7200 |
| `lease.quarantine_seconds` | an address rests this long after teardown | 60 |
| `vrrp.advert_interval_seconds` | VRRP advert period | 0.2 |
| `vrrp.priority_a`, `priority_b` | gw-a, gw-b priorities | 150, 100 |
| `vrrp.check_interval_seconds` | Traefik health-check period | 1 |
| `flavors.*` | Nova flavor per role (`gateway`, `control_plane`, `sandbox`, `relay`, `bastion`) | `G0.basic.*` |
| `images.base`, `images.sandbox` | base OS for platform VMs; the baked sandbox image | Debian 13, `ipo-sandbox-v1` |
| `network.*_cidr`, `vip`, `vrrp_router_id` | edge and mgmt networks, the VIP, VRRP id | 10.0.0.0/24, 10.30.0.0/24, 10.0.0.100, 51 |
| `network.external_network` | Neutron network that provides floating IPs | `INTERNET` |
| `network.hosts` | fixed address of every host on every network it is on | see the file |

Debian 13 is required by the container units (Podman 5.0 or newer); Debian 12 ships 4.3.

## `security-groups.yaml`

Every allowed network flow, once. Pulumi turns it into Neutron groups, the `hardening` role into
nftables, and the isolation lab into tests.

- `members`: which hosts are in which group (`sg-sandbox` is the whole IPAM range).
- `groups.<sg>.ingress`: a list of `{network, from, proto, ports, why}`. `from` is another group
  (a remote-group reference, never an address range) or `internet`. `proto` is `tcp`, `udp`,
  `vrrp` or `any`; `tcp` and `udp` need `ports`. `why` is mandatory.
- `public.floating_ip_ports`: the only ports the internet may reach on the floating IP (80, 443).
- Anything not listed is denied. Rules are ingress-only and stateful.

`ansible/filter_plugins/ipo_net.py` cross-checks the file (unknown groups, a sandbox rule from the
internet, TCP from the internet to anything but the gateways, ports outside the public list) and
`tests/isolation` fails until every rule is probed.

## Control plane environment

| Variable | Meaning |
| --- | --- |
| `IPO_DATABASE_URL` | PostgreSQL URL (required) |
| `IPO_PLATFORM` | path to `platform.yaml` (required) |
| `IPO_JWT_SECRET` | at least 32 bytes (required) |
| `IPO_CLOUD` | `openstack` (default) or `fake` |
| `OS_CLOUD`, `IPO_OS_NETWORK`, `IPO_OS_SECURITY_GROUPS`, `IPO_OS_KEY_NAME` | OpenStack connection, sandbox network and groups (use `sg-sandbox.sandbox`), key pair |
| `IPO_SIGNING_KEY` | base64 Ed25519 seed that signs configs; generated, with a warning, if unset (agents will not trust a generated one after a restart) |
| `IPO_AGENTS` | `gw-a=https://10.30.0.11:8443,gw-b=https://10.30.0.12:8443`; with `IPO_CLOUD=fake` and none set, in-process agent doubles are used |
| `IPO_AGENT_CERT`, `IPO_AGENT_KEY`, `IPO_AGENT_CA` | mTLS material for talking to agents |
| `IPO_VIP` | the VIP, for the registration probe |
| `IPO_GRAFANA_URL` | Grafana to embed: an `http(s)` URL, or a path such as `/grafana` when one front serves both |
| `IPO_WORKERS` | saga worker threads (default 2) |
| `IPO_BIND` | `host:port` for the API (default `0.0.0.0:8000`) |
| `IPO_MIGRATE` | `1` to run migrations at start |
| `IPO_ADMIN_EMAIL`, `IPO_ADMIN_PASSWORD` | create or reset this admin at start |
| `IPO_USERS_FILE` | JSON list of `{email, password, role}` created at start (the agents' logins) |

## Gateway agent environment

| Variable | Meaning | Default |
| --- | --- | --- |
| `IPO_AGENT_NAME` | `gw-a` or `gw-b` (required) | |
| `IPO_SIGNING_PUBLIC_KEY` | base64 Ed25519 public key of the control plane (required) | |
| `IPO_SANDBOX_CIDR` | backends must be inside it | 10.20.0.0/24 |
| `IPO_CONFIG_DIR` | `live.yml`, `staging.json`, `lkg-*.json` | `/var/lib/ipo-agent` |
| `IPO_VRRP_STATE_FILE`, `IPO_VRRP_FAULT_FILE` | written by keepalived's hook; the track file | `/run/ipo/vrrp_state`, `/run/ipo/fault` |
| `IPO_TRAEFIK_API`, `IPO_TRAEFIK_WEB` | Traefik's API and web entry point, on loopback | `:8080`, `:80` |
| `IPO_ACCESS_LOG` | Traefik JSON access log (`off` disables traffic reporting) | `/var/log/traefik/access.log` |
| `IPO_AGENT_LISTEN` | admin API address | `:8443` |
| `IPO_AGENT_CERT`, `IPO_AGENT_KEY`, `IPO_AGENT_CA`, `IPO_AGENT_ADMIN_CNS` | mTLS server certificate and the client common names allowed | `ipo-control-plane` |
| `IPO_CP_URL`, `IPO_CP_EMAIL`, `IPO_CP_PASSWORD` | where to heartbeat and report (empty disables) | |
| `IPO_HEARTBEAT_SECONDS`, `IPO_PULL_SECONDS`, `IPO_TRAFFIC_SECONDS` | periods | 5, 5, 10 |
| `IPO_METRICS_ADDR` | `/metrics` listener (`off` disables) | `127.0.0.1:9100` |
| `IPO_CANARY_ADDR` | canary responder | `127.0.0.1:8082` |
| `IPO_AGENT_INSECURE` | `1` serves without TLS, for local development only | |

## Runtime settings

`GET`/`PATCH /v1/settings` (admin) changes `pool_target`, `reserve_free_ips`, `lease_ttl_seconds`,
`quarantine_seconds` and `max_parallel_boots` live, with the previous value and the actor kept.

## Ansible and Pulumi settings

| Where | Setting | Meaning |
| --- | --- | --- |
| Pulumi | `ipo:keyPair`, `ipo:publicKey` | the Nova key pair name, and the public key to upload as it |
| Pulumi | `ipo:floatingIps` | `false` builds without the two public addresses |
| Pulumi | `ipo:antiAffinity` | `soft-anti-affinity` (default) or `anti-affinity` |
| Pulumi | `ipo:prefix` | name prefix when two stacks share a project |
| Ansible | `ansible/secrets.example.yaml` | the shape of every secret; the real file is SOPS-encrypted |
| Ansible | `ansible/inventory/group_vars/*.yml` | per-group overrides (bastion forwarding, relay rate limits, gateway ports) |
