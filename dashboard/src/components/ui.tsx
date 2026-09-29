import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";

export type Tone = "good" | "warn" | "bad" | "info" | "idle" | "violet";

const TONES: Record<Tone, string> = {
  good: "bg-good-bg text-good",
  warn: "bg-warn-bg text-warn",
  bad: "bg-bad-bg text-bad",
  info: "bg-info-bg text-info",
  idle: "bg-idle-bg text-muted",
  violet: "bg-violet-bg text-violet",
};

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`inline-flex shrink-0 items-center self-start whitespace-nowrap rounded-full px-2.5 py-0.5 text-sm font-semibold ${TONES[tone]}`}>{children}</span>;
}

export const TEAM_TONE: Record<string, Tone> = {
  active: "good", pending: "info", draining: "warn", deleted: "idle", failed: "bad",
};
export const LEASE_TONE: Record<string, Tone> = {
  free: "idle", pooled: "info", leased: "good", draining: "warn", quarantined: "violet",
};

type Variant = "primary" | "secondary" | "danger";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink hover:brightness-110",
  secondary: "bg-raised text-ink border border-line hover:brightness-95",
  danger: "bg-bad text-accent-ink hover:brightness-110",
};

export function Button({ variant = "secondary", className = "", ...rest }: ComponentProps<"button"> & { variant?: Variant }) {
  return (
    <button
      {...rest}
      className={`rounded-lg px-3.5 py-2 text-base font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    />
  );
}

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-line bg-surface p-5 ${className}`}>
      {(title || actions) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-lg font-bold">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  const color = tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : tone === "good" ? "text-good" : "";
  return (
    <div>
      <div className="text-sm font-medium text-muted">{label}</div>
      <div className={`text-3xl font-bold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-sm text-muted">{sub}</div>}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return children ? (
    <p role="alert" className="rounded-lg bg-bad-bg px-3 py-2 text-base font-medium text-bad">
      {children}
    </p>
  ) : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-muted">{children}</p>;
}

/** Runs an async action, exposing busy and error state for a button or a dialog. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const run = async (fn: () => Promise<unknown>): Promise<boolean> => {
    setBusy(true);
    setError(undefined);
    try {
      await fn();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, clear: () => setError(undefined) };
}

/** Every destructive action goes through this: it lists exactly what will change. */
export function ConfirmDialog(props: {
  title: string;
  changes: string[];
  confirmLabel: string;
  danger?: boolean;
  children?: ReactNode;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}) {
  const { busy, error, run } = useAction();
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => first.current?.focus(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && props.onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="dlg-title" className="w-full max-w-lg rounded-xl border border-line bg-surface p-6 shadow-2xl">
        <h2 id="dlg-title" className="text-xl font-bold">{props.title}</h2>
        <p className="mt-3 text-sm font-semibold uppercase tracking-wide text-muted">This will</p>
        <ul className="mt-1 list-disc space-y-1 pl-6 text-base">
          {props.changes.map((c) => <li key={c}>{c}</li>)}
        </ul>
        {props.children && <div className="mt-4">{props.children}</div>}
        <div className="mt-4"><ErrorNote>{error}</ErrorNote></div>
        <div className="mt-5 flex justify-end gap-3">
          <Button ref={first} onClick={props.onClose} disabled={busy}>Cancel</Button>
          <Button
            variant={props.danger ? "danger" : "primary"}
            disabled={busy}
            onClick={async () => (await run(props.onConfirm)) && props.onClose()}
          >
            {busy ? "Working…" : props.confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="py-6 text-center text-muted">Loading {what}…</p>;
}
