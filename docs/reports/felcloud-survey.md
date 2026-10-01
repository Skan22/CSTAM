# FelCloud project survey (plan M0, read-only)

Taken 2026-10-01 with a password login to the team's project (region North-Africa, one
availability zone, TN-Carthage). Nothing was created. Credentials are not in the repository.

## What the project offers

- **Images** (public): Debian 11, 12 and 13, Ubuntu 22.04 and 24.04, Rocky 10, CentOS Stream 9
  and 10, Fedora 41-43, Fedora CoreOS 43, Arch, the BSDs, CirrOS. The platform uses
  **Debian 13 - Trixie**: its Podman is 5.4, and the Quadlets need 5.0 (Debian 12 has 4.3).
- **Flavors**: `G0.{basic,shared,optimized}.<n>c<m>g`, from 1 vCPU/1 GB to 16 vCPU/32 GB; 20 GB
  disk up to 4c/4g, 50 GB above. The difference between basic, shared and optimized is
  undocumented here; `platform.yaml` uses `G0.basic` throughout.
- **External network**: `INTERNET` (197.5.133.0/24, gateway .254) provides floating IPs.
  `Rancher-mgmt` and `lb-mgmt-net` are other providers' networks, not ours.
- **Neutron**: port security, allowed address pairs, stateful security groups, L3 HA; no DVR (so
  r-edge is centralized, as the plan allows), no remote address groups.
- **Already in the project**: `private_network` (192.168.88.0/24) and `private_router`, and the
  default security group. Left alone.

## Quotas against the design

| Resource | Limit | Used by the platform |
| --- | --- | --- |
| Instances | 50 | 6 long-lived + warm pool + one per team: at most 44 pool and team VMs together |
| Cores / RAM | 128 / 256 GB | 6 VMs use 10 cores, 12 GB; each sandbox 1 core, 1 GB |
| Floating IPs | 10 | 2 (the VIP and the bastion) |
| Security groups / rules | 50 / 100 (4 in use) | 9 groups, 38 rules |
| Networks / subnets / routers / ports | 20 / 100 / 10 / 500 | 3 / 3 / 2 / 14 + one per sandbox |
| Server groups (members) | 10 (10) | 2 |

The instance limit is the one that bites: the plan's "at least 30 teams" fits with a pool of
five, but a registration burst past that fails at Nova, which the reserve-free-IPs guard does not
see (it counts addresses, not instances). Ask FelCloud for more if the burst test needs it.

## What the first real `pulumi up` found (2026-10-01)

- **No floating IPs.** The `INTERNET` pool (197.5.133.1-230) answered every floating-IP request
  with `ExternalIpAddressExhausted`, although the two routers did get gateway addresses from it.
  The program now builds without the VIP's and the bastion's public addresses when none can be
  allocated (`ipo:floatingIps=false`; the demo checks first by allocating and releasing one).
  Until FelCloud frees addresses, the platform has no public entry point and WireGuard cannot be
  reached from outside.
- **Hard anti-affinity fails.** With `anti-affinity` server groups, the four VMs in them (gateways
  and control plane) went to ERROR while the two without a group booted, which fits a single
  compute host in TN-Carthage. The default is now `soft-anti-affinity`; if there is one host, a
  host failure takes both gateways of a pair, which the failover design does not cover.
- **Timing.** From an empty project: 86 resources, VMs ACTIVE in about 45 s, and `pulumi destroy`
  in about 30 s.
