import { api, describeError, login, session } from "./session";

type Handler = (req: Request) => Response;
let handler: Handler;
let calls: string[];

beforeEach(() => {
  calls = [];
  session.logout();
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(new URL(String(input), "http://x"), init);
    calls.push(`${req.method} ${new URL(req.url).pathname} ${req.headers.get("Authorization") ?? ""}`.trim());
    if (req.body) await req.arrayBuffer(); // like the real fetch, consume the body
    return handler(req);
  });
});
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

test("login stores the token and role and sends the token afterwards", async () => {
  handler = (req) =>
    new URL(req.url).pathname === "/v1/auth/login"
      ? json({ access_token: "t1", token_type: "bearer", expires_in: 900, role: "operator" })
      : json({ size: 1, target: 2, free: 3, leased: 0, draining: 0, quarantined: 0 });
  expect(await login("a@x", "pw")).toBeNull();
  expect(session.get()).toMatchObject({ role: "operator", email: "a@x", token: "t1" });
  await api.GET("/v1/pool");
  expect(calls.at(-1)).toBe("GET /v1/pool Bearer t1");
});

test("a wrong password is reported and leaves no session", async () => {
  handler = () => json({ detail: "invalid email or password" }, 401);
  expect(await login("a@x", "bad")).toBe("Wrong email or password.");
  expect(session.get()).toBeNull();
});

test("an expired token is renewed once, silently, and the request retried", async () => {
  let n = 0;
  handler = (req) => {
    const path = new URL(req.url).pathname;
    if (path === "/v1/auth/login") return json({ access_token: `t${++n}`, expires_in: 900, role: "admin" });
    return req.headers.get("Authorization") === "Bearer t2" ? json({ size: 5 }) : json({ detail: "expired" }, 401);
  };
  await login("a@x", "pw");
  const res = await api.GET("/v1/pool");
  expect(res.data).toMatchObject({ size: 5 });
  expect(calls.filter((c) => c.startsWith("POST /v1/auth/login"))).toHaveLength(2);
});

test("a request with a body is retried with its body after the token is renewed", async () => {
  let n = 0;
  let retried: unknown;
  handler = (req) => {
    if (new URL(req.url).pathname === "/v1/auth/login") return json({ access_token: `t${++n}`, expires_in: 900, role: "admin" });
    if (req.headers.get("Authorization") !== "Bearer t2") return json({ detail: "expired" }, 401);
    retried = req.method;
    return json({ values: {}, history: [] });
  };
  await login("a@x", "pw");
  const res = await api.PATCH("/v1/settings", { body: { pool_target: 8 } });
  expect(res.data).toBeDefined();
  expect(retried).toBe("PATCH");
});

test("when renewal fails the session ends", async () => {
  let logins = 0;
  handler = (req) => {
    if (new URL(req.url).pathname === "/v1/auth/login") {
      return ++logins === 1 ? json({ access_token: "t1", expires_in: 900, role: "viewer" }) : json({ detail: "no" }, 401);
    }
    return json({ detail: "expired" }, 401);
  };
  await login("a@x", "pw");
  await api.GET("/v1/pool");
  expect(session.get()).toBeNull();
});

test("problem details become readable messages", () => {
  expect(describeError({ detail: "slug taken" })).toBe("slug taken");
  expect(describeError({ detail: [{ loc: ["body", "slug"], msg: "too short" }] })).toBe("slug: too short");
  expect(describeError(undefined)).toBe("Request failed.");
});
