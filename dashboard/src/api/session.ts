import createClient, { type Middleware } from "openapi-fetch";
import { asRole, type Role } from "../lib/roles";
import type { paths } from "./schema";

export interface Session {
  token: string;
  role: Role;
  email: string;
  /** epoch ms */
  expiresAt: number;
}

const KEY = "ipo.session";
let current: Session | null = load();
let credentials: { email: string; password: string } | null = null;
const listeners = new Set<() => void>();

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    const s = raw ? (JSON.parse(raw) as Session) : null;
    return s && s.expiresAt > Date.now() ? s : null;
  } catch {
    return null;
  }
}

function set(s: Session | null): void {
  current = s;
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* private mode: the session then lives only in memory */
  }
  listeners.forEach((f) => f());
}

export const session = {
  get: () => current,
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  logout(): void {
    credentials = null;
    set(null);
  },
};

// fetch consumes a request's body, so a retry needs a copy taken before the first attempt.
const pristine = new WeakMap<Request, Request>();

const middleware: Middleware = {
  onRequest({ request }) {
    pristine.set(request, request.clone());
    if (current) request.headers.set("Authorization", `Bearer ${current.token}`);
    return request;
  },
  async onResponse({ response, request }) {
    // A 401 on anything but login means the 15-minute token ran out. Log in again with the
    // credentials kept in memory (never stored), retry once, else fall back to the login page.
    if (response.status !== 401 || new URL(request.url).pathname === "/v1/auth/login") return undefined;
    if (!credentials || !(await renew())) {
      session.logout();
      return undefined;
    }
    const retry = new Request(pristine.get(request) ?? request, { headers: new Headers(request.headers) });
    retry.headers.set("Authorization", `Bearer ${current!.token}`);
    return fetch(retry);
  },
};

export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? "",
  fetch: (request) => globalThis.fetch(request), // looked up per call, so tests can replace it
});
api.use(middleware);

async function renew(): Promise<boolean> {
  return !!credentials && (await login(credentials.email, credentials.password)) === null;
}

/** Returns null on success or a message for the login form. */
export async function login(email: string, password: string): Promise<string | null> {
  let res;
  try {
    res = await api.POST("/v1/auth/login", { body: { email, password } });
  } catch {
    return "Cannot reach the control API.";
  }
  if (!res.data) return res.response.status === 401 ? "Wrong email or password." : describeError(res.error);
  credentials = { email, password };
  set({
    token: res.data.access_token,
    role: asRole(res.data.role),
    email,
    expiresAt: Date.now() + res.data.expires_in * 1000,
  });
  return null;
}

/** RFC 7807 problem bodies carry `detail`; FastAPI validation errors carry a list. */
export function describeError(err: unknown): string {
  if (!err || typeof err !== "object") return "Request failed.";
  const e = err as { detail?: unknown; title?: string };
  if (typeof e.detail === "string") return e.detail;
  if (Array.isArray(e.detail)) {
    return e.detail.map((d: { loc?: unknown[]; msg?: string }) => `${d.loc?.slice(1).join(".") ?? ""}: ${d.msg ?? ""}`).join("; ");
  }
  return e.title ?? "Request failed.";
}
