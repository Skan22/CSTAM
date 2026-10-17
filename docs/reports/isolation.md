# Isolation results

`tests/isolation` (plan M7), run with `uv run python -m isolation.run`: 35 tests, all passing,
about 90 seconds, three consecutive runs identical. The lab and its method are described in
`tests/isolation/lab.py`; the policy is `security-groups.yaml`.

## What is probed

Every machine (two sandboxes, both gateways, both control-plane hosts, the relay, the bastion and
an internet host) dials every other machine on every address it has and on 17 ports (15 TCP,
2 UDP): 1683 probes per pass, of which the policy opens 105. The open ones are positive controls:
a block proves something only if the same probe connects when allowed. Verdicts come from both
ends: a connect or a reply, and the listener's own log, so a datagram that arrives without an
answer still counts as a leak.

## The plan's checks (both layers on, as deployed)

| Check | Result |
| --- | --- |
| Sandbox A to sandbox B on any port | blocked: 17 probe ports and a sweep of TCP 1-2048 |
| Sandbox to a gateway's management address and the agent port (8443) | blocked; only 80 and 443 on the edge addresses answer |
| Sandbox to anything on mgmt-net (API, Postgres, Grafana, Prometheus, SSH) | blocked |
| Sandbox sending with another sandbox's IP or MAC, an unused pool address or a gateway's address | dropped by port security; only the honest datagram arrives |
| Internet to the floating IP | only TCP 80 and 443; the bastion's floating IP only UDP 51820 |

## Beyond the plan

- **Rerouting.** A sandbox routing edge-net and mgmt-net through each neighbour on its network
  (r-edge, both gateways, the relay, the other sandbox), and the internet routing every inside
  network through each router's external address: nothing the policy does not already allow.
- **Forged VRRP.** Only gw-a's adverts reach gw-b; a sandbox (to gw-b or the VIP), the control
  plane and the relay are all dropped. A forged advert would otherwise take the VIP on demand.
- **Rate limits.** A sandbox flooding the relay's syslog port at 1000 datagrams/s for 2 s gets
  about 200 + 2 x 100 through (100/s, burst 200, per source); another sandbox's 20 all arrive.
  With the host layer off the whole flood arrives.
- **Edge routing.** Without the `gateway_network` unit, sandboxes lose the gateways' web ports:
  replies leave by the sandbox port with an edge source and port security drops them.

## Each layer alone

| Layer | What still holds | What does not |
| --- | --- | --- |
| Neither | (the suite's control) every probe with a route gets through | - |
| Host firewall only | the whole platform, rerouting included | sandbox to sandbox, on every port: sandboxes run the teams' images, so their firewalls are not counted on; spoofing within the pool (the relay cannot tell a forged pool address from a real one) |
| Fabric (Neutron) only | the whole matrix, spoofing | one way: a sandbox that routes mgmt-net through the relay's sandbox address delivers UDP 5140 to the relay's mgmt address. Security groups filter per port, not per destination address, and Linux accepts any of its addresses on any interface. Replies are dropped by port security, so only one-way datagrams to a listener the sandbox may reach anyway get through. The host firewall pins destinations and closes it. |

So port security is the only defence between sandboxes and against spoofing inside the pool,
and must stay on: the Pulumi policy pack refuses a port or network without it. The host
firewall is the only defence against the weak-host path above.

A wrong rule injected into either layer alone (Postgres opened to the relay) is caught by the
suite, and with the other layer on, it is not reachable.

## Deliberate exposure

A sandbox reaches the gateways' edge addresses and the VIP on 80 and 443, through r-edge (the
plan's router joins edge-net and sandbox-net). `internet` in the policy means any address that
can route there, sandboxes included. That is the public service every team already reaches
through the floating IP.

## Real FelCloud run

`uv run python -m isolation.cloud_probe --clouds …` (2026-10-17, project `Default_Project_skan22`,
about three minutes, [raw verdicts](isolation-real-cloud.json)) runs the same probe model against
the real Neutron security groups that `pulumi up` created. It boots three throwaway probe VMs on
`sandbox-net`, each with exactly the group a real machine of that kind has, and runs the probes
from cloud-init; the platform's own VMs (gateways, control plane, relay, bastion) are the targets,
stock images with nothing listening. The probe VMs, their ports, the floating IP used to read the
verdicts back and the temporary group are deleted afterwards.

| Probe VM | Group | Dials | Result |
| --- | --- | --- | --- |
| `sbx-a` | `sg-sandbox.sandbox` | its neighbour `sbx-b`: 17 ports (15 TCP, 2 UDP) | **all 17 blocked** |
| `sbx-a` | | both gateways' sandbox addresses (where the agent's port 8443 would be reachable by a sandbox) | **all 34 blocked** |
| `sbx-a` | | the relay's sandbox address | 14 closed ports blocked; the 3 the policy opens (TCP 4317, 4318 and UDP 5140) reached the relay |
| `sbx-a` | | the management addresses of bastion, cp-1, cp-2, gw-a, gw-b, relay: 102 probes | **no host reached** (see below) |
| `gw` | `sg-gateway.sandbox` | `sbx-a` and `sbx-b` | **TCP 80 reached both** (positive control); the other 16 ports of each blocked |

255 probes, **0 leaks**. A fourth VM, a listener in a relay's group, takes part in the port-security
check below. Notes on what the numbers mean:

- *Sandbox to sandbox is blocked on every port*, and the same probes from a machine in the gateway
  group do get through on port 80 and only port 80, so the block is the security group and not a
  dead probe.
- *Management addresses answer "refused" on every port, but not because a host does.* There is no
  router between `sandbox-net` and `mgmt-net`, so the packets go to `r-edge` and out. The run also
  probes an address nobody owns on that network (`10.30.0.250`); it gets the same refusal, port
  for port. What answers is the cloud's router, for the whole prefix. The script counts a refusal
  as a router's when the unowned-address control says the same, and reports it separately
  (`answered_by_a_router_not_the_host: 102`). A silent verdict, as for the other networks, would
  have been cleaner; the refusal is the provider's choice.
- *Six expected-open paths were closed* in this run: a sandbox to the gateways' edge addresses and
  the VIP on TCP 80 and 443. The cause is known and is not a policy error: the gateways here are
  stock VMs, so the `gateway_network` role's policy route is missing and replies to edge-sourced
  traffic leave by the sandbox port, where port security drops them (the same finding as in the
  lab, above). The path is only provable once the Ansible roles have run.
- *Port security, tested by forging.* `sbx-a` runs a script (as root, with raw sockets) that sends
  to the listener on UDP 5140, the one port a sandbox may reach on a relay: an honest datagram, then
  datagrams whose **source IP is forged** (a neighbouring sandbox's address, a gateway's, an address
  nobody holds) and a raw Ethernet frame with a **forged source MAC**. The sender's log shows all
  32 forged packets left the VM; the listener received the honest ones and **none of the forged
  ones** (`port_security.forged_delivered: 0`). The same sender, run on a network without port
  security (loopback), delivers all of them, so a silent listener is a block and not a broken
  sender.
- Not exercised on the real cloud: forged VRRP adverts (the lab does it), the host firewall (the
  roles have not been run on real hosts), and internet-to-floating-IP probing
  (`isolation.real --as internet`).

## Not covered

The lab emulates Neutron; it is not Neutron, so the real run above covers what a normal network
stack can probe. `.ci/isolation-dev.sh` is the plan's nightly job and has not been run. Forged
frames and rerouting need the lab.
