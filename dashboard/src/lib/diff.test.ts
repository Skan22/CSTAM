import { lineDiff, prettyJson } from "./diff";

test("identical texts have only unchanged lines", () => {
  expect(lineDiff("a\nb", "a\nb").every((l) => l.kind === "same")).toBe(true);
});

test("added and removed lines are marked in order", () => {
  expect(lineDiff("a\nb\nc", "a\nx\nc\nd")).toEqual([
    { kind: "same", text: "a" },
    { kind: "del", text: "b" },
    { kind: "add", text: "x" },
    { kind: "same", text: "c" },
    { kind: "add", text: "d" },
  ]);
});

test("a route added to a config adds its lines and does not rewrite the existing route", () => {
  const before = prettyJson('{"http":{"routers":{"a":{"rule":"h1"}}}}');
  const after = prettyJson('{"http":{"routers":{"a":{"rule":"h1"},"b":{"rule":"h2"}}}}');
  const d = lineDiff(before, after);
  expect(d.some((l) => l.kind === "add" && l.text.includes('"b"'))).toBe(true);
  expect(d.some((l) => l.kind === "del" && l.text.includes('"rule"'))).toBe(false);
});

test("prettyJson leaves non-JSON alone", () => {
  expect(prettyJson("nope")).toBe("nope");
});
