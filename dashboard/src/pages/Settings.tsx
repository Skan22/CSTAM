import { useEffect, useState } from "react";
import { api } from "../api/session";
import { must, type SettingsPatch } from "../api/types";
import { Button, Card, Empty, ErrorNote, Loading, useAction } from "../components/ui";
import { useLive } from "../lib/live";

const FIELDS: { key: keyof SettingsPatch; label: string; hint: string }[] = [
  { key: "pool_target", label: "Warm pool target", hint: "Addresses kept booted and ready" },
  { key: "reserve_free_ips", label: "Free-address reserve", hint: "Never lease below this many free addresses" },
  { key: "lease_ttl_seconds", label: "Lease TTL (s)", hint: "Default team lease length" },
  { key: "quarantine_seconds", label: "Quarantine (s)", hint: "Cool-down before an address is reused" },
  { key: "max_parallel_boots", label: "Parallel boots", hint: "Concurrent VM boots on the cold path" },
];

export function Settings() {
  const settings = useLive(() => must(api.GET("/v1/settings")), []);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const { busy, error, run } = useAction();
  const values = settings.data?.values;

  useEffect(() => {
    if (values) setDraft(Object.fromEntries(FIELDS.map((f) => [f.key, String(values[f.key] ?? "")])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(values)]);

  const changed: SettingsPatch = {};
  for (const f of FIELDS) {
    const n = Number(draft[f.key]);
    if (values && draft[f.key] !== undefined && draft[f.key] !== "" && Number.isFinite(n) && n !== values[f.key]) changed[f.key] = n;
  }
  const dirty = Object.keys(changed).length > 0;

  return (
    <div className="space-y-5">
      <Card title="Runtime settings">
        {settings.error && <ErrorNote>{settings.error}</ErrorNote>}
        {!values ? <Loading what="settings" /> : (
          <form
            className="grid max-w-2xl gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await must(api.PATCH("/v1/settings", { body: changed }));
                settings.reload();
              });
            }}
          >
            {FIELDS.map((f) => (
              <label key={f.key} className="grid gap-1 sm:grid-cols-[14rem_10rem_1fr] sm:items-center sm:gap-4">
                <span className="font-semibold">{f.label}</span>
                <input
                  type="number"
                  min={0}
                  value={draft[f.key] ?? ""}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  className="rounded-lg border border-line bg-raised px-3 py-2 text-base"
                />
                <span className="text-muted">{f.hint}</span>
              </label>
            ))}
            <div className="flex items-center gap-3">
              <Button type="submit" variant="primary" disabled={!dirty || busy}>{busy ? "Saving…" : "Save changes"}</Button>
              {dirty && <span className="text-muted">Changing: {Object.keys(changed).join(", ")}</span>}
            </div>
            <ErrorNote>{error}</ErrorNote>
          </form>
        )}
      </Card>
      <Card title="Change history">
        {!settings.data?.history.length ? <Empty>No changes yet.</Empty> : (
          <ul className="space-y-1.5 text-base">
            {settings.data.history.map((h, i) => (
              <li key={i}><span className="tabular-nums text-muted">{new Date(h.at).toLocaleString()}</span> · {h.actor} · <span className="font-mono text-sm">{JSON.stringify(h.detail)}</span></li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
