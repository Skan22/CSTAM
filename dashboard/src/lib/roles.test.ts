import { asRole, can } from "./roles";

test("a role can do what its own level and below can do", () => {
  expect(can("admin", "operator")).toBe(true);
  expect(can("operator", "operator")).toBe(true);
  expect(can("viewer", "operator")).toBe(false);
  expect(can(null, "viewer")).toBe(false);
});

test("an unknown role is treated as the least powerful", () => {
  expect(asRole("root")).toBe("viewer");
  expect(asRole("admin")).toBe("admin");
});

test("a team account is on no rung of the staff ladder", () => {
  expect(asRole("team")).toBe("team");
  expect(can("team", "viewer")).toBe(false);
});
