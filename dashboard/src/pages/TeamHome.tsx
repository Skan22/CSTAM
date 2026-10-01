import { useState } from "react";
import { api } from "../api/session";
import { must } from "../api/types";
import { RecentTable, TrafficChart, TrafficTiles } from "../components/TrafficBits";
import { Badge, Button, Card, ErrorNote, Loading, TEAM_TONE } from "../components/ui";
import { countdown } from "../lib/format";
import { useLive, useNow } from "../lib/live";
import { WINDOWS } from "./Traffic";

const LIFECYCLE = ["team.active", "team.draining", "team.deleted", "team.failed"] as const;

/** What one team sees: its own host, how long its address lasts, and the traffic it received. */
export function TeamHome() {
  const [minutes, setMinutes] = useState<number>(60);
  const now = useNow();
  const me = useLive(() => must(api.GET("/v1/me")), LIFECYCLE, [], 30000);
  const traffic = useLive(
    () => must(api.GET("/v1/me/traffic", { params: { query: { window_minutes: minutes } } })),
    ["traffic.batch", ...LIFECYCLE], [minutes], 30000,
  );
  const recent = useLive(() => must(api.GET("/v1/me/traffic/recent", { params: { query: { limit: 30 } } })), ["traffic.batch"], [], 30000);

  const team = me.data?.team;
  const t = traffic.data;
  const avgMs = t && t.totals.requests ? t.teams.reduce((a, x) => a + x.avg_ms * x.requests, 0) / t.totals.requests : 0;

  return (
    <div className="space-y-5">
      {me.error && <ErrorNote>{me.error}</ErrorNote>}
      {!team ? <Loading what="your team" /> : (
        <Card
          title={team.slug}
          actions={<Badge tone={TEAM_TONE[team.state] ?? "idle"}>{team.state}</Badge>}
        >
          <dl className="grid gap-3 sm:grid-cols-2">
            <div><dt className="text-sm text-muted">Address</dt><dd className="font-mono text-lg">{team.host}</dd></div>
            <div><dt className="text-sm text-muted">Reserved for</dt><dd className="text-lg tabular-nums">{team.state === "active" ? countdown(team.expires_at, now) : "-"}</dd></div>
          </dl>
        </Card>
      )}

      <Card
        title="Your traffic"
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
            <TrafficTiles totals={t.totals} avgMs={avgMs} />
            <TrafficChart series={t.series} />
          </div>
        )}
      </Card>

      <Card title="Latest requests">
        {recent.error && <ErrorNote>{recent.error}</ErrorNote>}
        {recent.data ? <RecentTable rows={recent.data.requests} /> : <Loading what="requests" />}
      </Card>
    </div>
  );
}
