import { useState } from "react";
import { api } from "../api/session";
import { must } from "../api/types";
import { RecentTable, TrafficChart, TrafficTiles } from "../components/TrafficBits";
import { Badge, Button, Card, Empty, ErrorNote, Loading } from "../components/ui";
import { ago, bytes, count, pct } from "../lib/format";
import { useLive, useNow } from "../lib/live";

export const WINDOWS = [
  { label: "15 min", minutes: 15 },
  { label: "1 h", minutes: 60 },
  { label: "6 h", minutes: 360 },
  { label: "24 h", minutes: 1440 },
] as const;

const TRAFFIC_EVENTS = ["traffic.batch", "team.active", "team.deleted"] as const;

export function Traffic() {
  const [minutes, setMinutes] = useState<number>(60);
  const [host, setHost] = useState<string>();
  const now = useNow();
  const traffic = useLive(
    () => must(api.GET("/v1/traffic", { params: { query: { window_minutes: minutes } } })),
    TRAFFIC_EVENTS, [minutes], 15000,
  );
  const recent = useLive(
    async () => (host ? (await must(api.GET("/v1/traffic/recent", { params: { query: { host, limit: 30 } } }))).requests : []),
    ["traffic.batch"], [host], 15000,
  );
  const t = traffic.data;
  const totalMs = t ? t.teams.reduce((a, x) => a + x.avg_ms * x.requests, 0) : 0;

  return (
    <div className="space-y-5">
      <Card
        title="Traffic through the VIP"
        actions={
          <div className="flex gap-1.5" role="group" aria-label="Time window">
            {WINDOWS.map((w) => (
              <Button key={w.minutes} variant={w.minutes === minutes ? "primary" : "secondary"} aria-pressed={w.minutes === minutes} onClick={() => setMinutes(w.minutes)}>{w.label}</Button>
            ))}
          </div>
        }
      >
        {traffic.error && <ErrorNote>{traffic.error}</ErrorNote>}
        {!t ? <Loading what="traffic" /> : (
          <div className="space-y-5">
            <TrafficTiles totals={t.totals} avgMs={t.totals.requests ? totalMs / t.totals.requests : 0} />
            <TrafficChart series={t.series} />
          </div>
        )}
      </Card>

      <Card title="By team">
        {!t ? <Loading what="teams" /> : t.teams.length === 0 ? (
          <Empty>No teams are registered, so there is nothing to count.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-base">
              <thead className="text-sm text-muted">
                <tr><th className="py-1 pr-4">Team</th><th className="pr-4">Requests</th><th className="pr-4">4xx</th><th className="pr-4">5xx</th><th className="pr-4">Sent</th><th className="pr-4">Avg</th><th>Last request</th></tr>
              </thead>
              <tbody>
                {t.teams.map((x) => (
                  <tr key={x.team_id} className={`border-t border-line ${host === x.host ? "bg-raised" : ""}`}>
                    <td className="py-1.5 pr-4">
                      <button className="text-left font-semibold underline decoration-dotted underline-offset-4" aria-pressed={host === x.host} onClick={() => setHost(host === x.host ? undefined : x.host)}>
                        {x.host}
                      </button>
                    </td>
                    {x.requests === 0 ? (
                      <td colSpan={6} className="text-muted">no traffic</td>
                    ) : (
                      <>
                        <td className="pr-4 tabular-nums">{count(x.requests)}</td>
                        <td className="pr-4 tabular-nums">{x.s4xx ? pct(x.s4xx, x.requests) : "-"}</td>
                        <td className="pr-4">{x.s5xx ? <Badge tone="bad">{pct(x.s5xx, x.requests)}</Badge> : "-"}</td>
                        <td className="pr-4 tabular-nums">{bytes(x.bytes)}</td>
                        <td className="pr-4 tabular-nums">{Math.round(x.avg_ms)} ms</td>
                        <td className="text-muted">{ago(x.last_seen, now)}</td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {host && (
        <Card title={`Latest requests to ${host}`} actions={<Button onClick={() => setHost(undefined)}>Close</Button>}>
          {recent.error && <ErrorNote>{recent.error}</ErrorNote>}
          {recent.data ? <RecentTable rows={recent.data} /> : <Loading what="requests" />}
        </Card>
      )}
    </div>
  );
}
