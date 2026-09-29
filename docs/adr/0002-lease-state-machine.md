# 0002. Lease state machine and where it is enforced

Status: accepted. The plan refers to "the transitions drawn in the architecture doc"; that
document does not exist yet, so this ADR is the reference until it does.

## Decision

A lease is one pool address and is always in exactly one state. The only legal transitions are:

| From | To | When |
| --- | --- | --- |
| free | pooled | warm-pool manager reserves an address |
| free | leased | cold path: reserve for a team, then boot |
| pooled | leased | claim from the warm pool |
| pooled | draining | discard a warm resource |
| leased | pooled | saga undo of a claim |
| leased | draining | expiry or manual teardown |
| draining | quarantined | VM and port are gone |
| quarantined | free | quarantine window elapsed |

Same-state updates (attaching a port, refreshing a heartbeat) are allowed. Everything else is
rejected by a `BEFORE UPDATE` trigger with SQLSTATE `IPO01`, so no application bug, raw SQL
session or second writer can produce an illegal change. A `CHECK` constraint fixes which
columns must be set in each state, and a partial unique index allows one `leased` row per team.

## Consequences

- The Python domain layer only translates `IPO01` into `IllegalTransition`; it does not
  duplicate the rules.
- `pooled` does not require a port or VM at the moment of reservation. The pool manager
  reserves the address first, then creates the port and VM for that fixed IP and attaches them.
  The reconciler treats a pooled lease with no port after its grace period as a leak.
- Time comparisons (expiry, quarantine) use the database clock, never the caller's.
- `leases` rows cannot be deleted or inserted in any state but `free`.
