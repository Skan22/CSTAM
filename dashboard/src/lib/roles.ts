export type Role = "viewer" | "operator" | "admin" | "team";
export type StaffRole = Exclude<Role, "team">;

const ORDER: StaffRole[] = ["viewer", "operator", "admin"];

/** Whether `role` is at least `minimum`. A team account is not on the ladder and never is.
 * The API enforces this too; this only hides buttons. */
export function can(role: Role | null | undefined, minimum: StaffRole): boolean {
  return !!role && role !== "team" && ORDER.indexOf(role) >= ORDER.indexOf(minimum);
}

export function asRole(value: string): Role {
  return value === "team" || (ORDER as string[]).includes(value) ? (value as Role) : "viewer";
}
