# System tests

Tests that need real programs or several moving parts; unit tests live with each component.
Everything runs without root: the labs run inside `unshare --user --map-root-user --net --mount`.

```
uv sync --group deploy
sh lab/fetch-tools.sh              # Traefik and keepalived for the chaos lab
sh lab/fetch-tools.sh validators   # Loki, Tempo, Alloy, Caddy, amtool, Quadlet for deploy/
uv run pytest                      # deploy/, isolation/test_model.py; the labs skip
uv run python -m chaos.run         # about 4 minutes
uv run python -m isolation.run     # about 90 seconds
uv run python -m chaos.demo        # interactive demo dashboard (--auto: scripted and narrated)
uv run python -m chaos.demo --clouds ~/clouds.yaml --auto   # build the real cloud first, tear it down after
```

| Directory | What it proves |
| --- | --- |
| `chaos/` | Two gateways with real keepalived, Traefik and ipo-agent, a sandbox backend and the real control plane, under steady load through the VIP: failover gaps for VM death, Traefik crashes, keepalived stops, manual failover through the agent, and a partition that makes two MASTERs (the split-brain metric and event fire, and healing leaves one). Also per-team traffic from Traefik's access log to the API. |
| `isolation/` | Every source, destination, address and port of the platform against `security-groups.yaml`, through the host firewall the `hardening` role renders and a bridge-level emulation of Neutron port security and security groups; rerouting attacks, forged frames, forged VRRP, per-source rate limits, and each layer alone. `python -m isolation.real` runs the same probes against a deployment (`.ci/isolation-dev.sh`). Results: `docs/reports/isolation.md`. |
| `deploy/` | Every Ansible template rendered per host with Ansible's precedence and read by its real program at the pinned version; every Quadlet through Podman's generator; Grafana behind the real front. See `ansible/README.md`. |
| `lab/` | The namespace lab (`netns.py`), the real stack (`stack.py`), and `render.py`, which renders role templates and resolves role variables the way Ansible does. |

Lab logs stay in the temporary directory each run prints (`lab logs: ...`).

## The demo

`python -m chaos.demo` is a story in four stages, shown as a track in the header: **build**,
**operate**, **break**, **teardown**.

- **build** (`--clouds PATH`): runs `pulumi up` against the real cloud. The screen plans first
  (a preview, so the bars know the size of the job), asks for Enter before creating anything
  billed, then shows each resource being created with the guardrails passing live, beside what
  the OpenStack APIs say exists: a network map that lights up network by network and VM by VM,
  the VM table, and the project's quota.
- **operate** and **break**: the failover dashboard on the local lab (real keepalived, Traefik and
  agents; the OpenStack VMs are not configured by Ansible, so what runs here is the same software
  on namespaces). Press `c` to look back at the cloud that was built.
- **teardown**: asks, then runs `pulumi destroy` and watches the cloud empty. `--keep` skips it.

The demo uses its own stack (`live`) with a local state directory, a generated passphrase and an
SSH key under `~/.local/share/ipo-demo`; credentials stay where you keep them and only their path
is passed on. `python -m chaos.cloud status|up|destroy --clouds PATH` does the same steps as plain
text. If a run is interrupted, `python -m chaos.cloud destroy` removes whatever was built.
