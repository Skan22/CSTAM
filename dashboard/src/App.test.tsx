import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { session } from "./api/session";
import { FakeSource, json, mockApi, signIn } from "./test-utils";

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeSource);
  location.hash = "";
});
afterEach(() => {
  session.logout();
  vi.unstubAllGlobals();
});

const data = {
  "GET /v1/gateways": { gateways: [] },
  "GET /v1/pool": { size: 0, target: 0, free: 0, leased: 0, draining: 0, quarantined: 0 },
  "GET /v1/teams": [],
  "GET /v1/ipam": { total: 2, counts: { free: 1, leased: 1 }, leases: [
    { ip: "10.0.0.5", state: "leased", team: "alpha", expires_at: null, quarantined_until: null },
    { ip: "10.0.0.6", state: "free", team: null, expires_at: null, quarantined_until: null }] },
  "GET /v1/audit": { chain: { valid: true, first_bad_id: null }, entries: [
    { id: 2, at: new Date().toISOString(), actor: "admin@x", action: "team.register", target: "alpha", detail: { ip: "10.0.0.5" } }] },
};

test("without a session the login form shows, and a wrong password says so", async () => {
  session.logout();
  mockApi({ "POST /v1/auth/login": () => json({ detail: "no" }, 401) });
  render(<App />);
  await userEvent.type(screen.getByLabelText("Email"), "a@x");
  await userEvent.type(screen.getByLabelText("Password"), "bad");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByText("Wrong email or password.")).toBeInTheDocument();
});

test("a viewer's navigation leaves out the admin pages, and typing their URL is refused", async () => {
  await signIn("viewer");
  const api = mockApi(data);
  location.hash = "#/audit";
  render(<App />);
  expect(await screen.findByText("Not permitted")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Audit" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  expect(screen.getByRole("link", { name: "Teams" })).toBeInTheDocument();
  expect(api.count("GET /v1/audit")).toBe(0);
});

test("an admin sees the audit log with its chain status", async () => {
  await signIn("admin");
  mockApi(data);
  location.hash = "#/audit";
  render(<App />);
  expect(await screen.findByText("hash chain intact")).toBeInTheDocument();
  expect(screen.getByText("team.register")).toBeInTheDocument();
});

test("a broken audit chain is called out with the first bad entry", async () => {
  await signIn("admin");
  mockApi({ ...data, "GET /v1/audit": { chain: { valid: false, first_bad_id: 7 }, entries: [] } });
  location.hash = "#/audit";
  render(<App />);
  expect(await screen.findByText("chain broken at #7")).toBeInTheDocument();
});

test("clicking an address opens its detail, with admin-only history", async () => {
  await signIn("admin");
  mockApi(data);
  location.hash = "#/ipam";
  render(<App />);
  await userEvent.click(await screen.findByRole("listitem", { name: "10.0.0.5 leased" }));
  expect(screen.getByRole("heading", { name: "10.0.0.5" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText(/team.register/)).toBeInTheDocument());
});

test("logging out returns to the login form", async () => {
  await signIn("viewer");
  mockApi(data);
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: "Log out" }));
  expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
});
