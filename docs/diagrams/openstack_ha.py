"""Draws openstack-ha.svg and .png: how the gateway pair stays up on OpenStack, and what else is redundant.

    python docs/diagrams/openstack_ha.py
"""

from svgkit import arrow, begin, box, finish, panel, text

begin(1720, 1180, "OpenStack high availability",
      "One floating IP and one VIP port; two gateways that can each hold the VIP; everything the failover "
      "needs from Neutron and Nova, and what is redundant elsewhere.")


def side(x0: int, title: str, sub: str, a_state: str, b_state: str) -> None:
    kind = "ok" if a_state == "MASTER" else "bad"
    panel(x0, 80, 830, 560, "req" if a_state == "MASTER" else "bad", title, sub)
    cx = x0 + 415
    box(cx - 170, 140, 340, 56, "ext", "Visitors", "HTTP(S) to the platform's public name")
    box(cx - 170, 226, 340, 64, "req", "Floating IP", "attached to the vip-edge port, not to a VM")
    box(cx - 170, 320, 340, 70, "req", "vip-edge port 10.0.0.100", "no device: reserved, so nothing else can take it")
    arrow([(cx, 196), (cx, 226)], "", "req")
    arrow([(cx, 290), (cx, 320)], "", "req")
    for i, (name, state) in enumerate((("gw-a", a_state), ("gw-b", b_state))):
        gx = x0 + 40 + i * 400
        host = panel(gx - 10, 420, 390, 195, "ext", "compute host A" if i == 0 else "compute host B, if the cloud has one", "")
        k = {"MASTER": "ok", "BACKUP": "sig", "LOST": "bad"}[state]
        lines = {"MASTER": ("holds the VIP", "Traefik serving, adverts every 0.2 s"),
                 "BACKUP": ("listens for adverts", "Traefik loaded, routes identical"),
                 "LOST": ("VM gone: processes dead,", "NICs dark, adverts stop")}[state]
        box(gx, 470, 370, 120, k, f"{name}   {state}", *lines,
            "edge port: allowed_address_pairs = VIP")
        _ = host
    if a_state == "MASTER":
        arrow([(cx - 90, 390), (x0 + 225, 470)], "", "ok")
        arrow([(x0 + 410, 520), (x0 + 440, 520)], "", "sig", dashed=True)
        text(cx, 662, "", "ext")
    else:
        arrow([(cx + 90, 390), (x0 + 625, 470)], "", "ok")


side(20, "Normal", "gw-a is the MASTER, gw-b is hot standby", "MASTER", "BACKUP")
side(870, "After losing gw-a's VM", "gw-b takes the VIP; clients reconnect to the same address", "LOST", "MASTER")
text(36, 664, "VRRP adverts, unicast, every 0.2 s, gw-a to gw-b (dashed)", "sig", 12.5)
text(886, 664, "t+0 gw-a dies; about 0.6 s later gw-b misses three adverts, becomes MASTER, adds the VIP, sends "
     "gratuitous ARPs", "bad", 12.5)
text(886, 684, "Measured under 50 new connections/s: worst gap 0.52 to 0.66 s over 11 VM-death runs "
     "(Traefik crash: 1.2 to 1.7 s; keepalived stop: 0.12 s).", "bad", 12.5)

# what makes it work
panel(20, 706, 1680, 190, "cfg", "What the handover needs from OpenStack")
need = [
    ("Allowed address pair", "Both gateways' edge ports list the VIP, so", "port security lets them send and receive it"),
    ("A device-less VIP port", "Holds 10.0.0.100 and the floating IP, so", "the address survives either VM"),
    ("Unicast VRRP", "Multicast is often filtered on Neutron; the", "security group admits VRRP between gateways"),
    ("FAULT releases at once", "Check script with no weight: two failed checks", "= FAULT = priority-0 advert, no wait"),
    ("nopreempt", "The old MASTER returns as BACKUP,", "so a recovery is not a second outage"),
]
for i, (t, l1, l2) in enumerate(need):
    box(40 + i * 330, 760, 310, 104, "cfg", t, l1, l2)

# redundancy map
panel(20, 916, 1680, 232, "ok", "What is redundant, and what is not",
      "verified in the namespace lab with the real software; not yet on real OpenStack VMs")
red = [
    ("Gateways", "ok", ("VRRP pair, anti-affinity", "(soft: see the HA document)", "loss: about 0.6 s")),
    ("Control plane", "ok", ("two active replicas, leaders", "by advisory lock; gateways", "serve without it")),
    ("PostgreSQL", "sig", ("streaming replica on cp-2;", "promote_replica.yml is manual;", "writes pause meanwhile")),
    ("ipo-agent", "ok", ("last-known-good config kept;", "restart resumes it;", "systemd restarts it")),
    ("Sandbox VM", "sig", ("one team's outage only;", "reconciler repairs drift;", "warm pool refills")),
    ("CA, observability", "bad", ("single instance each:", "24 h certificates give a", "window; not redundant")),
]
for i, (t, k, ls) in enumerate(red):
    box(40 + i * 275, 974, 262, 150, k, t, *ls)
finish("openstack-ha")
