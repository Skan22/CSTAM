# 4. Rootless Quadlets on the host network, behind one front

## Status

Accepted.

## Context

Services run as Podman Quadlets (plan M2). Bridged container networks add NAT and a forward path
the host firewall would have to model, and Grafana panels framed by the dashboard on another
origin need cross-site cookies, which browsers increasingly refuse.

## Decision

- Every container runs rootless under the `ipo` user (lingering), with `Network=host`, so the
  host firewall from `security-groups.yaml` is exactly what guards each port. The gateways lower
  `ip_unprivileged_port_start` to 80 for Traefik.
- `Notify=healthy` with a health check that needs no quoting, so `podman auto-update` rolls back
  an image whose container never becomes healthy.
- On the control plane, Caddy serves the dashboard, `/v1` and Grafana (`/grafana/`) on one HTTPS
  origin. `IPO_GRAFANA_URL` is the path `/grafana`, Grafana's cookie is `Secure; SameSite=Lax`,
  and framing is allowed for that origin only. The bastion no longer reaches Grafana's port.

## Consequences

- Podman 5.0 or newer is required (Debian 13); the podman role refuses older versions.
- One process per port per host; ports are listed in `quadlets/README.md`.
- Loki and Tempo have no health check, because what their images contain could not be checked.
