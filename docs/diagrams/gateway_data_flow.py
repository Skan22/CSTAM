"""Draws gateway-data-flow.svg and .png: the three paths data takes through a gateway.

    python docs/diagrams/gateway_data_flow.py

Hand-laid out (Mermaid's automatic layout tangles this one): each lane is a left-to-right chain,
so no arrows cross. Edit the lists below and re-run; needs only `chromium` for the PNG.
"""

from svgkit import COL, arrow, begin, box, chain, escape, finish, lane, out, spread

W = 1720
begin(W, 1200, "Gateway data flow", "What moves through a gateway, and where it comes from: requests, "
      "configuration, and the signals and telemetry that flow back. gw-b runs the identical stack as the "
      "VRRP BACKUP.")
# lane 1: requests
lane(80, 290, "1  Request path", "a visitor reaching a team's sandbox, through whichever gateway holds the VIP", "req")
b = chain(150, [
    (200, "ext", "Visitor", ("GET /", "Host: alpha.cstam.felcloud.tn")),
    (200, "req", "Floating IP", ("Neutron DNAT", "to the VIP")),
    (230, "req", "VIP 10.0.0.100", ("held by the VRRP MASTER", "(an allowed address pair)")),
    (270, "req", "Traefik on the MASTER", ("entrypoint :80, one router per Host,", "optional middlewares")),
    (230, "req", "Team sandbox VM", ("10.20.0.x : 80", "fixed IP from the lease")),
], [("HTTP", "1"), ("", "2"), ("MASTER only", "3"), ("forward", "4")], "req")
(tx, ty, tw, th), (sx, sy, sw, sh) = b[3], b[4]
arrow([(sx - 2, sy + 62), (tx + tw + 2, sy + 62)], "response", "req", dashed=True, num="5", dy=22)
box(tx + 20, 280, 230, 60, "store", "access.log", "one JSON line per request", cyl=True)
arrow([(tx + 135, ty + th + 2), (tx + 135, 280)], "", "tel", num="6", dy=-6)
out.append(f'<text x="{tx + 270}" y="314" font-size="13" fill="{COL["tel"][2]}">tailed by ipo-agent, see lane 3'
           f'</text>')

# lane 2: configuration
lane(390, 440, "2  Configuration path", "a route added or removed reaches both gateways as a signed, verified, probed config version", "cfg")
r1 = chain(445, [
    (220, "cfg", "Saga or admin", ("register, teardown,", "rollback")),
    (250, "store", "routes table", ("PostgreSQL, NOTIFY", "ipo_routes_changed")),
    (260, "cfg", "Config compiler", ("render Traefik config,", "SHA-256, Ed25519 sign")),
    (260, "store", "config_versions", ("signed history; a rollback", "pins an earlier body")),
], [("route row", "A"), ("debounced", "B"), ("store version N", "")], "cfg")
(cx, cy, cw, ch) = r1[2]
out.append(f'<rect x="50" y="568" width="{W - 100}" height="150" rx="12" fill="#fff" stroke="{COL["cfg"][1]}" '
           f'stroke-width="1.6" stroke-dasharray="6 4"/>'
           f'<text x="64" y="590" font-weight="700" fill="{COL["cfg"][2]}">ipo-agent: the config pipeline, the only way '
           f'a config reaches Traefik (runs on both gateways)</text>')
arrow([(cx + cw / 2, cy + ch + 2), (cx + cw / 2, 568)], "", "cfg", num="C")
out.append(f'<text x="{cx + cw / 2 + 24}" y="552" font-size="12.5" fill="{COL["cfg"][2]}">'
           f'PUT /config over mTLS to both agents, or their 5 s pull</text>')
stages = [("verify", "Ed25519 over version,", "hash and body"), ("validate", "shape, unique Hosts,", "backends in sandbox CIDR"),
          ("stage", "write and fsync", "staging.json"), ("swap", "atomic rename", "onto live.yml"),
          ("converge", "Traefik API lists", "the new routers"), ("probe", "request each changed", "route and the canary"),
          ("commit", "keep as last", "known good")]
