import type { Gateway, Pool } from "../api/types";
import { computeAlerts } from "./alerts";

const now = Date.parse("2026-01-01T12:00:00Z");
const beat = "2026-01-01T11:59:58Z";
const gw = (gateway: string, vrrp_state: string, live_version: number | null = 3, last_heartbeat: string | null = beat): Gateway => ({
  gateway, vrrp_state, live_version, last_heartbeat,
});
const pool = (size: number, target: number): Pool => ({ size, target, free: 0, leased: 0, draining: 0, quarantined: 0 });
const ids = (a: ReturnType<typeof computeAlerts>) => a.map((x) => x.id);

test("a healthy pair raises nothing", () => {
  expect(computeAlerts([gw("a", "MASTER"), gw("b", "BACKUP")], pool(4, 4), now)).toEqual([]);
});

test("two masters is a split brain and none is a missing VIP", () => {
  expect(ids(computeAlerts([gw("a", "MASTER"), gw("b", "MASTER")], undefined, now))).toContain("split-brain");
  expect(ids(computeAlerts([gw("a", "BACKUP"), gw("b", "BACKUP")], undefined, now))).toContain("no-master");
});

test("different versions and silent gateways are flagged", () => {
  const a = computeAlerts([gw("a", "MASTER", 4), gw("b", "BACKUP", 3, "2026-01-01T11:58:00Z")], undefined, now);
  expect(ids(a)).toEqual(["version-skew", "stale-b"]);
});

test("a pool under half its target warns, a disabled pool does not", () => {
  expect(ids(computeAlerts([gw("a", "MASTER")], pool(1, 4), now))).toEqual(["pool-low"]);
  expect(computeAlerts([gw("a", "MASTER")], pool(0, 0), now)).toEqual([]);
});
