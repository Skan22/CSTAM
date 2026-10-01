import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { session } from "../api/session";
import { FakeSource, mockApi, signIn } from "../test-utils";

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeSource);
  FakeSource.last = undefined;
  location.hash = "";
});
afterEach(() => {
  session.logout();
  vi.unstubAllGlobals();
});

const team = {
  id: "t1", slug: "alpha", host: "alpha.example.net", state: "active",
  created_at: "2026-01-01T00:00:00Z", expires_at: new Date(Date.now() + 3 * 3600_000).toISOString(),
};

function traffic(requests: number, s5xx = 0) {
  return {
    window_minutes: 60,
    totals: { requests, s2xx: requests - s5xx, s3xx: 0, s4xx: 0, s5xx, bytes: 2048 },
    teams: [{ team_id: "t1", slug: "alpha", host: team.host, requests, s2xx: requests - s5xx, s3xx: 0, s4xx: 0, s5xx, bytes: 2048, avg_ms: 12.4, last_seen: new Date().toISOString() }],
    series: requests ? [{ at: "2026-01-01T12:00:00Z", requests, errors: s5xx }] : [],
  };
}

const recent = { requests: [{ at: "2026-01-01T12:00:00Z", gateway: "gw-a", host: team.host, slug: "alpha", method: "GET", path: "/hello", status: 200, duration_ms: 9 }] };

function routes(n = { requests: 200, s5xx: 4 }) {
  return {
    "GET /v1/me": { email: "alpha@team", role: "team", team },
    "GET /v1/me/traffic": () => new Response(JSON.stringify(traffic(n.requests, n.s5xx)), { headers: { "content-type": "application/json" } }),
    "GET /v1/me/traffic/recent": recent,
  };
}

test("a team login lands on its own page and reads only the /v1/me routes", async () => {
  await signIn("team");
  const api = mockApi(routes());
  location.hash = "#/audit";
  render(<App />);
  expect(await screen.findByRole("heading", { name: "alpha" })).toBeInTheDocument();
  expect(screen.getByText("alpha.example.net")).toBeInTheDocument();
  expect(screen.getByText("active")).toBeInTheDocument();
  expect(screen.getByText("2%")).toBeInTheDocument();
  expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  expect(screen.getByText("/hello")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /Teams|Gateways|IPAM|Audit|Settings/ })).toBeNull();
  const staff = api.calls.filter((c) => !c.key.includes("/v1/me"));
  expect(staff).toEqual([]);
  expect(screen.queryByText("Not permitted")).toBeNull();
});

test("it listens to the team's own event stream, not the staff one", async () => {
  await signIn("team");
  mockApi(routes());
  render(<App />);
  await screen.findByRole("heading", { name: "alpha" });
  await waitFor(() => expect(FakeSource.last?.url).toMatch(/^\/v1\/me\/events\?access_token=/));
});

test("the time window is a button and changes what is asked for", async () => {
  await signIn("team");
  const api = mockApi(routes());
  render(<App />);
  await screen.findByRole("heading", { name: "alpha" });
  await userEvent.click(screen.getByRole("button", { name: "24 h" }));
  await waitFor(() => expect(api.calls.some((c) => c.key === "GET /v1/me/traffic" && c.query.get("window_minutes") === "1440")).toBe(true));
});

test("a traffic nudge on the stream refreshes the numbers", async () => {
  await signIn("team");
  const n = { requests: 200, s5xx: 4 };
  mockApi(routes(n));
  render(<App />);
  await screen.findByRole("heading", { name: "alpha" });
  n.requests = 350;
  act(() => FakeSource.last!.emit("traffic.batch", { kind: "traffic.batch" }));
  const tiles = await screen.findByText("350");
  expect(within(tiles.parentElement!).getByText("Requests")).toBeInTheDocument();
});

test("a lifecycle event for the team reloads its state", async () => {
  await signIn("team");
  let state = "pending";
  mockApi({ ...routes(), "GET /v1/me": () => new Response(JSON.stringify({ email: "a", role: "team", team: { ...team, state } }), { headers: { "content-type": "application/json" } }) });
  render(<App />);
  expect(await screen.findByText("pending")).toBeInTheDocument();
  state = "active";
  act(() => FakeSource.last!.emit("team.active", { kind: "team.active", slug: "alpha" }));
  expect(await screen.findByText("active")).toBeInTheDocument();
});

test("a team with no traffic yet says so", async () => {
  await signIn("team");
  mockApi({ ...routes({ requests: 0, s5xx: 0 }), "GET /v1/me/traffic/recent": { requests: [] } });
  render(<App />);
  expect(await screen.findByText("No requests in this window.")).toBeInTheDocument();
  expect(screen.getByText("No recent requests.")).toBeInTheDocument();
});

test("a staff login still gets the staff pages", async () => {
  await signIn("viewer");
  mockApi({ "GET /v1/gateways": { gateways: [] }, "GET /v1/pool": { size: 0, target: 0, free: 0, leased: 0, draining: 0, quarantined: 0 }, "GET /v1/teams": [] });
  render(<App />);
  expect(await screen.findByRole("link", { name: "Teams" })).toBeInTheDocument();
});
