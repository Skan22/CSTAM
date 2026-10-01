"""Draws system-architecture.svg and .png: every component, the three networks, and who talks to whom.

    python docs/diagrams/system_architecture.py
"""

from svgkit import arrow, begin, box, finish, panel, text

begin(1720, 1150, "System architecture",
      "The Resilient IP Optimizer on FelCloud OpenStack: one floating IP, a VRRP gateway pair, "
      "a control plane that provisions and routes, and the isolated sandbox VMs behind them.")

box(670, 84, 340, 58, "ext", "Teams and visitors", "https://team.cstam.felcloud.tn")
box(1290, 84, 330, 58, "ext", "Operators", "WireGuard, then SSH or the dashboard")
panel(20, 170, 1680, 960, "ext", "FelCloud OpenStack: the team's project, region North-Africa, "
      "availability zone TN-Carthage")

# --- edge-net
panel(620, 215, 440, 560, "req", "edge-net 10.0.0.0/24", "behind r-edge; gateways also have a leg in each other network")
box(680, 280, 320, 64, "req", "VIP 10.0.0.100 + floating IP", "an address, not a machine: the MASTER holds it")
box(640, 390, 400, 118, "req", "gw-a   VRRP MASTER (priority 150)", "keepalived: VRRP, Traefik health check",
    "Traefik: routes by Host, hot-reloaded config", "ipo-agent: verifies signed config, reports")
box(640, 590, 400, 118, "req", "gw-b   VRRP BACKUP (priority 100)", "same stack, same routes, same signed config",
    "takes the VIP in about a second when gw-a fails", "nopreempt: does not hand it back")
arrow([(840, 142), (840, 280)], "", "req")
text(856, 200, "80 / 443", "req", 12.5)
arrow([(840, 344), (840, 390)], "", "req")
arrow([(840, 508), (840, 590)], "", "sig", dashed=True)
text(856, 556, "VRRP adverts, unicast, every 0.2 s", "sig", 12.5)

# --- sandbox-net
panel(40, 215, 520, 560, "ok", "sandbox-net 10.20.0.0/24", "DHCP off; Neutron's pool equals the control plane's IPAM range")
box(80, 290, 440, 100, "ok", "Team sandbox VMs", "one fixed IP per lease, port security on", "reachable only from the gateways, on port 80")
box(80, 450, 440, 90, "tel", "Relay", "OTLP and syslog from sandboxes, per-source rate limits",
    "forwards to the observability host")
box(80, 600, 440, 100, "ok", "Warm pool", "booted VMs parked on reserved addresses", "a registration claims one in seconds")
arrow([(640, 449), (600, 449), (600, 340), (520, 340)], "route by Host", "req", num="", dy=-8, seg=1)
arrow([(300, 390), (300, 450)], "", "tel")
text(316, 424, "telemetry", "tel", 12.5)

# --- mgmt-net
panel(1140, 215, 540, 800, "cfg", "mgmt-net 10.30.0.0/24", "behind r-mgmt; reached by operators only through the bastion")
box(1180, 275, 460, 66, "sig", "Bastion", "WireGuard on UDP 51820, the only way in")
out_x = 1180
panel(1170, 370, 480, 230, "cfg", "Control plane: cp-1 and cp-2, both active")
box(1190, 420, 440, 62, "cfg", "API and dashboard", "FastAPI behind Caddy, one HTTPS origin")
box(1190, 500, 440, 84, "cfg", "Workers and controllers", "sagas, config compiler, pool manager, reaper,",
    "reconciler; leaders elected with advisory locks")
box(1180, 620, 460, 66, "store", "PostgreSQL: primary cp-1, replica cp-2", "all state; nightly dumps to object storage")
box(1180, 702, 460, 62, "sig", "step-ca on cp-1", "private CA: 24 h host certificates, auto-renewed")
box(1180, 780, 460, 80, "tel", "Observability on cp-2", "Prometheus, Alertmanager, Loki, Tempo, Grafana",
    "(Grafana served under /grafana/ by the same front)")
box(1180, 890, 460, 80, "ext", "OpenStack APIs", "Nova, Neutron, Keystone: create and delete sandbox",
    "VMs and ports with an application credential", dashed=True)
arrow([(1455, 142), (1455, 275)], "WireGuard", "sig", dy=-8)
arrow([(1410, 341), (1410, 420)], "", "sig")
text(1424, 362, "SSH, dashboard, API", "sig", 12.5)
arrow([(1040, 435), (1190, 435)], "heartbeat, traffic", "tel", dy=-10)
for y in (470, 640):
    arrow([(1100, y), (1040, y)], "", "cfg")
arrow([(1190, 545), (1100, 545), (1100, 470)], "", "cfg")
arrow([(1100, 545), (1100, 640)], "", "cfg")
for i, s in enumerate(("signed config", "over mTLS to", "both gateways")):
    text(1088, 572 + 15 * i, s, "cfg", 11.5, anchor="end")
arrow([(1640, 560), (1665, 560), (1665, 930), (1640, 930)], "", "ext", dashed=True)
arrow([(300, 700), (300, 840), (1180, 840)], "", "tel")
arrow([(840, 708), (840, 840)], "", "tel")
text(330, 830, "metrics and logs from every host (Alloy), and sandbox telemetry via the relay", "tel", 12.5)

text(40, 1060, "Gateways, control plane, relay and bastion have fixed addresses from platform.yaml, so VRRP peers and the "
     "inventory are static.", "ext", 13)
text(40, 1084, "Every rule between these networks is in security-groups.yaml and is enforced twice: by Neutron "
     "security groups and by nftables on each host (docs/reports/isolation.md).", "ext", 13)
text(40, 1108, "Two networks cannot talk to each other except through a gateway or a router; sandboxes can reach only "
     "the relay, and the gateways' ports 80 and 443.", "ext", 13)
finish("system-architecture")
