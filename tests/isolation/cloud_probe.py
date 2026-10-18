"""The isolation probes against the real cloud's own security groups, with nothing to install.

    uv run python -m isolation.cloud_probe --clouds ~/clouds.yaml

Needs the platform built (`pulumi up`: networks, security groups and the platform's own VMs at
their fixed addresses). Boots three throwaway probe VMs on sandbox-net, each with exactly the
security group a real machine of that kind has:

  sbx-a  sg-sandbox.sandbox, the first pool address:   a team's sandbox (the attacker)
  sbx-b  sg-sandbox.sandbox, the second:               its neighbour (the victim)
  gw     sg-gateway.sandbox, a spare sandbox address:  stands in for a gateway (positive control)

Each VM runs `worker.py` from cloud-init, dialling the addresses `model.py` says it may or may not
reach, then serves the verdicts as JSON on TCP 8099. One floating IP is moved from probe to probe
and a throwaway security group opens 8099 to the internet on the probe VMs only (FelCloud keeps no
console log, and nothing else leaves a sandbox); the probes themselves never touch 8099, and the
group is deleted with the VMs. No SSH, no bastion. A path the policy closes must stay silent; a path it opens
must connect or be refused (a closed port on a host that is there): the positive controls prove
the probes can get through at all.

Exit status: 0 all as the policy says, 1 something closed was reachable, 2 only expected-open
paths were closed. The probe VMs and their ports are deleted at the end.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from isolation import model

IMAGE = "Debian 13 - Trixie"
RESULT_PORT = 8099


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def here(name: str) -> bytes:
    return Path(__file__).with_name(name).read_bytes()


def cloud_config(files: dict[str, bytes], command: str) -> str:
    return "#cloud-config\n" + yaml.safe_dump({
        "write_files": [{"path": f"/opt/ipo/{name}", "encoding": "b64", "content": b64(data)}
                        for name, data in files.items()],
        "runcmd": [["sh", "-c", command]],
    })


SERVE = f"cd /opt/ipo && nohup python3 -m http.server {RESULT_PORT} >/dev/null 2>&1 &"


def prober_data(batch: list[dict[str, Any]], tag: str, spoof: list[str] | None = None) -> str:
    """cloud-init for a probe VM: run the probes, serve the verdicts, and (for the attacker) then
    send forged datagrams to the victim."""
    files = {"worker.py": here("worker.py"),
             "request.json": json.dumps({"tag": tag, "probes": batch}).encode()}
    command = ("python3 /opt/ipo/worker.py < /opt/ipo/request.json > /opt/ipo/result.tmp && "
               f"mv /opt/ipo/result.tmp /opt/ipo/result.json; {SERVE}")
    if spoof:
        files["spoof_sender.py"] = here("spoof_sender.py")
        command += f" python3 /opt/ipo/spoof_sender.py {' '.join(spoof)} >/opt/ipo/spoof.log 2>&1 &"
    return cloud_config(files, command)


def listener_data() -> str:
    """cloud-init for the victim: record every datagram that reaches UDP 5140 and serve the list."""
    return cloud_config({"spoof_listener.py": here("spoof_listener.py")},
                        "nohup python3 /opt/ipo/spoof_listener.py >/opt/ipo/listener.log 2>&1 &")


def fetch_json(url: str, timeout: float, ready: Any = None) -> Any:
    """GET `url` until it answers with JSON (that `ready` accepts, when given)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            if ready is None or ready(data):
                return data
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(5)
    raise TimeoutError(f"no usable answer at {url} after {timeout:.0f}s")


def fetch_text(url: str, timeout: float, ready: Any = None) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                text = str(r.read().decode(errors="replace"))
            if ready is None or ready(text):
                return text
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return text


