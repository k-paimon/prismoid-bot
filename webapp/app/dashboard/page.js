"use client";

// Cloud dashboard. Polls /api/bot/status (which reads Supabase) every few
// seconds and queues actions as bot_commands rows for the VM agent. No raw
// console here — outcomes surface in the Activity feed; full logs stay in
// the bot_logs table for debugging.

import { useCallback, useEffect, useRef, useState } from "react";

const POLL_MS = 3000;
const HEARTBEAT_STALE_MS = 15_000;

const EXCHANGES = [
  ["gate_futures", "Gate.com — Futures Testnet"],
  ["gate", "Gate.com — Spot Testnet"],
  ["binance", "Binance — Spot Demo"],
];

const STRATEGIES = [
  ["grid", "Grid", "buy low / sell high across a price ladder"],
  ["pmm", "Market making", "quotes both sides around the mid price"],
  ["supertrend", "Supertrend", "follows the trend, exits on reversal"],
];

// field key -> [label, placeholder], grouped by the strategy that uses them
const PARAM_GROUPS = [
  ["grid", "Grid settings", [
    ["grid_start", "Lower price (or -3%)", "-3%"],
    ["grid_end", "Upper price (or 3%)", "3%"],
    ["grid_levels", "Number of levels", "10"],
    ["grid_max_open", "Max open orders", ""],
  ]],
  ["pmm", "Market making settings", [
    ["pmm_spreads", "Spreads per side", "0.05%, 0.15%"],
    ["pmm_refresh", "Refresh every (s)", "15"],
    ["pmm_skew", "Inventory skew (0–1)", "0.5"],
    ["pmm_max_inventory", "Max inventory (base)", ""],
  ]],
  ["supertrend", "Supertrend settings", [
    ["st_length", "ATR length", "10"],
    ["st_multiplier", "ATR multiplier", "3"],
    ["st_threshold", "Entry threshold", "0.1%"],
  ]],
];

const SHARED_FIELDS = [
  ["total_quote", "Budget (quote currency)", "1000"],
  ["max_loss", "Stop after losing", "50"],
  ["leverage", "Leverage (futures only)", "3"],
  ["days", "Backtest window (days)", "7"],
];

const ALL_FIELDS = [...PARAM_GROUPS.flatMap(([, , f]) => f), ...SHARED_FIELDS];

const ACTION_LABELS = {
  start: "Start", stop: "Stop", check: "Connection check", backtest: "Backtest",
};

function fmt(n, digits = 2) {
  return typeof n === "number" && isFinite(n)
    ? n.toLocaleString("en-US", { minimumFractionDigits: digits,
                                  maximumFractionDigits: digits })
    : "—";
}

