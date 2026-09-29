/** One SSE connection shared by every page. It reconnects on its own and says when it did, so
 * pages refetch what they may have missed: the server does not replay events. */

export const EVENT_KINDS = [
  "team.active", "team.deleted", "team.draining", "team.failed",
  "lease.changed", "gateway.vrrp", "gateway.config", "gateway.split_brain",
  "gateway.split_brain_resolved", "alert.vm_gone",
] as const;

export interface LiveEvent {
  kind: string;
  at: number;
  data: Record<string, unknown>;
}

export type Status = "connecting" | "live" | "reconnecting" | "stopped";

export interface SourceLike {
  addEventListener(type: string, fn: (e: MessageEvent) => void): void;
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  close(): void;
}

export interface HubOptions {
  url: () => string | null;
  make?: (url: string) => SourceLike;
  now?: () => number;
  /** Delay before reconnect attempt n (0-based), in ms. */
  backoff?: (attempt: number) => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
}

const HISTORY_LIMIT = 300;

type Listener = (e: LiveEvent) => void;

export class EventHub {
  status: Status = "stopped";
  /** Bumped on every (re)connect after the first, so pages know to reload. */
  reconnects = 0;
  /** The last events seen this session, oldest first, for timelines. */
  history: LiveEvent[] = [];
  private src: SourceLike | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<() => void>();
  private attempt = 0;
  private timer: unknown = null;
  private opened = false;
  private o: Required<HubOptions>;

  constructor(o: HubOptions) {
    this.o = {
      make: (u) => new EventSource(u) as unknown as SourceLike,
      now: () => Date.now(),
      backoff: (n) => Math.min(500 * 2 ** n, 10_000),
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>),
      ...o,
    };
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: () => void): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  start(): void {
    if (this.status !== "stopped") return;
    this.setStatus("connecting");
    this.connect();
  }

  stop(): void {
    if (this.timer) this.o.clearTimer(this.timer);
    this.timer = null;
    this.src?.close();
    this.src = null;
    this.opened = false;
    this.attempt = 0;
    this.setStatus("stopped");
  }

  private setStatus(s: Status): void {
    this.status = s;
    this.statusListeners.forEach((f) => f());
  }

  private connect(): void {
    const url = this.o.url();
    if (!url) return this.stop();
    const src = this.o.make(url);
    this.src = src;
    const emit = (kind: string) => (e: MessageEvent) => {
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(String(e.data));
      } catch {
        /* keep the empty payload; the kind alone still triggers a refresh */
      }
      const ev = { kind, at: this.o.now(), data };
      this.history = [...this.history, ev].slice(-HISTORY_LIMIT);
      this.listeners.forEach((f) => f(ev));
    };
    for (const k of EVENT_KINDS) src.addEventListener(k, emit(k));
    src.addEventListener("message", emit("message"));
    src.onopen = () => {
      // Also after a first connection that failed: events sent before it opened are lost too.
      if (this.opened || this.attempt > 0) this.reconnects++;
      this.opened = true;
      this.attempt = 0;
      this.setStatus("live");
      if (this.reconnects > 0) this.listeners.forEach((f) => f({ kind: "reconnected", at: this.o.now(), data: {} }));
    };
    src.onerror = () => {
      // The browser would retry with the same, possibly expired, URL; do it ourselves.
      src.close();
      if (this.src !== src) return;
      this.src = null;
      this.setStatus("reconnecting");
      this.timer = this.o.setTimer(() => {
        this.timer = null;
        this.connect();
      }, this.o.backoff(this.attempt++));
    };
  }
}
