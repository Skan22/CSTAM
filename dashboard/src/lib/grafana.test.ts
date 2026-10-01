import { panelUrl, PANELS } from "./grafana";

const base = "https://grafana.example";

test("a panel URL is a d-solo link with the panel id, theme and a live refresh", () => {
  const p = PANELS.find((x) => x.uid === "overview")!;
  const u = new URL(panelUrl(base, p, { theme: "dark" })!);
  expect(u.origin + u.pathname).toBe(`${base}/d-solo/overview/ipo-overview`);
  expect(u.searchParams.get("panelId")).toBe(String(p.id));
  expect(u.searchParams.get("theme")).toBe("dark");
  expect(u.searchParams.get("orgId")).toBe("1");
  expect(u.searchParams.get("refresh")).toBe("5s");
});

test("only the team dashboard gets a team variable, and it is encoded", () => {
  const team = PANELS.find((x) => x.team_var)!;
  const other = PANELS.find((x) => !x.team_var)!;
  expect(new URL(panelUrl(base, team, { theme: "light", team: "a&b=c" })!).searchParams.get("var-team")).toBe("a&b=c");
  expect(new URL(panelUrl(base, team, { theme: "light" })!).searchParams.get("var-team")).toBe("All");
  expect(new URL(panelUrl(base, other, { theme: "light", team: "alpha" })!).searchParams.has("var-team")).toBe(false);
});

test("no URL without a configured Grafana, or with one that is not http(s)", () => {
  const p = PANELS[0]!;
  expect(panelUrl("", p, { theme: "light" })).toBeUndefined();
  expect(panelUrl("javascript:alert(1)", p, { theme: "light" })).toBeUndefined();
});

test("the generated panel list is non-empty and has unique panels", () => {
  expect(PANELS.length).toBeGreaterThan(3);
  expect(new Set(PANELS.map((p) => `${p.uid}/${p.id}`)).size).toBe(PANELS.length);
});
