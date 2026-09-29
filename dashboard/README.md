# IPO dashboard

Admin UI for the control plane: overview, teams, gateways, IPAM, audit log and runtime settings.
Vite + React 19 + TypeScript + Tailwind 4. API types are generated from `docs/api/openapi.json`.

```
npm install
npm run dev          # http://127.0.0.1:5173, proxies /v1 to $IPO_API (default http://127.0.0.1:8000)
npm run typecheck && npm test
npm run build        # static files in dist/
npm run gen:api      # regenerate src/api/schema.d.ts after the API changes
```

To try it without OpenStack, start the control plane with `IPO_CLOUD=fake IPO_MIGRATE=1` and an
`IPO_ADMIN_EMAIL` / `IPO_ADMIN_PASSWORD` pair, then sign in with those.

## How it works

- **Same origin.** The app calls relative URLs, so production serves `dist/` and `/v1` from one
  reverse proxy (no CORS). In development the Vite proxy does the same.
- **Session.** `POST /v1/auth/login` gives a 15-minute token, kept in `sessionStorage`. The
  credentials are kept in memory only, so on a 401 the client logs in again once, retries the
  request, and otherwise returns to the login form.
- **Live updates.** One `EventSource` on `/v1/events` (token in `?access_token=`) is shared by every
  page. The server does not replay events, so pages refetch after a reconnect and also poll slowly
  as a safety net. The header badge shows the connection state.
- **Roles.** Navigation and buttons are hidden by role (`viewer` < `operator` < `admin`). That is
  convenience only; the API enforces the roles.
- **Destructive actions** (tear down, extend, fail over, roll back) always open a confirmation that
  lists what will change.

## Not built

- **Traffic page**: the API exposes no per-team request or bandwidth data yet.
- **Embedded Grafana panels**: use the dashboards in `observability/` directly.
- **IPAM history** is read from the audit log (admin only); there is no per-address history API.