function signed(n, digits = 2) {
  return `${n >= 0 ? "+" : ""}${fmt(n, digits)}`;
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [form, setForm] = useState({
    exchange: "gate_futures", symbol: "BTCUSDT",
    strategy: "pmm", params: {},
  });
  const [creds, setCreds] = useState({ api_key: "", api_secret: "" });
  const hydrated = useRef(false);

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/api/bot/status", { cache: "no-store" });
      if (res.status === 401) { window.location.href = "/login"; return; }
      const data = await res.json();
      setStatus(data);
      if (!hydrated.current && data.settings) {
        hydrated.current = true;
        const p = data.settings.params || {};
        setForm({
          exchange: data.settings.exchange || "gate_futures",
          symbol: data.settings.symbol || "BTCUSDT",
          strategy: (Array.isArray(p.strategies) && p.strategies[0]) || "pmm",
          params: Object.fromEntries(
            ALL_FIELDS.map(([k]) => [k, p[k] != null ? String(p[k]) : ""])),
        });
      }
    } catch {
      /* transient poll failure — the next tick retries */
    }
  }, []);

  useEffect(() => {
    poll();
    const t = setInterval(poll, POLL_MS);
    return () => clearInterval(t);
  }, [poll]);

  async function post(url, body, label) {
    setBusy(label);
    setNotice(null);
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      const ok = res.ok && data.ok !== false;
      setNotice(ok
        ? { ok: true, text: `${label} — sent` }
        : { ok: false, text: `${label} failed: ${data.message || data.error || res.status}` });
      poll();
      return ok;
    } catch (e) {
      setNotice({ ok: false, text: `${label} failed: ${e.message}` });
      return false;
    } finally {
      setBusy(null);
    }
  }

  function formParams(extra = {}) {
    const params = { symbol: form.symbol, exchange: form.exchange,
                     strategies: [form.strategy] };
    for (const [k] of ALL_FIELDS) {
      const v = (form.params[k] || "").trim();
      if (v) params[k] = v;
    }
    return { ...params, ...extra };
  }

  const saveSettings = () => post("/api/bot/settings", {
    exchange: form.exchange, symbol: form.symbol,
    params: { strategies: [form.strategy],
              ...Object.fromEntries(Object.entries(form.params)
                .filter(([, v]) => (v || "").trim() !== "")) },
  }, "Save settings");

  const sendCommand = (action, extra) => {
    if (action === "start" && extra?.trade &&
        !window.confirm(`Start LIVE trading ${form.symbol} on ${form.exchange}?\n` +
                        "The bot will place real orders on the exchange.")) {
      return;
    }
    return post("/api/bot/command",
                { action, params: action === "stop" ? {} : formParams(extra) },
                ACTION_LABELS[action]);
  };

  const saveCreds = async () => {
    const ok = await post("/api/bot/credentials",
      { exchange: form.exchange, ...creds }, "Save API keys");
    if (ok) setCreds({ api_key: "", api_secret: "" });
  };

  // ------------------------------------------------------------------ derived

  const st = status?.state;
  const summary = st?.exchange_summary;
  const stats = st?.stats;
  const backtest = st?.backtest;
  const configured = status?.configured;
  const agentOnline = st?.heartbeat_at &&
    Date.now() - new Date(st.heartbeat_at).getTime() < HEARTBEAT_STALE_MS;
  const running = agentOnline && st?.running;
  const inFlight = (status?.queue || []).length > 0;
  const position = summary?.position;
  const credFamily = form.exchange.startsWith("gate") ? "gate" : "binance";
  const savedCred = status?.credentials?.find((c) => c.exchange === credFamily);
  const futures = form.exchange.includes("futures");
  const actionsReady = configured && agentOnline && !busy && !inFlight;

  const setupSteps = [
    ["Supabase connected", !!configured],
    ["VM agent online", !!agentOnline],
    ["API keys saved", !!savedCred],
    ["Bot running", !!running],
  ];
  const setupDone = configured && agentOnline && savedCred;

  const botLabel = !status ? "…"
    : !configured ? "not configured"
    : !agentOnline ? "agent offline"
    : running ? (st.mode || "running") : "stopped";

  const tiles = [
    ["Portfolio value",
     summary?.total_usd != null ? `$${fmt(summary.total_usd)}` : "—",
     summary?.daily_pnl != null
       ? <span className={summary.daily_pnl >= 0 ? "pos" : "neg"}>
           {signed(summary.daily_pnl)} today
         </span>
       : "your exchange wallet, valued in USD"],
    ["Session profit",
     stats
       ? <span className={stats.total >= 0 ? "pos" : "neg"}>
           {signed(stats.total)} {stats.quote}
         </span>
       : "—",
     stats ? `${stats.fills} fills · fees ${fmt(stats.fees)}`
           : "profit since the bot was started"],
    ["Open position",
     position
       ? `${Number(position.size_base) > 0 ? "Long" : "Short"} ${Math.abs(Number(position.size_base))}`
       : "None",
     position ? `entry ${position.entry_price ?? "?"}` : "futures position, if any"],
    ["Bot",
     <><span className={`dot ${running ? "on" : agentOnline ? "" : "err"}`} />{botLabel}</>,
     agentOnline ? `${stats?.open_orders ?? 0} open orders on ${form.symbol}`
                 : "waiting for the VM heartbeat"],
  ];

  const setParam = (k, v) =>
    setForm((f) => ({ ...f, params: { ...f.params, [k]: v } }));

  const activeGroup = PARAM_GROUPS.find(([key]) => key === form.strategy);
  const activeBlurb = STRATEGIES.find(([key]) => key === form.strategy)?.[2];

  const chipFor = (c) =>
    c.status === "done" && c.result?.ok !== false ? ["ok", "done"]
    : c.status === "error" || c.result?.ok === false ? ["err", "failed"]
    : c.status === "running" ? ["wait", "running"]
    : ["wait", "queued"];

  return (
    <>
      {status && !configured && (
        <div className="devbanner">
          <b>Not connected:</b> set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
          in the Vercel project env, and run bot/supabase_schema.sql once.
        </div>
      )}
      <header className="topbar">
        <h1>Grid Strike Bot</h1>
        <span className={`badge ${running ? "on" : ""}`}>
          <span className={`dot ${running ? "on" : agentOnline ? "" : "err"}`} />
          {botLabel}
        </span>
        {inFlight && (
          <span className="badge dev">
            working: {status.queue.map((q) => ACTION_LABELS[q.action] || q.action).join(", ")}
          </span>
        )}
        <form action="/api/auth/logout" method="post" style={{ marginLeft: "auto" }}>
          <button type="submit" style={{ margin: 0, padding: "4px 12px" }}>
            Sign out
          </button>
        </form>
      </header>

      {status && !setupDone && (
        <div className="checklist">
          {setupSteps.map(([label, done], i) => (
            <span key={label} className={`step ${done ? "done" : ""}`}>
              {done ? "✓" : `${i + 1}.`} {label}
            </span>
          ))}
        </div>
      )}

      <div className="statsbar">
        {tiles.map(([label, value, sub]) => (
          <div className="tile" key={label}>
            <div className="tlabel">{label}</div>
            <div className="tvalue" style={{ color: "var(--ink)" }}>{value}</div>
            <div className="tsub">{sub}</div>
          </div>
        ))}
      </div>

      <main className="grid">
        <div>
          <section className="card">
            <h2>Exchange &amp; API keys</h2>
            <label className="field">
              Exchange
              <select value={form.exchange}
                      onChange={(e) => setForm((f) => ({ ...f, exchange: e.target.value }))}>
                {EXCHANGES.map(([v, label]) => (
                  <option key={v} value={v}>{label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Trading pair
              <input type="text" value={form.symbol}
                     onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value.toUpperCase() }))} />
            </label>
            <label className="field">
              API key {savedCred && <span className="chip ok">saved {savedCred.api_key_masked}</span>}
              <input type="text" value={creds.api_key}
                     placeholder={savedCred ? "paste a new key to replace it" : "paste your API key"}
                     onChange={(e) => setCreds((c) => ({ ...c, api_key: e.target.value }))} />
            </label>
            <label className="field">
              API secret
              <input type="password" value={creds.api_secret} placeholder="API secret"
                     onChange={(e) => setCreds((c) => ({ ...c, api_secret: e.target.value }))} />
            </label>
            <button onClick={saveCreds}
                    disabled={!!busy || !creds.api_key || !creds.api_secret}>
              Save API keys
            </button>
            <div className="hint">
              Keys are stored server-side and only ever read by your VM —
              they never come back to the browser.
            </div>
          </section>

          <section className="card">
            <h2>Strategy</h2>
            <div className="seg">
              {STRATEGIES.map(([key, label]) => (
                <button key={key} type="button"
                        className={form.strategy === key ? "active" : ""}
                        onClick={() => setForm((f) => ({ ...f, strategy: key }))}>
                  {label}
                </button>
              ))}
            </div>
            <div className="hint">{activeBlurb}</div>

            {activeGroup && (
              <div className="row2">
                {activeGroup[2].map(([k, label, placeholder]) => (
                  <label className="field" key={k}>
                    {label}
                    <input type="text" value={form.params[k] || ""}
                           placeholder={placeholder}
                           onChange={(e) => setParam(k, e.target.value)} />
                  </label>
                ))}
              </div>
            )}

            <div className="subhead">Risk &amp; budget</div>
            <div className="row2">
              {SHARED_FIELDS
                .filter(([k]) => k !== "leverage" || futures)
                .map(([k, label, placeholder]) => (
                  <label className="field" key={k}>
                    {label}
                    <input type="text" value={form.params[k] || ""}
                           placeholder={placeholder}
                           onChange={(e) => setParam(k, e.target.value)} />
                  </label>
                ))}
            </div>

            <div className="actions">
              <button className="primary"
                      disabled={!actionsReady || running || !savedCred}
                      onClick={() => sendCommand("start", { trade: true })}>
                Start trading
              </button>
              <button disabled={!actionsReady || running}
                      onClick={() => sendCommand("start")}>
                Practice run
              </button>
              <button className="danger" disabled={!agentOnline || !running || !!busy}
                      onClick={() => sendCommand("stop")}>
                Stop
              </button>
            </div>
            <div className="actions secondary">
              <button disabled={!!busy || !configured} onClick={saveSettings}>
                Save settings
              </button>
              <button disabled={!actionsReady || running || !savedCred}
                      onClick={() => sendCommand("check")}>
                Test connection
              </button>
              <button disabled={!actionsReady || running}
                      onClick={() => sendCommand("backtest")}>
                Backtest
              </button>
            </div>
            {notice && (
              <div className={notice.ok ? "hint" : "error"}>{notice.text}</div>
            )}
            <div className="hint">
              Practice run and Backtest are safe — no orders are placed.
              Stopping cancels the bot&apos;s open orders first.
            </div>
          </section>
        </div>

        <div>
          <section className="card">
            <h2>Activity</h2>
            {!(status?.recent || []).length ? (
              <div className="hint">
                Nothing yet — actions you take will show up here with their result.
              </div>
            ) : (
              <ul className="activity">
                {status.recent.map((c) => {
                  const [variant, word] = chipFor(c);
                  return (
                    <li key={c.id}>
                      <span className="what">{ACTION_LABELS[c.action] || c.action}</span>
                      <span className={`chip ${variant}`}>{word}</span>
                      <span className="msg">{c.result?.message || ""}</span>
                      <span className="when">{timeAgo(c.created_at)}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="card">
            <h2>On the exchange</h2>
            {!summary ? (
              <div className="hint">
                {savedCred
                  ? "Waiting for the first snapshot from your VM (updates every ~30 s)."
                  : "Save your API keys to see balances, orders and positions here."}
              </div>
            ) : summary.error ? (
              <div className="error">{summary.error}</div>
            ) : (
              <>
                <table className="data">
                  <thead>
                    <tr><th>Asset</th><th className="num">Balance</th></tr>
                  </thead>
                  <tbody>
                    {(summary.balances || []).map((b) => (
                      <tr key={b.asset}>
                        <td>{b.asset}</td>
                        <td className="num">{fmt(Number(b.free) + Number(b.locked), 4)}</td>
                      </tr>
                    ))}
                    <tr>
                      <td style={{ color: "var(--muted)" }}>Open orders</td>
                      <td className="num">{(summary.open_orders || []).length}</td>
                    </tr>
                    <tr>
                      <td style={{ color: "var(--muted)" }}>Recent trades</td>
                      <td className="num">{(summary.trades || []).length}</td>
                    </tr>
                  </tbody>
                </table>
                <div className="hint">updated {timeAgo(summary.as_of)}</div>
              </>
            )}
          </section>

          {backtest && (
            <section className="card">
              <h2>Last backtest — {backtest.symbol}, {backtest.days} days</h2>
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th className="num">Trades</th>
                    <th className="num">Fees</th>
                    <th className="num">Profit</th>
                    <th className="num">Return</th>
                  </tr>
                </thead>
                <tbody>
                  {(backtest.strategies || []).map((s) => (
                    <tr key={s.name}>
                      <td>{s.name}</td>
                      <td className="num">{s.buys + s.sells}</td>
                      <td className="num">{fmt(s.fees)}</td>
                      <td className={`num ${s.total >= 0 ? "pos" : "neg"}`}>
                        {signed(s.total)}
                      </td>
                      <td className={`num ${s.pct >= 0 ? "pos" : "neg"}`}>
                        {signed(s.pct, 1)}%
                      </td>
                    </tr>
                  ))}
                  <tr>
                    <td style={{ color: "var(--muted)" }}>Buy &amp; hold</td>
                    <td className="num">—</td>
                    <td className="num">—</td>
                    <td className={`num ${backtest.buyhold >= 0 ? "pos" : "neg"}`}>
                      {signed(backtest.buyhold)}
                    </td>
                    <td className={`num ${backtest.span_pct >= 0 ? "pos" : "neg"}`}>
                      {signed(backtest.span_pct, 1)}%
                    </td>
                  </tr>
                </tbody>
              </table>
              <div className="hint">
                Simulated on {backtest.candles} candles with a {fmt(backtest.fee_pct, 3)}%
                fee — directional, not exact.
              </div>
            </section>
          )}
        </div>
      </main>
    </>
  );
}
