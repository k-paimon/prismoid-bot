"use client";

// Settings: exchange, trading pair, and API keys — everything the bot needs
// before it can trade. Bot parameters (strategy, spreads, budget) live on the
// dashboard. "Test connection" queues a real check through the VM agent and
// reports the exchange's own answer inline.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

const EXCHANGES = [
  ["gate_futures", "Gate.com — Futures Testnet"],
  ["gate", "Gate.com — Spot Testnet"],
  ["binance", "Binance — Spot Demo"],
];

const FAMILY = (exchange) => (exchange?.startsWith("gate") ? "gate" : "binance");
const FAMILY_LABEL = { gate: "Gate.com", binance: "Binance" };

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export default function Settings() {
  const [status, setStatus] = useState(null);
  const [market, setMarket] = useState({ exchange: "gate_futures", symbol: "BTCUSDT" });
  const [creds, setCreds] = useState({ api_key: "", api_secret: "" });
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [check, setCheck] = useState(null);   // {status, message}
  const hydrated = useRef(false);
  const checkTimer = useRef(null);

  const load = useCallback(async () => {
    const res = await fetch("/api/bot/status", { cache: "no-store" });
    if (res.status === 401) { window.location.href = "/login"; return null; }
    const data = await res.json();
    setStatus(data);
    if (!hydrated.current && data.settings) {
      hydrated.current = true;
      setMarket({ exchange: data.settings.exchange || "gate_futures",
                  symbol: data.settings.symbol || "BTCUSDT" });
    }
    return data;
  }, []);

  useEffect(() => {
    load();
    return () => clearInterval(checkTimer.current);
  }, [load]);

  async function post(url, opts, label) {
    setBusy(label);
    setNotice(null);
    try {
      const res = await fetch(url, opts);
      const data = await res.json().catch(() => ({}));
      const ok = res.ok && data.ok !== false;
      setNotice(ok ? { ok: true, text: `${label} — saved` }
                   : { ok: false, text: `${label}: ${data.message || data.error || res.status}` });
      await load();
      return ok ? data : null;
    } catch (e) {
      setNotice({ ok: false, text: `${label}: ${e.message}` });
      return null;
    } finally {
      setBusy(null);
    }
  }

  const jsonPost = (url, body, label) => post(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, label);

  const saveMarket = () => jsonPost("/api/bot/settings", market, "Market");

  const saveKeys = async () => {
    const data = await jsonPost("/api/bot/credentials",
      { exchange: market.exchange, ...creds }, "API keys");
    if (data) setCreds({ api_key: "", api_secret: "" });
  };

  const deleteKeys = async (family) => {
    if (!window.confirm(`Remove the saved ${FAMILY_LABEL[family]} API keys?`)) return;
    await post(`/api/bot/credentials?exchange=${family}`, { method: "DELETE" },
               "Remove keys");
  };

  async function testConnection() {
    // save the market first so the check uses what's on screen
    const saved = await jsonPost("/api/bot/settings", market, "Market");
    if (!saved) return;
    setCheck({ status: "running", message: "asking the exchange…" });
    let data = null;
    try {
      const res = await fetch("/api/bot/command", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "check" }),
      });
      data = await res.json();
    } catch { /* handled below */ }
    if (!data?.id) {
      setCheck({ status: "error", message: data?.message || "could not queue the check" });
      return;
    }
    const startedAt = Date.now();
    clearInterval(checkTimer.current);
    checkTimer.current = setInterval(async () => {
      const s = await load();
      const cmd = s?.recent?.find((c) => c.id === data.id);
      if (cmd && (cmd.status === "done" || cmd.status === "error")) {
        clearInterval(checkTimer.current);
        setCheck({ status: cmd.status, message: cmd.result?.message || "" });
      } else if (Date.now() - startedAt > 60_000) {
        clearInterval(checkTimer.current);
        setCheck({ status: "error",
                   message: "no answer after 60 s — is the VM agent online?" });
      }
    }, 2500);
  }

  const agentOnline = status?.state?.heartbeat_at &&
    Date.now() - new Date(status.state.heartbeat_at).getTime() < 15_000;
  const family = FAMILY(market.exchange);
  const savedForFamily = status?.credentials?.find((c) => c.exchange === family);

  return (
    <>
      <header className="topbar">
        <h1>Settings</h1>
        <Link className="navlink" href="/dashboard" style={{ marginLeft: "auto" }}>
          ← Back to dashboard
        </Link>
      </header>

      <main className="narrow">
        <section className="card">
          <h2>Market</h2>
          <label className="field">
            Exchange
            <select value={market.exchange}
                    onChange={(e) => setMarket((m) => ({ ...m, exchange: e.target.value }))}>
              {EXCHANGES.map(([v, label]) => (
                <option key={v} value={v}>{label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Trading pair
            <input type="text" value={market.symbol}
                   placeholder="BTCUSDT"
                   onChange={(e) => setMarket((m) => ({ ...m, symbol: e.target.value.toUpperCase() }))} />
          </label>
          <div className="hint">
            Futures pairs are always coin + USDT: BTCUSDT, ETHUSDT, SOLUSDT, …
            Use <b>Test connection</b> below to confirm the pair exists before
            starting the bot.
          </div>
          <button onClick={saveMarket} disabled={!!busy}>Save market</button>
          <button className="primary" onClick={testConnection}
                  disabled={!!busy || !agentOnline || !savedForFamily}>
            Test connection
          </button>
          {!agentOnline && (
            <div className="hint">Test connection needs the VM agent online.</div>
          )}
          {check && (
            <div className={check.status === "error" ? "error" : "hint"}>
              {check.status === "running" ? "⏳ " : check.status === "done" ? "✓ " : ""}
              {check.message}
            </div>
          )}
        </section>

        <section className="card">
          <h2>API keys</h2>
          {(status?.credentials || []).length > 0 && (
            <div style={{ marginBottom: 10 }}>
              {status.credentials.map((c) => (
                <div className="keyrow" key={c.exchange}>
                  <span className="exname">{FAMILY_LABEL[c.exchange] || c.exchange}</span>
                  <span className="chip ok">{c.api_key_masked}</span>
                  <span className="when">saved {timeAgo(c.updated_at)}</span>
                  <button onClick={() => deleteKeys(c.exchange)} disabled={!!busy}>
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="hint">
            Keys are saved for <b>{FAMILY_LABEL[family]}</b> (the exchange
            selected above — Gate spot and futures share one key pair). Create
            testnet keys at{" "}
            {family === "gate" ? "testnet.gate.com → API Keys" :
             "binance.com → Demo Trading → API Management"}.
          </div>
          <label className="field">
            API key
            <input type="text" value={creds.api_key}
                   placeholder={savedForFamily ? "paste a new key to replace the saved one" : "paste your API key"}
                   onChange={(e) => setCreds((c) => ({ ...c, api_key: e.target.value }))} />
          </label>
          <label className="field">
            API secret
            <input type="password" value={creds.api_secret} placeholder="API secret"
                   onChange={(e) => setCreds((c) => ({ ...c, api_secret: e.target.value }))} />
          </label>
          <button onClick={saveKeys}
                  disabled={!!busy || !creds.api_key || !creds.api_secret}>
            Save API keys
          </button>
          <div className="hint">
            Stored server-side, read only by your VM — never sent back to the
            browser. After saving, run Test connection to confirm they work.
          </div>
        </section>

        {notice && (
          <div className={notice.ok ? "hint" : "error"}>{notice.text}</div>
        )}
      </main>
    </>
  );
}
