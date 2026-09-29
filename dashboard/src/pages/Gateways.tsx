import { useMemo, useState } from "react";
import { api } from "../api/session";
import { must, type ConfigVersionInfo } from "../api/types";
import { Badge, Button, Card, ConfirmDialog, Empty, ErrorNote, Loading } from "../components/ui";
import { useGateways } from "../lib/data";
import { lineDiff, prettyJson } from "../lib/diff";
import { ago, clock } from "../lib/format";
import { useHistory, useLive, useNow, useSession } from "../lib/live";
import { can } from "../lib/roles";

const VRRP_TONE = { MASTER: "good", BACKUP: "info", FAULT: "bad", UNKNOWN: "idle" } as const;

export function Gateways() {
  const role = useSession()?.role;
  const admin = can(role, "admin");
  const gateways = useGateways();
  const now = useNow();
  const timeline = useHistory(["gateway.vrrp"]);
  const [failover, setFailover] = useState(false);
  const master = gateways.data?.find((g) => g.vrrp_state === "MASTER");

  return (
    <div className="space-y-5">
      <Card
        title="Gateways"
        actions={admin && <Button variant="danger" disabled={!master} onClick={() => setFailover(true)}>Fail over</Button>}
      >
        {gateways.error && <ErrorNote>{gateways.error}</ErrorNote>}
        {!gateways.data ? <Loading what="gateways" /> : (
          <div className="grid gap-4 sm:grid-cols-2">
            {gateways.data.map((g) => (
              <div key={g.gateway} className="rounded-lg border border-line bg-raised p-4">
                <div className="flex items-center justify-between">
                  <span className="text-xl font-bold">{g.gateway}</span>
                  <Badge tone={VRRP_TONE[g.vrrp_state as keyof typeof VRRP_TONE] ?? "idle"}>{g.vrrp_state}</Badge>
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-1 text-base">
                  <dt className="text-muted">Live config</dt><dd>v{g.live_version ?? "-"}</dd>
                  <dt className="text-muted">Last heartbeat</dt><dd>{ago(g.last_heartbeat, now)}</dd>
                </dl>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="VRRP timeline">
        {timeline.length === 0 ? (
          <Empty>No VRRP transitions since this page was opened.</Empty>
        ) : (
          <ol className="space-y-1.5">
            {[...timeline].reverse().map((e, i) => (
              <li key={`${e.at}-${i}`} className="flex flex-wrap items-center gap-3 text-base">
                <span className="tabular-nums text-muted">{clock(e.at)}</span>
                <span className="font-semibold">{String(e.data.gateway)}</span>
                <span>{String(e.data.from ?? "?")} → {String(e.data.to ?? e.data.state ?? "?")}</span>
              </li>
            ))}
          </ol>
        )}
      </Card>

      {admin && <ConfigCard />}

      {failover && master && (
        <ConfirmDialog
          title="Fail over the VIP?"
          danger
          confirmLabel="Fail over"
          changes={[
            `Put ${master.gateway} into FAULT so it releases the VIP`,
            "The other gateway takes the VIP; in-flight connections to the current holder are dropped",
            "Recorded in the audit log",
          ]}
          onConfirm={() => must(api.POST("/v1/gateways/failover", { body: {} }))}
          onClose={() => setFailover(false)}
        />
      )}
    </div>
  );
}

function ConfigCard() {
  const versions = useLive(() => must(api.GET("/v1/config/versions")), ["gateway.config", "team.active", "team.deleted"], [], 10000);
  const [pick, setPick] = useState<{ a?: number; b?: number }>({});
  const [rollback, setRollback] = useState<{ to: number | null } | null>(null);
  const list = versions.data?.versions ?? [];
  const pinned = versions.data?.pinned ?? null;

  return (
    <Card
      title="Config versions"
      actions={pinned !== null && <Button onClick={() => setRollback({ to: null })}>Unpin (resume latest)</Button>}
    >
      {versions.error && <ErrorNote>{versions.error}</ErrorNote>}
      {pinned !== null && <p className="mb-3 font-medium text-warn">Gateways are pinned to v{pinned}; new routes will not roll out until unpinned.</p>}
      {!versions.data ? <Loading what="versions" /> : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-base">
            <thead className="text-sm text-muted">
              <tr><th className="py-2 pr-4">Version</th><th className="pr-4">Status</th><th className="pr-4">SHA-256</th><th className="pr-4">Created</th><th>Compare</th><th /></tr>
            </thead>
            <tbody>
              {list.map((v: ConfigVersionInfo) => (
                <tr key={v.version} className="border-t border-line">
                  <td className="py-2 pr-4 font-semibold">v{v.version}{v.version === pinned && " (pinned)"}</td>
                  <td className="pr-4"><Badge tone={v.status === "live" ? "good" : v.status === "pending" ? "info" : "idle"}>{v.status}</Badge></td>
                  <td className="pr-4 font-mono text-sm">{v.sha256.slice(0, 12)}</td>
                  <td className="pr-4">{new Date(v.created_at).toLocaleString()}</td>
                  <td className="space-x-3 whitespace-nowrap">
                    <label><input type="radio" name="from" aria-label={`compare from v${v.version}`} checked={pick.a === v.version} onChange={() => setPick((p) => ({ ...p, a: v.version }))} /> from</label>
                    <label><input type="radio" name="to" aria-label={`compare to v${v.version}`} checked={pick.b === v.version} onChange={() => setPick((p) => ({ ...p, b: v.version }))} /> to</label>
                  </td>
                  <td className="text-right"><Button disabled={v.version === pinned} onClick={() => setRollback({ to: v.version })}>Pin</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pick.a !== undefined && pick.b !== undefined && <DiffView a={pick.a} b={pick.b} />}

      {rollback && (
        <ConfirmDialog
          title={rollback.to === null ? "Unpin the config?" : `Roll back to v${rollback.to}?`}
          danger={rollback.to !== null}
          confirmLabel={rollback.to === null ? "Unpin" : "Roll back"}
          changes={
            rollback.to === null
              ? ["Let the compiler roll out the newest config again"]
              : [`Both gateways converge on v${rollback.to}`, "Newer routes stay out of service until you unpin", "Recorded in the audit log"]
          }
          onConfirm={async () => {
            await must(api.POST("/v1/config/rollback", { body: { version: rollback.to } }));
            versions.reload();
          }}
          onClose={() => setRollback(null)}
        />
      )}
    </Card>
  );
}

function DiffView({ a, b }: { a: number; b: number }) {
  const bodies = useLive(
    async () => {
      const [x, y] = await Promise.all([a, b].map((version) => must(api.GET("/v1/config/versions/{version}", { params: { path: { version } } }))));
      return [prettyJson(x!.body), prettyJson(y!.body)] as const;
    },
    [], [a, b],
  );
  const lines = useMemo(() => (bodies.data ? lineDiff(bodies.data[0], bodies.data[1]) : []), [bodies.data]);
  if (bodies.error) return <ErrorNote>{bodies.error}</ErrorNote>;
  if (!bodies.data) return <Loading what="diff" />;
  const changed = lines.some((l) => l.kind !== "same");
  return (
    <div className="mt-4">
      <h3 className="mb-2 font-semibold">v{a} → v{b}</h3>
      {!changed ? <Empty>The two versions are identical.</Empty> : (
        <pre className="max-h-96 overflow-auto rounded-lg border border-line bg-raised p-3 font-mono text-sm" aria-label="config diff">
          {lines.map((l, i) => (
            <div key={i} className={l.kind === "add" ? "bg-good-bg text-good" : l.kind === "del" ? "bg-bad-bg text-bad" : "text-muted"}>
              {l.kind === "add" ? "+ " : l.kind === "del" ? "- " : "  "}{l.text}
            </div>
          ))}
        </pre>
      )}
    </div>
  );
}
