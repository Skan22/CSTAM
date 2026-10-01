"""The isolation probes against a real deployment (the plan's nightly job against `dev`).

    uv run python -m isolation.real --as internet --floating-ip 198.51.100.7 --bastion-ip 198.51.100.8
    uv run python -m isolation.real --as sandbox --peer 10.20.0.57 --ssh "ssh -J bastion probe@10.20.0.56"

Probes are computed here from platform.yaml and security-groups.yaml; worker.py, which needs only
the standard library, runs where the probes must start from: here for `internet`, over `--ssh` on
a sandbox VM for `sandbox`. Nothing listens on most probed ports, so a path that is open shows as
connected or refused and only a filtered one stays silent. A UDP path can only be proven open by a
reply, so expected-open UDP is reported as unverified, not failed. The forged-frame, rerouting
and one-layer-off checks need the lab and run there (`python -m isolation.run`).

Exit status: 0 when every check holds, 1 when something the policy closes was reachable, 2 when
only expected-open paths were closed (the deployment is broken, and the run proves less).
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from isolation import model, worker


def remote(ssh: str, request: dict[str, Any]) -> list[str]:
    source = base64.b64encode((Path(__file__).with_name("worker.py")).read_bytes()).decode()
    py = ("import base64; exec(compile(base64.b64decode('" + source + "'), 'worker', 'exec'),"
          " {'__name__': '__main__'})")
    # ssh hands the remote shell one string, so the command is quoted once, here.
    out = subprocess.run([*shlex.split(ssh), f"python3 -c {shlex.quote(py)}"],
                         input=json.dumps(request), capture_output=True, text=True, timeout=600,
                         check=True)
    verdicts: list[str] = json.loads(out.stdout)
    return verdicts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--as", dest="role", choices=["sandbox", "internet"], required=True)
    ap.add_argument("--peer", help="another sandbox's address (required with --as sandbox)")
    ap.add_argument("--ssh", help="command that reaches the probing sandbox (default: run here)")
    ap.add_argument("--floating-ip", help="the floating IP that maps to the VIP")
    ap.add_argument("--bastion-ip", help="the bastion's floating IP")
    args = ap.parse_args(argv)
    platform, spec = model.load()
    nodes = model.topology(platform, spec)
    remap: dict[str, str] = {}
    if args.role == "sandbox":
        if not args.peer:
            ap.error("--as sandbox needs --peer: probes to a sandbox that does not exist prove nothing")
        src = "sbx-a"
        remap[nodes["sbx-b"].addrs["sandbox"]] = args.peer
    else:
        if not (args.floating_ip and args.bastion_ip):
            ap.error("--as internet needs --floating-ip and --bastion-ip")
        src = "internet"
        remap = {model.FLOATING_IP: args.floating_ip, model.BASTION_FIP: args.bastion_ip}
    probes = [dataclasses.replace(p, addr=remap.get(p.addr, p.addr))
              for p in model.probes_from(platform, spec, src)]
    batch: list[dict[str, Any]] = [{"addr": p.addr, "proto": p.proto, "port": p.port} for p in probes]
    verdicts = (remote(args.ssh, {"tag": "ipo-isolation", "probes": batch}) if args.ssh
                else worker.probe(batch))
    leaks: list[str] = []
    broken: list[str] = []
    unverified: list[str] = []
    for p, v in zip(probes, verdicts, strict=True):
        reachable = v in ("open", "refused")
        if reachable and not p.expect_open:
            leaks.append(f"LEAK {p.name}: {v}")
        elif p.expect_open and not reachable:
            (unverified if p.proto == "udp" else broken).append(f"{p.name}: {v}")
    for line in leaks + [f"CLOSED {b}" for b in broken] + [f"unverified {u}" for u in unverified]:
        print(line)
    closed = sum(1 for p in probes if not p.expect_open)
    print(f"{len(probes)} probes from {args.role}: {closed} must be blocked, {len(leaks)} were not; "
          f"{len(probes) - closed} must be open, {len(broken)} were not, {len(unverified)} unverified",
          file=sys.stderr)
    return 1 if leaks else 2 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
