import { ago, countdown } from "./format";

const now = Date.parse("2026-01-01T12:00:00Z");

test("countdown shows minutes and seconds, then hours, then expired", () => {
  expect(countdown("2026-01-01T12:04:05Z", now)).toBe("4:05");
  expect(countdown("2026-01-01T14:30:00Z", now)).toBe("2h 30m");
  expect(countdown("2026-01-01T11:59:00Z", now)).toBe("expired");
  expect(countdown(null, now)).toBe("-");
});

test("ago is coarse and never negative", () => {
  expect(ago("2026-01-01T11:59:50Z", now)).toBe("10s ago");
  expect(ago("2026-01-01T11:00:00Z", now)).toBe("1h ago");
  expect(ago("2026-01-01T12:00:10Z", now)).toBe("0s ago");
  expect(ago(null, now)).toBe("never");
});