def cleanup(conn: Any, created: list[tuple[str, str]]) -> None:
    """Floating IP, servers (and wait for them), ports, then the group; each step tolerates
    what is already gone."""
    def each(kind: str, action: Any) -> None:
        for k, rid in created:
            if k == kind:
                with contextlib.suppress(Exception):
                    action(rid)

    each("fip", lambda r: conn.network.delete_ip(r))
    each("server", lambda r: conn.compute.delete_server(r))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and any(
            conn.compute.find_server(r, ignore_missing=True)
            for k, r in created if k == "server"):
        time.sleep(3)
    each("port", lambda r: conn.network.delete_port(r))
    each("sg", lambda r: conn.network.delete_security_group(r))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clouds", type=Path, required=True)
    ap.add_argument("--cloud", default="")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", type=Path, help="write the verdicts as JSON")
    ap.add_argument("--pool-end", action="store_true",
                    help="use the last pool addresses (a deployment holds the first ones)")
    args = ap.parse_args(argv)

    import openstack

    clouds = yaml.safe_load(args.clouds.expanduser().read_text())["clouds"]
    os.environ["OS_CLIENT_CONFIG_FILE"] = str(args.clouds.expanduser())
    conn = openstack.connect(cloud=args.cloud or next(iter(clouds)))
    platform, spec = model.load()
    nodes = model.topology(platform, spec)

    net = conn.network.find_network("sandbox-net", ignore_missing=False)
    flavor = conn.compute.find_flavor(platform["flavors"]["sandbox"], ignore_missing=False)
    image = conn.image.find_image(IMAGE, ignore_missing=False)
    # The first pool addresses by default; --pool-end takes the last ones instead, so the probes run
    # beside a deployment whose sandboxes hold the first addresses.
    pool = platform["ip_pool"]
    first = ipaddress.IPv4Address(pool["last"]) - 4 if args.pool_end else ipaddress.IPv4Address(pool["first"])
    moved = {nodes["sbx-a"].addrs["sandbox"]: str(first), nodes["sbx-b"].addrs["sandbox"]: str(first + 1)}
    victim = str(first + 3)
    plan = {  # name -> (security group, address, probing as)
        "sbx-a": ("sg-sandbox.sandbox", str(first), "sbx-a"),
        "sbx-b": ("sg-sandbox.sandbox", str(first + 1), None),
        "gw": ("sg-gateway.sandbox", str(first + 2), "gw-a"),
        # a relay's group, with a listener: the one thing a sandbox may send to (UDP 5140)
        "victim": ("sg-relay.sandbox", victim, "listener"),
    }
    forged = [victim, str(first + 1), str(ipaddress.IPv4Address(pool["first"]) + 60),
              platform["network"]["hosts"]["gw-a"]["sandbox"]]  # to, neighbour, unused, gateway
    created: list[tuple[str, str]] = []  # (kind, id), see cleanup()
    servers: dict[str, Any] = {}
    ports: dict[str, Any] = {}
    probes_by: dict[str, list[model.Probe]] = {}
    try:
        result_sg = conn.network.create_security_group(
            name="ipo-isolation-result", description="temporary: the probe VMs serve their verdicts")
        created.append(("sg", result_sg.id))
        conn.network.create_security_group_rule(
            security_group_id=result_sg.id, direction="ingress", ethertype="IPv4", protocol="tcp",
            port_range_min=RESULT_PORT, port_range_max=RESULT_PORT, remote_ip_prefix="0.0.0.0/0")
        external = conn.network.find_network(platform["network"]["external_network"],
                                             ignore_missing=False)
        fip = conn.network.create_ip(floating_network_id=external.id,
                                     subnet_id=external.subnet_ids[0])
        created.append(("fip", fip.id))
        for name, (group, addr, as_node) in plan.items():
            sg = conn.network.find_security_group(group, ignore_missing=False)
            port = conn.network.create_port(
                name=f"ipo-isolation-{name}", network_id=net.id,
                security_group_ids=[sg.id] + ([result_sg.id] if as_node else []),
                fixed_ips=[{"ip_address": addr}])
            ports[name] = port
            created.append(("port", port.id))
            data = None
            if as_node == "listener":
                data = listener_data()
            elif as_node:
                probes = [dataclasses.replace(p, addr=moved.get(p.addr, p.addr))
                          for p in model.probes_from(platform, spec, as_node)]
                if as_node == "gw-a":  # only what a gateway shares a segment with: the sandboxes
                    probes = [p for p in probes if p.net == "sandbox" and p.dst in ("sbx-a", "sbx-b")]
                if as_node == "sbx-a":  # addresses nobody owns: whatever answers there is a router
                    probes += [dataclasses.replace(p, dst="nobody", addr=f"{prefix}.250", how="control",
                                                   expect_open=False, policy_open=False)
                               for prefix in ("10.30.0", "10.0.0")
                               for p in probes if p.dst == "cp-1" and p.net == "mgmt"]
                probes_by[name] = probes
                batch = [{"addr": p.addr, "proto": p.proto, "port": p.port} for p in probes]
                data = prober_data(batch, "ipo-isolation", forged if as_node == "sbx-a" else None)
            extra = {"user_data": base64.b64encode(data.encode()).decode()} if data else {}
            server = conn.compute.create_server(
                name=f"ipo-isolation-{name}", flavor_id=flavor.id, image_id=image.id,
                networks=[{"port": port.id}], config_drive=True, **extra)
            created.append(("server", server.id))
            servers[name] = server
            print(f"booting {name} ({group}, {addr})", flush=True)
        for name in servers:
            conn.compute.wait_for_server(servers[name], status="ACTIVE", wait=300)
        results: dict[str, list[str]] = {}
        base = f"http://{fip.floating_ip_address}:{RESULT_PORT}"
        for name in probes_by:
            print(f"waiting for {name}'s verdicts on {fip.floating_ip_address}:{RESULT_PORT}",
                  flush=True)
            conn.network.update_ip(fip, port_id=ports[name].id)
            results[name] = fetch_json(f"{base}/result.json", args.timeout)
            conn.network.update_ip(fip, port_id=None)
        print("waiting for the victim's list of datagrams (the attacker is still sending)",
              flush=True)
        conn.network.update_ip(fip, port_id=ports["victim"].id)
        received = fetch_json(f"{base}/", args.timeout,
                              ready=lambda seen: any(d["payload"] == "end" for d in seen))
        conn.network.update_ip(fip, port_id=ports["sbx-a"].id)
        sent_log = fetch_text(f"{base}/spoof.log", 60, ready=lambda text: "done" in text)
    finally:
        cleanup(conn, created)

    leaks: list[str] = []
    broken: list[str] = []
    unverified: list[str] = []
    controls = reached = 0
    total = 0
    # What answers for an address nobody owns is a router, not a host: the same verdicts for an
    # address with a platform VM on it and no route to it say that VM was not reached either.
    router_says: dict[tuple[str, str, int], str] = {}
    for name, probes in probes_by.items():
        for p, v in zip(probes, results[name], strict=True):
            if p.how == "control":
                router_says[(p.addr.rsplit(".", 1)[0], p.proto, p.port)] = v
    unrouted = 0
    for name, probes in probes_by.items():
        for p, v in zip(probes, results[name], strict=True):
            if p.how == "control":
                continue
            total += 1
            reachable = v in ("open", "refused")
            if p.expect_open:
                controls += 1
                reached += reachable
            if reachable and not p.expect_open:
                if p.how == "none" and router_says.get((p.addr.rsplit(".", 1)[0], p.proto, p.port)) == v:
                    unrouted += 1  # no route exists: the answer is the router's, as for any address
                    continue
                leaks.append(f"LEAK {p.name}: {v}")
            elif p.expect_open and not reachable:
                (unverified if p.proto == "udp" else broken).append(f"{p.name}: {v}")
    # port security: the honest datagrams must arrive (the path is open) and no forged one may
    honest = [d for d in received if d["payload"] in ("honest", "end")]
    delivered = [d for d in received if d["payload"].startswith("spoof")]
    leaks += [f"LEAK forged datagram delivered: {d['payload']} claiming {d['src']}" for d in delivered]
    if not any("spoof-mac" in ln for ln in sent_log.splitlines() if ln.startswith("sent")):
        broken.append("the attacker could not forge a MAC: " + (sent_log.strip().splitlines() or ["?"])[-2])
    if not honest:
        broken.append("the honest datagram never arrived: the forged ones prove nothing")
    for line in leaks + [f"CLOSED {b}" for b in broken] + [f"unverified {u}" for u in unverified]:
        print(line)
    summary = {"probes": total, "expected_open": controls, "expected_open_reached": reached,
               "leaks": len(leaks), "closed_but_should_be_open": len(broken),
               "udp_unverified": len(unverified), "answered_by_a_router_not_the_host": unrouted,
               "sources": {k: len(v) for k, v in probes_by.items()},
               "port_security": {
                   "forged_kinds_sent": ["spoof-ip-neighbour", "spoof-ip-unused", "spoof-ip-gateway",
                                         "spoof-mac"],
                   "sent": [ln.removeprefix("sent ") for ln in sent_log.splitlines()
                            if ln.startswith("sent spoof")],
                   "honest_delivered": len(honest), "forged_delivered": len(delivered)}}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({
            "summary": summary, "victim_received": received,
            "verdicts": {n: [dataclasses.asdict(p) | {"verdict": v} for p, v in
                             zip(probes_by[n], results[n], strict=True)] for n in probes_by}}))
    return 1 if leaks else 2 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
