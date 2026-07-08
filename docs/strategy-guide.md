# Strategy guide — how to use the algorithms

A practical guide to the three strategies the bot runs, what every parameter
does, and how to read the results. For the architecture and rate-limit design
see [trading-bot.md](trading-bot.md).

The dashboard lets you run **one strategy at a time** — pick it in the
Strategy card and only that strategy's parameters are shown. Every field has
a hover tooltip; this document is the long version.

---

## Running a session

1. Start `GridBotLauncher` → **Start all** → the dashboard opens at
   `localhost:8800`.
2. Save your demo credentials (binance.com → Demo Trading → API Management).
3. Pick a strategy, set the parameters (defaults are sensible), leave
   **Duration blank** so it runs until you press Stop.
4. First run: leave "place real orders" **off** and press **Start bot** — the
   console shows exactly what it *would* do. When it looks right, tick the
   box and start again for real demo orders.
5. Watch: the **stat tiles** (PnL + balances) at the top, the **console** for
   every order, and <https://demo.binance.com> to see the orders on Binance
   itself. **Stop** cancels all open orders and prints the final reports.

CLI equivalent (same flags the dashboard uses):

```powershell
py bot\bot.py --strategies grid --total-quote 200 "--grid-start=-2%" --trade
```

---

## General parameters (all strategies)

| Parameter | Default | Meaning |
| --- | --- | --- |
| Trading pair | BTCUSDT | Market to trade: buy/sell BTC, priced and budgeted in USDT |
| Total amount (quote) | 200 | Grid/PMM: budget split across all levels. Supertrend: the amount spent per entry |
| Tick interval | 10 s | How often the bot re-reads the market and adjusts orders. Lower = more responsive, more API requests |
| Duration | blank | Auto-stop after N seconds. **Blank = run until Stop** — the strategies themselves never "finish"; they keep cycling |

---

## Grid Strike

**Idea:** place a ladder of buy orders below the current price. When one
fills, immediately offer that coin for sale one rung higher. Price wobbling
up and down through the grid turns each wobble into a small profit.

**Makes money when** the market moves sideways inside your grid range.
**Loses when** price falls through the whole grid and keeps going — you're
left holding coins bought above the market (the Limit price caps this).

| Parameter | Default | Meaning |
| --- | --- | --- |
| Grid start price | −3% | Bottom of the grid. Absolute (59000) or offset from the price at start (−3%) |
| Grid end price | +3% | Top of the grid. Levels are spaced evenly between start and end |
| Limit (stop) price | start − 2% | Safety cut-off: below this the bot pulls all orders and stops buying |
| Grid levels | 8 | How many rungs. More levels = tighter spacing = smaller, more frequent round trips |
| Max open orders | 3 | How many buys rest on the book at once (the rungs nearest below the price) |

**Reading it:** each completed `L# buy → T# sell` pair earns roughly
`(grid span ÷ levels) × order size`. With defaults (6% span, 8 levels,
200 USDT): rung spacing ≈ 0.75%, order size 25 USDT → ≈ 0.19 USDT gross per
round trip. The cycle repeats for as long as the bot runs — after a
take-profit sells, the same level re-arms its buy.

**Tuning:** quiet market → tighter grid (−1%/+1%, more levels) for more
action. Volatile market → wider grid so you don't fall out the bottom.

---

## PMM Simple (pure market making)

**Idea:** be the shop. Quote a buy slightly below the mid price and a sell
slightly above, permanently. Each time both sides trade, you pocket the
spread. Quotes follow the price as it moves.

**Makes money when** price oscillates gently and both sides keep filling.
**Loses when** price moves hard in one direction — one side keeps filling
("adverse selection") and you accumulate inventory that's losing value.

| Parameter | Default | Meaning |
| --- | --- | --- |
| Spreads (per side) | 0.1%, 0.3% | One buy AND one sell per spread — 2 spreads = 4 orders, 3 = 6. Distance from the mid price |
| Quote refresh | 60 s | Quotes are cancelled/re-placed after this age, or sooner if price drifts off them |

**Order count:** number of orders = 2 × number of spreads. To run more
levels, add spreads: `0.1%, 0.2%, 0.35%, 0.5%` → 8 orders. The budget is
split evenly across all of them (keep each level ≥ 5 USDT — Binance's
minimum notional).

