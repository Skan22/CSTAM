import { Badge, Card, Empty, Stat } from "../components/ui";
import { computeAlerts } from "../lib/alerts";
import { useGateways, usePool, useTeams } from "../lib/data";
import { ago } from "../lib/format";
import { useHistory, useNow } from "../lib/live";

const VM_GONE_MS = 10 * 60_000;

export function Overview() {
  const gateways = useGateways();
  const pool = usePool();
  const teams = useTeams(false);
  const now = useNow();
  // The reconciler tears the team down by itself, so the alert is stale after a while.
  const vmGone = useHistory(["alert.vm_gone"]).filter((e) => now - e.at < VM_GONE_MS);

  const gws = gateways.data ?? [];
  const master = gws.find((g) => g.vrrp_state === "MASTER");
  const alerts = computeAlerts(gws, pool.data, now);
  const byState = (teams.data ?? []).reduce<Record<string, number>>((m, t) => ({ ...m, [t.state]: (m[t.state] ?? 0) + 1 }), {});
  const p = pool.data;

  return (
    <div className="grid gap-5 lg:grid-cols-3">
      <Card title="VIP holder" className="lg:col-span-1">
        <Stat label="Serving traffic" value={master?.gateway ?? "none"} tone={master ? "good" : "bad"} sub={master ? `config v${master.live_version ?? "-"}` : "no gateway reports MASTER"} />
      </Card>
      <Card title="Teams" className="lg:col-span-1">
        <div className="grid grid-cols-3 gap-4">
          <Stat label="Active" value={byState.active ?? 0} tone="good" />
          <Stat label="Pending" value={byState.pending ?? 0} />
          <Stat label="Failed" value={byState.failed ?? 0} tone={byState.failed ? "bad" : undefined} />
        </div>
      </Card>
      <Card title="Address pool" className="lg:col-span-1">
        {p ? (
          <div className="grid grid-cols-3 gap-4">
            <Stat label="Warm" value={`${p.size}/${p.target}`} tone={p.target > 0 && p.size < p.target / 2 ? "warn" : undefined} />
            <Stat label="Free" value={p.free} />
            <Stat label="Leased" value={p.leased} />
          </div>
        ) : (
          <Empty>Loading…</Empty>
        )}
      </Card>

      <Card title="Gateways" className="lg:col-span-2">
        {gws.length === 0 ? (
          <Empty>No gateways configured.</Empty>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            {gws.map((g) => (
              <div key={g.gateway} className="rounded-lg border border-line bg-raised p-4">
                <div className="flex items-center justify-between">
                  <span className="text-xl font-bold">{g.gateway}</span>
                  <Badge tone={g.vrrp_state === "MASTER" ? "good" : g.vrrp_state === "BACKUP" ? "info" : "bad"}>{g.vrrp_state}</Badge>
                </div>
                <div className="mt-2 text-base text-muted">
                  config v{g.live_version ?? "-"} · heartbeat {ago(g.last_heartbeat, now)}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={`Active alerts (${alerts.length + vmGone.length})`} className="lg:col-span-1">
        {alerts.length + vmGone.length === 0 ? (
          <p className="font-medium text-good">All clear.</p>
        ) : (
          <ul className="space-y-2">
            {alerts.map((a) => (
              <li key={a.id} className="flex gap-2">
                <Badge tone={a.level}>{a.level === "bad" ? "critical" : "warning"}</Badge>
                <span>{a.text}</span>
              </li>
            ))}
            {vmGone.map((e, i) => (
              <li key={i} className="flex gap-2">
                <Badge tone="bad">critical</Badge>
                <span>VM behind {String(e.data.ip)} disappeared; its team is being torn down.</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
