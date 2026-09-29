export type Role = "viewer" | "operator" | "admin";

const ORDER: Role[] = ["viewer", "operator", "admin"];

/** Whether `role` is at least `minimum`. The API enforces this too; this only hides buttons. */
export function can(role: Role | null | undefined, minimum: Role): boolean {
  return !!role && ORDER.indexOf(role) >= ORDER.indexOf(minimum);
}

export function asRole(value: string): Role {
  return (ORDER as string[]).includes(value) ? (value as Role) : "viewer";
}
