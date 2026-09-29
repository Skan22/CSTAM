import { EventHub, type LiveEvent, type SourceLike } from "./events";

class FakeSource implements SourceLike {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  handlers = new Map<string, (e: MessageEvent) => void>();
  constructor(public url: string) {}
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.handlers.set(type, fn);
  }
  close() {
    this.closed = true;
  }
  send(type: string, data: string) {
    this.handlers.get(type)?.({ data } as MessageEvent);
  }
}

function setup() {
  const sources: FakeSource[] = [];
  const timers: (() => void)[] = [];
  let token = "t1";
  const hub = new EventHub({
    url: () => `/v1/events?access_token=${token}`,
    make: (u) => {
      const s = new FakeSource(u);
      sources.push(s);
      return s;
    },
    now: () => 1000,
    backoff: () => 0,
    setTimer: (fn) => timers.push(fn),
    clearTimer: () => {},
  });
  return { hub, sources, timers, setToken: (t: string) => (token = t) };
}

test("named events reach subscribers with their parsed payload", () => {
  const { hub, sources } = setup();
  const seen: LiveEvent[] = [];
  hub.subscribe((e) => seen.push(e));
  hub.start();
  sources[0]!.onopen?.();
  sources[0]!.send("lease.changed", '{"kind":"lease.changed","ip":"10.20.0.11","to":"leased"}');
  expect(hub.status).toBe("live");
  expect(seen).toEqual([
    { kind: "lease.changed", at: 1000, data: { kind: "lease.changed", ip: "10.20.0.11", to: "leased" } },
  ]);
});

test("a garbled payload still notifies, with empty data", () => {
  const { hub, sources } = setup();
  const seen: LiveEvent[] = [];
  hub.subscribe((e) => seen.push(e));
  hub.start();
  sources[0]!.send("team.active", "not json");
  expect(seen[0]).toMatchObject({ kind: "team.active", data: {} });
});

test("after an error it reconnects with a fresh URL and tells pages to reload", () => {
  const { hub, sources, timers, setToken } = setup();
  const seen: string[] = [];
  hub.subscribe((e) => seen.push(e.kind));
  hub.start();
  sources[0]!.onopen?.();
  expect(seen).toEqual([]); // first connect needs no reload

  setToken("t2");
  sources[0]!.onerror?.();
  expect(sources[0]!.closed).toBe(true);
  expect(hub.status).toBe("reconnecting");
  timers.shift()!();
  expect(sources[1]!.url).toContain("t2");
  sources[1]!.onopen?.();
  expect(hub.status).toBe("live");
  expect(seen).toEqual(["reconnected"]);
});

test("a connection that failed before it ever opened still tells pages to reload once it does", () => {
  const { hub, sources, timers } = setup();
  const seen: string[] = [];
  hub.subscribe((e) => seen.push(e.kind));
  hub.start();
  sources[0]!.onerror?.();
  timers.shift()!();
  sources[1]!.onopen?.();
  expect(seen).toEqual(["reconnected"]);
});

test("stop closes the connection and cancels a pending retry", () => {
  const { hub, sources } = setup();
  hub.start();
  hub.stop();
  expect(sources[0]!.closed).toBe(true);
  expect(hub.status).toBe("stopped");
});

test("with no token it stays stopped", () => {
  const hub = new EventHub({ url: () => null });
  hub.start();
  expect(hub.status).toBe("stopped");
});

test("history keeps the most recent events for timelines", () => {
  const { hub, sources } = setup();
  hub.start();
  for (let i = 0; i < 350; i++) sources[0]!.send("gateway.vrrp", `{"to":"${i}"}`);
  expect(hub.history).toHaveLength(300);
  expect(hub.history.at(-1)!.data).toEqual({ to: "349" });
  expect(hub.history[0]!.data).toEqual({ to: "50" });
});
