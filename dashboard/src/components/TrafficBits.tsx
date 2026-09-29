import { Badge, Empty, Stat, type Tone } from "./ui";
import type { RecentRequest, TrafficPoint, TrafficTotals } from "../api/types";
import { bytes, clock, count, pct } from "../lib/format";

export function TrafficTiles({ totals, avgMs }: { totals: TrafficTotals; avgMs: number }) {
  const server = totals.s5xx;
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <Stat label="Requests" value={count(totals.requests)} />
      <Stat label="Server errors" value={pct(server, totals.requests)} tone={server ? "bad" : undefined} sub={`${count(server)} × 5xx`} />
      <Stat label="Client errors" value={pct(totals.s4xx, totals.requests)} sub={`${count(totals.s4xx)} × 4xx`} />
      <Stat label="Sent" value={bytes(totals.bytes)} sub={`avg ${Math.round(avgMs)} ms`} />
    </div>
  );
}

/** Requests per minute as bars, with the 5xx share drawn over each bar in red. */
export function TrafficChart({ series }: { series: TrafficPoint[] }) {
  if (series.length === 0) return <Empty>No requests in this window.</Empty>;
  const W = 600, H = 120, gap = 2;
  const max = Math.max(...series.map((p) => p.requests), 1);
  const w = Math.max(2, W / series.length - gap);
  const peak = series.reduce((a, p) => (p.requests > a.requests ? p : a), series[0]!);
  return (
    <svg
      role="img"
      aria-label={`Requests per minute; busiest minute ${peak.requests} at ${clock(peak.at)}`}
      viewBox={`0 0 ${W} ${H}`}
      className="h-32 w-full"
      preserveAspectRatio="none"
    >
      {series.map((p, i) => {
        const h = (p.requests / max) * (H - 4);
        const eh = (p.errors / max) * (H - 4);
        const x = i * (w + gap);
        return (
          <g key={p.at}>
            <rect x={x} y={H - h} width={w} height={h} className="fill-accent" opacity={0.75}>
              <title>{`${clock(p.at)}: ${p.requests} requests, ${p.errors} server errors`}</title>
            </rect>
            {eh > 0 && <rect x={x} y={H - eh} width={w} height={eh} className="fill-bad" />}
          </g>
        );
      })}
    </svg>
  );
}

export const statusTone = (s: number): Tone => (s >= 500 ? "bad" : s >= 400 ? "warn" : s >= 300 ? "info" : "good");

export function RecentTable({ rows, showHost = false }: { rows: RecentRequest[]; showHost?: boolean }) {
  if (rows.length === 0) return <Empty>No recent requests.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-base">
        <thead className="text-sm text-muted">
          <tr><th className="py-1 pr-4">Time</th>{showHost && <th className="pr-4">Host</th>}<th className="pr-4">Request</th><th className="pr-4">Status</th><th>Took</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.at}-${i}`} className="border-t border-line">
              <td className="py-1.5 pr-4 tabular-nums text-muted">{clock(r.at)}</td>
              {showHost && <td className="pr-4">{r.host}</td>}
              <td className="pr-4 font-mono"><span className="font-semibold">{r.method}</span> {r.path}</td>
              <td className="pr-4"><Badge tone={statusTone(r.status)}>{r.status}</Badge></td>
              <td className="tabular-nums">{r.duration_ms} ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
