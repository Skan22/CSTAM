import { useEffect, useState } from "react";
import { api } from "../api/session";
import { must } from "../api/types";
import { Card, ErrorNote, Loading } from "../components/ui";
import { useTeams } from "../lib/data";
import { PANELS, panelUrl } from "../lib/grafana";
import { useLive } from "../lib/live";

function useTheme(): "light" | "dark" {
  const read = (): "light" | "dark" => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const [theme, setTheme] = useState(read);
  useEffect(() => {
    const o = new MutationObserver(() => setTheme(read()));
    o.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => o.disconnect();
  }, []);
  return theme;
}

export function Metrics() {
  const ui = useLive(() => must(api.GET("/v1/ui")), [], []);
  const teams = useTeams(false);
  const theme = useTheme();
  const [team, setTeam] = useState("");

  if (ui.loading && !ui.data) return <Loading what="Metrics" />;
  if (ui.error && !ui.data) return <ErrorNote>{ui.error}</ErrorNote>;
  const base = ui.data?.grafana_url ?? "";
  if (!base) {
    return (
      <Card title="Metrics">
        <p className="text-muted">Grafana is not connected. Set <code>IPO_GRAFANA_URL</code> on the control plane to the address of a Grafana that allows embedding, then reload.</p>
      </Card>
    );
  }

  const slugs = [...new Set((teams.data ?? []).map((t) => t.slug))].sort();
  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-muted">
          Team
          <select className="rounded border border-line bg-raised px-2 py-1 text-ink" value={team} onChange={(e) => setTeam(e.target.value)}>
            <option value="">All teams</option>
            {slugs.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <a className="text-sm text-accent underline" href={base} target="_blank" rel="noreferrer noopener">Open Grafana</a>
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        {PANELS.map((p) => {
          const src = panelUrl(base, p, { theme, team });
          return (
            <Card key={`${p.uid}/${p.id}`} title={p.title}>
              {src && (
                <iframe
                  title={`${p.dashboard}: ${p.title}`}
                  src={src}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  sandbox="allow-scripts allow-same-origin"
                  className="h-64 w-full rounded border-0"
                />
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