sw_, sg_ = 205, 14
x0 = 50 + 14
pos = []
for i, (t, l1, l2) in enumerate(stages):
    x = x0 + i * (sw_ + sg_)
    pos.append(x)
    box(x, 610, sw_, 84, "cfg", f"{i + 1}  {t}", l1, l2)
    if i:
        arrow([(x - sg_ + 1, 652), (x - 1, 652)], "", "cfg")
box(pos[3] + sw_ // 2 - 85, 740, 170, 56, "store", "live.yml", "Traefik reloads it", cyl=True)
arrow([(pos[3] + sw_ / 2, 696), (pos[3] + sw_ / 2, 740)], "", "cfg")
box(pos[4] - 20, 740, 240, 56, "req", "Traefik reloads, no restart", "in-flight requests unaffected")
arrow([(pos[3] + sw_ // 2 + 85, 768), (pos[4] - 20, 768)], "", "cfg")
box(pos[5] + 10, 740, 200, 56, "bad", "Probe fails or times out", "restore last known good")
arrow([(pos[5] + sw_ / 2 + 10, 696), (pos[5] + sw_ / 2 + 10, 740)], "", "bad", dashed=True)
box(pos[6] + 20, 740, 190, 56, "ok", "ack: live_version N", "per gateway")
arrow([(pos[6] + sw_ / 2 + 10, 696), (pos[6] + sw_ / 2 + 10, 740)], "", "ok", num="D", dy=-8)
out.append(f'<text x="50" y="768" font-size="13" fill="{COL["cfg"][2]}"><tspan font-weight="700">Rejections</tspan> '
           f'403 bad signature</text>'
           f'<text x="50" y="788" font-size="13" fill="{COL["cfg"][2]}">422 policy violation, 409 replay or older</text>')
out.append(f'<text x="50" y="860" font-size="13" fill="{COL["cfg"][2]}"></text>')
out.append(f'<text x="40" y="816" font-size="13" fill="{COL["cfg"][2]}"><tspan font-weight="700">Version rules:</tspan> '
           f'a version goes live only when both gateways ack it; a rejected one is never retried (a transport failure is, '
           f'with backoff); rollback re-issues an earlier body under a higher number. '
           f'<tspan font-weight="700">If the control plane is down</tspan> both gateways keep serving the last version they '
           f'verified.</text>')

# lane 3: signals and telemetry
lane(850, 330, "3  Signals and telemetry", "what the gateways report back, and what the control plane does with it", "sig")
out.append(f'<text x="40" y="930" font-weight="700" fill="{COL["sig"][2]}">VRRP state and liveness</text>')
chain(942, [
    (200, "sig", "keepalived", ("VRRP, unicast, nopreempt", "Traefik check, no weight")),
    (215, "store", "/run/ipo/vrrp_state", ("written by the notify hook", "on every change")),
    (215, "sig", "ipo-agent heartbeat", ("every 5 s: VRRP state", "and live config version")),
    (215, "cfg", "POST .../heartbeat", ("mTLS, operator role", "")),
    (215, "store", "gateway_status", ("one row per gateway:", "vrrp_since, last_heartbeat")),
    (215, "bad", "Split-brain check", ("two fresh MASTERs after the", "newest promotion = alarm")),
], [("notify", "I"), ("read", "J"), ("", "K"), ("upsert", "L"), ("", "")], "sig")
out.append(f'<text x="40" y="1066" font-weight="700" fill="{COL["tel"][2]}">Traffic and metrics</text>')
chain(1078, [
    (200, "store", "access.log", ("Traefik JSON: host, status,", "bytes, duration")),
    (215, "tel", "ipo-agent reporter", ("counts per host for 10 s;", "no IPs, no query strings")),
    (215, "cfg", "POST .../traffic", ("only hosts that have", "a route are kept")),
    (215, "store", "traffic_buckets", ("per-minute buckets and a", "recent-requests ring")),
    (215, "tel", "/metrics at scrape", ("ipo_team_requests_5m,", "ipo_gateway_masters ...")),
    (215, "tel", "Prometheus", ("Grafana panels, dashboard,", "alert rules")),
], [("tail", "M"), ("batch", "N"), ("", "O"), ("gauges", "P"), ("", "")], "tel")
finish("gateway-data-flow")
