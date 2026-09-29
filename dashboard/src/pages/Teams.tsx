import { useRef, useState } from "react";
import { api } from "../api/session";
import { must, type Job, type Team } from "../api/types";
import { Badge, Button, Card, ConfirmDialog, Empty, ErrorNote, Loading, TEAM_TONE, useAction } from "../components/ui";
import { useTeams } from "../lib/data";
import { countdown } from "../lib/format";
import { useLive, useNow, useSession } from "../lib/live";
import { can } from "../lib/roles";

// randomUUID exists only in secure contexts; the dashboard may be served over plain http.
const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);

const EXTENSIONS = [
  { label: "15 minutes", seconds: 900 },
  { label: "1 hour", seconds: 3600 },
  { label: "4 hours", seconds: 14400 },
];

type Pending = { kind: "teardown"; team: Team } | { kind: "extend"; team: Team } | null;

export function Teams() {
  const role = useSession()?.role;
  const [showDeleted, setShowDeleted] = useState(false);
  const teams = useTeams(showDeleted);
  const [pending, setPending] = useState<Pending>(null);
  const [jobId, setJobId] = useState<string>();
  const [seconds, setSeconds] = useState(EXTENSIONS[1]!.seconds);
  const now = useNow();
  const operator = can(role, "operator");

  return (
    <div className="space-y-5">
      {operator && <RegisterCard onRegistered={setJobId} />}
      {jobId && <JobCard jobId={jobId} onDismiss={() => setJobId(undefined)} />}

      <Card
        title="Teams"
        actions={
          <label className="flex items-center gap-2 text-base">
            <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted(e.target.checked)} className="size-4" />
            Show deleted
          </label>
        }
      >
        {teams.error && <ErrorNote>{teams.error}</ErrorNote>}
        {teams.loading && !teams.data ? (
          <Loading what="teams" />
        ) : !teams.data?.length ? (
          <Empty>No teams yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-base">
              <thead className="text-sm text-muted">
                <tr>
                  <th className="py-2 pr-4">Subdomain</th>
                  <th className="pr-4">IP</th>
                  <th className="pr-4">State</th>
                  <th className="pr-4">Owner</th>
                  <th className="pr-4">Lease left</th>
                  {operator && <th />}
                </tr>
              </thead>
              <tbody>
                {teams.data.map((t) => {
                  const live = t.state === "active" || t.state === "pending";
                  return (
                    <tr key={t.id} className="border-t border-line">
                      <td className="py-2.5 pr-4 font-semibold">{t.subdomain}</td>
                      <td className="pr-4 font-mono">{t.ip ?? "-"}</td>
                      <td className="pr-4"><Badge tone={TEAM_TONE[t.state] ?? "idle"}>{t.state}</Badge></td>
                      <td className="pr-4">{t.owner}</td>
                      <td className="pr-4 tabular-nums">{live ? countdown(t.expires_at, now) : "-"}</td>
                      {operator && (
                        <td className="space-x-2 whitespace-nowrap text-right">
                          <Button disabled={t.state !== "active"} onClick={() => setPending({ kind: "extend", team: t })}>Extend</Button>
                          <Button variant="danger" disabled={t.state === "deleted" || t.state === "draining"} onClick={() => setPending({ kind: "teardown", team: t })}>Tear down</Button>
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {pending?.kind === "teardown" && (
        <ConfirmDialog
          title={`Tear down ${pending.team.slug}?`}
          danger
          confirmLabel="Tear down"
          changes={[
            `Remove the route for ${pending.team.subdomain} from both gateways`,
            `Delete the VM at ${pending.team.ip ?? "its address"}`,
            "Quarantine the address before it can be leased again",
          ]}
          onConfirm={() => must(api.DELETE("/v1/teams/{team_id}", { params: { path: { team_id: pending.team.id } } }))}
          onClose={() => setPending(null)}
        />
      )}
      {pending?.kind === "extend" && (
        <ConfirmDialog
          title={`Extend ${pending.team.slug}?`}
          confirmLabel="Extend"
          changes={[`Push the lease expiry of ${pending.team.subdomain} back by ${EXTENSIONS.find((e) => e.seconds === seconds)?.label ?? `${seconds}s`}`]}
          onConfirm={() => must(api.POST("/v1/teams/{team_id}/extend", { params: { path: { team_id: pending.team.id } }, body: { seconds } }))}
          onClose={() => setPending(null)}
        >
          <label className="flex items-center gap-3 text-base">
            Extend by
            <select value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} className="rounded-lg border border-line bg-raised px-3 py-2">
              {EXTENSIONS.map((e) => <option key={e.seconds} value={e.seconds}>{e.label}</option>)}
            </select>
          </label>
        </ConfirmDialog>
      )}
    </div>
  );
}

function RegisterCard({ onRegistered }: { onRegistered: (jobId: string) => void }) {
  const [slug, setSlug] = useState("");
  const { busy, error, run } = useAction();
  const [last, setLast] = useState<string>();
  // One key per intended registration: a resubmit after a timeout must not register twice.
  const key = useRef<string>(undefined);
  const valid = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(slug);

  return (
    <Card title="Register a team">
      <form
        className="flex flex-wrap items-start gap-3"
        onSubmit={async (e) => {
          e.preventDefault();
          await run(async () => {
            const r = await must(api.POST("/v1/teams", { params: { header: { "Idempotency-Key": key.current ??= newKey() } }, body: { slug } }));
            key.current = undefined;
            setLast(`${r.subdomain} via the ${r.path} path, ${r.ip}`);
            onRegistered(r.job_id);
            setSlug("");
          });
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-muted">Team slug</span>
          <input
            value={slug}
            onChange={(e) => { key.current = undefined; setSlug(e.target.value.toLowerCase()); }}
            placeholder="alpha"
            className="w-64 rounded-lg border border-line bg-raised px-3 py-2 text-base"
            aria-invalid={slug !== "" && !valid}
          />
        </label>
        <Button type="submit" variant="primary" className="mt-6" disabled={!valid || busy}>{busy ? "Registering…" : "Register"}</Button>
        {last && <p className="mt-6 font-medium text-good">Registered {last}</p>}
      </form>
      <div className="mt-3"><ErrorNote>{error}</ErrorNote></div>
    </Card>
  );
}

const TERMINAL = ["succeeded", "compensated", "failed"];

function JobCard({ jobId, onDismiss }: { jobId: string; onDismiss: () => void }) {
  // Saga steps announce nothing, so poll while the job is running.
  const [finished, setFinished] = useState(false);
  const job = useLive<Job>(
    async () => {
      const j = await must(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } }));
      setFinished(TERMINAL.includes(j.state));
      return j;
    },
    ["team.active", "team.failed"], [jobId], finished ? 0 : 1000,
  );
  const j = job.data;
  return (
    <Card title={`Registration job ${j ? `(${j.state})` : ""}`} actions={<Button onClick={onDismiss}>Dismiss</Button>}>
      {job.error && <ErrorNote>{job.error}</ErrorNote>}
      {j && (
        <ol className="flex flex-wrap gap-2">
          {j.steps.map((s) => (
            <li key={s.step}><Badge tone={s.status === "done" ? "good" : s.status === "failed" ? "bad" : s.status === "undone" ? "warn" : "info"}>{s.step}: {s.status}</Badge></li>
          ))}
        </ol>
      )}
      {j?.last_error && <div className="mt-3"><ErrorNote>{j.last_error}</ErrorNote></div>}
    </Card>
  );
}
