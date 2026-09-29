export interface DiffLine {
  kind: "same" | "add" | "del";
  text: string;
}

/** Line diff by longest common subsequence. Configs are a few hundred lines, so O(n*m) is fine. */
export function lineDiff(a: string, b: string): DiffLine[] {
  const x = a.split("\n");
  const y = b.split("\n");
  const w = y.length + 1;
  const lcs = new Uint32Array((x.length + 1) * w);
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) {
      lcs[i * w + j] =
        x[i] === y[j] ? lcs[(i + 1) * w + j + 1]! + 1 : Math.max(lcs[(i + 1) * w + j]!, lcs[i * w + j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) out.push({ kind: "same", text: x[i++]! }), j++;
    else if (lcs[(i + 1) * w + j]! >= lcs[i * w + j + 1]!) out.push({ kind: "del", text: x[i++]! });
    else out.push({ kind: "add", text: y[j++]! });
  }
  while (i < x.length) out.push({ kind: "del", text: x[i++]! });
  while (j < y.length) out.push({ kind: "add", text: y[j++]! });
  return out;
}

/** Pretty-print a config body so a diff is line-per-route; falls back to the raw text. */
export function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}
