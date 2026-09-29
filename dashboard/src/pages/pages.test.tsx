import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Gateways } from "./Gateways";
import { Overview } from "./Overview";
import { Settings } from "./Settings";
import { Teams } from "./Teams";
import { FakeSource, json, mockApi, renderLive, signIn } from "../test-utils";

afterEach(() => vi.unstubAllGlobals());

const inFuture = (s: number) => new Date(Date.now() + s * 1000).toISOString();
const team = (o: object = {}) => ({
  id: "t1", slug: "alpha", subdomain: "alpha.hack.example", ip: "10.0.0.5", owner: "op@x",
  state: "active", created_at: inFuture(-60), expires_at: inFuture(3600), ...o,
});
const gw = (o: object = {}) => ({ gateway: "gw1", vrrp_state: "MASTER", live_version: 3, last_heartbeat: new Date().toISOString(), ...o });

describe("Teams", () => {
  test("an operator tears a team down only after the confirmation lists what changes", async () => {
    await signIn("operator");
    const api = mockApi({ "GET /v1/teams": [team()], "DELETE /v1/teams/t1": () => json({ job_id: "j", team_id: "t1", status_url: "/v1/jobs/j" }, 202) });
    renderLive(<Teams />);
    await userEvent.click(await screen.findByRole("button", { name: "Tear down" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Remove the route for alpha.hack.example/)).toBeInTheDocument();
    expect(api.count("DELETE /v1/teams/t1")).toBe(0);

    await userEvent.click(within(dialog).getByRole("button", { name: "Tear down" }));
    await waitFor(() => expect(api.count("DELETE /v1/teams/t1")).toBe(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("cancelling the confirmation deletes nothing", async () => {
    await signIn("operator");
    const api = mockApi({ "GET /v1/teams": [team()] });
    renderLive(<Teams />);
    await userEvent.click(await screen.findByRole("button", { name: "Tear down" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(api.count("DELETE /v1/teams/t1")).toBe(0);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("a viewer sees the teams but no way to change them", async () => {
    await signIn("viewer");
    mockApi({ "GET /v1/teams": [team()] });
    renderLive(<Teams />);
    expect(await screen.findByText("alpha.hack.example")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tear down" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Register" })).toBeNull();
  });

  test("registering sends the slug with an Idempotency-Key and shows the job's steps", async () => {
    await signIn("operator");
    const api = mockApi({
      "GET /v1/teams": [],
      "POST /v1/teams": () => json({ team_id: "t2", job_id: "j1", ip: "10.0.0.9", path: "warm", subdomain: "beta.hack.example", status_url: "/v1/jobs/j1" }, 202),
      "GET /v1/jobs/j1": { id: "j1", kind: "register_team", state: "running", attempts: 1, created_at: inFuture(0), last_error: null, team_id: "t2", trace_id: null,
        steps: [{ step: "claim", status: "done", started_at: inFuture(0), finished_at: inFuture(0), result: null }, { step: "route", status: "running", started_at: inFuture(0), finished_at: null, result: null }] },
    });
    renderLive(<Teams />);
    const register = await screen.findByRole("button", { name: "Register" });
    expect(register).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Team slug"), "beta");
    await userEvent.click(register);

    expect(await screen.findByText(/Registered beta.hack.example via the warm path/)).toBeInTheDocument();
    const post = api.calls.find((c) => c.key === "POST /v1/teams")!;
    expect(post.body).toEqual({ slug: "beta" });
    expect(post.headers.get("Idempotency-Key")).toMatch(/[0-9a-f-]{36}/);
    expect(await screen.findByText("route: running")).toBeInTheDocument();
  });

  test("resubmitting after a failure reuses the Idempotency-Key", async () => {
    await signIn("operator");
    let n = 0;
    const api = mockApi({ "GET /v1/teams": [], "POST /v1/teams": () => json({ detail: "timeout" }, ++n === 1 ? 504 : 409) });
    renderLive(<Teams />);
    await userEvent.type(await screen.findByLabelText("Team slug"), "beta");
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    await screen.findByText(/timeout/);
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    await waitFor(() => expect(api.count("POST /v1/teams")).toBe(2));
    const keys = api.calls.filter((c) => c.key === "POST /v1/teams").map((c) => c.headers.get("Idempotency-Key"));
    expect(keys[0]).toBe(keys[1]);
  });

  test("a server refusal is shown, not swallowed", async () => {
    await signIn("operator");
    mockApi({ "GET /v1/teams": [], "POST /v1/teams": () => json({ detail: "slug already taken" }, 409) });
    renderLive(<Teams />);
    await userEvent.type(await screen.findByLabelText("Team slug"), "beta");
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByText(/slug already taken/)).toBeInTheDocument();
  });

  test("a lease.changed event refetches the list", async () => {
    await signIn("viewer");
    let rows = [team()];
    const api = mockApi({ "GET /v1/teams": () => json(rows) });
    renderLive(<Teams />);
    await screen.findByText("alpha.hack.example");
    rows = [team(), team({ id: "t2", slug: "beta", subdomain: "beta.hack.example" })];
    act(() => FakeSource.last!.emit("lease.changed", { ip: "10.0.0.9", from: "pooled", to: "leased" }));
    expect(await screen.findByText("beta.hack.example")).toBeInTheDocument();
    expect(api.count("GET /v1/teams")).toBeGreaterThan(1);
  });
});

describe("Overview", () => {
  test("shows the VIP holder and raises an alert for a stale gateway", async () => {
    await signIn("viewer");
    mockApi({
      "GET /v1/gateways": { gateways: [gw(), gw({ gateway: "gw2", vrrp_state: "BACKUP", last_heartbeat: inFuture(-300) })] },
      "GET /v1/pool": { size: 5, target: 5, free: 20, leased: 2, draining: 0, quarantined: 0 },
      "GET /v1/teams": [team()],
    });
    renderLive(<Overview />);
    expect(await screen.findByText("gw1", { selector: "div" })).toBeInTheDocument();
    expect(await screen.findByText(/gw2/, { selector: "li span" })).toBeInTheDocument();
  });
});

describe("Gateways", () => {
  const cfg = (version: number, hosts: string[]) => ({ version, sha256: "a".repeat(64), status: "live", created_at: inFuture(-10), signature: "s", body: JSON.stringify({ routes: hosts }) });

  test("an admin compares two config versions and sees only the changed lines", async () => {
    await signIn("admin");
    mockApi({
      "GET /v1/gateways": { gateways: [gw()] },
      "GET /v1/config/versions": { pinned: null, versions: [2, 1].map((v) => ({ version: v, sha256: "b".repeat(64), status: "live", created_at: inFuture(-10) })) },
      "GET /v1/config/versions/1": () => json(cfg(1, ["a"])),
      "GET /v1/config/versions/2": () => json(cfg(2, ["a", "b"])),
    });
    renderLive(<Gateways />);
    await userEvent.click(await screen.findByLabelText("compare from v1"));
    await userEvent.click(screen.getByLabelText("compare to v2"));
    const diff = await screen.findByLabelText("config diff");
    expect(within(diff).getByText('+ "b"')).toBeInTheDocument();
    expect(within(diff).getByText('- "a"')).toBeInTheDocument();
    expect(within(diff).getByText("]")).not.toHaveClass("bg-good-bg");
  });

  test("pinning a version asks first, then posts the rollback", async () => {
    await signIn("admin");
    const api = mockApi({
      "GET /v1/gateways": { gateways: [gw()] },
      "GET /v1/config/versions": { pinned: null, versions: [{ version: 1, sha256: "b".repeat(64), status: "live", created_at: inFuture(-10) }] },
      "POST /v1/config/rollback": () => json({ pinned: 1 }, 202),
    });
    renderLive(<Gateways />);
    await userEvent.click(await screen.findByRole("button", { name: "Pin" }));
    expect(api.count("POST /v1/config/rollback")).toBe(0);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(api.calls.find((c) => c.key === "POST /v1/config/rollback")?.body).toEqual({ version: 1 }));
  });

  test("failover and config controls are hidden from operators", async () => {
    await signIn("operator");
    const api = mockApi({ "GET /v1/gateways": { gateways: [gw()] } });
    renderLive(<Gateways />);
    await screen.findByText("gw1");
    expect(screen.queryByRole("button", { name: "Fail over" })).toBeNull();
    expect(screen.queryByText("Config versions")).toBeNull();
    expect(api.count("GET /v1/config/versions")).toBe(0);
  });

  test("the VRRP timeline lists transitions as they arrive", async () => {
    await signIn("viewer");
    mockApi({ "GET /v1/gateways": { gateways: [gw()] } });
    renderLive(<Gateways />);
    await screen.findByText(/No VRRP transitions/);
    act(() => FakeSource.last!.emit("gateway.vrrp", { gateway: "gw2", from: "BACKUP", to: "MASTER" }));
    expect(await screen.findByText("BACKUP → MASTER")).toBeInTheDocument();
  });
});

describe("Settings", () => {
  test("an unsaved edit survives events that do not concern settings", async () => {
    await signIn("admin");
    const values = { pool_target: 5, reserve_free_ips: 2, lease_ttl_seconds: 3600, quarantine_seconds: 300, max_parallel_boots: 3 };
    const api = mockApi({ "GET /v1/settings": () => json({ values: { ...values }, history: [] }) });
    renderLive(<Settings />);
    const input = await screen.findByLabelText(/Warm pool target/);
    await userEvent.clear(input);
    await userEvent.type(input, "9");
    act(() => FakeSource.last!.emit("lease.changed", { ip: "10.0.0.9", from: "pooled", to: "leased" }));
    await new Promise((r) => setTimeout(r, 150));
    expect(api.count("GET /v1/settings")).toBe(1);
    expect(input).toHaveValue(9);
  });

  test("only the fields that changed are sent", async () => {
    await signIn("admin");
    const values = { pool_target: 5, reserve_free_ips: 2, lease_ttl_seconds: 3600, quarantine_seconds: 300, max_parallel_boots: 3 };
    const api = mockApi({
      "GET /v1/settings": { values, history: [] },
      "PATCH /v1/settings": () => json({ values: { ...values, pool_target: 8 }, history: [] }),
    });
    renderLive(<Settings />);
    const input = await screen.findByLabelText(/Warm pool target/);
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, "8");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.calls.find((c) => c.key === "PATCH /v1/settings")?.body).toEqual({ pool_target: 8 }));
  });
});
