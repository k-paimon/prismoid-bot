# bare-features trading bot — grid_strike + pmm_simple + supertrend_v1 on Binance Spot Demo Mode

A standalone trading bot that runs live versions of the three proof-of-concept
strategies against **Binance Spot Demo Mode** (`https://demo-api.binance.com`),
with first-class **request accounting**: every REST call is counted, weighted, and
reconciled against the rate-limit headers Binance returns. Because this is a
market-making bot, staying inside the exchange's request budget is as much a part
of the strategy as the quotes themselves.

Everything is pure Python standard library — no hummingbot install needed — so it
runs on the host with `py` (same as `poc/exchange_test_trade.py`).

```
bare-features/
├── docs/trading-bot.md        # this document
└── bot/
    ├── binance_client.py      # signed REST client + RequestMeter (rate-limit accounting)
    ├── strategies.py          # GridStrike, PMMSimple, Supertrend live implementations
    ├── bot.py                 # entry point: connection check + trading loop + reports
    ├── gui.py                 # simple direct tkinter form UI for Grid Strike
    ├── launcher.py            # XAMPP-style control panel + packaged-app entry point
    ├── api_server.py          # bot backend API service (port 8801)
    ├── web_server.py          # web UI server (port 8800)
    ├── web/index.html         # browser dashboard (credentials, params, console)
    ├── build_windows.ps1      # -> dist/GridBotLauncher.exe
    └── build_macos.sh         # -> dist/GridBotLauncher.dmg (run on a Mac)
```

## 1. Goal

1. Prove the connection to Binance's demo API: authentication,
   latency, and exchange-side order validation.
2. Run the three strategies from `strategies/controllers/` as a live bot,
   simplified but parameter-compatible with the hummingbot controllers.
3. Measure **how many requests the bot actually sends** per strategy tick and
   per minute, and how much of Binance's rate-limit budget that consumes —
   before any of this ever touches a real account.

## 2. Exchange environment

| | Binance Spot Demo Mode |
| --- | --- |
| Base URL | `https://demo-api.binance.com` |
| Keys | your regular Binance account → switch to Demo Trading → create a key in API Management |
| Web UI to watch orders | **<https://demo.binance.com>** — the full spot trading UI; the bot's resting orders and fills appear there live |
| Market data | mirrors the live exchange |
| Funds | simulated, resettable from the demo UI |

`--mode live` is allowed **only** for the read-only connection check — the
bot refuses to trade against `api.binance.com`.

Keys are read from `BINANCE_API_KEY` / `BINANCE_API_SECRET` env vars, or from the
encrypted credential store (`poc/store_binance_keys.py` writes it,
`--credentials-account <name>` + `CONFIG_PASSWORD` reads it).

**PnL tracking**: every fill (actual executed qty × price from the exchange
response) feeds an average-cost tracker — realized PnL from completed round
trips, unrealized from open inventory marked to the mid — shown on each tick
line (`PnL +0.14 (R+0.10 U+0.04)`) and in the end-of-session summary. The
dashboard's "On the exchange" panel additionally queries Binance directly
(balances, open orders with Binance order ids, executed trades with trade
ids) as independent proof of what the bot did.

Signed requests use **server time**, not the local clock: the client syncs an
offset via `GET /api/v3/time` before the first signed call and resyncs+retries
once on a `-1021` rejection, so a drifting Windows clock can't break auth.
Client order ids stick to Binance's `[a-zA-Z0-9-_]` charset (`GS-L1_7`).

## 3. Binance rate limits (why we count requests)

Binance enforces three independent limits (returned live by `GET /api/v3/exchangeInfo`;
the bot prints the actual values at startup):

| Limit | Typical value | Scope | Feedback header |
| --- | --- | --- | --- |
| `REQUEST_WEIGHT` | 6 000 weight / min | per IP | `X-MBX-USED-WEIGHT-1M` on every response |
| `ORDERS` | 50 / 10 s and 160 000 / day | per account | `X-MBX-ORDER-COUNT-10S`, `X-MBX-ORDER-COUNT-1D` on order endpoints |
| `RAW_REQUESTS` | 61 000 / 5 min | per IP | — |

