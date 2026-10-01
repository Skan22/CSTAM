# Quadlets

Systemd units for every containerised service, run rootless by the `ipo` service user (lingering,
so they start at boot) from `~/.config/containers/systemd/`. The roles install them unchanged and
put per-host values in env files, Podman secrets and drop-ins (`<unit>.d/50-ipo.conf`), so this
directory is the whole runtime shape of the platform.

| Unit | Hosts | Port |
| --- | --- | --- |
| `traefik.container` | gateways | 80, 443; API, ping and metrics on 127.0.0.1:8080 |
| `ipo-control-plane.container` | cp-1, cp-2 | 8001 |
| `ipo-web.container` | cp-1, cp-2 | 8000: dashboard, `/v1`, `/grafana/` |
| `postgres.container` (+ `postgres-backup*`, timer) | cp-1 primary, cp-2 replica | 5432 |
| `step-ca.container` | cp-1 | 9000 |
| `prometheus`, `alertmanager`, `loki`, `tempo`, `grafana` | cp-2 | 9090, 9093 (loopback), 3100, 4317, 3000 |
| `podman-exporter.container` | every Podman host | 127.0.0.1:9882 |

Choices that apply to all of them:

- **Host networking.** No NAT and no forwarding path: the host firewall rendered from
  `security-groups.yaml` (roles/hardening) is exactly what guards every port, and the isolation
  lab tests that firewall. Rootless processes cannot bind below 1024, so the gateways lower
  `net.ipv4.ip_unprivileged_port_start` to 80 for Traefik.
- **Auto-update with rollback.** Images are pinned to a tag (`AutoUpdate=registry` follows it);
  `Notify=healthy` makes a unit active only once its health check passes, so
  `podman auto-update` rolls back an image whose container never becomes healthy.
  `HealthOnFailure=kill` and `Restart=always` restart one that turns unhealthy later. Loki and
  Tempo have no health check: what their images contain could not be checked here, and a check
  that cannot run would kill a healthy container.
- **Read-only root filesystems** and no capabilities wherever the image allows it.
- **Podman 5.0 or newer** (`Notify=healthy`, `Entrypoint=`, `UserNS=keep-id:uid=`); the podman
  role refuses older versions. Debian 12's Podman 4.3 predates Quadlet altogether, so hosts need
  Debian 13 or a backport.

`tests/deploy/test_quadlets.py` runs every unit through Podman 5.4.2's own Quadlet generator (the
Podman Debian 13 ships), checks the rules above, and checks that each health check reaches Podman
as one well-quoted command (a nested-quote `HealthCmd` once came out unbalanced, which would have
restarted a healthy container forever). The containers themselves have not been started: no
Podman runtime was available where this was written.
