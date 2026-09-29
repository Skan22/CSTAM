import type { Gateway, Pool } from "../api/types";

export interface Alert {
  id: string;
  level: "bad" | "warn";
  text: string;
}

const STALE_MS = 20_000;

/** Conditions the dashboard can see from what the API already reports. The Prometheus rules in
 * observability/ are the real alerting; this is the on-screen banner. */
export function computeAlerts(gateways: Gateway[], pool: Pool | undefined, now: number): Alert[] {
  const out: Alert[] = [];
  const masters = gateways.filter((g) => g.vrrp_state === "MASTER");
  if (gateways.length && masters.length === 0) {
    out.push({ id: "no-master", level: "bad", text: "No gateway reports MASTER: the VIP may be unowned." });
  }
  if (masters.length > 1) {
    out.push({ id: "split-brain", level: "bad", text: `Split brain: ${masters.map((g) => g.gateway).join(" and ")} both report MASTER.` });
  }
  const versions = new Set(gateways.map((g) => g.live_version ?? 0));
  if (versions.size > 1) {
    out.push({ id: "version-skew", level: "warn", text: "Gateways run different config versions." });
  }
  for (const g of gateways) {
    if (!g.last_heartbeat || now - Date.parse(g.last_heartbeat) > STALE_MS) {
      out.push({ id: `stale-${g.gateway}`, level: "warn", text: `${g.gateway} has not sent a heartbeat in the last ${STALE_MS / 1000}s.` });
    }
  }
  if (pool && pool.target > 0 && pool.size < pool.target / 2) {
    out.push({ id: "pool-low", level: "warn", text: `Warm pool is at ${pool.size} of ${pool.target}.` });
  }
  return out;
}
