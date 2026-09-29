import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { login, session } from "./api/session";
import { EventHub } from "./lib/events";
import { LiveProvider } from "./lib/live";

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

export type Route = (req: Request, body: unknown) => Response | Promise<Response> | undefined;

/** Replaces fetch with a router keyed by "METHOD /path"; records what was called. */
export function mockApi(routes: Record<string, Route | unknown>) {
  const calls: { key: string; body: unknown; headers: Headers; query: URLSearchParams }[] = [];
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(new URL(String(input), "http://x"), init);
    const text = req.method === "GET" ? "" : await req.clone().text();
    const body = text ? JSON.parse(text) : undefined;
    const key = `${req.method} ${new URL(req.url).pathname}`;
    calls.push({ key, body, headers: req.headers, query: new URL(req.url).searchParams });
    const r = routes[key];
    if (r === undefined) return json({ detail: `no mock for ${key}` }, 500);
    if (typeof r === "function") return (await (r as Route)(req, body)) ?? json({});
    return json(r);
  });
  return { calls, count: (key: string) => calls.filter((c) => c.key === key).length };
}

export class FakeSource {
  static last: FakeSource | undefined;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private handlers = new Map<string, (e: MessageEvent) => void>();
  constructor(public url: string) {
    FakeSource.last = this;
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.handlers.set(type, fn);
  }
  close() {}
  emit(kind: string, data: unknown) {
    this.handlers.get(kind)?.(new MessageEvent(kind, { data: JSON.stringify(data) }));
  }
}

export function renderLive(ui: ReactElement) {
  const hub = new EventHub({ url: () => "/v1/events", make: (u) => new FakeSource(u) });
  return { hub, ...render(<LiveProvider hub={hub}>{ui}</LiveProvider>) };
}

export async function signIn(role: "viewer" | "operator" | "admin") {
  session.logout();
  mockApi({ "POST /v1/auth/login": { access_token: "tok", token_type: "bearer", expires_in: 900, role } });
  await login(`${role}@x`, "pw");
}
