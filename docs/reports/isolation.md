# Isolation results

`tests/isolation` (plan M7), run with `uv run python -m isolation.run`: 35 tests, all passing,
about 90 seconds, three consecutive runs identical. The lab and its method are described in
`tests/isolation/lab.py`; the policy is `security-groups.yaml`.

## What is probed

Every machine (two sandboxes, both gateways, both control-plane hosts, the relay, the bastion and
an internet host) dials every other machine on every address it has and on 18 ports (16 TCP,
2 UDP): 1782 probes per pass, of which the policy opens 112. The open ones are positive controls:
a block proves something only if the same probe connects when allowed. Verdicts come from both
ends: a connect or a reply, and the listener's own log, so a datagram that arrives without an
answer still counts as a leak.

## The plan's checks (both layers on, as deployed)

| Check | Result |
| --- | --- |
| Sandbox A to sandbox B on any port | blocked: 18 probe ports and a sweep of TCP 1-2048 |
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

`uv run python -m isolation.cloud_probe --clouds … --pool-end` runs the same probe model against
the real Neutron security groups, beside a fully configured deployment (2026-10-18,
[raw verdicts](isolation-real-cloud.json); [the deployment](real-deployment.md)). It boots four
throwaway VMs on `sandbox-net`, each with exactly the group a real machine of that kind has, runs
the probes from cloud-init, and reads the verdicts back through a temporary floating IP. The
targets are the platform's own hosts, configured by the Ansible roles. Everything the run
creates is deleted afterwards.

| Probe VM | Group | Dials | Result |
| --- | --- | --- | --- |
| `sbx-a` | `sg-sandbox.sandbox` | its neighbour `sbx-b`: 18 ports (16 TCP, 2 UDP) | **all blocked** |
| `sbx-a` | | both gateways' sandbox addresses (where the agent's 8443 is) | **all blocked** |
| `sbx-a` | | the gateways' edge addresses and the VIP, through the router | **TCP 80 and 443 reach Traefik**; everything else blocked |
| `sbx-a` | | the relay's sandbox address | TCP 4317 and 4318 reached; UDP 5140 unverified (syslog never replies); the rest blocked |
| `sbx-a` | | the management addresses of bastion, cp-1, cp-2, gw-a, gw-b, relay | **no host reached** (see below) |
| `gw` | `sg-gateway.sandbox` | `sbx-a` and `sbx-b` | **TCP 80 reached both** (the positive control); the other ports blocked |
| `sbx-a` → `victim` | `sg-relay.sandbox` | forged packets to UDP 5140 | **0 of 32 delivered**; the honest ones arrived |

270 probes, **0 leaks, every expected-open TCP path reached.** Notes:

- *Sandbox to sandbox is blocked on every port*, while the same probes from a machine in the
  gateway group get through on port 80, and only on port 80. So the block is the security group,
  not a dead probe.
- *Management addresses answer "refused" on every port, but no host is answering.* There is no
  router between `sandbox-net` and `mgmt-net`, so the packets reach `r-edge`, which refuses for the
  whole prefix: an address nobody owns on that network (`10.30.0.250`, probed as a control) gets
  the same refusal, port for port. The script counts a refusal as the router's when the control
  says the same (`answered_by_a_router_not_the_host: 108`).
- *Port security, tested by forging.* `sbx-a` runs a script (as root, raw sockets) that sends to
  the listener on UDP 5140, the one port a sandbox may reach on a relay. It sends an honest
  datagram, then datagrams with a **forged source IP** (a neighbouring sandbox's, a gateway's, an
  address nobody holds) and a raw Ethernet frame with a **forged source MAC**. Its log shows all
  32 forged packets left the VM; the listener received the honest ones and **none of the forged
  ones**. The same sender on a network without port security (loopback) delivers all of them, so
  a silent listener means a block, not a broken sender.
- An earlier run, before the hosts were configured, could not prove the sandbox → gateway 80/443
  paths. Those replies need the `gateway_network` role's policy route, which only Ansible sets up.
  On the configured deployment they are open.
- Not exercised on the real cloud: forged VRRP adverts (the lab does it) and internet-to-floating-IP
  probing (`isolation.real --as internet`). The host firewall is exercised indirectly: the probes'
  targets run it, rendered by the `hardening` role.

## Not covered

The lab emulates Neutron; it is not Neutron, so the real run above covers what a normal network
stack can probe. `.ci/isolation-dev.sh` is the plan's nightly job and has not been run. Forged
frames and rerouting need the lab.