**Reading it:** watch `realized` climb in small steps as opposing fills
complete, and watch the cancel/replace churn in the request report — that
churn is PMM's cost of doing business on the ORDERS rate-limit bucket.

**Tuning:** tighter spreads fill more often but earn less per fill and are
more exposed to trends. Longer refresh = fewer requests, staler quotes.

---

## Supertrend (trend following)

**Idea:** the Supertrend indicator draws a trailing support line under the
price using ATR (average volatility). When price is above the line, the
trend is up — buy near the line; when it crosses below, the trend flipped —
exit everything.

**Makes money when** the market trends up for a sustained stretch.
**Loses when** the market chops sideways — it buys near the line, the trend
flips, it exits slightly lower, repeatedly ("whipsaw").

| Parameter | Default | Meaning |
| --- | --- | --- |
| ATR length | 20 | How many 3-minute candles the volatility average looks back over. Longer = smoother, slower |
| ATR multiplier | 4.0 | How far the trend line trails the price. Higher = fewer, later, more reliable signals |
| Entry threshold | 1% | Only buy when price is within this distance of the trend line — avoids chasing a price far above support |

**Reading it:** the tick line shows the live signal —
`ST direction=LONG distance=0.31% (ACTIVE)` means uptrend, price 0.31% from
the line, close enough to enter. It market-buys `Total amount` once, then
holds until direction flips to SHORT, market-sells, and waits for the next
long signal — repeating until you stop it. PnL for this strategy is mostly
*unrealized* while a position is open.

**Tuning:** choppy market → raise the multiplier or length to filter noise.
Missing entries → widen the entry threshold (e.g. 2%).

---

## Backtesting — test parameters before running them

The **Backtest** button (Control card) replays the last N days of real
Binance candles through **the exact same strategy code the live bot runs**,
with your current parameters. No orders are placed and no keys are needed;
results print to the console:

- per-strategy PnL (realized / unrealized at the final price / fees),
  fill counts and volumes
- a **buy & hold comparison** — did the strategy beat just holding?
- simulation stats (placements, cancels, fills over how many candles)

CLI: `py bot\backtest.py --strategies grid --days 7` (plus any strategy
flags; `--candles 1m` for finer fills, `--fee 0%` to match demo's zero fees —
the default 0.1% per fill matches live Binance spot and is deliberately
harsher).

**Honest limits:** fills are simulated from candle highs/lows — a resting
buy "fills" if a later candle's low touches it, at the order's own price.
That ignores order-book queues and depth, so results are **directional, not
exact**; a strategy that loses in backtest will very likely lose live, but a
small backtest profit is not a guarantee. The repo also keeps hummingbot's
own engine-based backtests in `poc/backtest_strategies.py` (runs the actual
hummingbot controllers inside the Docker image) if you want a second opinion
from the framework itself.

## Reading the PnL

- **Realized** — profit locked in by completed sells (the money is banked).
- **Unrealized** — the gain/loss on coins currently held, marked at the
  latest price. It swings with the market until sold.
- **Total = realized + unrealized − fees.** The stat tiles at the top of the
  dashboard show this live; the session summary breaks it down per strategy,
  with fill counts, volumes, and held inventory at its average cost.
- The **"On the exchange"** panel is the ground truth — balances, resting
  orders, and executed trades pulled straight from Binance's records.

## FAQ

**Why did PMM place only 4 orders when I set 10 grid levels?**
Grid levels belongs to Grid Strike. PMM's count comes from its spreads list
(2 per spread). The dashboard now shows only the picked strategy's fields,
so the wrong-knob trap is gone.

**Why is nothing filling?**
Maker strategies rest orders *away* from the price and wait for it to come
to them. Grid needs a dip of one rung spacing (~0.75% on defaults) before
the first fill; PMM's closest quote is 0.1% away. Patience, or tighten the
parameters. Dry-run mode never fills resting orders at all.

**Why did the bot stop by itself?**
A Duration was set. Leave the field blank to run until you press Stop (the
dashboard no longer remembers old Duration values between visits, so a
leftover test value can't sneak into a long session).

**Is my money at risk?**
No. The bot only trades Demo Mode (simulated funds) and refuses to run its
trading loop against live Binance.
