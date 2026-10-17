"""The isolation probes against the real cloud's own security groups, with nothing to install.

    uv run python -m isolation.cloud_probe --clouds ~/clouds.yaml

Needs the platform built (`pulumi up`: networks, security groups and the platform's own VMs at
their fixed addresses). Boots three throwaway probe VMs on sandbox-net, each with exactly the
security group a real machine of that kind has:

  sbx-a  sg-sandbox.sandbox, the first pool address:   a team's sandbox (the attacker)
  sbx-b  sg-sandbox.sandbox, the second:               its neighbour (the victim)
  gw     sg-gateway.sandbox, a spare sandbox address:  stands in for a gateway (positive control)

Each VM runs `worker.py` from cloud-init, dialling the addresses `model.py` says it may or may not
reach, and prints the verdicts to its serial console, which this script reads back through Nova.
No SSH, no floating IP, no bastion. A path the policy closes must stay silent; a path it opens
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
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml

from isolation import model

IMAGE = "Debian 13 - Trixie"
MARK = "IPOR "


def user_data(batch: list[dict[str, Any]], tag: str) -> str:
    worker = Path(__file__).with_name("worker.py").read_bytes()
    request = json.dumps({"tag": tag, "probes": batch}).encode()
    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    return "#cloud-config\n" + yaml.safe_dump({
        "write_files": [
            {"path": "/opt/ipo/worker.py", "encoding": "b64", "content": b64(worker)},
            {"path": "/opt/ipo/request.json", "encoding": "b64", "content": b64(request)},
        ],
        "runcmd": [[
            "sh", "-c",
            "python3 /opt/ipo/worker.py < /opt/ipo/request.json > /opt/ipo/result.json; "
            f"base64 -w 180 /opt/ipo/result.json | sed 's/^/{MARK}/' > /dev/console; "
            f"echo '{MARK}END' > /dev/console",
        ]],
    })


def read_console(conn: Any, server_id: str, timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = conn.compute.get_server_console_output(server_id) or {}
        lines = [ln.strip() for ln in (out.get("output") or "").splitlines()]
        rows = [ln.split(MARK, 1)[1] for ln in lines if MARK in ln]
        if rows and rows[-1] == "END":
            return rows[:-1]
        time.sleep(5)
    raise TimeoutError(f"no result on the console of {server_id} after {timeout:.0f}s")


def decode(rows: list[str]) -> list[str]:
    verdicts: list[str] = json.loads(base64.b64decode("".join(rows)))
    return verdicts


def cleanup(conn: Any, created: list[tuple[str, str]]) -> None:
    """Servers first (and wait for them), then their ports; each step tolerates what is gone."""
    for kind, rid in created:
        if kind == "server":
            with contextlib.suppress(Exception):
                conn.compute.delete_server(rid)
    for kind, rid in created:
        if kind == "server":
            with contextlib.suppress(Exception):
                conn.compute.wait_for_delete(conn.compute.get_server(rid), wait=180)
    for kind, rid in created:
        if kind == "port":
            with contextlib.suppress(Exception):
                conn.network.delete_port(rid)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clouds", type=Path, required=True)
    ap.add_argument("--cloud", default="")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", type=Path, help="write the verdicts as JSON")
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
    spare = str(__import__("ipaddress").IPv4Address(platform["ip_pool"]["first"]) + 2)
    plan = {  # name -> (security group, address, probing as)
        "sbx-a": ("sg-sandbox.sandbox", nodes["sbx-a"].addrs["sandbox"], "sbx-a"),
        "sbx-b": ("sg-sandbox.sandbox", nodes["sbx-b"].addrs["sandbox"], None),
        "gw": ("sg-gateway.sandbox", spare, "gw-a"),
    }
    created: list[tuple[str, str]] = []  # (kind, id), deleted in reverse
    servers: dict[str, Any] = {}
    probes_by: dict[str, list[model.Probe]] = {}
    try:
        for name, (group, addr, as_node) in plan.items():
            sg = conn.network.find_security_group(group, ignore_missing=False)
            port = conn.network.create_port(
                name=f"ipo-isolation-{name}", network_id=net.id, security_group_ids=[sg.id],
                fixed_ips=[{"ip_address": addr}])
            created.append(("port", port.id))
            data = None
            if as_node:
                probes = model.probes_from(platform, spec, as_node)
                if as_node == "gw-a":  # only what a gateway shares a segment with: the sandboxes
                    probes = [p for p in probes if p.net == "sandbox" and p.dst in ("sbx-a", "sbx-b")]
                probes_by[name] = probes
                batch = [{"addr": p.addr, "proto": p.proto, "port": p.port} for p in probes]
                data = user_data(batch, "ipo-isolation")
            server = conn.compute.create_server(
                name=f"ipo-isolation-{name}", flavor_id=flavor.id, image_id=image.id,
                networks=[{"port": port.id}], config_drive=True,
                user_data=base64.b64encode(data.encode()).decode() if data else None)
            created.append(("server", server.id))
            servers[name] = server
            print(f"booting {name} ({group}, {addr})", flush=True)
        for name in servers:
            conn.compute.wait_for_server(servers[name], status="ACTIVE", wait=300)
        results: dict[str, list[str]] = {}
        for name in probes_by:
            print(f"waiting for {name}'s verdicts on its console", flush=True)
            results[name] = decode(read_console(conn, servers[name].id, args.timeout))
    finally:
        cleanup(conn, created)

    leaks: list[str] = []
    broken: list[str] = []
    unverified: list[str] = []
    controls = reached = 0
    total = 0
    for name, probes in probes_by.items():
        for p, v in zip(probes, results[name], strict=True):
            total += 1
            reachable = v in ("open", "refused")
            if p.expect_open:
                controls += 1
                reached += reachable
            if reachable and not p.expect_open:
                leaks.append(f"LEAK {p.name}: {v}")
            elif p.expect_open and not reachable:
                (unverified if p.proto == "udp" else broken).append(f"{p.name}: {v}")
    for line in leaks + [f"CLOSED {b}" for b in broken] + [f"unverified {u}" for u in unverified]:
        print(line)
    summary = {"probes": total, "expected_open": controls, "expected_open_reached": reached,
               "leaks": len(leaks), "closed_but_should_be_open": len(broken),
               "udp_unverified": len(unverified), "sources": {k: len(v) for k, v in probes_by.items()}}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({
            "summary": summary,
            "verdicts": {n: [dataclasses.asdict(p) | {"verdict": v} for p, v in
                             zip(probes_by[n], results[n], strict=True)] for n in probes_by}}))
    return 1 if leaks else 2 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
