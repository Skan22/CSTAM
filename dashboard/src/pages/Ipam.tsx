import { useState } from "react";
import { Badge, Card, Empty, ErrorNote, LEASE_TONE, Loading } from "../components/ui";
import { api } from "../api/session";
import { must, type IpamLease } from "../api/types";
import { useIpam } from "../lib/data";
import { countdown } from "../lib/format";
import { useLive, useNow, useSession } from "../lib/live";
import { can } from "../lib/roles";

const CELL: Record<string, string> = {
  free: "bg-idle-bg text-muted",
  pooled: "bg-info-bg text-info",
  leased: "bg-good-bg text-good",
  draining: "bg-warn-bg text-warn",
  quarantined: "bg-violet-bg text-violet",
};

export function Ipam() {
  const ipam = useIpam();
  const [selected, setSelected] = useState<string>();
  const lease = ipam.data?.leases.find((l) => l.ip === selected);

  return (
    <div className="space-y-5">
      <Card title="Address space">
        {ipam.error && <ErrorNote>{ipam.error}</ErrorNote>}
        {!ipam.data ? <Loading what="addresses" /> : (
          <>
            <div className="mb-4 flex flex-wrap gap-3">
              {Object.entries(ipam.data.counts).map(([state, n]) => (
                <Badge key={state} tone={LEASE_TONE[state] ?? "idle"}>{state}: {n}</Badge>
              ))}
              <span className="text-muted">{ipam.data.total} addresses</span>
            </div>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(4.5rem,1fr))] gap-1.5" role="list">
              {ipam.data.leases.map((l) => (
                <button
                  key={l.ip}
                  role="listitem"
                  onClick={() => setSelected(l.ip)}
                  title={`${l.ip} ${l.state}`}
                  aria-label={`${l.ip} ${l.state}`}
                  aria-pressed={selected === l.ip}
                  className={`rounded-md px-1 py-2 font-mono text-sm font-semibold ring-offset-2 ${CELL[l.state] ?? CELL.free} ${selected === l.ip ? "ring-2 ring-accent" : ""}`}
                >
                  .{l.ip.split(".").pop()}
                </button>
              ))}
            </div>
          </>
        )}
      </Card>
      {lease && <Detail lease={lease} onClose={() => setSelected(undefined)} />}
    </div>
  );
}

function Detail({ lease, onClose }: { lease: IpamLease; onClose: () => void }) {
  const now = useNow();
  const admin = can(useSession()?.role, "admin");
  // The audit log is the only history the API keeps for an address; it is admin-only.
  const audit = useLive(
    async () => (admin ? (await must(api.GET("/v1/audit", { params: { query: { limit: 1000 } } }))).entries : []),
    [], [lease.ip, admin],
  );
  const mine = (audit.data ?? []).filter((e) => e.target === lease.ip || e.detail.ip === lease.ip || (lease.team !== null && e.target === lease.team));

  return (
    <Card title={lease.ip} actions={<button className="text-muted underline" onClick={onClose}>Close</button>}>
      <dl className="grid max-w-md grid-cols-2 gap-y-1.5 text-base">
        <dt className="text-muted">State</dt><dd><Badge tone={LEASE_TONE[lease.state] ?? "idle"}>{lease.state}</Badge></dd>
        <dt className="text-muted">Team</dt><dd>{lease.team ?? "-"}</dd>
        <dt className="text-muted">Lease left</dt><dd>{lease.expires_at ? countdown(lease.expires_at, now) : "-"}</dd>
        <dt className="text-muted">Quarantine left</dt><dd>{lease.quarantined_until ? countdown(lease.quarantined_until, now) : "-"}</dd>
      </dl>
      <h3 className="mb-2 mt-5 font-semibold">History</h3>
      {!admin ? <Empty>Session history is visible to admins.</Empty> : mine.length === 0 ? <Empty>No audit entries for this address in the latest 1000.</Empty> : (
        <ul className="space-y-1.5 text-base">
          {mine.map((e) => (
            <li key={e.id}><span className="tabular-nums text-muted">{new Date(e.at).toLocaleString()}</span> · {e.action} · {e.actor}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}
