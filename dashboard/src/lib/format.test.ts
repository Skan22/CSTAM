import { ago, bytes, count, countdown, pct } from "./format";

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

test("bytes are humanized in powers of 1024", () => {
  expect([0, 1023, 1024, 1536, 5 * 1024 ** 2, 3 * 1024 ** 3].map(bytes)).toEqual(["0 B", "1023 B", "1.0 KB", "1.5 KB", "5.0 MB", "3.0 GB"]);
});

test("a rate is a percentage that avoids dividing by zero", () => {
  expect(pct(1, 200)).toBe("0.5%");
  expect(pct(0, 0)).toBe("0%");
  expect(pct(200, 200)).toBe("100%");
});

test("a request count is grouped for reading", () => {
  expect(count(1234567)).toBe("1,234,567");
});
