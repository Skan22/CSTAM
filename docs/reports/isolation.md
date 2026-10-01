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

## Not covered

The lab emulates Neutron; it is not Neutron. `.ci/isolation-dev.sh` runs the same probes from a
test sandbox in `dev` (and the floating IPs from outside) as the plan's nightly job, but has not
been run: no deployment existed when this was written. Forged frames and rerouting need the lab.