Every endpoint has a *weight* (e.g. `bookTicker` = 2, `openOrders` with symbol = 6,
`account` = 20, placing/cancelling an order = 1). Exceeding the budget returns
HTTP **429** (back off for `Retry-After` seconds); ignoring 429s escalates to
HTTP **418**, an IP ban of minutes to days. A market-making bot that refreshes
quotes aggressively can burn the `ORDERS` bucket fast — that is the failure mode
this project measures for.

### The bot's request-side design

- `RequestMeter` (in `binance_client.py`) records, per endpoint: call count,
  errors, cumulative *local* weight estimate, and average/max latency.
- Every response's `X-MBX-USED-WEIGHT-1M` is captured, so the local estimate is
  continuously reconciled against the exchange's own accounting.
- On **429** the client sleeps `Retry-After` and the event is counted; on **418**
  the bot aborts immediately.
- Order placements per tick are capped (`MAX_ORDER_ACTIONS_PER_TICK = 8`) so a
  quote-refresh storm can never hit the 50-orders/10s bucket.
- On exit (Ctrl+C or `--duration`) the bot prints the full request report:
  per-endpoint table, total requests, requests/min, peak used-weight-1m, and
  peak order counts.

### Expected steady-state budget (10 s tick, all three strategies)

| Call | Frequency | Weight/min |
| --- | --- | --- |
| `bookTicker` (mid price) | every tick (6/min) | 12 |
| `openOrders` (reconcile) | every tick (6/min) | 36 |
| `klines` (supertrend candles) | every 60 s | 2 |
| `account` (balances) | every 60 s | 20 |
| order place/cancel | bursty, ≈ 2–10/min | 2–10 |
| **Total** | | **≈ 75–80 / 6 000 ≈ 1.3 %** |

So the design leaves ~98 % headroom; the report proves it empirically.

## 4. Strategies

All three mirror the parameters of the controllers in
`strategies/controllers/`, simplified for spot + standalone execution.
Common: one symbol (default `BTCUSDT`), sizes in quote currency, prices/qtys
rounded to the exchange's `PRICE_FILTER` / `LOT_SIZE` / `NOTIONAL` filters.
Each order carries a `newClientOrderId` prefix (`GS-`, `PMM-`, `ST-`) so the bot
can reconcile its own orders from `openOrders` after a restart and never touches
orders it did not place.

### grid_strike (from `controllers/generic/grid_strike.py`)

- Grid of `n_levels` (default 8) BUY limit levels between `start_price` and
  `end_price`. Prices accept an absolute value (`59000`) or a percent offset
  from the mid captured at startup (`-3%`, `+3%`); defaults are −3 %/+3 %.
  On the command line use the `=` form for negative offsets:
  `--grid-start=-3%`.
- At most `max_open_orders` (default 3) resting orders at the levels nearest
  below the mid, `total_amount_quote` split evenly across levels,
  `LIMIT_MAKER` so the order can never take.
