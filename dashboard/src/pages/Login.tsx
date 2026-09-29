import { useState, type FormEvent } from "react";
import { login } from "../api/session";
import { Button, Card, ErrorNote } from "../components/ui";

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError((await login(email, password)) ?? undefined);
    setBusy(false);
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm">
        <Card title="IPO Control">
          <div className="grid gap-4">
            <label className="grid gap-1">
              <span className="font-semibold">Email</span>
              <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} className="rounded-lg border border-line bg-raised px-3 py-2 text-base" />
            </label>
            <label className="grid gap-1">
              <span className="font-semibold">Password</span>
              <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="rounded-lg border border-line bg-raised px-3 py-2 text-base" />
            </label>
            <ErrorNote>{error}</ErrorNote>
            <Button type="submit" variant="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
          </div>
        </Card>
      </form>
    </main>
  );
}
