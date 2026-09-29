import { describeError } from "./session";
import type { components } from "./schema";

type S = components["schemas"];
export type Team = S["Team"];
export type Pool = S["Pool"];
export type Ipam = S["Ipam"];
export type IpamLease = S["IpamLease"];
export type Gateway = S["Gateway"];
export type Job = S["Job"];
export type ConfigVersionInfo = S["ConfigVersionInfo"];
export type AuditEntry = S["AuditEntry"];
export type SettingsView = S["SettingsView"];
export type SettingsPatch = S["SettingsPatch"];

/** Throws with the API's own message so callers can show it. */
export async function must<T>(p: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const r = await p;
  if (r.data === undefined) throw new Error(describeError(r.error));
  return r.data;
}

export type Traffic = S["Traffic"];
export type TrafficPoint = S["TrafficPoint"];
export type TrafficTotals = S["TrafficTotals"];
export type TeamTraffic = S["TeamTraffic"];
export type RecentRequest = S["RecentTraffic"];