- When a grid buy fills, a take-profit SELL is placed one grid step above the
  fill (the controller's per-level take-profit barrier).
- Trading stops (orders pulled) if price leaves `[limit_price, end_price]`.

### pmm_simple (from `controllers/market_making/pmm_simple.py`)

- Symmetric maker quotes: `buy_spreads` / `sell_spreads` (default 0.1 % and
  0.3 % — two levels per side) around the mid price, `LIMIT_MAKER`.
- Quotes refresh when the mid drifts more than 20 % of the level's spread or
  after `executor_refresh_time` (default 60 s) — this cancel/replace churn is
  the main consumer of the `ORDERS` rate-limit bucket, which is exactly what
  the request report is for.
- `total_amount_quote` split evenly across the four levels.

### supertrend_v1 (from `controllers/directional_trading/supertrend_v1.py`)

- Supertrend (Wilder ATR, `length` 20, `multiplier` 4.0) computed from live
  `3m` klines with the same pure-python implementation validated in the PoC.
- Long signal: direction = 1 **and** |close − trend| / close <
  `percentage_threshold` (1 %) → market BUY of `order_amount_quote`.
- Direction flip to −1 (or short signal) → market SELL of the held position.
  Spot has no shorting, so "short" means exit-to-quote, noted per the
  controller's perpetual origins.

## 5. Order lifecycle & safety

- The runner diffs *desired* orders (from strategies) against *open* orders
  (by client-order-id tag) each tick: keep if the price still matches, else
  cancel and re-place. Disappeared orders are queried once to classify
  FILLED vs CANCELED and routed back to the owning strategy.
- On shutdown the bot cancels every order carrying its prefix
  (skip with `--keep-orders`) and prints the strategy + request reports.
- No keys → **dry-run**: public endpoints are hit and metered normally, and
  every signed action is printed instead of sent, so connection + request
  measurement works before keys exist.

## 6. Running it

```powershell
# 1. Connection check (works keyless; with keys also validates auth + a test order)
py bare-features\bot\bot.py --check

# 2. Get demo keys (binance.com > Demo Trading > API Management) then:
$env:BINANCE_API_KEY='...'; $env:BINANCE_API_SECRET='...'

# 3. Dry-run the loop for 2 minutes (no keys needed — prints intended actions):
py bare-features\bot\bot.py --duration 120

# 4. Trade on the demo account (all three strategies, 10s tick, until Ctrl+C):
py bare-features\bot\bot.py --trade

# useful flags
#   --strategies grid,pmm,supertrend   subset to run
#   --symbol ETHUSDT                   different pair
#   --interval 5                       tick seconds
#   --duration 300                     stop after N seconds and print reports
#   --total-quote 200                  quote budget per maker strategy
#   --keep-orders                      don't cancel open orders on exit
```

The `--check` mode answers "is the connection good and what does one round of
the API cost": ping RTT, server-time drift, rate limits as advertised by the
exchange, auth check, one exchange-validated test order
(`POST /api/v3/order/test`), then the request report.

### XAMPP-style control panel (Windows / macOS)

`launcher.py` is a tkinter control panel modelled on XAMPP: one row per
service with a Start/Stop button, a port-health status light, and the PID —
so you always know what is running:

| Service | Port | What it is |
| --- | --- | --- |
| Bot API | 8801 | `api_server.py` — JSON API wrapping bot.py (`/api/start`, `/api/stop`, `/api/status`, `/api/logs`, `/api/credentials`, `/api/check`) |
| Web UI | 8800 | `web_server.py` — serves `web/index.html`, the browser dashboard |

```powershell
py bare-features\bot\launcher.py         # Windows (python3 ... on macOS)
```

Starting the Web UI automatically opens `http://localhost:8800` in the
browser (the XAMPP "Admin" redirect). The dashboard sets credentials (held in
the API process memory only, never on disk), configures Grid Strike (plus
optional pmm/supertrend), runs the connection check, starts/stops the bot,
and streams the bot console live. Stopping — from the dashboard, the panel,
or by closing the panel — sends the bot a graceful shutdown so it cancels its
open orders and prints the request report first.

Everything is pure stdlib (`tkinter`, `http.server`); `gui.py`, the earlier
single-window form UI, still works for a no-browser workflow.

### Packaged builds: .exe and .dmg

`launcher.py` is also the single entry point for PyInstaller builds — the one
binary is the control panel, both servers, and the bot (it re-invokes itself
with `--service api|web|bot`). Graceful stop uses a `STOP` line on stdin
because windowed apps have no console signals.

```powershell
# Windows — produces dist\GridBotLauncher.exe (~12 MB, single file, no Python needed)
cd bare-features\bot; .\build_windows.ps1

# macOS — run ON a Mac (PyInstaller cannot cross-compile); produces dist/GridBotLauncher.dmg
cd bare-features/bot && bash build_macos.sh
```

The mac app is unsigned: first launch needs right-click → Open (Gatekeeper).

## 7. Relationship to the real hummingbot stack

This bot is the measurement/PoC layer. The production path stays what it is in
the repo: the dashboard deploys `hummingbot/hummingbot` containers running
`v2_with_controllers.py` with these controllers configured. Parameters proven
here (spreads, refresh times, grid geometry, request budget) transfer 1:1 to
the controller configs used for deployment.
