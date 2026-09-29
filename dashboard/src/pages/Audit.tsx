import { useCallback, useEffect, useState } from "react";
import { api } from "../api/session";
import { must, type AuditEntry } from "../api/types";
import { Badge, Button, Card, Empty, ErrorNote, Loading } from "../components/ui";

const PAGE = 100;

export function Audit() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [chain, setChain] = useState<{ valid: boolean; first_bad_id: number | null }>();
  const [more, setMore] = useState(true);
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (beforeId?: number) => {
    setLoading(true);
    try {
      const page = await must(api.GET("/v1/audit", { params: { query: { limit: PAGE, before_id: beforeId } } }));
      setChain(page.chain);
      setEntries((prev) => (beforeId === undefined ? page.entries : [...prev, ...page.entries]));
      setMore(page.entries.length === PAGE);
      setError(undefined);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => void load(), [load]);

  const exportJson = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(entries, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "audit-log.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card
      title="Audit log"
      actions={<>
        {chain && <Badge tone={chain.valid ? "good" : "bad"}>{chain.valid ? "hash chain intact" : `chain broken at #${chain.first_bad_id}`}</Badge>}
        <Button onClick={exportJson} disabled={!entries.length}>Export JSON</Button>
      </>}
    >
      {error && <ErrorNote>{error}</ErrorNote>}
      {loading && !entries.length ? <Loading what="audit log" /> : !entries.length ? <Empty>The log is empty.</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-base">
            <thead className="text-sm text-muted"><tr><th className="py-2 pr-4">#</th><th className="pr-4">When</th><th className="pr-4">Actor</th><th className="pr-4">Action</th><th className="pr-4">Target</th><th>Detail</th></tr></thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className="border-t border-line align-top">
                  <td className="py-2 pr-4 tabular-nums text-muted">{e.id}</td>
                  <td className="whitespace-nowrap pr-4">{new Date(e.at).toLocaleString()}</td>
                  <td className="pr-4">{e.actor}</td>
                  <td className="pr-4 font-semibold">{e.action}</td>
                  <td className="pr-4 font-mono text-sm">{e.target ?? "-"}</td>
                  <td className="font-mono text-sm text-muted">{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {more && entries.length > 0 && (
        <div className="mt-4 text-center"><Button disabled={loading} onClick={() => void load(entries[entries.length - 1]!.id)}>{loading ? "Loading…" : "Load older"}</Button></div>
      )}
    </Card>
  );
}
