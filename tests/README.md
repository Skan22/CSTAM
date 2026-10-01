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
```

| Directory | What it proves |
| --- | --- |
| `chaos/` | Two gateways with real keepalived, Traefik and ipo-agent, a sandbox backend and the real control plane, under steady load through the VIP: failover gaps for VM death, Traefik crashes, keepalived stops, manual failover through the agent, and a partition that makes two MASTERs (the split-brain metric and event fire, and healing leaves one). Also per-team traffic from Traefik's access log to the API. |
| `isolation/` | Every source, destination, address and port of the platform against `security-groups.yaml`, through the host firewall the `hardening` role renders and a bridge-level emulation of Neutron port security and security groups; rerouting attacks, forged frames, forged VRRP, per-source rate limits, and each layer alone. `python -m isolation.real` runs the same probes against a deployment (`.ci/isolation-dev.sh`). Results: `docs/reports/isolation.md`. |
| `deploy/` | Every Ansible template rendered per host with Ansible's precedence and read by its real program at the pinned version; every Quadlet through Podman's generator; Grafana behind the real front. See `ansible/README.md`. |
| `lab/` | The namespace lab (`netns.py`), the real stack (`stack.py`), and `render.py`, which renders role templates and resolves role variables the way Ansible does. |

Lab logs stay in the temporary directory each run prints (`lab logs: ...`).
