import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { session } from "../api/session";
import { EventHub, type LiveEvent } from "./events";

const HubContext = createContext<EventHub | null>(null);

/** Owns the single SSE connection for the logged-in user. */
export function LiveProvider({ children, hub: injected, stream = "/v1/events" }: { children: ReactNode; hub?: EventHub; stream?: string }) {
  const hub = useMemo(
    () =>
      injected ??
      new EventHub({
        url: () => {
          const s = session.get();
          return s ? `${stream}?access_token=${encodeURIComponent(s.token)}` : null;
        },
      }),
    [injected, stream],
  );
  useEffect(() => {
    hub.start();
    return () => hub.stop();
  }, [hub]);
  return <HubContext.Provider value={hub}>{children}</HubContext.Provider>;
}

export function useHub(): EventHub {
  const hub = useContext(HubContext);
  if (!hub) throw new Error("LiveProvider is missing");
  return hub;
}

export function useConnectionStatus() {
  const hub = useHub();
  return useSyncExternalStore(
    (cb) => hub.onStatus(cb),
    () => hub.status,
  );
}

/** Calls `fn` for each event whose kind matches (all kinds when `kinds` is empty). */
export function useEvents(fn: (e: LiveEvent) => void, kinds: readonly string[] = []) {
  const hub = useHub();
  const ref = useRef(fn);
  ref.current = fn;
  const key = kinds.join(",");
  useEffect(
    () =>
      hub.subscribe((e) => {
        if (e.kind === "reconnected" || kinds.length === 0 || kinds.includes(e.kind)) ref.current(e);
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hub, key],
  );
}

// An empty list means "every event" to useEvents; a resource that wants none listens only for a reconnect.
const NO_EVENTS = ["reconnected"] as const;

export interface Resource<T> {
  data?: T;
  error?: string;
  loading: boolean;
  reload: () => void;
}

/** Loads on mount and again, shortly after, whenever a matching event arrives or the stream
 * reconnects. The short delay coalesces the several events one action tends to produce. */
export function useLive<T>(load: () => Promise<T>, kinds: readonly string[], deps: unknown[] = [], pollMs = 0): Resource<T> {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const seq = useRef(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const run = useCallback(() => {
    const mine = ++seq.current;
    loadRef.current().then(
      (data) => mine === seq.current && setState({ data, loading: false }),
      (e: unknown) => mine === seq.current && setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e), loading: false })),
    );
  }, []);

  useEffect(() => {
    setState((s) => ({ ...s, loading: true }));
    run();
    return () => {
      seq.current++;
      clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, ...deps]);

  useEvents(() => {
    clearTimeout(timer.current);
    timer.current = setTimeout(run, 50);
  }, kinds.length ? kinds : NO_EVENTS);

  // A safety net for state no event announces (a gateway's config version, say).
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(run, pollMs);
    return () => clearInterval(t);
  }, [run, pollMs]);

  return { ...state, reload: run };
}

/** Re-renders every second, for countdowns. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function useSession() {
  return useSyncExternalStore(session.subscribe, session.get);
}

/** Recent events of the given kinds, newest last, re-rendering as more arrive. */
export function useHistory(kinds: readonly string[]): LiveEvent[] {
  const hub = useHub();
  const [, bump] = useState(0);
  useEvents(() => bump((n) => n + 1), kinds);
  return hub.history.filter((e) => kinds.includes(e.kind));
}
