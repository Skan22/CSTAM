import { useEffect, useState, type ReactNode } from "react";
import { session } from "../api/session";
import { useConnectionStatus, useSession } from "../lib/live";
import { can, type Role } from "../lib/roles";
import { Badge, Button, type Tone } from "./ui";

export interface Route {
  path: string;
  label: string;
  min: Role;
}

export const ROUTES: Route[] = [
  { path: "overview", label: "Overview", min: "viewer" },
  { path: "teams", label: "Teams", min: "viewer" },
  { path: "gateways", label: "Gateways", min: "viewer" },
  { path: "traffic", label: "Traffic", min: "viewer" },
  { path: "ipam", label: "IPAM", min: "viewer" },
  { path: "audit", label: "Audit", min: "admin" },
  { path: "settings", label: "Settings", min: "admin" },
];

const STATUS: Record<string, { tone: Tone; text: string }> = {
  live: { tone: "good", text: "Live" },
  connecting: { tone: "info", text: "Connecting" },
  reconnecting: { tone: "warn", text: "Reconnecting" },
  stopped: { tone: "bad", text: "Offline" },
};

function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      const saved = localStorage.getItem("ipo.theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch { /* storage blocked */ }
    return matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("ipo.theme", theme); } catch { /* storage blocked */ }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))] as const;
}

export function Shell({ route, children }: { route: string; children: ReactNode }) {
  const s = useSession();
  const status = useConnectionStatus();
  const [theme, toggle] = useTheme();
  const live = STATUS[status] ?? STATUS.stopped!;
  const nav = ROUTES.filter((r) => can(s?.role, r.min));

  return (
    <div className="min-h-screen md:grid md:grid-cols-[13rem_1fr]">
      <aside className="border-b border-line bg-surface p-4 md:border-b-0 md:border-r">
        <div className="mb-4 text-xl font-extrabold">IPO Control</div>
        <nav aria-label="Main" className="flex flex-wrap gap-1 md:flex-col">
          {nav.map((r) => (
            <a
              key={r.path}
              href={`#/${r.path}`}
              aria-current={route === r.path ? "page" : undefined}
              className={`rounded-lg px-3 py-2 text-base font-semibold ${route === r.path ? "bg-accent text-accent-ink" : "hover:bg-raised"}`}
            >
              {r.label}
            </a>
          ))}
        </nav>
      </aside>
      <div className="min-w-0">
        <header className="flex flex-wrap items-center justify-end gap-3 border-b border-line bg-surface px-5 py-3">
          <span title="Server-sent events connection"><Badge tone={live.tone}>{live.text}</Badge></span>
          <Button onClick={toggle} aria-label="Toggle colour theme">{theme === "dark" ? "Light" : "Dark"}</Button>
          <span className="text-base">{s?.email} <Badge tone="idle">{s?.role}</Badge></span>
          <Button onClick={() => session.logout()}>Log out</Button>
        </header>
        <main className="p-5">{children}</main>
      </div>
    </div>
  );
}
