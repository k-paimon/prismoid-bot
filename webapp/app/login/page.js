"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (r.ok) {
        router.push("/dashboard");
        return;
      }
      const d = await r.json().catch(() => ({}));
      setError(d.message || "login failed");
    } catch {
      setError("network error");
    }
    setBusy(false);
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <h1>
          Grid Strike Bot
          <span className="devtag">in development</span>
        </h1>
        <div className="hint" style={{ marginTop: 0 }}>
          Cloud dashboard — sign in to continue
        </div>
        <label className="field">
          Username
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field">
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "signing in…" : "Sign in"}
        </button>
        <div className="hint">
          Auth is environment-variable based for now — Supabase accounts come
          later.
        </div>
      </form>
    </div>
  );
}
