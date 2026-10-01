import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { session } from "../api/session";
import { FakeSource, mockApi, signIn } from "../test-utils";

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeSource);
  location.hash = "#/metrics";
});
afterEach(() => {
  session.logout();
  vi.unstubAllGlobals();
});

const frames = () => [...document.querySelectorAll("iframe")];

const base = {
  "GET /v1/teams": [
    { id: "t1", slug: "alpha", subdomain: "alpha.x", state: "active", owner: "o", ip: "1.2.3.4", created_at: "2026-01-01T00:00:00Z", expires_at: "2026-01-02T00:00:00Z" },
  ],
};

test("with Grafana configured the page embeds its panels as iframes", async () => {
  await signIn("viewer");
  mockApi({ ...base, "GET /v1/ui": { grafana_url: "https://grafana.example" } });
  render(<App />);
  await waitFor(() => expect(frames().length).toBeGreaterThan(3));
  for (const f of frames()) {
    expect(f.getAttribute("src")).toMatch(/^https:\/\/grafana\.example\/d-solo\//);
    expect(f.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(f.getAttribute("sandbox")).toContain("allow-scripts");
    expect(f.getAttribute("sandbox")).not.toContain("allow-top-navigation");
  }
  expect(screen.getByRole("link", { name: /open grafana/i })).toHaveAttribute("href", "https://grafana.example");
});

test("choosing a team filters the team panels only", async () => {
  await signIn("viewer");
  mockApi({ ...base, "GET /v1/ui": { grafana_url: "https://grafana.example" } });
  render(<App />);
  await waitFor(() => expect(frames().length).toBeGreaterThan(3));
  await userEvent.selectOptions(screen.getByLabelText("Team"), "alpha");
  await waitFor(() => {
    const srcs = frames().map((f) => f.getAttribute("src") ?? "");
    expect(srcs.filter((s) => s.includes("var-team=alpha")).length).toBeGreaterThan(0);
    expect(srcs.filter((s) => s.includes("var-team=")).every((s) => s.includes("team-traffic"))).toBe(true);
  });
});

test("without Grafana the page says how to turn it on and embeds nothing", async () => {
  await signIn("viewer");
  mockApi({ ...base, "GET /v1/ui": { grafana_url: "" } });
  render(<App />);
  expect(await screen.findByText(/IPO_GRAFANA_URL/)).toBeInTheDocument();
  expect(document.querySelector("iframe")).toBeNull();
});

test("a team login has no Metrics page", async () => {
  await signIn("team");
  const api = mockApi({
    "GET /v1/me": { email: "a@t", role: "team", team: { id: "t1", slug: "alpha", host: "alpha.x", state: "active", created_at: "2026-01-01T00:00:00Z", expires_at: "2027-01-01T00:00:00Z" } },
    "GET /v1/me/traffic": { window_minutes: 60, totals: { requests: 0, s2xx: 0, s3xx: 0, s4xx: 0, s5xx: 0, bytes: 0 }, teams: [], series: [] },
    "GET /v1/me/traffic/recent": { requests: [] },
  });
  render(<App />);
  await screen.findByRole("heading", { name: "alpha" });
  expect(screen.queryByRole("link", { name: "Metrics" })).toBeNull();
  expect(api.count("GET /v1/ui")).toBe(0);
});
