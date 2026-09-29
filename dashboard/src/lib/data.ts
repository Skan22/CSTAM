import { api } from "../api/session";
import { must } from "../api/types";
import { useLive } from "./live";

export const TEAM_EVENTS = ["team.active", "team.deleted", "team.draining", "team.failed", "lease.changed"] as const;
const LEASE_EVENTS = ["lease.changed", "team.active", "team.deleted"] as const;
const GATEWAY_EVENTS = ["gateway.vrrp", "gateway.config", "gateway.split_brain", "gateway.split_brain_resolved"] as const;

export const usePool = () => useLive(() => must(api.GET("/v1/pool")), LEASE_EVENTS, [], 5000);
export const useIpam = () => useLive(() => must(api.GET("/v1/ipam")), LEASE_EVENTS, [], 5000);
export const useGateways = () => useLive(() => must(api.GET("/v1/gateways")), GATEWAY_EVENTS, [], 3000);
export const useTeams = (includeDeleted: boolean) =>
  useLive(() => must(api.GET("/v1/teams", { params: { query: { include_deleted: includeDeleted } } })), TEAM_EVENTS, [includeDeleted], 10000);
